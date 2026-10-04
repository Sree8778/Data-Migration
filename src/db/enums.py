"""Controlled vocabularies shared by the ORM models and the Pydantic schemas."""

from __future__ import annotations

import enum


class SystemType(str, enum.Enum):
    ORACLE = "ORACLE"
    SAP_ECC = "SAP_ECC"
    SAP_S4HANA = "SAP_S4HANA"
    MSSQL = "MSSQL"
    FLAT_FILE = "FLAT_FILE"


class TargetType(str, enum.Enum):
    SOURCE = "SOURCE"
    STAGING = "STAGING"
    TARGET = "TARGET"


class PiiClassification(str, enum.Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED_PII = "RESTRICTED_PII"


class MappingStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RuleType(str, enum.Enum):
    DIRECT_COPY = "DIRECT_COPY"
    VALUE_MAPPING = "VALUE_MAPPING"
    SQL_EXPRESSION = "SQL_EXPRESSION"
    PYTHON_SNIPPET = "PYTHON_SNIPPET"
    STATIC_VALUE = "STATIC_VALUE"


class MigrationPhase(str, enum.Enum):
    EXTRACT = "EXTRACT"
    PROFILE = "PROFILE"
    TRANSFORM = "TRANSFORM"
    VALIDATE = "VALIDATE"
    LOAD = "LOAD"
    RECONCILE = "RECONCILE"
    COMPLETE = "COMPLETE"


class RunStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
