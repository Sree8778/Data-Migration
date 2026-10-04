"""Module 4 tests: lookups, DuckDB transformation, hybrid deduplication, run audit trail.

The dataset is a dirty 5,000+ row legacy customer extract. Expected results are computed by an
independent pure-Python model of the rules, then compared with the engine output.
"""

from __future__ import annotations

import csv
import random
from decimal import Decimal

import duckdb
import pytest
import sqlalchemy as sa

from src.db.enums import MappingStatus, RuleType, RunStatus, MigrationPhase, SystemType, TargetType
from src.db.models import (
    CatColumn,
    MapFieldRule,
    MapTableRule,
    MapValueLookup,
    MigCellLineage,
    MigMigrationRun,
)
from src.schemas.execution import (
    DedupConfig,
    ExactKey,
    LineageMode,
    LookupFallback,
    LookupStatus,
)
from src.schemas.metadata import (
    BulkColumnRegistration,
    ColumnCreate,
    DataSourceCreate,
    MappingSetCreate,
    TableCreate,
)
from src.seeds.seed_sap_catalog import seed_sap_catalog
from src.services._duckdb_util import DEFAULT_MEMORY_LIMIT_MB, bounded_connection
from src.services.deduplication_engine import DeduplicationEngine
from src.services.execution_orchestrator import ExecutionError, ExecutionOrchestrator
from src.services.lookup_service import COUNTRY_SEED, PAYMENT_TERMS_SEED, LookupService
from src.services.metadata_service import ConflictError, MappingValidationError, NotFoundError
from src.services.transformation_engine import TransformationCompileError, TransformationEngine

HEADER = ["CUST_ID", "CUST_NAME", "TAX_ID", "EMAIL", "PHONE", "ADDR_STREET", "CITY", "COUNTRY",
          "POSTAL_CODE", "PAYMENT_TERMS"]
BASE_ROWS = 4900
TAX_DUPES, EMAIL_DUPES = 80, 40
COUNTRY_VALUES = ["US", "USA", "United States", "usa", "Germany", " Germany ", "DE", "Deutschland", "France",
                  "FR", "UK", "India", "Canada"]
PAYMENT_VALUES = ["NET30", "Net 30", "NET60", "COD", "", "NET90"]
CITIES = ["Boston", "Austin", "Denver", "Seattle", "Chicago", "Berlin", "Munich", "Paris", "Lyon", "Pune",
          "Leeds", "Toronto", "Dallas", "Phoenix", "Miami"]
SYLLABLES = ["ka", "lo", "mi", "ra", "ve", "tu", "zo", "pe", "xi", "da", "no", "su"]
WORDS = ["Holdings", "Systems", "Logistics", "Foods", "Motors", "Textiles", "Analytics", "Pharma"]
STREET_WORDS = ["Oak", "Maple", "Cedar", "Pine", "Elm", "Birch", "Lake", "Hill", "River", "Sunset"]


# ------------------------------------------------------------------ dataset
def _row(i, name, street, city, postal, country="US", pay="NET30", tax="", email="", phone=""):
    return {"CUST_ID": str(i), "CUST_NAME": name, "TAX_ID": tax, "EMAIL": email, "PHONE": phone,
            "ADDR_STREET": street, "CITY": city, "COUNTRY": country, "POSTAL_CODE": postal,
            "PAYMENT_TERMS": pay}


def expected_errors(r: dict) -> set[str]:
    """Pure-Python model of which target columns fail for a source row."""
    bad = set()
    if len(r["CUST_NAME"].strip()) > 40:
        bad.add("BUT000.NAME_ORG1")
    if not r["CITY"].strip():
        bad.add("ADRC.CITY1")
    country = r["COUNTRY"].strip()
    if not country or country.upper() not in COUNTRY_SEED:
        bad.add("ADRC.COUNTRY")
    pay = r["PAYMENT_TERMS"].strip()
    if pay and pay.upper() not in PAYMENT_TERMS_SEED:
        bad.add("KNB1.ZTERM")
    return bad


