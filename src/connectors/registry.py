"""One place that knows every kind of connection: databases (SQL) and file storage (Azure Blob, S3, SFTP)."""

from __future__ import annotations

from typing import Optional

from src.connectors.dialects import CONNECTORS
from src.connectors.storage import STORAGE

ALL_KINDS = tuple(sorted({*CONNECTORS, *STORAGE}))
DATABASE_LABEL = {"oracle": "Service name", "postgresql": "Database", "mssql": "Database", "mysql": "Database (schema)", "mongodb": "Authentication database", "firestore": "Firebase project ID",
                  "azure_blob": "Default container (optional)", "s3": "Default bucket (optional)", "sftp": "Base directory"}


def family(kind: str) -> Optional[str]:
    return "database" if kind in CONNECTORS else "storage" if kind in STORAGE else None


def info(kind: str):
    return CONNECTORS.get(kind) or STORAGE[kind]
