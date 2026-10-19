"""Remote file storage as a source: Azure Blob (Azurite emulator), S3 API (Scality CloudServer) and SFTP, plus the pure logic around them.

    docker compose -f docker-compose.servers.yml --profile storage up -d
    python -m pytest tests/test_storage.py

Servers that are not running are skipped. The real-server tests upload their own fixtures with each vendor's official SDK, then check that
our connectors list, download and land them exactly: bytes identical, unicode and leading zeros intact, caps enforced, credentials never
echoed, folders cannot be escaped, and the SFTP server's identity has to be confirmed before anyone logs in.
"""

from __future__ import annotations

import gzip
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
from openpyxl import Workbook

from src.api.routes.sources import _table_from_key
from src.connectors.base import ConnectionSpec, ConnectorAuthError, ConnectorError
from src.connectors.storage import AzureBlobConnector, HostKeyError, S3Connector, SftpConnector, check_key, check_prefix, storage_for
from src.connectors.store import ConnectionInputError, ConnectionStore
from tests.test_real_servers import port_open
from tests.test_module7_api import client, ctx, dataset, encoder, env  # noqa: F401

CSV = "CUST_ID,CUST_NAME,ZIP,AMOUNT\n1001,Müller & Söhne,02134,10.50\n1002,Café Ñandú,10001,7\n1003,Zoë AG,80331,1234567.89\n"
SEMI = "CUST_ID;CUST_NAME;ZIP\r\n7;Müller GmbH;02134\r\n8;Café Ñandú;10001\r\n".encode("cp1252")
DUPS = "ID,NAME\n1,A\n1,B\n"


def xlsx_bytes() -> bytes:
    wb = Workbook()
    wb.active.title = "Info"
    wb.active.append(["notes"])
    sheet = wb.create_sheet("Customers")
    for row in (["CUST_ID", "CUST_NAME"], ["1", "Acme"], ["2", "Beta"]):
        sheet.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


FILES = {  # key -> bytes
    "customers.csv": CSV.encode("utf-8"), "semi_cp1252.txt": SEMI, "book.xlsx": xlsx_bytes(), "empty.csv": b"", "dups.csv": DUPS.encode(),
    "sub/ar_customers.csv.gz": gzip.compress(CSV.encode("utf-8")), "sub/deep/note.txt": b"not a table\n",
}


# ====================================================================== pure logic (no server)
def test_paths_and_names_are_checked_before_any_backend_sees_them():
    for bad in ("", "../x", "a/../b", "a\\b", "a\x00b", ".."):
        with pytest.raises(ConnectorError):
            check_key(bad)
    assert check_key("sub/ar_customers.csv.gz") == "sub/ar_customers.csv.gz"
    for bad in ("../", "a/../../b", "a\\b", "a\x00"):
        with pytest.raises(ConnectorError):
            check_prefix(bad)
    assert check_prefix("") == "" and check_prefix("/") == "" and check_prefix("/sub/") == "sub/"
    assert _table_from_key("exports/2024/AR_Customers v2.csv.gz") == "AR_CUSTOMERS_V2" and _table_from_key("1st quarter.xlsx") == "T_1ST_QUARTER"
    assert _table_from_key("data/.csv") == "SOURCE_FILE" and _table_from_key("x.tar.gz") == "X"


def test_connection_options_for_storage_are_validated(tmp_path):
    store = ConnectionStore(tmp_path)
    base = dict(name="st", dialect="azure_blob", host="acct.blob.core.windows.net", user="acct", database="")
    ok = store.validate({**base, "options": {"https": "false", "path_style": "yes", "auth": "sas", "region": "eu-west-1"}})
    assert ok["options"] == {"https": False, "path_style": True, "auth": "sas", "region": "eu-west-1"} and ok["port"] == 443
    assert store.validate({**base, "dialect": "sftp", "host": "sftp.example.com"})["port"] == 22
    pin = "SHA256:" + "A" * 43
    assert store.validate({**base, "dialect": "sftp", "options": {"host_key_sha256": pin}})["options"]["host_key_sha256"] == pin
    for bad in ({"auth": "token"}, {"host_key_sha256": "MD5:abc"}, {"https": "maybe"}):
        with pytest.raises(ConnectionInputError):
            store.validate({**base, "options": bad})
    assert store.validate({**base, "dialect": "s3", "database": ""})["database"] == ""          # a bucket is optional for S3 / Azure