def build_dataset() -> list[dict]:
    rng = random.Random(7)
    rows, stems = [], set()
    for i in range(BASE_ROWS):
        while True:
            stem = "".join(rng.choice(SYLLABLES) for _ in range(4)).capitalize()
            if stem not in stems:
                stems.add(stem)
                break
        name = f"{stem} {rng.choice(WORDS)}"
        if i % 97 == 0:
            name = f"  {name}  "  # padding to be trimmed
        if i % 101 == 0:
            name = f"{stem} International Manufacturing and Distribution Holdings"  # > 40 chars
        if i % 211 == 0:
            name = f'{stem}, "Quoted" & Sons'
        postal = "" if i % 12 == 0 else (f"0{rng.randint(1000, 9999)}" if i % 9 == 0 else str(rng.randint(10000, 99999)))
        country = "Atlantis" if i % 50 == 7 else ("" if i % 140 == 3 else rng.choice(COUNTRY_VALUES))
        pay = "XYZ-UNKNOWN" if i % 60 == 11 else rng.choice(PAYMENT_VALUES)
        city = "" if i % 70 == 5 else rng.choice(CITIES)
        rows.append(_row(
            1000 + i, name,
            f"{rng.randint(1, 999)} {rng.choice(STREET_WORDS)} {rng.choice(['Street', 'Avenue', 'Road'])} {i}",
            city, postal, country, pay,
            tax="" if i % 3 == 0 else f"TX-{i:07d}",
            email="" if i % 5 == 0 else f"contact{i}@mail.example.com",
            phone="" if i % 4 == 0 else f"+1-555-{i:07d}",
        ))

    clean = [r for r in rows if not expected_errors(r) and r["TAX_ID"] and r["EMAIL"] and r["PHONE"]
             and r["POSTAL_CODE"]]
    rng.shuffle(clean)
    nxt = 20_000
    for orig in clean[:TAX_DUPES]:  # same tax id, written differently; thinner record, other address
        t = orig["TAX_ID"].lower().replace("-", " ")
        rows.append(_row(nxt, orig["CUST_NAME"].strip().upper(), f"{nxt % 97} Other Lane {nxt}", "Dallas", "",
                         "DE", "COD", tax=t))
        nxt += 1
    for orig in clean[TAX_DUPES:TAX_DUPES + EMAIL_DUPES]:  # same e-mail (case/space), new tax id and name
        rows.append(_row(nxt, f"Zz{nxt} Partners", f"{nxt % 89} Side Street {nxt}", "Miami", "", "US", "NET30",
                         tax=f"NEW-{nxt}", email=f"  {orig['EMAIL'].upper()} "))
        nxt += 1

    rows += [  # fuzzy company-name families (no shared tax / e-mail / phone)
        _row(900001, "Siemens Healthineers Inc", "100 Medical Parkway", "Malvern", "19355", "USA", ""),
        _row(900002, "Siemens Healthineers LLC", "100 Medical Parkway", "Malvern", "19355", "USA", "NET30",
             phone="+1-610-555-0100"),
        _row(900003, "Siemens Healthineers Inc", "5 Other Road", "Austin", "73301", "USA", "NET30"),  # other site
        _row(900004, "Initech Software Solutions", "12 Tech Way", "Reston", "20190", "USA", "NET60",
             phone="+1-703-555-0101"),
        _row(900005, "Initech Sofware Solutions", "12 Tech Way", "Reston", "20190", "USA", ""),
        _row(900006, "Acme Rockets Ltd", "55 Market Street", "Denver", "80202", "US", "NET30",
             phone="+1-303-555-0102"),
        _row(900007, "ACME ROCKETS LIMITED", "55 Market St", "Denver", "80202", "US", ""),
    ]
    rng.shuffle(rows)
    return rows


@pytest.fixture(scope="module")
def dataset():
    return build_dataset()


@pytest.fixture(scope="module")
def csv_path(tmp_path_factory, dataset):
    path = tmp_path_factory.mktemp("legacy4") / "AR_CUSTOMERS.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(dataset)
    return path


# ------------------------------------------------------------------ catalog + mapping fixture
RULES = [  # (source, target table, target column, rule type, logic)
    ("CUST_ID", "BUT000", "BP_EXT", RuleType.SQL_EXPRESSION, "LPAD(TRIM(CUST_ID), 10, '0')"),
    ("CUST_NAME", "BUT000", "NAME_ORG1", RuleType.DIRECT_COPY, None),
    ("CUST_NAME", "BUT000", "BU_SORT1", RuleType.SQL_EXPRESSION, "UPPER(SUBSTRING(TRIM(CUST_NAME), 1, 20))"),
    (None, "BUT000", "BU_GROUP", RuleType.STATIC_VALUE, "'BP01'"),
    (None, "BUT000", "TYPE", RuleType.STATIC_VALUE, "'2'"),
    ("COUNTRY", "ADRC", "COUNTRY", RuleType.VALUE_MAPPING, None),
    ("CITY", "ADRC", "CITY1", RuleType.DIRECT_COPY, None),
    ("POSTAL_CODE", "ADRC", "POST_CODE1", RuleType.DIRECT_COPY, None),
    ("ADDR_STREET", "ADRC", "STREET", RuleType.SQL_EXPRESSION, "UPPER(TRIM(ADDR_STREET))"),
    ("PAYMENT_TERMS", "KNB1", "ZTERM", RuleType.VALUE_MAPPING, None),
]
TARGETS = [f"{t}.{c}" for _, t, c, _, _ in RULES]


