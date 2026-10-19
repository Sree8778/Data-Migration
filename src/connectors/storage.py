"""Remote FILE storage as a source: Azure Blob Storage / Data Lake (through its blob endpoint), Amazon S3 and S3-compatible stores, SFTP.

A storage connection lets a steward browse containers / buckets / folders, pick extract files and land them exactly like an uploaded
file: the same six formats, encodings, control totals, snapshots and lineage. Nothing is ever written, renamed or deleted on the
remote side. Rules shared with the database connectors: credentials never appear in errors, names are validated before use, downloads
are streamed to disk under a hard size cap, and a connection is a SOURCE only.

Field mapping onto a saved connection: host / port = the endpoint, user = account name / access key id / login, password = account key
(or SAS token) / secret key / password, database = default container / bucket (or the SFTP base directory), options = https, path_style,
auth (key | sas), region (S3, in `database` when no bucket is fixed), host_key_sha256 (SFTP).
"""

from __future__ import annotations

import base64
import hashlib
import posixpath
import socket
import stat as statmod
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from src.connectors.base import ConnectionSpec, ConnectorAuthError, ConnectorError

CHUNK = 1024 * 1024
MAX_LIST = 2000


class HostKeyError(ConnectorError):
    """The SFTP server presented a key that is not the pinned one. `fingerprint` is what it presented, so a user can verify and trust it."""

    def __init__(self, message: str, fingerprint: str) -> None:
        super().__init__(message)
        self.fingerprint = fingerprint


@dataclass(frozen=True)
class StorageEntry:
    name: str                           # last path segment, for display
    key: str                            # full key / path inside the container
    size: Optional[int]
    modified: Optional[str]
    is_dir: bool


def check_key(key: str) -> str:
    """A key / path that is safe to hand to any backend: no empty names, NUL bytes, backslashes or '..' segments."""
    if not key or "\x00" in key or "\\" in key or any(part == ".." for part in key.split("/")):
        raise ConnectorError(f"'{key}' is not an acceptable file path")
    return key


def check_prefix(prefix: str) -> str:
    if prefix in ("", "/"):
        return ""
    if "\x00" in prefix or "\\" in prefix or any(part == ".." for part in prefix.split("/")):
        raise ConnectorError(f"'{prefix}' is not an acceptable folder path")
    return prefix.lstrip("/")


