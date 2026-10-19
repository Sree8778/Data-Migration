"""Encrypted connections, the way Azure databases demand them.

Unit tests prove how each connector turns the options into driver settings. The REAL servers (docker compose --profile tls, see
docker-compose.servers.yml; run `python scripts/make_test_cert.py` first) prove it end to end: TLS-only PostgreSQL / MySQL / SQL Server,
a self-signed test certificate, and the outcomes that matter: encrypted works, plain is refused with a clear message, a certificate that
cannot be verified is refused with a clear message. A server that is not running is skipped.
"""

from __future__ import annotations

from pathlib import Path

import certifi
import pytest

from src.connectors.base import ConnectionSpec, ConnectorError
from src.connectors.dialects import MssqlConnector, MysqlConnector, PostgresConnector, connector_for
from src.connectors.store import ConnectionInputError, ConnectionStore
from tests.test_real_servers import port_open
from tests.test_module7_api import client, ctx, dataset, encoder, env  # noqa: F401

CERT = Path(__file__).resolve().parent.parent / "test_certs" / "server.crt"


def spec(dialect, port, user, pw, db, **options):
    return ConnectionSpec(dialect, "localhost", port, db, user, pw, {"connect_timeout": 15, **options})


# ------------------------------------------------------------------ options -> driver settings (no server needed)
def test_options_are_validated_and_booleans_are_really_booleans(tmp_path):
    store = ConnectionStore(tmp_path)
    base = dict(name="az", dialect="mssql", host="x.database.windows.net", user="u", database="d")
    ok = store.validate({**base, "options": {"encrypt": "false", "validate_host": "yes", "ssl": 0, "ssl_verify": "true", "sslmode": "verify-full", "sslrootcert": "/ca.pem"}})
    assert ok["options"] == {"encrypt": False, "validate_host": True, "ssl": False, "ssl_verify": True, "sslmode": "verify-full", "sslrootcert": "/ca.pem"}   # "false" is False
    for bad in ({"sslmode": "yes-please"}, {"encrypt": "maybe"}, {"bogus": 1}):
        with pytest.raises(ConnectionInputError):
            store.validate({**base, "options": bad})


def test_the_drivers_receive_the_tls_settings(monkeypatch):
    import psycopg2
    import pymysql
    import pytds
    seen = {}
    monkeypatch.setattr(pytds, "connect", lambda **kw: seen.setdefault("mssql", kw) or object())
    monkeypatch.setattr(psycopg2, "connect", lambda **kw: seen.setdefault("pg", kw) or object())
    monkeypatch.setattr(pymysql, "connect", lambda **kw: seen.setdefault("mysql", kw) or object())

    MssqlConnector(spec("mssql", 1433, "u", "p", "d", encrypt=True))._connect()
    assert seen["mssql"]["cafile"] == certifi.where() and seen["mssql"]["validate_host"] is True       # encrypt alone = public CA bundle (what Azure chains to)
    seen.clear(); MssqlConnector(spec("mssql", 1433, "u", "p", "d", encrypt=True, cafile="/my/ca.pem", validate_host=False))._connect()
    assert seen["mssql"]["cafile"] == "/my/ca.pem" and seen["mssql"]["validate_host"] is False
    seen.clear(); MssqlConnector(spec("mssql", 1433, "u", "p", "d"))._connect()
    assert seen["mssql"]["cafile"] is None                                                           # no encryption requested: the driver stays plain unless the server insists

    PostgresConnector(spec("postgresql", 5432, "u", "p", "d", sslmode="verify-full", sslrootcert="/ca.pem"))._connect()
    assert seen["pg"]["sslmode"] == "verify-full" and seen["pg"]["sslrootcert"] == "/ca.pem"
    seen.clear(); PostgresConnector(spec("postgresql", 5432, "u", "p", "d"))._connect()
    assert seen["pg"]["sslmode"] == "prefer" and "sslrootcert" not in seen["pg"]

    MysqlConnector(spec("mysql", 3306, "u", "p", "d", ssl=True))._connect()
    assert seen["mysql"]["ssl_ca"] == certifi.where() and seen["mysql"]["ssl_verify_cert"] is True and seen["mysql"]["ssl_verify_identity"] is True
    seen.clear(); MysqlConnector(spec("mysql", 3306, "u", "p", "d", ssl=True, ssl_verify=False, ssl_ca="/ca.pem"))._connect()
    assert seen["mysql"]["ssl_ca"] == "/ca.pem" and seen["mysql"]["ssl_verify_cert"] is False
    seen.clear(); MysqlConnector(spec("mysql", 3306, "u", "p", "d"))._connect()
    assert "ssl_ca" not in seen["mysql"]
    seen.clear(); MysqlConnector(spec("mysql", 3306, "u", "p", "d", ssl=False))._connect()
    assert seen["mysql"]["ssl_disabled"] is True and "ssl_ca" not in seen["mysql"]


def test_the_installed_tls_libraries_work_together():
    """python-tds validates a server certificate's host name with X509.get_extension, which pyOpenSSL 26 removed: without it every
    certificate-checked connection to Azure SQL fails. This guards the pin in requirements.txt."""
    import OpenSSL.crypto
    import pytds.tls
    assert hasattr(OpenSSL.crypto.X509, "get_extension"), "pyOpenSSL is too new for python-tds; install pyOpenSSL>=24,<26"
    assert callable(pytds.tls.create_context)