@pytest.fixture()
def env(service, session_factory):
    seed_sap_catalog(service)
    source = service.register_data_source(DataSourceCreate(
        system_name="LEGACY_ORACLE_AR", system_type=SystemType.ORACLE, target_type=TargetType.SOURCE))
    table = service.register_table(TableCreate(source_id=source.id, table_name="AR_CUSTOMERS"))
    cols = service.bulk_register_columns(BulkColumnRegistration(table_id=table.id, columns=[
        ColumnCreate(column_name=h, data_type="VARCHAR2", char_max_length=100, is_primary_key=(h == "CUST_ID"))
        for h in HEADER
    ]))
    src = {c.column_name: c.id for c in cols}
    target = {(t.table.table_name, c.column_name): c.id
              for t in service.get_target_dictionary().tables for c in t.columns}
    but000 = next(t.table for t in service.get_target_dictionary().tables if t.table.table_name == "BUT000")
    mapping = service.create_mapping_set(MappingSetCreate(
        source_table_id=table.id, target_table_id=but000.id, created_by="ai-mapper"))
    rule_ids = {}
    with session_factory.begin() as s:
        for source_col, tt, tc, rtype, logic in RULES:
            rule = MapFieldRule(
                mapping_set_id=mapping.id, source_column_id=src[source_col] if source_col else None,
                target_column_id=target[(tt, tc)], rule_type=rtype, transformation_logic=logic)
            s.add(rule)
            s.flush()
            rule_ids[f"{tt}.{tc}"] = rule.id
    lookups = LookupService(session_factory)
    lookups.seed_standard_lookups(rule_ids["ADRC.COUNTRY"], "COUNTRY")
    lookups.seed_standard_lookups(rule_ids["KNB1.ZTERM"], "PAYMENT_TERMS")
    return {"mapping": mapping, "rule_ids": rule_ids, "lookups": lookups, "table": table, "src": src}


def read_parquet(path) -> list[dict]:
    con = duckdb.connect()
    cur = con.execute("SELECT * FROM read_parquet(?)", [str(path)])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def orchestrator(session_factory, tmp_path, **kwargs):
    return ExecutionOrchestrator(session_factory, TransformationEngine(session_factory),
                                 DeduplicationEngine(), output_dir=tmp_path / "out", **kwargs)


DEDUP = DedupConfig(
    exact_keys=[ExactKey(name="tax_id", columns=["SRC_TAX_ID"], normalizer="alnum"),
                ExactKey(name="email", columns=["SRC_EMAIL"], normalizer="email"),
                ExactKey(name="phone", columns=["SRC_PHONE"], normalizer="digits")],
    name_column="BUT000_NAME_ORG1",
    address_columns=["ADRC_STREET", "ADRC_CITY1", "ADRC_POST_CODE1"],
)
PASSTHROUGH = ["TAX_ID", "EMAIL", "PHONE"]


# ------------------------------------------------------------------ 1. lookups
def test_lookup_registration_retrieval_and_resolution(env):
    svc, rule = env["lookups"], env["rule_ids"]["ADRC.COUNTRY"]
    stored = svc.get_lookups(rule)
    assert stored["USA"] == "US" and stored["DEUTSCHLAND"] == "DE"

    svc.register_lookup(rule, "Narnia", "NA")
    svc.register_lookup(rule, "Narnia", "NR")  # upsert
    assert svc.get_lookups(rule)["Narnia"] == "NR"
    assert svc.register_lookups(rule, {}) == 0

    results = svc.apply_value_lookups(rule, ["usa", " Germany ", "Atlantis", None, "  "])
    assert [(r.target_value, r.status) for r in results] == [
        ("US", LookupStatus.MAPPED), ("DE", LookupStatus.MAPPED), (None, LookupStatus.LOOKUP_FAILED),
        (None, LookupStatus.NULL_INPUT), (None, LookupStatus.NULL_INPUT)]

    passed = svc.apply_value_lookups(rule, ["Atlantis"], LookupFallback.PASS_THROUGH)[0]
    assert (passed.target_value, passed.status) == ("Atlantis", LookupStatus.PASSED_THROUGH)
    default = svc.apply_value_lookups(rule, ["Atlantis"], LookupFallback.USE_DEFAULT, "ZZ")[0]
    assert (default.target_value, default.status) == ("ZZ", LookupStatus.DEFAULTED)
    with pytest.raises(ValueError):
        svc.apply_value_lookups(rule, ["x"], LookupFallback.USE_DEFAULT)

    terms = env["rule_ids"]["KNB1.ZTERM"]
    assert svc.apply_value_lookups(terms, ["NET30"])[0].target_value == "NT30"
    assert svc.clear_lookups(terms) == len(PAYMENT_TERMS_SEED)
    assert svc.get_lookups(terms) == {}