class StorageConnector(ABC):
    kind: str = ""
    label: str = ""
    default_port: int = 443
    container_label: str = "Container"
    system_type: str = "FLAT_FILE"

    def __init__(self, spec: ConnectionSpec, client_factory: Optional[Callable[[], Any]] = None) -> None:
        self.spec = spec
        self._factory = client_factory                    # tests inject a fake client

    # ------------------------------------------------------------------ to implement
    @abstractmethod
    def _client(self) -> Any: ...
    @abstractmethod
    def _info(self, client: Any) -> str: ...
    @abstractmethod
    def _containers(self, client: Any) -> list[str]: ...
    @abstractmethod
    def _list(self, client: Any, container: str, prefix: str) -> list[StorageEntry]: ...
    @abstractmethod
    def _download(self, client: Any, container: str, key: str, dest: Path, max_bytes: int) -> int: ...

    def _close(self, client: Any) -> None:
        return None

    # ------------------------------------------------------------------ plumbing
    def _scrub(self, text: str) -> str:
        text = str(text)
        for secret in (self.spec.password,):
            if secret:
                text = text.replace(secret, "***")
        return text.strip()[:240]

    def _translate(self, exc: Exception) -> ConnectorError:
        if isinstance(exc, ConnectorError):
            return exc
        text = self._scrub(exc)
        low = text.lower()
        if "base64" in low or "incorrect padding" in low:
            return ConnectorAuthError("the account key / token is malformed (not valid base64): copy it again exactly as the storage service shows it")
        if any(k in low for k in ("403", "forbidden", "invalidaccesskeyid", "signaturedoesnotmatch", "authenticationfailed", "authentication failed",
                                  "not authorized", "access denied", "unauthorized", "401", "authorization", "mac signature", "invalid credentials")):
            return ConnectorAuthError(f"the storage service rejected the credentials ({text[:160]})")
        if any(k in low for k in ("certificate verify failed", "ssl:", "sslerror")):
            return ConnectorError(f"the server's TLS certificate was not trusted - {text[:160]}")
        if any(k in low for k in ("nosuchbucket", "containernotfound", "does not exist", "not found", "404", "no such file")):
            return ConnectorError(f"not found: {text[:160]}")
        return ConnectorError(f"cannot reach {self.spec.host}:{self.spec.port}: {text}")

    def _use(self, fn: Callable[[Any], Any]) -> Any:
        try:
            client = self._factory() if self._factory else self._client()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from None
        try:
            return fn(client)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from None
        finally:
            try:
                self._close(client)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ public
    def test(self) -> dict[str, Any]:
        started = time.monotonic()
        info = self._use(self._info)
        return {"ok": True, "server_version": info, "latency_ms": int((time.monotonic() - started) * 1000)}

    def containers(self) -> list[str]:
        return sorted(self._use(self._containers))

    def list(self, container: str, prefix: str = "") -> list[StorageEntry]:
        container, prefix = self._check_container(container), check_prefix(prefix)
        entries = self._use(lambda c: self._list(c, container, prefix))
        return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))[:MAX_LIST]

    def download(self, container: str, key: str, dest: Path, max_bytes: int) -> int:
        container, key = self._check_container(container), check_key(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._use(lambda c: self._download(c, container, key, dest, max_bytes))
        except ConnectorError:
            dest.unlink(missing_ok=True)
            raise

    def _pinned(self) -> Optional[str]:
        """The one container this connection may read, when the connection fixes it (its default bucket / container / base directory)."""
        return self.spec.database or None

    def _check_container(self, container: str) -> str:
        if not container or "\x00" in container or "\\" in container or ".." in container.split("/"):
            raise ConnectorError(f"'{container}' is not an acceptable {self.container_label.lower()} name")
        pinned = self._pinned()
        if pinned is not None and container != pinned:
            raise ConnectorError(f"this connection is limited to {self.container_label.lower()} '{pinned}'; '{container}' is outside it")
        return container

    @staticmethod
    def _stream_to(chunks: Any, dest: Path, max_bytes: int) -> int:
        """Write an iterator of byte chunks to `dest`; refuse (and remove the partial file) when it exceeds the cap."""
        written = 0
        with open(dest, "wb") as out:
            for chunk in chunks:
                written += len(chunk)
                if written > max_bytes:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise ConnectorError(f"the file is larger than the {max_bytes // 1048576} MB limit (MIGRATION_MAX_UPLOAD_MB)")
                out.write(chunk)
        return written


# ====================================================================== Azure Blob Storage / Data Lake
class AzureBlobConnector(StorageConnector):
    kind, label, default_port, container_label = "azure_blob", "Azure Blob Storage / Data Lake", 443, "Container"

    def _account_url(self) -> str:
        s = self.spec
        scheme = "https" if s.options.get("https", True) else "http"
        port = "" if (scheme == "https" and s.port == 443) or (scheme == "http" and s.port == 80) else f":{s.port}"
        base = f"{scheme}://{s.host}{port}"
        return f"{base}/{s.user}" if s.options.get("path_style") else base            # the storage emulator uses path-style URLs

    def _client(self) -> Any:
        from azure.storage.blob import BlobServiceClient
        s = self.spec
        credential: Any = s.password if s.options.get("auth") == "sas" else {"account_name": s.user, "account_key": s.password}
        return BlobServiceClient(account_url=self._account_url(), credential=credential, connection_timeout=s.connect_timeout, read_timeout=120, retry_total=2)

    def _info(self, client: Any) -> str:
        if self.spec.database:                                                       # a container-scoped key / SAS token has no right to list the account
            client.get_container_client(self.spec.database).get_container_properties()
        else:
            next(iter(client.list_containers(results_per_page=1).by_page()), None)   # one request, however many containers there are
        return "Azure Blob Storage"

    def _containers(self, client: Any) -> list[str]:
        if self.spec.database:
            return [self.spec.database]
        return [c.name for c in client.list_containers()]

    def _list(self, client: Any, container: str, prefix: str) -> list[StorageEntry]:
        out: list[StorageEntry] = []
        for item in client.get_container_client(container).walk_blobs(name_starts_with=prefix or None, delimiter="/"):
            if len(out) >= MAX_LIST:
                break
            name = item.name
            if name.endswith("/"):
                out.append(StorageEntry(name=posixpath.basename(name.rstrip("/")) + "/", key=name, size=None, modified=None, is_dir=True))
            else:
                out.append(StorageEntry(name=posixpath.basename(name), key=name, size=getattr(item, "size", None),
                                        modified=item.last_modified.isoformat(timespec="seconds") if getattr(item, "last_modified", None) else None, is_dir=False))
        return out

    def _download(self, client: Any, container: str, key: str, dest: Path, max_bytes: int) -> int:
        blob = client.get_container_client(container).get_blob_client(key)
        return self._stream_to(blob.download_blob().chunks(), dest, max_bytes)

    def _close(self, client: Any) -> None:
        client.close()


# ====================================================================== Amazon S3 and S3-compatible
class S3Connector(StorageConnector):
    kind, label, default_port, container_label = "s3", "Amazon S3 / S3-compatible", 443, "Bucket"

    def _client(self) -> Any:
        import boto3
        from botocore.config import Config
        s = self.spec
        scheme = "https" if s.options.get("https", True) else "http"
        port = "" if (scheme == "https" and s.port == 443) or (scheme == "http" and s.port == 80) else f":{s.port}"
        region = s.options.get("region") or "us-east-1"
        kwargs: dict[str, Any] = {}
        if not (s.host.endswith("amazonaws.com") and s.host.startswith("s3.")):          # real AWS resolves its own endpoint from the region
            kwargs["endpoint_url"] = f"{scheme}://{s.host}{port}"
        config = Config(connect_timeout=s.connect_timeout, read_timeout=120, retries={"max_attempts": 2}, s3={"addressing_style": "path" if s.options.get("path_style") else "auto"})
        return boto3.client("s3", aws_access_key_id=s.user, aws_secret_access_key=s.password, region_name=region, config=config, **kwargs)

    def _info(self, client: Any) -> str:
        if self.spec.database:
            client.head_bucket(Bucket=self.spec.database)
        else:
            client.list_buckets()
        return "S3 API"

    def _containers(self, client: Any) -> list[str]:
        if self.spec.database:
            return [self.spec.database]
        return [b["Name"] for b in client.list_buckets().get("Buckets", [])]

    def _list(self, client: Any, container: str, prefix: str) -> list[StorageEntry]:
        out: list[StorageEntry] = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=container, Prefix=prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                out.append(StorageEntry(name=posixpath.basename(cp["Prefix"].rstrip("/")) + "/", key=cp["Prefix"], size=None, modified=None, is_dir=True))
            for obj in page.get("Contents", []):
                if obj["Key"] == prefix or obj["Key"].endswith("/"):
                    continue
                out.append(StorageEntry(name=posixpath.basename(obj["Key"]), key=obj["Key"], size=int(obj["Size"]),
                                        modified=obj["LastModified"].isoformat(timespec="seconds"), is_dir=False))
            if len(out) >= MAX_LIST:
                break
        return out

    def _download(self, client: Any, container: str, key: str, dest: Path, max_bytes: int) -> int:
        body = client.get_object(Bucket=container, Key=key)["Body"]
        try:
            return self._stream_to(body.iter_chunks(CHUNK), dest, max_bytes)
        finally:
            body.close()


# ====================================================================== SFTP
def fingerprint(key: Any) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class SftpConnector(StorageConnector):
    kind, label, default_port, container_label = "sftp", "SFTP server", 22, "Base directory"

    def _base(self) -> str:
        return "/" + (self.spec.database or "").strip("/")

    def _pinned(self) -> Optional[str]:
        return self._base()                                      # an SFTP connection is always confined to its base directory (default: the login's root)

    def _client(self) -> Any:
        import paramiko
        s = self.spec
        sock = socket.create_connection((s.host, s.port), timeout=s.connect_timeout)
        transport = paramiko.Transport(sock)
        transport.banner_timeout = s.connect_timeout
        try:
            transport.start_client(timeout=s.connect_timeout)
            presented = fingerprint(transport.get_remote_server_key())
            pinned = str(s.options.get("host_key_sha256") or "").strip()
            if pinned != presented:                               # never log in to a server whose identity has not been confirmed
                raise HostKeyError(("the server's host key is not the trusted one" if pinned else "the server's host key has not been trusted yet")
                                   + f": it presented {presented}. Check it with the server's owner, then trust it.", presented)
            transport.auth_password(s.user, s.password)
            sftp = paramiko.SFTPClient.from_transport(transport)
            sftp.get_channel().settimeout(120)
            return sftp
        except Exception:
            transport.close()
            raise

    def _close(self, client: Any) -> None:
        try:
            transport = client.get_channel().get_transport()
            client.close()
            transport.close()
        except Exception:  # noqa: BLE001
            pass

    def _info(self, client: Any) -> str:
        client.listdir(self._base())
        return client.get_channel().get_transport().remote_version

    def _containers(self, client: Any) -> list[str]:
        return [self._base()]

    def _inside(self, container: str, prefix: str) -> str:
        path = posixpath.normpath(posixpath.join(container, prefix))
        if path != container.rstrip("/") and not path.startswith(container.rstrip("/") + "/"):
            raise ConnectorError(f"'{prefix}' is outside the base directory")
        return path

    def _list(self, client: Any, container: str, prefix: str) -> list[StorageEntry]:
        folder = self._inside(container, prefix)
        out = []
        try:
            attrs = client.listdir_attr(folder)[:MAX_LIST]
        except FileNotFoundError:
            return []                                              # like a blob / S3 prefix with nothing under it
        for a in attrs:
            full = posixpath.join(folder, a.filename)
            rel = posixpath.relpath(full, container.rstrip("/") or "/")
            if statmod.S_ISDIR(a.st_mode or 0):
                out.append(StorageEntry(name=a.filename + "/", key=rel + "/", size=None, modified=None, is_dir=True))
            elif statmod.S_ISREG(a.st_mode or 0):
                out.append(StorageEntry(name=a.filename, key=rel, size=int(a.st_size or 0), modified=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(a.st_mtime or 0)), is_dir=False))
        return out

    def _download(self, client: Any, container: str, key: str, dest: Path, max_bytes: int) -> int:
        path = self._inside(container, key)
        if not statmod.S_ISREG(client.stat(path).st_mode or 0):
            raise ConnectorError(f"'{key}' is not a regular file")
        with client.open(path, "rb") as fh:
            fh.prefetch()
            return self._stream_to(iter(lambda: fh.read(CHUNK), b""), dest, max_bytes)


STORAGE: dict[str, type[StorageConnector]] = {c.kind: c for c in (AzureBlobConnector, S3Connector, SftpConnector)}


def storage_for(spec: ConnectionSpec, client_factory: Optional[Callable[[], Any]] = None) -> StorageConnector:
    cls = STORAGE.get(spec.dialect)
    if cls is None:
        raise ConnectorError(f"unsupported storage type '{spec.dialect}' (supported: {', '.join(sorted(STORAGE))})")
    return cls(spec, client_factory)