def test_tls_failures_get_actionable_messages_without_the_password():
    c = PostgresConnector(spec("postgresql", 5432, "u", "s3cret-pw", "d"))
    for raw, expected in [("Client does not have encryption enabled but it is required by server", "requires an encrypted"),
                          ("(3159, 'Connections using insecure transport are prohibited while --require_secure_transport=ON.')", "requires an encrypted"),
                          ('FATAL:  no pg_hba.conf entry for host "1.2.3.4", user "u", database "d", no encryption', "requires an encrypted"),
                          ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate", "certificate was not trusted"),
                          ("Certificate does not match host name 'x'", "certificate was not trusted"),
                          ("password authentication failed for user u with s3cret-pw", "rejected the user name or password")]:
        err = c._translate(Exception(raw))
        assert expected in str(err) and "s3cret-pw" not in str(err), (raw, str(err))


def test_the_azure_presets_are_offered(client):
    presets = {p["id"]: p for p in client.get("/api/connections/presets").json()}
    assert set(presets) == {"azure-sql", "azure-postgresql", "azure-mysql"}
    assert presets["azure-sql"]["dialect"] == "mssql" and presets["azure-sql"]["options"]["encrypt"] is True and presets["azure-sql"]["port"] == 1433
    assert presets["azure-postgresql"]["options"] == {"sslmode": "require"} and presets["azure-mysql"]["options"]["ssl"] is True
    assert all("windows.net" in p["hint"] or "azure.com" in p["hint"] for p in presets.values())
    saved = client.post("/api/connections", json={"name": "az1", "dialect": "mssql", "host": "demo.database.windows.net", "user": "mig", "password": "pw", "database": "legacy",
                                                  "options": presets["azure-sql"]["options"]})
    assert saved.status_code == 201 and saved.json()["options"]["encrypt"] is True and "pw" not in saved.text.replace("password", "")


# ------------------------------------------------------------------ real TLS-only servers
def need(port):
    if not port_open("localhost", port):
        pytest.skip(f"no TLS test server on localhost:{port} (python scripts/make_test_cert.py; docker compose -f docker-compose.servers.yml --profile tls up -d --wait)")
    if not CERT.is_file():
        pytest.skip("run python scripts/make_test_cert.py first")


def test_postgres_that_requires_tls():
    need(15433)
    pg = lambda **o: PostgresConnector(spec("postgresql", 15433, "postgres", "Pg_pw_Test1", "legacy_test", **o))   # noqa: E731
    with pytest.raises(ConnectorError, match="requires an encrypted"):
        pg(sslmode="disable").test()                                                  # plain connections are refused, and the message says what to do
    assert pg(sslmode="require").test()["ok"]                                         # encrypted, certificate not checked
    assert pg(sslmode="verify-full", sslrootcert=str(CERT)).test()["ok"]              # encrypted AND the certificate is verified against our CA file
    with pytest.raises(ConnectorError, match="not trusted"):
        pg(sslmode="verify-full", sslrootcert=certifi.where()).test()                 # a public CA bundle cannot vouch for the self-signed test server


def test_mysql_that_requires_tls():
    need(33307)
    my = lambda **o: MysqlConnector(spec("mysql", 33307, "root", "Root_pw_Test1", "legacy_test", **o))   # noqa: E731
    with pytest.raises(ConnectorError, match="requires an encrypted"):
        my(ssl=False).test()                                                          # explicitly plain: refused, with the instruction
    assert my().test()["ok"]                                                          # left alone, the driver negotiates TLS because the server offers it
    assert my(ssl=True, ssl_ca=str(CERT)).test()["ok"]                                # encrypted and verified against our CA file
    assert my(ssl=True, ssl_verify=False).test()["ok"]                                # encrypted, certificate deliberately not checked
    with pytest.raises(ConnectorError, match="not trusted"):
        my(ssl=True).test()                                                           # verification on (the default) with the public bundle: refused


def test_sql_server_that_requires_tls():
    need(14334)
    ms = lambda **o: MssqlConnector(spec("mssql", 14334, "sa", "Strong_Pw_123!", "master", **o))   # noqa: E731
    with pytest.raises(ConnectorError, match="requires an encrypted"):
        ms().test()                                                                   # exactly what happens against Azure SQL without the Encrypt option
    assert ms(encrypt=True, cafile=str(CERT)).test()["ok"]
    with pytest.raises(ConnectorError, match="not trusted"):
        ms(encrypt=True).test()                                                       # the public bundle does not trust the self-signed test certificate
    by_ip = lambda **o: MssqlConnector(ConnectionSpec("mssql", "127.0.0.1", 14334, "master", "sa", "Strong_Pw_123!", {"encrypt": True, "cafile": str(CERT), "connect_timeout": 15, **o}))   # noqa: E731
    with pytest.raises(ConnectorError, match="not trusted"):
        by_ip().test()                                                                # python-tds checks DNS names only: by IP address the check cannot pass...
    assert by_ip(validate_host=False).test()["ok"]                                    # ...unless host-name validation is turned off (the CA is still verified)