def test_lookup_validation(env, session_factory):
    svc = env["lookups"]
    with pytest.raises(NotFoundError):
        svc.register_lookup(999_999, "A", "B")
    with pytest.raises(ValueError, match="not VALUE_MAPPING"):
        svc.register_lookup(env["rule_ids"]["BUT000.NAME_ORG1"], "A", "B")
    rule = env["rule_ids"]["ADRC.COUNTRY"]
    with pytest.raises(ValueError):
        svc.register_lookup(rule, "  ", "B")
    with pytest.raises(ValueError):
        svc.register_lookup(rule, "A", "  ")
    with pytest.raises(ConflictError):  # same key after case folding, different targets
        svc.register_lookups(rule, {"zz": "AA", "ZZ": "BB"})
    with pytest.raises(ConflictError):
        svc.register_lookup(rule, "germany", "XX")  # collides with stored 'GERMANY' -> 'DE'
    with pytest.raises(ValueError):
        svc.seed_standard_lookups(rule, "UNKNOWN")
    # an ambiguous pair already in the DB is caught when the matching table is built
    with session_factory.begin() as s:
        s.add(MapValueLookup(field_rule_id=rule, source_value="Atlantis", target_value="AT"))
        s.add(MapValueLookup(field_rule_id=rule, source_value="ATLANTIS", target_value="XX"))
    with pytest.raises(ConflictError):
        svc.get_normalized_lookup(rule)


# ------------------------------------------------------------------ 2. transformation rules
@pytest.fixture()
def transformed(env, session_factory, csv_path, tmp_path):
    engine = TransformationEngine(session_factory)
    out = tmp_path / "staging.parquet"
    result = engine.transform(env["mapping"].id, csv_path, out, passthrough_columns=PASSTHROUGH)
    return result, read_parquet(out), env


def test_transformation_rules_against_dirty_dataset(transformed, dataset):
    result, rows, env = transformed
    assert len(dataset) >= 5000
    assert result.source_records == len(dataset) == len(rows)
    assert result.pk_strategy == "CATALOG_PK"
    by_pk = {r["_SRC_PK"]: r for r in rows}
    assert len(by_pk) == len(rows)

    for src in dataset:
        out = by_pk[src["CUST_ID"]]
        # SQL_EXPRESSION: LPAD customer ids
        assert out["BUT000_BP_EXT"] == src["CUST_ID"].zfill(10)
        # STATIC_VALUE: SAP defaults injected
        assert out["BUT000_BU_GROUP"] == "BP01" and out["BUT000_TYPE"] == "2"
        # SQL_EXPRESSION with function chain
        assert out["BUT000_BU_SORT1"] == src["CUST_NAME"].strip()[:20].upper()
        assert out["ADRC_STREET"] == src["ADDR_STREET"].strip().upper()
        # DIRECT_COPY: trimmed; length violations are NULL + error, never truncated
        name = src["CUST_NAME"].strip()
        assert out["BUT000_NAME_ORG1"] == (name if len(name) <= 40 else None)
        # DIRECT_COPY keeps leading zeros and turns blanks into NULL
        assert out["ADRC_POST_CODE1"] == (src["POSTAL_CODE"].strip() or None)
        assert out["ADRC_CITY1"] == (src["CITY"].strip() or None)
        # VALUE_MAPPING: trimmed, case-insensitive lookup; unmapped -> NULL
        country = src["COUNTRY"].strip().upper()
        assert out["ADRC_COUNTRY"] == (COUNTRY_SEED.get(country) if country else None)
        pay = src["PAYMENT_TERMS"].strip().upper()
        assert out["KNB1_ZTERM"] == (PAYMENT_TERMS_SEED.get(pay) if pay else None)
        # pass-through columns are raw copies
        assert out["SRC_TAX_ID"] == (src["TAX_ID"] or None)

    assert any(r["ADRC_POST_CODE1"] and r["ADRC_POST_CODE1"].startswith("0") for r in rows)  # zeros kept


def test_failed_rows_and_error_messages(transformed, dataset):
    result, rows, _ = transformed
    expected = {s["CUST_ID"]: expected_errors(s) for s in dataset}
    by_pk = {r["_SRC_PK"]: r for r in rows}
    for pk, bad in expected.items():
        row = by_pk[pk]
        assert (row["_TRANSFORM_STATUS"] == "FAILED") == bool(bad), pk
        for target in bad:
            assert f"{target}:" in row["_TRANSFORM_ERRORS"], (pk, target)
        if not bad:
            assert row["_TRANSFORM_ERRORS"] is None

    failed = sum(1 for bad in expected.values() if bad)
    assert result.failed_records == failed > 0
    assert result.ok_records == len(dataset) - failed
    per_rule = {r.target: r.failed_cells for r in result.rules}
    for target in ("BUT000.NAME_ORG1", "ADRC.CITY1", "ADRC.COUNTRY", "KNB1.ZTERM"):
        assert per_rule[target] == sum(1 for bad in expected.values() if target in bad) > 0
    assert per_rule["BUT000.BP_EXT"] == 0

    errors = " ".join(r["_TRANSFORM_ERRORS"] for r in rows if r["_TRANSFORM_ERRORS"])
    assert "LENGTH_EXCEEDED" in errors and "MANDATORY_EMPTY" in errors and "LOOKUP_FAILED" in errors


