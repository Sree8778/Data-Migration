"""Shared Business Partner assembly: ALPHA conversion + streaming reader over `valid.parquet`.

Both the Migration Cockpit generator and the SAP loader consume `iter_bp_records`, so the two
outputs can never disagree about keys, roles or conversions.

Staging columns follow the Module 4 convention `<TABLE>_<COLUMN>` (e.g. BUT000_NAME_ORG1).
Roles come from `BUT100_RLTYP` when staging has it (comma/space separated), otherwise from the
configured defaults.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from src.schemas.sap import BusinessPartnerRecord
from src.services._duckdb_util import DEFAULT_MEMORY_LIMIT_MB, bounded_connection, lit, q

PK_COLUMN = "_SRC_PK"
KEY_LENGTH = 10
VALID_ROLES = ("FLCU00", "FLCU01", "FLVN00", "FLVN01")

TABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "BUT000": ("TYPE", "BU_GROUP", "NAME_ORG1", "NAME_ORG2", "BU_SORT1"),
    "ADRC": ("COUNTRY", "STREET", "HOUSE_NUM1", "CITY1", "POST_CODE1", "REGION", "LANGU"),
    "KNB1": ("BUKRS", "AKONT", "ZTERM", "ZWELS"),
    "KNVV": ("VKORG", "VTWEG", "SPART", "WAERS", "INCO1"),
}
COMPANY_CODE_KEY = ("BUKRS",)
SALES_AREA_KEY = ("VKORG", "VTWEG", "SPART")
# Fields that SAP stores with the ALPHA conversion exit: numeric values are left-padded with zeros.
ALPHA_FIELDS: dict[str, int] = {"BP_EXT": KEY_LENGTH, "AKONT": KEY_LENGTH}


def alpha_input(value: Optional[str], length: int = KEY_LENGTH) -> Optional[str]:
    """SAP ALPHA conversion exit (input direction): pure-digit values are zero-padded to `length`.

    Non-numeric values are returned trimmed but unchanged; longer values are never truncated.
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if re.fullmatch(r"[0-9]+", v) and len(v) < length:
        return v.zfill(length)
    return v


@dataclass(frozen=True)
class BpConfig:
    default_roles: tuple[str, ...] = ("FLCU00", "FLCU01")

    def __post_init__(self) -> None:
        bad = [r for r in self.default_roles if r not in VALID_ROLES]
        if bad:
            raise ValueError(f"unsupported BP roles: {bad}")


def _clean(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def assemble_record(row: dict[str, Optional[str]], config: BpConfig, has_role_column: bool) -> BusinessPartnerRecord:
    """Build one BP record from a staging row (values are text, already trimmed or None)."""
    pk = str(row[PK_COLUMN])
    issues: list[str] = []
    warnings: list[str] = []

    def fields(table: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in TABLE_FIELDS[table]:
            value = _clean(row.get(f"{table}_{name}"))
            if value is not None:
                out[name] = alpha_input(value, ALPHA_FIELDS[name]) if name in ALPHA_FIELDS else value
        return out

    bp_ext = alpha_input(_clean(row.get("BUT000_BP_EXT")), KEY_LENGTH)
    if bp_ext is None:
        issues.append("BP_EXT (partner key) is empty")
    elif len(bp_ext) > KEY_LENGTH:
        issues.append(f"BP_EXT '{bp_ext}' is longer than {KEY_LENGTH} characters")

    company = fields("KNB1")
    if company and not all(k in company for k in COMPANY_CODE_KEY):
        warnings.append("company-code data present without BUKRS: KNB1 row dropped")
        company = {}
    sales = fields("KNVV")
    if sales and not all(k in sales for k in SALES_AREA_KEY):
        warnings.append("sales data present without VKORG/VTWEG/SPART: KNVV row dropped")
        sales = {}

    roles = list(config.default_roles)
    if has_role_column:
        listed = re.split(r"[,\s;]+", _clean(row.get("BUT100_RLTYP")) or "")
        roles = [r.upper() for r in listed if r]
        invalid = [r for r in roles if r not in VALID_ROLES]
        if invalid:
            issues.append(f"unsupported BP role(s): {invalid}")
        if not roles:
            issues.append("no BP role assigned")

    return BusinessPartnerRecord(
        source_pk=pk, bp_ext=bp_ext, general=fields("BUT000"), roles=roles, address=fields("ADRC"),
        company_code=company, sales_area=sales, blocking_issues=issues, warnings=warnings,
    )


def iter_bp_records(
    parquet_path: str | Path,
    config: BpConfig = BpConfig(),
    chunk_size: int = 1000,
    memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB,
) -> Iterator[BusinessPartnerRecord]:
    """Stream BP records from a staging Parquet in bounded chunks (DuckDB does the reading)."""
    path = Path(parquet_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    with bounded_connection(memory_limit_mb) as con:
        relation = f"read_parquet({lit(path.as_posix())})"
        available = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()]
        if PK_COLUMN not in available:
            raise ValueError(f"{PK_COLUMN} missing from {path.name}")
        wanted = ["BUT000_BP_EXT", "BUT100_RLTYP"] + [f"{t}_{f}" for t, fs in TABLE_FIELDS.items() for f in fs]
        selected = [c for c in wanted if c in available]
        cols = ", ".join([f"CAST({q(PK_COLUMN)} AS VARCHAR) AS {q(PK_COLUMN)}"]
                         + [f"CAST({q(c)} AS VARCHAR) AS {q(c)}" for c in selected])
        cursor = con.execute(f"SELECT {cols} FROM {relation}")
        names = [d[0] for d in cursor.description]
        has_role = "BUT100_RLTYP" in selected
        while True:
            rows = cursor.fetchmany(chunk_size)
            if not rows:
                return
            for values in rows:
                yield assemble_record(dict(zip(names, values)), config, has_role)
