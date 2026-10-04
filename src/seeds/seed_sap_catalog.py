"""Idempotent seed of the SAP S/4HANA Business Partner target catalog.

Run:  alembic upgrade head && python -m src.seeds.seed_sap_catalog

Re-running is safe: data sources and tables are matched by name and columns by
(table, column_name); existing rows are updated in place, never duplicated.
Profiling statistics already captured on a column are preserved.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.db.enums import PiiClassification as Pii
from src.db.enums import SystemType, TargetType
from src.db.session import create_db_engine, create_session_factory
from src.schemas.metadata import (
    BulkColumnRegistration,
    ColumnCreate,
    DataSourceCreate,
    TableCreate,
)
from src.services.metadata_service import MetadataService

SAP_TARGET_SYSTEM_NAME = "SAP_S4HANA_TARGET"
BUSINESS_PARTNER = "BUSINESS_PARTNER"


def _col(
    name: str,
    data_type: str,
    length: int,
    *,
    mandatory: bool = False,
    pk: bool = False,
    check_table: str | None = None,
    pii: Pii = Pii.INTERNAL,
    desc: str | None = None,
    allowed: list[str] | None = None,
) -> ColumnCreate:
    return ColumnCreate(
        column_name=name,
        data_type=data_type,
        char_max_length=length,
        is_nullable=not (mandatory or pk),
        is_primary_key=pk,
        is_mandatory=mandatory or pk,
        check_table=check_table,
        pii_classification=pii,
        description=desc,
        sample_values=allowed,
    )


@dataclass(frozen=True)
class _TableSpec:
    name: str
    business_name: str
    description: str
    columns: tuple[ColumnCreate, ...]


SAP_BP_TABLES: tuple[_TableSpec, ...] = (
    _TableSpec(
        "BUT000",
        "BP: General Data I",
        "Business partner header: category, grouping and name data.",
        (
            _col("PARTNER", "CHAR", 10, pk=True, desc="Business partner number"),
            _col("TYPE", "CHAR", 1, mandatory=True, check_table="TB0BK", desc="BP category",
                 allowed=["1", "2", "3"]),
            _col("BU_GROUP", "CHAR", 4, mandatory=True, check_table="TB001", desc="BP grouping"),
            _col("BP_EXT", "CHAR", 20, desc="External BP number (legacy key)"),
            _col("NAME_ORG1", "CHAR", 40, mandatory=True, pii=Pii.CONFIDENTIAL, desc="Organization name 1"),
            _col("NAME_ORG2", "CHAR", 40, pii=Pii.CONFIDENTIAL, desc="Organization name 2"),
            _col("BU_SORT1", "CHAR", 20, pii=Pii.CONFIDENTIAL, desc="Search term 1"),
        ),
    ),
    _TableSpec(
        "BUT100",
        "BP: Roles",
        "Business partner role assignments.",
        (
            _col("PARTNER", "CHAR", 10, pk=True, desc="Business partner number"),
            _col("RLTYP", "CHAR", 6, pk=True, check_table="TB003", desc="BP role",
                 allowed=["FLCU00", "FLCU01", "FLVN00", "FLVN01"]),
        ),
    ),
    _TableSpec(
        "BUT020",
        "BP: Address Assignment",
        "Links a business partner to its address numbers.",
        (
            _col("PARTNER", "CHAR", 10, pk=True, desc="Business partner number"),
            _col("ADDRNUMBER", "CHAR", 10, pk=True, check_table="ADRC", desc="Address number"),
        ),
    ),
    _TableSpec(
        "ADRC",
        "Addresses (Business Address Services)",
        "Central address data.",
        (
            _col("ADDRNUMBER", "CHAR", 10, pk=True, desc="Address number"),
            _col("COUNTRY", "CHAR", 3, mandatory=True, check_table="T005", desc="Country key"),
            _col("STREET", "CHAR", 60, pii=Pii.RESTRICTED_PII, desc="Street"),
            _col("HOUSE_NUM1", "CHAR", 10, pii=Pii.RESTRICTED_PII, desc="House number"),
            _col("CITY1", "CHAR", 40, mandatory=True, pii=Pii.RESTRICTED_PII, desc="City"),
            _col("POST_CODE1", "CHAR", 10, pii=Pii.RESTRICTED_PII, desc="Postal code"),
            _col("REGION", "CHAR", 3, check_table="T005S", desc="Region (state, province)"),
            _col("LANGU", "LANG", 1, check_table="T002", desc="Communication language"),
        ),
    ),
    _TableSpec(
        "KNB1",
        "Customer Master (Company Code)",
        "FI customer data per company code.",
        (
            _col("KUNNR", "CHAR", 10, pk=True, desc="Customer number"),
            _col("BUKRS", "CHAR", 4, pk=True, check_table="T001", desc="Company code"),
            _col("AKONT", "CHAR", 10, mandatory=True, check_table="SKB1",
                 desc="Reconciliation account in G/L"),
            _col("ZTERM", "CHAR", 4, check_table="T052", desc="Terms of payment key"),
            _col("ZWELS", "CHAR", 10, check_table="T042Z", desc="List of payment methods"),
        ),
    ),
    _TableSpec(
        "KNVV",
        "Customer Master (Sales Data)",
        "SD customer data per sales area.",
        (
            _col("KUNNR", "CHAR", 10, pk=True, desc="Customer number"),
            _col("VKORG", "CHAR", 4, pk=True, check_table="TVKO", desc="Sales organization"),
            _col("VTWEG", "CHAR", 2, pk=True, check_table="TVTW", desc="Distribution channel"),
            _col("SPART", "CHAR", 2, pk=True, check_table="TSPA", desc="Division"),
            _col("WAERS", "CUKY", 5, mandatory=True, check_table="TCURC", desc="Currency"),
            _col("INCO1", "CHAR", 3, check_table="TINC", desc="Incoterms part 1"),
        ),
    ),
)


@dataclass(frozen=True)
class SeedResult:
    source_id: int
    tables: int
    columns: int


def seed_sap_catalog(service: MetadataService) -> SeedResult:
    """Register the SAP S/4HANA target system, its BP tables and their columns."""
    source = service.register_data_source(
        DataSourceCreate(
            system_name=SAP_TARGET_SYSTEM_NAME,
            system_type=SystemType.SAP_S4HANA,
            target_type=TargetType.TARGET,
            connection_config={"seeded_by": "seed_sap_catalog", "object": BUSINESS_PARTNER},
        )
    )
    column_count = 0
    for spec in SAP_BP_TABLES:
        table = service.register_table(
            TableCreate(
                source_id=source.id,
                table_name=spec.name,
                business_name=spec.business_name,
                description=spec.description,
                business_object=BUSINESS_PARTNER,
            )
        )
        columns = service.bulk_register_columns(
            BulkColumnRegistration(table_id=table.id, columns=list(spec.columns))
        )
        column_count += len(columns)
    return SeedResult(source_id=source.id, tables=len(SAP_BP_TABLES), columns=column_count)


def main() -> None:
    engine = create_db_engine()
    result = seed_sap_catalog(MetadataService(create_session_factory(engine)))
    print(f"Seeded {result.tables} tables / {result.columns} columns into source id {result.source_id}")
    engine.dispose()


if __name__ == "__main__":
    main()