def test_transformation_is_deterministic(env, session_factory, csv_path, tmp_path):
    engine = TransformationEngine(session_factory)
    engine.transform(env["mapping"].id, csv_path, tmp_path / "a.parquet", passthrough_columns=PASSTHROUGH)
    engine.transform(env["mapping"].id, csv_path, tmp_path / "b.parquet", passthrough_columns=PASSTHROUGH)
    assert read_parquet(tmp_path / "a.parquet") == read_parquet(tmp_path / "b.parquet")


def test_lookup_fallback_modes(env, session_factory, csv_path, dataset, tmp_path):
    engine = TransformationEngine(session_factory)
    out = tmp_path / "default.parquet"
    engine.transform(env["mapping"].id, csv_path, out, lookup_fallback=LookupFallback.USE_DEFAULT, lookup_default="ZZ")
    rows = {r["_SRC_PK"]: r for r in read_parquet(out)}
    for src in dataset:
        if src["COUNTRY"].strip() and src["COUNTRY"].strip().upper() not in COUNTRY_SEED:
            assert rows[src["CUST_ID"]]["ADRC_COUNTRY"] == "ZZ"
            assert "ADRC.COUNTRY" not in (rows[src["CUST_ID"]]["_TRANSFORM_ERRORS"] or "")

    out2 = tmp_path / "pass.parquet"  # pass-through carries the raw value, which then fails the length check
    engine.transform(env["mapping"].id, csv_path, out2, lookup_fallback=LookupFallback.PASS_THROUGH)
    atlantis = next(r for r in read_parquet(out2) if r["ADRC_COUNTRY"] is None
                    and "ADRC.COUNTRY: LENGTH_EXCEEDED" in (r["_TRANSFORM_ERRORS"] or ""))
    assert atlantis
    with pytest.raises(ValueError):
        engine.transform(env["mapping"].id, csv_path, tmp_path / "x.parquet", lookup_fallback=LookupFallback.USE_DEFAULT)


def test_parquet_source_and_row_number_pk(env, session_factory, csv_path, tmp_path):
    src_parquet = tmp_path / "src.parquet"
    duckdb.connect().execute(
        f"COPY (SELECT * FROM read_csv('{csv_path.as_posix()}', all_varchar=true)) TO '{src_parquet.as_posix()}' (FORMAT parquet)")
    engine = TransformationEngine(session_factory)
    res = engine.transform(env["mapping"].id, src_parquet, tmp_path / "o.parquet", source_pk_columns=["cust_id"])
    assert res.pk_strategy == "PARAMETER" and res.source_records >= 5000
    with session_factory.begin() as s:  # drop the catalog PK -> row-number keys
        s.execute(sa.update(CatColumn).where(CatColumn.id == env["src"]["CUST_ID"]).values(is_primary_key=False))
    res2 = engine.transform(env["mapping"].id, csv_path, tmp_path / "o2.parquet")
    assert res2.pk_strategy == "ROW_NUMBER"
    assert read_parquet(tmp_path / "o2.parquet")[0]["_SRC_PK"] == "ROW:1"


def test_compile_guards(env, session_factory, csv_path, tmp_path):
    engine = TransformationEngine(session_factory)
    rid = env["rule_ids"]["BUT000.BP_EXT"]
    for bad in ("UPPER(CUST_ID); DROP TABLE cat_columns", "pg_sleep(5)", "(SELECT 1)", "UNKNOWN_COL"):
        with session_factory.begin() as s:
            s.execute(sa.update(MapFieldRule).where(MapFieldRule.id == rid).values(transformation_logic=bad))
        with pytest.raises(TransformationCompileError, match="guardrails"):
            engine.transform(env["mapping"].id, csv_path, tmp_path / "x.parquet")

    with session_factory.begin() as s:
        s.execute(sa.update(MapFieldRule).where(MapFieldRule.id == rid).values(transformation_logic="TRIM(CUST_ID)"))
    short = tmp_path / "short.csv"
    short.write_text("CUST_ID,CUST_NAME\n1,A\n", encoding="utf-8")
    with pytest.raises(TransformationCompileError, match="missing columns"):
        engine.transform(env["mapping"].id, short, tmp_path / "y.parquet")

    with session_factory.begin() as s:
        s.execute(sa.update(MapTableRule).where(MapTableRule.id == env["mapping"].id).values(status=MappingStatus.REJECTED))
    with pytest.raises(MappingValidationError, match="REJECTED"):
        engine.transform(env["mapping"].id, csv_path, tmp_path / "z.parquet")
    with pytest.raises(NotFoundError):
        engine.transform(999_999, csv_path, tmp_path / "z.parquet")