def test_endpoints_are_built_for_real_clouds_and_for_emulators(monkeypatch):
    def spec(kind, host, port, **o):
        return ConnectionSpec(kind, host, port, "", "acct", "key", {"connect_timeout": 5, **o})
    assert AzureBlobConnector(spec("azure_blob", "acct.blob.core.windows.net", 443))._account_url() == "https://acct.blob.core.windows.net"
    assert AzureBlobConnector(spec("azure_blob", "localhost", 10000, https=False, path_style=True))._account_url() == "http://localhost:10000/acct"
    seen = {}
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: seen.update(kw) or object())
    S3Connector(spec("s3", "s3.amazonaws.com", 443, region="eu-west-1"))._client()
    assert "endpoint_url" not in seen and seen["region_name"] == "eu-west-1"                     # real AWS resolves its own endpoint from the region
    seen.clear(); S3Connector(spec("s3", "storage.example.com", 9000, https=False, path_style=True))._client()
    assert seen["endpoint_url"] == "http://storage.example.com:9000" and seen["config"].s3["addressing_style"] == "path"
    seen.clear(); S3Connector(spec("s3", "storage.example.com", 443))._client()
    assert seen["endpoint_url"] == "https://storage.example.com"


def test_downloads_are_capped_and_the_partial_file_is_removed(tmp_path):
    from src.connectors.storage import StorageConnector
    target = tmp_path / "x.bin"
    assert StorageConnector._stream_to(iter([b"a" * 10, b"b" * 10]), target, 100) == 20 and target.stat().st_size == 20
    with pytest.raises(ConnectorError, match="larger than the"):
        StorageConnector._stream_to(iter([b"a" * 60, b"b" * 60]), target, 100)
    assert not target.exists()


def test_sftp_paths_cannot_leave_the_base_directory():
    c = SftpConnector(ConnectionSpec("sftp", "h", 22, "/incoming", "u", "p", {}))
    assert c._inside("/incoming", "sub/a.csv") == "/incoming/sub/a.csv" and c._inside("/incoming", "") == "/incoming"
    for evil in ("../etc/passwd", "sub/../../etc", "/etc/passwd"):                # parent segments and absolute paths both end up outside
        with pytest.raises(ConnectorError, match="outside"):
            c._inside("/incoming", evil)
    assert SftpConnector(ConnectionSpec("sftp", "h", 22, "", "u", "p", {}))._base() == "/"


def test_errors_are_translated_without_echoing_the_secret():
    c = S3Connector(ConnectionSpec("s3", "h", 443, "", "AKIA", "super-secret-key", {}))
    for raw, expect, kind in [("An error occurred (InvalidAccessKeyId) when calling the ListBuckets operation: super-secret-key", "rejected the credentials", ConnectorAuthError),
                              ("403 Forbidden: AuthenticationFailed", "rejected the credentials", ConnectorAuthError),
                              ("SSL: CERTIFICATE_VERIFY_FAILED", "not trusted", ConnectorError),
                              ("NoSuchBucket: the bucket does not exist", "not found", ConnectorError),
                              ("Max retries exceeded: connection refused", "cannot reach", ConnectorError)]:
        err = c._translate(Exception(raw))
        assert isinstance(err, kind) and expect in str(err) and "super-secret-key" not in str(err), (raw, str(err))


# ====================================================================== real servers
@dataclass
class Store:
    name: str
    kind: str
    host: str
    port: int
    user: str
    password: str
    database: str
    container: str
    options: dict
    setup: Callable[["Store"], None]

    def spec(self, **override) -> ConnectionSpec:
        return ConnectionSpec(self.kind, self.host, self.port, self.database, override.get("user", self.user), override.get("password", self.password),
                              {"connect_timeout": 10, **self.options, **override.get("options", {})})


def _setup_azure(s: Store) -> None:
    from azure.storage.blob import BlobServiceClient
    svc = BlobServiceClient(account_url=f"http://{s.host}:{s.port}/{s.user}", credential={"account_name": s.user, "account_key": s.password})
    try:
        svc.delete_container(s.container)
    except Exception:  # noqa: BLE001
        pass
    for _ in range(20):                                         # a deleted container name is unavailable for a moment
        try:
            svc.create_container(s.container)
            break
        except Exception:  # noqa: BLE001
            __import__("time").sleep(0.5)
    for key, data in FILES.items():
        svc.get_blob_client(s.container, key).upload_blob(data, overwrite=True)


