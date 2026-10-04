"""Module 1 integration test: schema, SAP seed, legacy Oracle source, draft mapping, integrity."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from src.db.enums import MappingStatus, PiiClassification, RuleType, SystemType, TargetType
from src.db.models import (
    CatColumn,
    CatTable,
    MapFieldRule,
    MapTableRule,
    MapValueLookup,
    MigMigrationRun,
)
from src.schemas.metadata import (
    BulkColumnRegistration,
    ColumnCreate,
    DataSourceCreate,
    MappingSetCreate,
    TableCreate,
)
from src.seeds.seed_sap_catalog import SAP_BP_TABLES, SAP_TARGET_SYSTEM_NAME, seed_sap_catalog
from src.services.metadata_service import (
    ConflictError,
    MappingValidationError,
    NotFoundError,
)

EXPECTED_TABLES = {
    "cat_data_sources",
    "cat_tables",
    "cat_columns",
    "map_table_rules",
    "map_field_rules",
    "map_value_lookups",
    "mig_migration_runs",
    "mig_cell_lineage",
    "gov_defect_aggregates",
}


def _register_oracle_ar_customers(service):
    source = service.register_data_source(
        DataSourceCreate(
            system_name="LEGACY_ORACLE_AR",
            system_type=SystemType.ORACLE,
            target_type=TargetType.SOURCE,
            connection_config={"host": "legacy-db.example.com", "port": 1521, "password_ref": "vault:oracle/ar"},
        )
    )
    table = service.register_table(
        TableCreate(
            source_id=source.id,
            table_name="AR_CUSTOMERS",
            business_name="Legacy AR customers",
            row_count_estimate=125_000,
        )
    )
    columns = service.bulk_register_columns(
        BulkColumnRegistration(
            table_id=table.id,
            columns=[
                ColumnCreate(column_name="CUST_ID", data_type="NUMBER", numeric_precision=10,
                             numeric_scale=0, is_primary_key=True),
                ColumnCreate(column_name="CUST_NAME", data_type="VARCHAR2", char_max_length=100,
                             is_nullable=False, pii_classification=PiiClassification.CONFIDENTIAL,
                             null_ratio="0.0000", distinct_ratio="0.9800",
                             sample_values=["ACME GmbH", "Globex Corp"]),
                ColumnCreate(column_name="CUST_TYPE", data_type="VARCHAR2", char_max_length=1,
                             sample_values=["C", "V"]),
                ColumnCreate(column_name="COUNTRY_CD", data_type="VARCHAR2", char_max_length=2),
            ],
        )
    )
    return source, table, columns


def _bt(service, name):
    target = service.get_target_dictionary()
    return next(t for t in target.tables if t.table.table_name == name)


# ------------------------------------------------------------------ schema
def test_schema_contains_all_module1_tables(engine):
    assert EXPECTED_TABLES <= set(sa.inspect(engine).get_table_names())


def test_lineage_pk_is_bigint_and_jsonb_columns(engine):
    insp = sa.inspect(engine)
    pk_col = next(c for c in insp.get_columns("mig_cell_lineage") if c["name"] == "id")
    assert isinstance(pk_col["type"], sa.BigInteger)
    cfg = next(c for c in insp.get_columns("cat_data_sources") if c["name"] == "connection_config")
    assert cfg["type"].__class__.__name__ == "JSONB"


# ------------------------------------------------------------------ SAP seed
def test_seed_registers_sap_bp_dictionary(service):
    result = seed_sap_catalog(service)
    assert result.tables == 6
    assert result.columns == sum(len(t.columns) for t in SAP_BP_TABLES)

    dictionary = service.get_target_dictionary("BUSINESS_PARTNER")
    assert [t.table.table_name for t in dictionary.tables] == ["BUT000", "BUT100", "BUT020", "ADRC", "KNB1", "KNVV"]

    but000 = _bt(service, "BUT000")
    assert {c.column_name for c in but000.columns} >= {"TYPE", "BU_GROUP", "BP_EXT", "NAME_ORG1", "NAME_ORG2", "BU_SORT1"}
    mandatory = {c.column_name for c in but000.columns if c.is_mandatory}
    assert {"TYPE", "BU_GROUP", "NAME_ORG1"} <= mandatory

    rltyp = next(c for c in _bt(service, "BUT100").columns if c.column_name == "RLTYP")
    assert rltyp.sample_values == ["FLCU00", "FLCU01", "FLVN00", "FLVN01"]

    adrc = {c.column_name: c for c in _bt(service, "ADRC").columns}
    assert {"COUNTRY", "STREET", "HOUSE_NUM1", "CITY1", "POST_CODE1", "REGION", "LANGU"} <= set(adrc)
    assert adrc["COUNTRY"].check_table == "T005"
    assert adrc["STREET"].pii_classification == PiiClassification.RESTRICTED_PII

    knb1 = {c.column_name: c for c in _bt(service, "KNB1").columns}
    assert {"BUKRS", "AKONT", "ZTERM", "ZWELS"} <= set(knb1)
    assert knb1["AKONT"].is_mandatory and knb1["AKONT"].check_table == "SKB1"

    knvv = {c.column_name for c in _bt(service, "KNVV").columns}
    assert {"VKORG", "VTWEG", "SPART", "WAERS", "INCO1"} <= knvv

    for table in dictionary.tables:  # every column has type + length info
        for col in table.columns:
            assert col.data_type and col.char_max_length


def test_seed_is_idempotent_and_preserves_profiling_stats(service, session_factory):
    first = seed_sap_catalog(service)
    with session_factory.begin() as s:
        s.execute(
            sa.update(CatColumn)
            .where(CatColumn.column_name == "NAME_ORG1")
            .values(null_ratio=sa.literal(0.25))
        )
    second = seed_sap_catalog(service)
    assert first == second

    with session_factory() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(CatTable)) == 6
        assert s.scalar(sa.select(sa.func.count()).select_from(CatColumn)) == first.columns
        stat = s.scalar(sa.select(CatColumn.null_ratio).where(CatColumn.column_name == "NAME_ORG1"))
        assert float(stat) == 0.25


def test_unknown_object_raises(service):
    seed_sap_catalog(service)
    with pytest.raises(NotFoundError):
        service.get_target_dictionary("MATERIAL")


# ------------------------------------------------------------------ legacy source + mapping
def test_register_oracle_source_and_columns(service):
    source, table, columns = _register_oracle_ar_customers(service)
    assert source.system_type == SystemType.ORACLE and source.target_type == TargetType.SOURCE
    assert table.table_name == "AR_CUSTOMERS" and table.row_count_estimate == 125_000
    assert [c.column_name for c in columns] == ["CUST_ID", "CUST_NAME", "CUST_TYPE", "COUNTRY_CD"]
    cust_id = columns[0]
    assert cust_id.is_primary_key and not cust_id.is_nullable
    assert columns[1].sample_values == ["ACME GmbH", "Globex Corp"]


def test_plaintext_secret_in_connection_config_rejected():
    with pytest.raises(ValueError, match="plaintext secret"):
        DataSourceCreate(
            system_name="X", system_type=SystemType.ORACLE, target_type=TargetType.SOURCE,
            connection_config={"password": "hunter2"},
        )


def test_create_draft_mapping_set_and_versioning(service):
    seed_sap_catalog(service)
    _, ar_customers, _ = _register_oracle_ar_customers(service)
    but000 = _bt(service, "BUT000").table

    first = service.create_mapping_set(
        MappingSetCreate(source_table_id=ar_customers.id, target_table_id=but000.id, created_by="alice")
    )
    assert first.status == MappingStatus.DRAFT
    assert first.version == 1
    assert first.approved_by is None and first.approved_at is None

    second = service.create_mapping_set(
        MappingSetCreate(source_table_id=ar_customers.id, target_table_id=but000.id, created_by="alice")
    )
    assert second.version == 2

    with pytest.raises(ConflictError):
        service.create_mapping_set(
            MappingSetCreate(source_table_id=ar_customers.id, target_table_id=but000.id,
                             created_by="alice", version=1)
        )


def test_mapping_set_direction_and_existence_validation(service):
    seed_sap_catalog(service)
    _, ar_customers, _ = _register_oracle_ar_customers(service)
    but000 = _bt(service, "BUT000").table
    adrc = _bt(service, "ADRC").table

    with pytest.raises(MappingValidationError):  # target system as source
        service.create_mapping_set(MappingSetCreate(source_table_id=but000.id, target_table_id=adrc.id, created_by="a"))
    with pytest.raises(MappingValidationError):  # legacy table as target
        service.create_mapping_set(
            MappingSetCreate(source_table_id=ar_customers.id, target_table_id=ar_customers.id, created_by="a")
        )
    with pytest.raises(NotFoundError):
        service.create_mapping_set(MappingSetCreate(source_table_id=999_999, target_table_id=but000.id, created_by="a"))


# ------------------------------------------------------------------ referential integrity
@pytest.fixture()
def mapping_env(service):
    seed_sap_catalog(service)
    _, ar_customers, ar_cols = _register_oracle_ar_customers(service)
    but000 = _bt(service, "BUT000")
    mapping = service.create_mapping_set(
        MappingSetCreate(source_table_id=ar_customers.id, target_table_id=but000.table.id, created_by="alice")
    )
    return {
        "mapping": mapping,
        "src": {c.column_name: c for c in ar_cols},
        "tgt": {c.column_name: c for c in but000.columns},
        "ar_table": ar_customers,
        "but000": but000.table,
    }


def test_field_rules_lookups_runs_and_lineage_link_correctly(mapping_env, session_factory):
    env = mapping_env
    with session_factory.begin() as s:
        rule = MapFieldRule(
            mapping_set_id=env["mapping"].id,
            source_column_id=env["src"]["CUST_TYPE"].id,
            target_column_id=env["tgt"]["TYPE"].id,
            rule_type=RuleType.VALUE_MAPPING,
            ai_suggested=True, ai_model_name="claude-sonnet-5-5", ai_confidence_score=0.93,
            ai_reasoning="Legacy C/V codes map to BP category 2 (organization).",
            is_mandatory_target=True,
        )
        s.add(rule)
        s.flush()
        s.add_all([
            MapValueLookup(field_rule_id=rule.id, source_value="C", target_value="2"),
            MapValueLookup(field_rule_id=rule.id, source_value="V", target_value="2"),
        ])
        run = MigMigrationRun(mapping_set_id=env["mapping"].id, wave_name="WAVE_1")
        s.add(run)
        s.flush()
        rule_id, run_id = rule.id, run.id

    with session_factory() as s:
        run = s.get(MigMigrationRun, run_id)
        assert run.records_loaded == 0 and run.status.value == "PENDING"
        assert s.get(MapFieldRule, rule_id).ai_confidence_score == Decimal("0.9300")


def _expect_integrity_error(session_factory, obj):
    with session_factory() as s:
        s.add(obj)
        with pytest.raises(IntegrityError):
            s.flush()


def test_foreign_keys_reject_orphans(mapping_env, session_factory):
    env = mapping_env
    _expect_integrity_error(session_factory, CatTable(source_id=999_999, table_name="ORPHAN"))
    _expect_integrity_error(
        session_factory,
        MapTableRule(source_table_id=env["ar_table"].id, target_table_id=999_999, created_by="x"),
    )
    _expect_integrity_error(
        session_factory,
        MapFieldRule(mapping_set_id=999_999, source_column_id=env["src"]["CUST_ID"].id,
                     target_column_id=env["tgt"]["TYPE"].id, rule_type=RuleType.DIRECT_COPY),
    )
    _expect_integrity_error(session_factory, MigMigrationRun(mapping_set_id=999_999, wave_name="W"))


def test_governed_rows_cannot_be_deleted_while_referenced(mapping_env, session_factory):
    env = mapping_env
    with session_factory.begin() as s:
        s.add(MapFieldRule(
            mapping_set_id=env["mapping"].id, source_column_id=env["src"]["CUST_NAME"].id,
            target_column_id=env["tgt"]["NAME_ORG1"].id, rule_type=RuleType.DIRECT_COPY,
        ))

    for stmt in (
        sa.delete(MapTableRule).where(MapTableRule.id == env["mapping"].id),
        sa.delete(CatTable).where(CatTable.id == env["but000"].id),
        sa.delete(CatTable).where(CatTable.id == env["ar_table"].id),
    ):
        with session_factory() as s:
            with pytest.raises(IntegrityError):
                s.execute(stmt)


def test_unique_and_check_constraints(mapping_env, session_factory):
    env = mapping_env
    mapping_id = env["mapping"].id
    tgt, src = env["tgt"], env["src"]

    # one rule per target column per mapping set
    with session_factory.begin() as s:
        s.add(MapFieldRule(mapping_set_id=mapping_id, source_column_id=src["CUST_ID"].id,
                           target_column_id=tgt["BP_EXT"].id, rule_type=RuleType.DIRECT_COPY))
    _expect_integrity_error(
        session_factory,
        MapFieldRule(mapping_set_id=mapping_id, source_column_id=src["CUST_ID"].id,
                     target_column_id=tgt["BP_EXT"].id, rule_type=RuleType.DIRECT_COPY),
    )
    # DIRECT_COPY needs a source column
    _expect_integrity_error(
        session_factory,
        MapFieldRule(mapping_set_id=mapping_id, target_column_id=tgt["BU_GROUP"].id, rule_type=RuleType.DIRECT_COPY),
    )
    # STATIC_VALUE needs logic
    _expect_integrity_error(
        session_factory,
        MapFieldRule(mapping_set_id=mapping_id, target_column_id=tgt["BU_GROUP"].id, rule_type=RuleType.STATIC_VALUE),
    )
    # AI suggestion needs a model name; confidence must be within [0, 1]
    _expect_integrity_error(
        session_factory,
        MapFieldRule(mapping_set_id=mapping_id, target_column_id=tgt["BU_GROUP"].id,
                     rule_type=RuleType.STATIC_VALUE, transformation_logic="'BP01'", ai_suggested=True),
    )
    _expect_integrity_error(
        session_factory,
        MapFieldRule(mapping_set_id=mapping_id, target_column_id=tgt["BU_GROUP"].id,
                     rule_type=RuleType.STATIC_VALUE, transformation_logic="'BP01'",
                     ai_suggested=True, ai_model_name="m", ai_confidence_score=1.5),
    )


def test_sox_approval_controls(mapping_env, session_factory):
    env = mapping_env
    mapping_id = env["mapping"].id
    now = datetime.now(timezone.utc)

    def update(**values):
        with session_factory() as s:
            with pytest.raises(IntegrityError):
                s.execute(sa.update(MapTableRule).where(MapTableRule.id == mapping_id).values(**values))

    update(status=MappingStatus.APPROVED)                                   # approver required
    update(status=MappingStatus.APPROVED, approved_by="alice", approved_at=now)  # maker != checker

    with session_factory.begin() as s:                                      # valid approval
        s.execute(sa.update(MapTableRule).where(MapTableRule.id == mapping_id)
                  .values(status=MappingStatus.APPROVED, approved_by="bob", approved_at=now))
    with session_factory() as s:
        assert s.get(MapTableRule, mapping_id).status == MappingStatus.APPROVED