def test_engines_cap_duckdb_memory_at_1_5_gb():
    assert DEFAULT_MEMORY_LIMIT_MB == 1536
    with bounded_connection() as con:
        assert con.execute("SELECT current_setting('memory_limit')").fetchone()[0] in ("1.5 GiB", "1.4 GiB")
        assert con.execute("SELECT current_setting('threads')").fetchone()[0] == 2


# ------------------------------------------------------------------ 3. deduplication
@pytest.fixture()
def deduped(env, session_factory, csv_path, tmp_path):
    report = orchestrator(session_factory, tmp_path).execute_run(
        env["mapping"].id, csv_path, "WAVE_1", DEDUP, passthrough_columns=PASSTHROUGH)
    return report, read_parquet(report.deduplicated_path)


def test_exact_and_fuzzy_clusters_with_surviving_master(deduped, dataset):
    report, rows = deduped
    by_pk = {r["_SRC_PK"]: r for r in rows}
    assert len(rows) == len(dataset)

    # --- exact tax-id duplicates: formatting differences collapse; the fuller (original) record survives
    tax_children = [r for r in rows if r["dedup_match_type"] == "EXACT:tax_id" and r["is_duplicate_child"]]
    assert len(tax_children) == TAX_DUPES
    for child in tax_children:
        master = by_pk[child["surviving_master_pk"]]
        assert not master["is_duplicate_child"] and master["surviving_master_pk"] == master["_SRC_PK"]
        assert master["dedup_cluster_id"] == child["dedup_cluster_id"]
        assert int(child["_SRC_PK"]) >= 20_000 and int(master["_SRC_PK"]) < 20_000

    # --- exact e-mail duplicates (case/space-insensitive)
    email_children = [r for r in rows if r["dedup_match_type"] == "EXACT:email" and r["is_duplicate_child"]]
    assert len(email_children) == EMAIL_DUPES

    # --- fuzzy company names: Inc vs LLC (same site) -> one cluster, the fuller LLC record survives
    siemens_inc, siemens_llc, siemens_other = by_pk["900001"], by_pk["900002"], by_pk["900003"]
    assert siemens_inc["is_duplicate_child"] and siemens_inc["surviving_master_pk"] == "900002"
    assert not siemens_llc["is_duplicate_child"] and siemens_llc["surviving_master_pk"] == "900002"
    assert siemens_inc["dedup_cluster_id"] == siemens_llc["dedup_cluster_id"] is not None
    assert siemens_inc["dedup_match_type"] == "FUZZY"
    # same name, different site -> not merged
    assert not siemens_other["is_duplicate_child"] and siemens_other["dedup_cluster_id"] is None
    assert siemens_other["surviving_master_pk"] == "900003"

    assert by_pk["900005"]["surviving_master_pk"] == "900004" and by_pk["900005"]["is_duplicate_child"]  # typo
    assert by_pk["900007"]["surviving_master_pk"] == "900006" and by_pk["900007"]["is_duplicate_child"]  # Ltd/Limited, St/Street

    # --- unrelated records stay untouched
    singles = [r for r in rows if r["dedup_cluster_id"] is None]
    assert all(not r["is_duplicate_child"] and r["surviving_master_pk"] == r["_SRC_PK"] for r in singles)

    # --- summary report
    d = report.deduplication
    clusters = TAX_DUPES + EMAIL_DUPES + 3
    assert d.duplicate_clusters == d.exact_match_clusters + d.fuzzy_match_clusters
    assert d.duplicate_clusters == clusters and d.duplicate_children == clusters
    assert d.fuzzy_match_clusters == 3 and d.exact_match_clusters == TAX_DUPES + EMAIL_DUPES
    assert d.exact_edges_by_key["tax_id"] == TAX_DUPES and d.exact_edges_by_key["email"] == EMAIL_DUPES
    assert d.input_rows == len(dataset)
    assert d.eligible_rows + d.excluded_failed_rows == d.input_rows
    assert d.surviving_records == d.eligible_rows - d.duplicate_children
    assert d.fuzzy_pairs_evaluated > 0 and d.fuzzy_pairs_matched >= 3
    assert {s.master_pk for s in d.samples} and all(len(s.member_pks) == 2 for s in d.samples)


def test_failed_records_are_excluded_from_deduplication(deduped, dataset):
    report, rows = deduped
    failed = [r for r in rows if r["_TRANSFORM_STATUS"] == "FAILED"]
    assert failed and report.deduplication.excluded_failed_rows == len(failed)
    assert all(not r["is_duplicate_child"] and r["dedup_cluster_id"] is None for r in failed)