def _setup_s3(s: Store) -> None:
    import boto3
    c = boto3.client("s3", endpoint_url=f"http://{s.host}:{s.port}", aws_access_key_id=s.user, aws_secret_access_key=s.password, region_name="us-east-1")
    try:
        for obj in c.list_objects_v2(Bucket=s.container).get("Contents", []):
            c.delete_object(Bucket=s.container, Key=obj["Key"])
        c.delete_bucket(Bucket=s.container)
    except Exception:  # noqa: BLE001
        pass
    c.create_bucket(Bucket=s.container)
    for key, data in FILES.items():
        c.put_object(Bucket=s.container, Key=key, Body=data)


def _setup_sftp(s: Store) -> None:
    import paramiko
    t = paramiko.Transport((s.host, s.port))
    t.connect(username=s.user, password=s.password)                    # test fixtures upload through the SDK; it does not need to pin
    sftp = paramiko.SFTPClient.from_transport(t)

    def rm(path: str) -> None:
        for a in sftp.listdir_attr(path):
            full = f"{path}/{a.filename}"
            if a.st_mode and (a.st_mode & 0o040000):
                rm(full)
                sftp.rmdir(full)
            else:
                sftp.remove(full)
    rm("/incoming")
    for d in ("/incoming/sub", "/incoming/sub/deep"):
        sftp.mkdir(d)
    for key, data in FILES.items():
        with sftp.open(f"/incoming/{key}", "wb") as fh:
            fh.write(data)
    t.close()


AZURE_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="     # the published Azurite development key
STORES = {
    "azure_blob": Store("azure_blob", "azure_blob", "localhost", 10000, "devstoreaccount1", AZURE_KEY, "", "legacy", {"https": False, "path_style": True}, _setup_azure),
    "s3": Store("s3", "s3", "localhost", 18000, "s3_test_access", "S3_secret_Test1", "", "legacy", {"https": False, "path_style": True, "region": "us-east-1"}, _setup_s3),
    "sftp": Store("sftp", "sftp", "localhost", 12222, "legacy", "Legacy_pw_Test1", "incoming", "/incoming", {}, _setup_sftp),
}


@pytest.fixture(params=sorted(STORES))
def store(request):
    s = STORES[request.param]
    if not port_open(s.host, s.port):
        pytest.skip(f"{s.name} test server is not running (docker compose -f docker-compose.servers.yml --profile storage up -d)")
    s.setup(s)
    if s.kind == "sftp":                                                # the server identity must be confirmed first: ask it, then pin what it presented
        with pytest.raises(HostKeyError) as unknown:
            storage_for(s.spec(options={"host_key_sha256": ""})).test()
        return Store(**{**s.__dict__, "options": {**s.options, "host_key_sha256": unknown.value.fingerprint}})   # a copy: the shared definition stays unpinned
    return s


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_connect_and_browse_one_level_at_a_time(store):
    c = storage_for(store.spec())
    info = c.test()
    assert info["ok"] and info["latency_ms"] >= 0
    assert store.container in c.containers()
    root = c.list(store.container)
    names = [e.name for e in root]
    assert names[0] == "sub/" and root[0].is_dir and root[0].size is None                         # folders come first
    assert {"customers.csv", "semi_cp1252.txt", "book.xlsx", "empty.csv", "dups.csv"} <= set(names)
    sizes = {e.name: e.size for e in root if not e.is_dir}
    assert sizes["customers.csv"] == len(FILES["customers.csv"]) and sizes["empty.csv"] == 0
    sub = c.list(store.container, "sub/")
    assert [(e.name, e.is_dir) for e in sub] == [("deep/", True), ("ar_customers.csv.gz", False)]
    assert [e.name for e in c.list(store.container, "sub/deep/")] == ["note.txt"]
    assert c.list(store.container, "no/such/folder/") == []
    for evil in ("../", "sub/../../", "a\\b"):
        with pytest.raises(ConnectorError):
            c.list(store.container, evil)


