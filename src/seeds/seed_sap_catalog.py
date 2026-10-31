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
            _col("TYPE", "CHAR", 1, mandatory=True, check_table="TB0BK", desc="Business partner category: 1 = person, 2 = organization, 3 = group (partner type, customer type)",
                 allowed=["1", "2", "3"]),
            _col("BU_GROUP", "CHAR", 4, mandatory=True, check_table="TB001", desc="BP grouping: the customer or supplier account group (KTOKD, KTOKK, account group, customer group, partner group)"),
            _col("BP_EXT", "CHAR", 20, desc="External business partner number: the key the legacy system uses for the customer or supplier (KUNNR, LIFNR, customer number, customer id, account number)"),
            _col("NAME_ORG1", "CHAR", 40, mandatory=True, pii=Pii.CONFIDENTIAL, desc="Organization name 1: the name of the customer, supplier or company (NAME1, company name, FIRMENNAME, Firma, Kundenname; business partner category 2)"),
            _col("NAME_ORG2", "CHAR", 40, pii=Pii.CONFIDENTIAL, desc="Organization name 2: second name line of the company (NAME2, ZUSATZ, Namenszusatz, c/o, attention, division name)"),
            _col("BU_SORT1", "CHAR", 20, pii=Pii.CONFIDENTIAL, desc="Search term 1: short sort key to find the partner (SORTL, match code, search name)"),
            _col("NAME_FIRST", "CHAR", 40, pii=Pii.CONFIDENTIAL, desc="First name, given name of a person (business partner category 1)"),
            _col("NAME_LAST", "CHAR", 40, pii=Pii.CONFIDENTIAL, desc="Last name, family name, surname of a person (business partner category 1)"),
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
            _col("COUNTRY", "CHAR", 3, mandatory=True, check_table="T005", desc="Country key: the ISO country code of the partner's address (LAND1, country, country code, nation)"),
            _col("STREET", "CHAR", 60, pii=Pii.RESTRICTED_PII, desc="Street name of the address (STRAS, street, address line 1)"),
            _col("HOUSE_NUM1", "CHAR", 10, pii=Pii.RESTRICTED_PII, desc="House number of the address (HAUSN, house no, street number)"),
            _col("CITY1", "CHAR", 40, mandatory=True, pii=Pii.RESTRICTED_PII, desc="City of the address (ORT01, town, locality)"),
            _col("POST_CODE1", "CHAR", 10, pii=Pii.RESTRICTED_PII, desc="Postal code of the address (PSTLZ, ZIP code, postcode)"),
            _col("REGION", "CHAR", 3, check_table="T005S", desc="Region: state or province of the address (REGIO, BLAND, state)"),
            _col("LANGU", "LANG", 1, check_table="T002", desc="Communication language key of the address, one letter such as D, E, F (SPRAS, LANG, language code)"),
        ),
    ),
    _TableSpec(
        "ADR6",
        "E-Mail Addresses",
        "E-mail addresses of an address (optional segment).",
        (
            _col("ADDRNUMBER", "CHAR", 10, pk=True, desc="Address number"),
            _col("SMTP_ADDR", "CHAR", 241, pii=Pii.RESTRICTED_PII, desc="E-mail address (SMTP_ADDR, mail, e-mail, email)"),
        ),
    ),
    _TableSpec(
        "ADR2",
        "Telephone Numbers",
        "Telephone numbers of an address (optional segment).",
        (
            _col("ADDRNUMBER", "CHAR", 10, pk=True, desc="Address number"),
            _col("TEL_NUMBER", "CHAR", 30, pii=Pii.RESTRICTED_PII, desc="Telephone number (TELF1, phone, tel, telephone)"),
            _col("TEL_EXTENS", "CHAR", 10, pii=Pii.RESTRICTED_PII, desc="Telephone extension"),
        ),
    ),
    _TableSpec(
        "ADR3",
        "Fax Numbers",
        "Fax numbers of an address (optional segment).",
        (
            _col("ADDRNUMBER", "CHAR", 10, pk=True, desc="Address number"),
            _col("FAX_NUMBER", "CHAR", 30, pii=Pii.RESTRICTED_PII, desc="Fax number (TELFX, fax)"),
            _col("FAX_EXTENS", "CHAR", 10, pii=Pii.RESTRICTED_PII, desc="Fax extension"),
        ),
    ),
    _TableSpec(
        "DFKKBPTAXNUM",
        "BP: Tax Numbers",
        "Tax and VAT numbers of a business partner (optional segment).",
        (
            _col("PARTNER", "CHAR", 10, pk=True, desc="Business partner number"),
            _col("TAXTYPE", "CHAR", 4, check_table="TFKTAXNUMTYPE", desc="Tax number category, e.g. DE0 (VAT), US1 (EIN) (TAXTYPE, tax type, KTAXTYP)"),
            _col("TAXNUM", "CHAR", 20, pii=Pii.RESTRICTED_PII, desc="Tax number: VAT or tax id of the partner (STCEG, STCD1, UST_IDNR, USt-IdNr, Umsatzsteuer-ID, Steuernummer, VAT number, tax id, EIN)"),
        ),
    ),
    _TableSpec(
        "BUT0BK",
        "BP: Bank Details",
        "Bank details of a business partner (optional segment).",
        (
            _col("PARTNER", "CHAR", 10, pk=True, desc="Business partner number"),
            _col("BKVID", "CHAR", 4, desc="Bank details ID (0001 when empty)"),
            _col("BANKS", "CHAR", 3, check_table="T005", desc="Country of the BANK, for bank details only (BANKS, bank country key, where the bank is located)"),
            _col("BANKL", "CHAR", 15, pii=Pii.RESTRICTED_PII, desc="Bank key of the BANK, for bank details only (BANKL, bank number, sort code, routing number)"),
            _col("BANKN", "CHAR", 18, pii=Pii.RESTRICTED_PII, desc="Bank account number"),
            _col("KOINH", "CHAR", 60, pii=Pii.RESTRICTED_PII, desc="Name of the holder of the bank account, for bank details only (KOINH, account holder)"),
            _col("IBAN", "CHAR", 34, pii=Pii.RESTRICTED_PII, desc="IBAN (without blanks)"),
        ),
    ),
    _TableSpec(
        "KNB1",
        "Customer Master (Company Code)",
        "FI customer data per company code.",
        (
            _col("KUNNR", "CHAR", 10, pk=True, desc="Customer number"),
            _col("BUKRS", "CHAR", 4, pk=True, check_table="T001", desc="Company code: the legal entity the CUSTOMER is created in (BUKRS, company)"),
            _col("AKONT", "CHAR", 10, mandatory=True, check_table="SKB1",
                 desc="Reconciliation account of the CUSTOMER in the general ledger (AKONT, G/L account, receivables control account)"),
            _col("ZTERM", "CHAR", 4, check_table="T052", desc="Terms of payment key of the CUSTOMER, e.g. NT30 (ZTERM, payment terms, net 30 days)"),
            _col("ZWELS", "CHAR", 10, check_table="T042Z", desc="List of payment methods the CUSTOMER pays with (ZWELS, payment method, transfer, check)"),
        ),
    ),
    _TableSpec(
        "KNVV",
        "Customer Master (Sales Data)",
        "SD customer data per sales area.",
        (
            _col("KUNNR", "CHAR", 10, pk=True, desc="Customer number"),
            _col("VKORG", "CHAR", 4, pk=True, check_table="TVKO", desc="Sales organization (VKORG, sales org)"),
            _col("VTWEG", "CHAR", 2, pk=True, check_table="TVTW", desc="Distribution channel (VTWEG, channel)"),
            _col("SPART", "CHAR", 2, pk=True, check_table="TSPA", desc="Division (SPART, product division)"),
            _col("WAERS", "CUKY", 5, mandatory=True, check_table="TCURC", desc="Currency key of the customer, ISO code such as EUR (WAERS, currency code)"),
            _col("INCO1", "CHAR", 3, check_table="TINC", desc="Incoterms part 1 for selling to the CUSTOMER (INCO1, delivery terms)"),
        ),
    ),
    _TableSpec(
        "LFB1",
        "Vendor Master (Company Code)",
        "FI vendor data per company code (optional segment: suppliers).",
        (
            _col("LIFNR", "CHAR", 10, pk=True, desc="Vendor number"),
            _col("BUKRS", "CHAR", 4, check_table="T001", desc="Company code: the legal entity the SUPPLIER (vendor) is created in (BUKRS, company)"),
            _col("AKONT", "CHAR", 10, check_table="SKB1", desc="Reconciliation account of the SUPPLIER (vendor) in the general ledger (AKONT, payables control account)"),
            _col("ZTERM", "CHAR", 4, check_table="T052", desc="Terms of payment key for purchasing from the SUPPLIER (vendor), e.g. NT30 (ZTERM, payment terms)"),
            _col("ZWELS", "CHAR", 10, check_table="T042Z", desc="List of payment methods used to pay the SUPPLIER (vendor) (ZWELS, payment method)"),
        ),
    ),
    _TableSpec(
        "LFM1",
        "Vendor Master (Purchasing Organization)",
        "MM vendor data per purchasing organization (optional segment: suppliers).",
        (
            _col("LIFNR", "CHAR", 10, pk=True, desc="Vendor number"),
            _col("EKORG", "CHAR", 4, check_table="T024E", desc="Purchasing organization of the SUPPLIER (vendor) (EKORG, purchasing org)"),
            _col("WAERS", "CUKY", 5, check_table="TCURC", desc="Purchase order currency for the SUPPLIER (vendor), ISO code such as EUR (WAERS, order currency)"),
            _col("ZTERM", "CHAR", 4, check_table="T052", desc="Terms of payment key for purchasing from the SUPPLIER (vendor), e.g. NT30 (ZTERM, payment terms)"),
            _col("INCO1", "CHAR", 3, check_table="TINC", desc="Incoterms part 1 for purchasing from the SUPPLIER (vendor)"),
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