def test_golden_record_tie_breaks_and_config_validation(tmp_path):
    staging = tmp_path / "tiny.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * FROM (VALUES ('B', 'ACME', 'x@y.com', NULL, 'OK'), ('A', 'ACME', 'X@Y.COM', NULL, 'OK'), "
        "('C', 'ZETA', 'c@c.com', 'filled', 'OK')) t(_SRC_PK, name, email, extra, _TRANSFORM_STATUS)) "
        f"TO '{staging.as_posix()}' (FORMAT parquet)")
    engine = DeduplicationEngine()
    cfg = DedupConfig(exact_keys=[ExactKey(name="email", columns=["email"], normalizer="email")])
    report = engine.deduplicate(staging, tmp_path / "out.parquet", cfg)
    rows = {r["_SRC_PK"]: r for r in read_parquet(tmp_path / "out.parquet")}
    # equal completeness -> smallest pk wins
    assert rows["B"]["surviving_master_pk"] == "A" and rows["B"]["is_duplicate_child"]
    assert rows["A"]["surviving_master_pk"] == "A" and not rows["A"]["is_duplicate_child"]
    assert rows["C"]["dedup_cluster_id"] is None and report.duplicate_children == 1

    with pytest.raises(ValueError, match="at least one"):
        DedupConfig()
    with pytest.raises(ValueError, match="missing columns"):
        engine.deduplicate(staging, tmp_path / "o2.parquet", DedupConfig(name_column="nope"))
    with pytest.raises(FileNotFoundError):
        engine.deduplicate(tmp_path / "none.parquet", tmp_path / "o3.parquet", cfg)
    with pytest.raises(ValueError, match="already has dedup output"):
        engine.deduplicate(tmp_path / "out.parquet", tmp_path / "o4.parquet", cfg)


# ------------------------------------------------------------------ 4. lineage + run audit trail
def test_run_record_counters_and_status(env, session_factory, csv_path, dataset, tmp_path):
    report = orchestrator(session_factory, tmp_path).execute_run(
        env["mapping"].id, csv_path, "WAVE_7", DEDUP, passthrough_columns=PASSTHROUGH)
    failed = sum(1 for s in dataset if expected_errors(s))

    with session_factory() as s:
        run = s.get(MigMigrationRun, report.run_id)
        assert run.mapping_set_id == env["mapping"].id and run.wave_name == "WAVE_7"
        assert run.status == RunStatus.SUCCEEDED and report.status == "SUCCEEDED"
        assert run.current_phase == MigrationPhase.TRANSFORM
        assert run.records_extracted == len(dataset) == report.source_records
        assert run.records_failed == failed == report.failed_records
        assert run.records_transformed == len(dataset) - failed == report.transformed_records
        assert run.records_loaded == 0
        assert run.started_at and run.completed_at and run.completed_at >= run.started_at
    assert report.report_path and report.deduplicated_path and report.deduplication


def test_cell_lineage_is_recorded_for_every_rule_and_row(env, session_factory, csv_path, dataset, tmp_path):
    report = orchestrator(session_factory, tmp_path).execute_run(env["mapping"].id, csv_path, "WAVE_1")
    expected_total = len(dataset) * len(RULES)
    assert report.lineage_rows == expected_total

    with session_factory() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(MigCellLineage)
                        .where(MigCellLineage.run_id == report.run_id)) == expected_total
        # one rule: every row recorded once, with raw -> transformed and the rule id
        rid = env["rule_ids"]["BUT000.BP_EXT"]
        rows = s.execute(sa.select(MigCellLineage).where(MigCellLineage.rule_applied_id == rid)).scalars().all()
        assert len(rows) == len(dataset)
        sample = next(r for r in rows if r.source_pk_value == "1007")
        assert (sample.source_raw_value, sample.transformed_value, sample.target_column_name) == (
            "1007", "0000001007", "BUT000.BP_EXT")
        assert sample.loaded_successfully is False and sample.error_message is None and sample.target_pk_value is None

        # STATIC rules have no raw source value
        static = s.scalars(sa.select(MigCellLineage).where(
            MigCellLineage.rule_applied_id == env["rule_ids"]["BUT000.BU_GROUP"]).limit(1)).one()
        assert static.source_raw_value is None and static.transformed_value == "BP01"

        # failures carry their error text
        bad = next(r for r in dataset if "ADRC.COUNTRY" in expected_errors(r))
        row = s.scalars(sa.select(MigCellLineage).where(
            MigCellLineage.rule_applied_id == env["rule_ids"]["ADRC.COUNTRY"],
            MigCellLineage.source_pk_value == bad["CUST_ID"])).one()
        assert row.transformed_value is None and row.error_message
        assert row.source_raw_value == (bad["COUNTRY"] or None)

        # value-mapping rows show the translation
        mapped = s.scalars(sa.select(MigCellLineage).where(
            MigCellLineage.rule_applied_id == env["rule_ids"]["KNB1.ZTERM"],
            MigCellLineage.source_raw_value == "NET30").limit(1)).one()
        assert mapped.transformed_value == "NT30"

        # every lineage row points at a real rule of this mapping set
        orphans = s.scalar(sa.select(sa.func.count()).select_from(MigCellLineage).where(
            MigCellLineage.run_id == report.run_id,
            ~MigCellLineage.rule_applied_id.in_(sa.select(MapFieldRule.id).where(
                MapFieldRule.mapping_set_id == env["mapping"].id))))
        assert orphans == 0