def test_download_is_byte_exact_capped_and_safe(store, tmp_path):
    c = storage_for(store.spec())
    for key, data in FILES.items():
        out = tmp_path / key.replace("/", "_")
        assert c.download(store.container, key, out, 1_000_000) == len(data)
        assert sha(out.read_bytes()) == sha(data), key
    big = tmp_path / "cap.bin"
    with pytest.raises(ConnectorError, match="larger than the"):
        c.download(store.container, "customers.csv", big, 30)
    assert not big.exists()
    with pytest.raises(ConnectorError):
        c.download(store.container, "no/such/file.csv", tmp_path / "missing.csv", 1000)
    assert not (tmp_path / "missing.csv").exists()
    for evil in ("../customers.csv", "sub/../../customers.csv", "a\\b.csv", ""):
        with pytest.raises(ConnectorError):
            c.download(store.container, evil, tmp_path / "evil.csv", 1000)
    with pytest.raises(ConnectorError):
        c.download("../" + store.container.strip("/"), "customers.csv", tmp_path / "evil2.csv", 1000)


def test_wrong_credentials_and_unreachable_servers_are_clear_and_never_echo_the_secret(store):
    wrong = "ZGVmaW5pdGVseS13cm9uZy1zZWNyZXQ=" if store.kind == "azure_blob" else "definitely-wrong-secret"     # Azure keys must at least be valid base64
    with pytest.raises(ConnectorError) as bad:
        storage_for(store.spec(password=wrong)).test()
    assert wrong not in str(bad.value) and "definitely-wrong-secret" not in str(bad.value)
    assert isinstance(bad.value, ConnectorAuthError) and "rejected the credentials" in str(bad.value), str(bad.value)
    with pytest.raises(ConnectorAuthError, match="malformed|rejected"):                  # a key that is not even well formed
        storage_for(store.spec(password="not base64 !!")).test()
    with pytest.raises(ConnectorError) as down:
        storage_for(ConnectionSpec(store.kind, "127.0.0.1", 1, store.database, store.user, "secret-pw", {"connect_timeout": 2, **store.options})).test()
    assert "secret-pw" not in str(down.value)


def test_the_sftp_server_identity_must_be_confirmed():
    s = STORES["sftp"]
    if not port_open(s.host, s.port):
        pytest.skip("sftp test server is not running")
    with pytest.raises(HostKeyError, match="has not been trusted yet") as first:
        storage_for(s.spec()).test()
    fp = first.value.fingerprint
    assert fp.startswith("SHA256:") and len(fp) == 50
    with pytest.raises(HostKeyError, match="not the trusted one"):
        storage_for(s.spec(options={"host_key_sha256": "SHA256:" + "A" * 43})).test()               # a different (pinned) key: refused before any login
    assert storage_for(s.spec(options={"host_key_sha256": fp})).test()["ok"]
    with pytest.raises(ConnectorError):                                                                # the right key but a wrong password
        storage_for(s.spec(password="nope", options={"host_key_sha256": fp})).test()


# ---------------------------------------------------------------- through the application
def _save(client, store, name=None, **extra):
    body = {"name": name or f"{store.name}_src", "dialect": store.kind, "host": store.host, "port": store.port, "database": store.database, "user": store.user,
            "password": store.password, "options": dict(store.options), **extra}
    r = client.post("/api/connections", json=body)
    assert r.status_code == 201, r.text
    assert store.password not in r.text
    return body["name"]


