"""Saved database connections.

Only non-secret properties (host, port, database, user) are written to disk, to workspace/connections.json. The password is
never persisted: it is either held in server memory after the user types it (lost on restart, like the Gemini key stack) or
read from an environment variable named by `password_env`. Anything that returns a connection to the browser reports only
whether a password is available, never the password itself.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.connectors.base import ConnectionSpec, ConnectorError
from src.connectors.firestore import GOOGLE_HOST, LOOPBACK
from src.connectors.registry import ALL_KINDS, info

NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
HOST = re.compile(r"^[A-Za-z0-9_.:\-\[\]]{1,253}$")
ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-:.]{3,98}$")
DATABASE_ID = re.compile(r"^(\(default\)|[a-z][a-z0-9-]{2,61}[a-z0-9])$")
def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(value)


SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
# encrypt / cafile / validate_host: SQL Server (Azure SQL always encrypts); sslmode / sslrootcert: PostgreSQL; ssl / ssl_ca / ssl_verify: MySQL.
# The *file* options name a CA bundle on the SERVER; they are only ever used to verify the database's certificate.
ALLOWED_OPTIONS = {"connect_timeout": int, "sslmode": str, "sslrootcert": str, "ssl_ca": str, "cafile": str, "encrypt": _to_bool, "validate_host": _to_bool,
                   "ssl": _to_bool, "ssl_verify": _to_bool,
                   # MongoDB: the database the user is defined in, TLS on/off, and a CA bundle on the SERVER to verify the server certificate
                   "auth_source": str, "tls": _to_bool, "tls_ca_file": str,
                   # Firestore: the database inside the Firebase project ("(default)" unless the project has several)
                   "database_id": str,
                   # file storage: https / path_style (emulators, MinIO), auth = key | sas (Azure), region (S3), host_key_sha256 (SFTP: the pinned server key)
                   "https": _to_bool, "path_style": _to_bool, "auth": str, "region": str, "host_key_sha256": str}


class ConnectionInputError(ValueError):
    """The submitted connection properties are not acceptable (message is safe to show)."""


@dataclass
class ConnectionRecord:
    name: str
    dialect: str
    host: str
    port: int
    database: str
    user: str
    password_env: Optional[str] = None
    options: dict[str, Any] = field(default_factory=dict)
    role: str = "source"                 # source = read-only extraction; target = the application may create tables and load into it
    default_schema: Optional[str] = None
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""


class ConnectionStore:
    def __init__(self, directory: Path, allowed_hosts: Optional[tuple[str, ...]] = None, env: Optional[dict[str, str]] = None) -> None:
        self._file = directory / "connections.json"
        self._allowed = tuple(h.lower() for h in allowed_hosts) if allowed_hosts else None
        self._env = env if env is not None else os.environ
        self._lock = threading.Lock()
        self._passwords: dict[str, str] = {}

    # ------------------------------------------------------------------ validation
    def validate(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name", "")).strip()
        dialect = str(data.get("dialect", "")).strip().lower()
        host = str(data.get("host", "")).strip()
        user = str(data.get("user", "")).strip()
        database = str(data.get("database", "")).strip()
        if dialect == "firestore":                                      # the real service has one address and the key says who it is: only the project ID has to be typed
            host = host or GOOGLE_HOST
            user = user or "service-account"
        if not NAME.match(name):
            raise ConnectionInputError("name must be 1-64 letters, digits, '.', '_' or '-'")
        if dialect not in ALL_KINDS:
            raise ConnectionInputError(f"connection type must be one of: {', '.join(ALL_KINDS)}")
        if not HOST.match(host):
            raise ConnectionInputError("host must be a host name or IP address (no spaces, paths or URLs)")
        if self._allowed is not None and host.lower() not in self._allowed and not (dialect == "firestore" and host.lower() == GOOGLE_HOST):
            raise ConnectionInputError(f"host '{host}' is not on this server's allowed list (MIGRATION_DB_ALLOWED_HOSTS)")
        try:
            port = int(data.get("port") or info(dialect).default_port)
        except (TypeError, ValueError):
            raise ConnectionInputError("port must be a number") from None
        if not 1 <= port <= 65535:
            raise ConnectionInputError("port must be between 1 and 65535")
        if not user or len(user) > 128:
            raise ConnectionInputError("user is required (at most 128 characters)")
        if len(database) > 128 or (dialect in ("oracle", "postgresql") and not database):
            raise ConnectionInputError("database / service name is required (at most 128 characters)" if dialect in ("oracle", "postgresql") else "database name is too long")
        role = str(data.get("role") or "source").strip().lower()
        if role not in ("source", "target"):
            raise ConnectionInputError("role must be 'source' or 'target'")
        default_schema = str(data.get("default_schema") or "").strip() or None
        if default_schema and not re.match(r"^[A-Za-z_][A-Za-z0-9_$#]{0,127}$", default_schema) and not (dialect == "firestore" and DATABASE_ID.match(default_schema)):
            raise ConnectionInputError("the default schema is not a valid schema name")
        env_name = str(data.get("password_env") or "").strip() or None
        if env_name and not ENV.match(env_name):
            raise ConnectionInputError("password_env must be an environment variable name")
        options: dict[str, Any] = {}
        for key, value in (data.get("options") or {}).items():
            if key not in ALLOWED_OPTIONS:
                raise ConnectionInputError(f"unknown option '{key}' (allowed: {', '.join(sorted(ALLOWED_OPTIONS))})")
            try:
                options[key] = ALLOWED_OPTIONS[key](value)
            except (TypeError, ValueError):
                raise ConnectionInputError(f"option '{key}' has an invalid value") from None
        if dialect == "mongodb" and role == "target":
            raise ConnectionInputError("MongoDB can only be a source: data is read from it, never written to it")
        if dialect == "firestore":
            if not PROJECT_ID.match(database):
                raise ConnectionInputError("the Firebase project ID is required (for example my-app-1234: Firebase console > Project settings)")
            if host.lower() != GOOGLE_HOST and host.lower() not in LOOPBACK:
                raise ConnectionInputError(f"host must be {GOOGLE_HOST} (the real service) or localhost / 127.0.0.1 (the Firestore emulator)")
            database_id = options.get("database_id")
            if database_id is not None and not DATABASE_ID.match(database_id):
                raise ConnectionInputError("database_id must be (default) or the ID of a Firestore database in the project (lower-case letters, digits and '-')")
        if "auth" in options and options["auth"] not in ("key", "sas"):
            raise ConnectionInputError("auth must be 'key' or 'sas'")
        if "host_key_sha256" in options and options["host_key_sha256"] and not re.match(r"^SHA256:[A-Za-z0-9+/]{43}$", options["host_key_sha256"]):
            raise ConnectionInputError("host_key_sha256 must look like SHA256:<43 base64 characters>")
        if "sslmode" in options and options["sslmode"] not in SSL_MODES:
            raise ConnectionInputError(f"sslmode must be one of: {', '.join(SSL_MODES)}")
        if "connect_timeout" in options and not 1 <= options["connect_timeout"] <= 120:
            raise ConnectionInputError("connect_timeout must be 1-120 seconds")
        return {"name": name, "dialect": dialect, "host": host, "port": port, "database": database, "user": user, "password_env": env_name, "options": options,
                "role": role, "default_schema": default_schema}

    # ------------------------------------------------------------------ persistence
    def _read(self) -> dict[str, dict]:
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self._file)

    def list(self) -> list[ConnectionRecord]:
        with self._lock:
            return [ConnectionRecord(**v) for _, v in sorted(self._read().items())]

    def get(self, name: str) -> Optional[ConnectionRecord]:
        with self._lock:
            raw = self._read().get(name)
        return ConnectionRecord(**raw) if raw else None

    def save(self, data: dict[str, Any], user: str, password: Optional[str] = None) -> ConnectionRecord:
        clean = self.validate(data)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            stored = self._read()
            previous = stored.get(clean["name"], {})
            moved = previous and any(previous.get(k) != clean[k] for k in ("dialect", "host", "port", "user"))
            if moved and not password:                      # a typed password belongs to the server it was typed for: repointing the connection forgets it
                self._passwords.pop(clean["name"], None)
            record = ConnectionRecord(**clean, created_by=previous.get("created_by") or user, created_at=previous.get("created_at") or now, updated_at=now)
            stored[record.name] = asdict(record)
            self._write(stored)
            if password:
                self._passwords[record.name] = password        # memory only
        return record

    def delete(self, name: str) -> bool:
        with self._lock:
            stored = self._read()
            if stored.pop(name, None) is None:
                return False
            self._write(stored)
            self._passwords.pop(name, None)
            return True

    # ------------------------------------------------------------------ secrets
    def password_source(self, record: ConnectionRecord) -> str:
        """'memory', 'env:NAME' or 'missing' - never the value."""
        with self._lock:
            if record.name in self._passwords:
                return "memory"
        if record.password_env and self._env.get(record.password_env):
            return f"env:{record.password_env}"
        return "missing"

    def spec_for(self, name: str, password_override: Optional[str] = None) -> ConnectionSpec:
        record = self.get(name)
        if record is None:
            raise ConnectorError(f"connection '{name}' does not exist")
        password = password_override or None                     # an empty override means "none given", not "use an empty password"
        if password is None:
            with self._lock:
                password = self._passwords.get(name)
        if password is None and record.password_env:
            password = self._env.get(record.password_env)
        if password is None and record.dialect == "firestore" and record.host.lower() in LOOPBACK:
            password = ""                                          # the Firestore emulator needs no key
        if password is None:
            raise ConnectorError(f"no password is available for connection '{name}': enter it again (it is kept in memory only) or set the environment variable named in password_env")
        return ConnectionSpec(dialect=record.dialect, host=record.host, port=record.port, database=record.database, user=record.user, password=password, options=dict(record.options))

    def resolve_password(self, password: Optional[str], env_name: Optional[str]) -> str:
        """A typed password, or else the server environment variable the user named."""
        if password:
            return password
        if env_name:
            if not ENV.match(env_name):
                raise ConnectionInputError("password_env must be an environment variable name")
            value = self._env.get(env_name)
            if not value:
                raise ConnectionInputError(f"the environment variable {env_name} is not set on the server")
            return value
        return ""

    def spec_inline(self, data: dict[str, Any], password: str) -> ConnectionSpec:
        clean = self.validate({**data, "name": data.get("name") or "inline"})
        return ConnectionSpec(dialect=clean["dialect"], host=clean["host"], port=clean["port"], database=clean["database"], user=clean["user"], password=password, options=clean["options"])