def test_lineage_modes_and_audited_subset(env, session_factory, csv_path, dataset, tmp_path):
    orch = orchestrator(session_factory, tmp_path)
    none = orch.execute_run(env["mapping"].id, csv_path, "W", lineage_mode=LineageMode.NONE)
    assert none.lineage_rows == 0

    subset = orch.execute_run(env["mapping"].id, csv_path, "W", audited_targets=["adrc.country", "BUT000.BP_EXT"])
    assert subset.lineage_rows == 2 * len(dataset)

    exceptions = orch.execute_run(env["mapping"].id, csv_path, "W", lineage_mode=LineageMode.EXCEPTIONS)
    assert 0 < exceptions.lineage_rows < len(dataset) * len(RULES)
    with session_factory() as s:
        unchanged_ok = s.scalar(sa.select(sa.func.count()).select_from(MigCellLineage).where(
            MigCellLineage.run_id == exceptions.run_id, MigCellLineage.error_message.is_(None),
            MigCellLineage.source_raw_value == MigCellLineage.transformed_value))
        assert unchanged_ok == 0


def test_failed_run_is_marked_failed(env, session_factory, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("CUST_ID,CUST_NAME\n1,A\n", encoding="utf-8")
    with pytest.raises(ExecutionError, match="missing columns") as exc:
        orchestrator(session_factory, tmp_path).execute_run(env["mapping"].id, bad, "WAVE_X")
    with session_factory() as s:
        run = s.get(MigMigrationRun, exc.value.run_id)
        assert run.status == RunStatus.FAILED and run.completed_at is not None
        assert run.records_extracted == 0 and run.current_phase == MigrationPhase.TRANSFORM
    assert (tmp_path / "out" / f"run_{exc.value.run_id}" / "report.json").is_file()


def test_failed_ratio_limit_marks_run_failed(env, session_factory, csv_path, tmp_path):
    report = orchestrator(session_factory, tmp_path, max_failed_ratio=0.001).execute_run(
        env["mapping"].id, csv_path, "W")
    assert report.status == "FAILED" and "exceeds limit" in report.message
    with session_factory() as s:
        run = s.get(MigMigrationRun, report.run_id)
        assert run.status == RunStatus.FAILED and run.records_failed == report.failed_records > 0


def test_run_rejects_unknown_or_rejected_mapping_without_creating_a_run(env, session_factory, csv_path, tmp_path):
    orch = orchestrator(session_factory, tmp_path)
    with pytest.raises(NotFoundError):
        orch.execute_run(999_999, csv_path)
    with session_factory.begin() as s:
        s.execute(sa.update(MapTableRule).where(MapTableRule.id == env["mapping"].id).values(status=MappingStatus.REJECTED))
    with pytest.raises(MappingValidationError):
        orch.execute_run(env["mapping"].id, csv_path)
    with session_factory() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(MigMigrationRun)) == 0


def test_approved_mapping_executes(env, session_factory, csv_path, tmp_path):
    from datetime import datetime, timezone
    with session_factory.begin() as s:
        s.execute(sa.update(MapTableRule).where(MapTableRule.id == env["mapping"].id).values(
            status=MappingStatus.APPROVED, approved_by="bob", approved_at=datetime.now(timezone.utc)))
    report = orchestrator(session_factory, tmp_path).execute_run(env["mapping"].id, csv_path, lineage_mode=LineageMode.NONE)
    assert report.status == "SUCCEEDED"


def test_numbered_entities_and_different_sites_are_not_merged(tmp_path):
    staging = tmp_path / "numbered.parquet"
    duckdb.connect().execute(
        "COPY (SELECT * FROM (VALUES "
        "('1', 'Plant 1 Logistics', '10 Main St', 'OK'), ('2', 'Plant 2 Logistics', '10 Main St', 'OK'), "
        "('3', 'Harbor Freight Lines', '5 Dock Road', 'OK'), ('4', 'Harbor Freight Lines', '6 Dock Road', 'OK'), "
        "('5', 'Harbour Freight Lines', '5 Dock Rd', 'OK')) t(_SRC_PK, name, street, _TRANSFORM_STATUS)) "
        f"TO '{staging.as_posix()}' (FORMAT parquet)")
    cfg = DedupConfig(name_column="name", address_columns=["street"])
    DeduplicationEngine().deduplicate(staging, tmp_path / "o.parquet", cfg)
    rows = {r["_SRC_PK"]: r for r in read_parquet(tmp_path / "o.parquet")}
    assert rows["1"]["dedup_cluster_id"] is None and rows["2"]["dedup_cluster_id"] is None
    assert rows["4"]["dedup_cluster_id"] is None  # other house number
    assert rows["5"]["surviving_master_pk"] == "3" and rows["5"]["is_duplicate_child"]  # spelling variant, Road/Rd