def test_browse_and_import_files_through_the_api(store, client, ctx):
    name = _save(client, store)
    t = client.post("/api/connections/test", json={"name": name}).json()
    assert t["ok"] and t["schemas"] >= 1, t
    assert store.container in client.get(f"/api/connections/{name}/containers").json()
    listing = client.get(f"/api/connections/{name}/files", params={"container": store.container}).json()
    assert listing[0]["is_dir"] and {f["name"] for f in listing} >= {"customers.csv", "book.xlsx"}
    assert client.get(f"/api/connections/{name}/schemas").status_code == 409                       # a storage connection has no database schemas

    r = client.post("/api/sources/import-file", json={"connection": name, "container": store.container, "key": "customers.csv", "primary_key_columns": ["CUST_ID"], "expected_rows": 3})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rows_landed"] == 3 and body["control_status"] == "MATCH" and body["table_name"] == "CUSTOMERS" and body["system_name"] == name
    assert body["origin"]["kind"] == "storage" and body["origin"]["key"] == "customers.csv" and body["origin"]["provider"] == store.kind
    profile = client.post(f"/api/profile/{body['table_id']}").json()
    zips = {v["value"] for c in profile["columns"] if c["column_name"] == "ZIP" for v in c["top_values"]}
    assert "02134" in zips                                                                       # the leading zero survived download and landing

    gz = client.post("/api/sources/import-file", json={"connection": name, "container": store.container, "key": "sub/ar_customers.csv.gz"})
    assert gz.status_code == 201 and gz.json()["rows_landed"] == 3 and gz.json()["table_name"] == "AR_CUSTOMERS"
    semi = client.post("/api/sources/import-file", json={"connection": name, "container": store.container, "key": "semi_cp1252.txt"})
    assert semi.status_code == 201 and semi.json()["rows_landed"] == 2
    snap = client.get(f"/api/tables/{semi.json()['table_id']}/snapshot").json()
    assert snap["detection"]["encoding"] == "cp1252" and snap["detection"]["delimiter"] == ";"      # detected exactly as for an upload
    xl = client.post("/api/sources/import-file", json={"connection": name, "container": store.container, "key": "book.xlsx", "options": {"sheet": "Customers"}})
    assert xl.status_code == 201 and xl.json()["format"] == "excel" and xl.json()["rows_landed"] == 2


def test_import_rejections_leave_nothing_behind(store, client, ctx, session_factory):
    from src.db.models import CatTable
    name = _save(client, store)
    imp = lambda **kw: client.post("/api/sources/import-file", json={"connection": name, "container": store.container, **kw})   # noqa: E731
    assert imp(key="dups.csv", primary_key_columns=["ID"]).status_code == 422                        # a repeating key is refused like for any file
    assert "does not identify rows uniquely" in imp(key="dups.csv", primary_key_columns=["ID"]).json()["detail"]
    assert imp(key="customers.csv", expected_rows=99).status_code == 422                              # control total mismatch
    assert imp(key="customers.csv", primary_key_columns=["NOPE"]).status_code == 422
    assert imp(key="empty.csv").status_code == 422 and "empty" in imp(key="empty.csv").json()["detail"]
    assert imp(key="sub/deep/note.txt").status_code in (201, 422)                                     # a text file is read as a one-column table or refused, never a crash
    assert imp(key="no/such.csv").status_code == 502
    assert imp(key="../customers.csv").status_code == 502
    assert client.post("/api/sources/import-file", json={"connection": "ghost", "container": "c", "key": "k"}).status_code == 404
    with session_factory() as s:
        assert {t.table_name for t in s.query(CatTable)} <= {"NOTE"}                                  # nothing from the rejected files
    assert not list((ctx.workspace / "tmp").glob("*"))


def test_connection_kinds_cannot_be_mixed_up(store, client):
    name = _save(client, store)
    assert client.post("/api/sources/import-db", json={"connection": name, "schema_name": "s", "table": "t"}).status_code in (404, 409, 422, 502)
    tgt = _save(client, store, name=f"{store.name}_as_target", role="target")
    refused = client.post("/api/sources/import-file", json={"connection": tgt, "container": store.container, "key": "customers.csv"})
    assert refused.status_code == 409 and "not a file-storage source" in refused.json()["detail"]
    assert client.get(f"/api/connections/{name}/tables", params={"schema_name": "x"}).status_code == 409   # database browsing refuses a storage connection


def test_a_connection_fixed_to_one_container_stays_inside_it(store):
    pinned = store.spec(**({} if store.kind == "sftp" else {}))
    spec = ConnectionSpec(store.kind, store.host, store.port, store.database or store.container, store.user, store.password, pinned.options)
    c = storage_for(spec)
    assert c.containers() == [store.database or store.container] or c.containers() == [store.container]
    assert "customers.csv" in [e.name for e in c.list(c.containers()[0])]
    for other in ("somewhere-else", "/", "/etc", "other-bucket"):
        if other == c.containers()[0]:
            continue
        with pytest.raises(ConnectorError, match="limited to"):
            c.list(other)
        with pytest.raises(ConnectorError, match="limited to"):
            c.download(other, "customers.csv", Path("never.csv"), 1000)
