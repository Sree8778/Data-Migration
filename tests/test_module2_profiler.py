"""Module 2 integration tests: deterministic DuckDB profiler + catalog persistence.

Metrics are verified against an independent pure-Python calculation over the same rows.
"""

from __future__ import annotations

import csv
import random
import re
from collections import Counter
from decimal import Decimal

import duckdb
import pytest
import sqlalchemy as sa

from src.db.enums import PiiClassification, SystemType, TargetType
from src.db.models import CatColumn, CatTable
from src.schemas.metadata import (
    BulkColumnRegistration,
    ColumnCreate,
    DataSourceCreate,
    TableCreate,
)
from src.seeds.seed_sap_catalog import seed_sap_catalog
from src.services.metadata_service import NotFoundError
from src.services.profiler_service import PATTERNS, ProfilerService

HEADER = ["CUST_ID", "CUST_NAME", "EMAIL", "COUNTRY_CD", "POSTAL_CODE", "CUST_TYPE", "CREDIT_LIMIT"]
VALID_COUNTRIES = ["US", "DE", "FR", "GB", "IN"]
BAD_COUNTRIES = ["United States", "Germany", "U.S.A", "XX1"]
BASE_ROWS, EXACT_DUPES, KEY_DUPES = 5000, 150, 50


def _build_rows() -> list[list[str]]:
    rng = random.Random(42)
    rows = []
    for i in range(BASE_ROWS):
        cust_id = 100_000 + i
        email = f"user{i}@example.com"
        if i % 20 == 0:
            email = ""  # missing
        elif i % 25 == 0:
            email = "not-an-email"  # invalid
        country = BAD_COUNTRIES[i % len(BAD_COUNTRIES)] if i % 50 == 0 else rng.choice(VALID_COUNTRIES)
        if i % 10 == 0:
            postal = ""  # missing
        elif i % 7 == 0:
            postal = f"0{rng.randint(1000, 9999)}"  # leading zero
        else:
            postal = str(rng.randint(10000, 99999))
        rows.append([
            str(cust_id), f"Customer {i:05d}", email, country, postal,
            rng.choice(["C", "V"]), str(rng.randint(1, 500) * 100),
        ])
    rows += [list(r) for r in rows[1000 : 1000 + EXACT_DUPES]]  # exact duplicate records
    for r in rows[2000 : 2000 + KEY_DUPES]:  # same customer id, different attributes
        rows.append([r[0], r[1] + " (DUP)", r[2], r[3], r[4], r[5], r[6]])
    rng.shuffle(rows)
    return rows


@pytest.fixture(scope="module")
def rows():
    return _build_rows()


@pytest.fixture(scope="module")
def csv_path(tmp_path_factory, rows):
    path = tmp_path_factory.mktemp("legacy") / "AR_CUSTOMERS.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(rows)
    return path


@pytest.fixture(scope="module")
def profile(csv_path):
    return ProfilerService().profile_file(csv_path)


def _col(profile, name):
    return next(c for c in profile.columns if c.column_name == name)


def _values(rows, name):
    idx = HEADER.index(name)
    return [r[idx].strip() for r in rows if r[idx].strip() != ""]


# ------------------------------------------------------------------ metrics accuracy
def test_dataset_is_large_and_row_count_matches(rows, profile):
    assert len(rows) == BASE_ROWS + EXACT_DUPES + KEY_DUPES >= 5000
    assert profile.total_rows == len(rows)
    assert profile.column_count == len(HEADER)
    assert profile.file_format == "csv"


@pytest.mark.parametrize("name", HEADER)
def test_scalar_metrics_match_independent_calculation(rows, profile, name):
    vals = _values(rows, name)
    col = _col(profile, name)
    assert col.total_rows == len(rows)
    assert col.null_count == len(rows) - len(vals)
    assert col.null_ratio == pytest.approx((len(rows) - len(vals)) / len(rows), abs=1e-4)
    assert col.distinct_count == len(set(vals))
    assert col.distinct_ratio == pytest.approx(len(set(vals)) / len(vals), abs=1e-4)
    assert col.min_length == min(map(len, vals))
    assert col.max_length == max(map(len, vals))

    expected_top = sorted(Counter(vals).items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    assert [(t.value, t.count) for t in col.top_values] == expected_top

    assert 0 < len(col.sample_values) <= 5
    assert set(col.sample_values) <= set(vals)


@pytest.mark.parametrize("name", HEADER)
def test_regex_conformity_matches_independent_calculation(rows, profile, name):
    vals = _values(rows, name)
    col = _col(profile, name)
    for key, pattern in PATTERNS.items():
        expected = sum(1 for v in vals if re.fullmatch(pattern, v))
        assert col.conformity[key].matched == expected, (name, key)
        assert col.conformity[key].checked == len(vals)


def test_detected_native_types(profile):
    assert _col(profile, "CUST_ID").detected_type == "BIGINT"
    assert _col(profile, "CREDIT_LIMIT").detected_type == "BIGINT"
    assert _col(profile, "CUST_NAME").detected_type == "VARCHAR"
    assert _col(profile, "COUNTRY_CD").detected_type == "VARCHAR"


# ------------------------------------------------------------------ dirty-data findings
def test_missing_postal_codes_and_leading_zeros(rows, profile):
    postal = _col(profile, "POSTAL_CODE")
    missing = sum(1 for r in rows if r[4] == "")
    assert missing > 400
    assert postal.null_count == missing
    zeros = sum(1 for r in rows if r[4].startswith("0"))
    assert postal.conformity["leading_zero_numeric"].matched == zeros > 0
    # DuckDB's sniffer keeps zero-padded codes as text, so no corruption risk is flagged here
    assert postal.detected_type == "VARCHAR"
    assert postal.leading_zero_risk is False
    assert _col(profile, "CUST_ID").leading_zero_risk is False


def test_leading_zero_risk_flagged_when_sniffer_sample_misses_zero_padding(tmp_path):
    path = tmp_path / "late_zeros.csv"
    lines = ["ZIP"] + [str(10000 + i) for i in range(30000)] + ["02134", "01001"]
    path.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    zip_col = ProfilerService(type_sample_size=100).profile_file(path).columns[0]
    assert zip_col.detected_type == "BIGINT"
    assert zip_col.conformity["leading_zero_numeric"].matched == 2
    assert zip_col.leading_zero_risk is True


def test_invalid_country_values_are_visible(rows, profile):
    country = _col(profile, "COUNTRY_CD")
    invalid = [r[3] for r in rows if not re.fullmatch(r"[A-Z]{2}", r[3])]
    assert invalid
    assert country.conformity["iso_country_2"].matched == len(rows) - len(invalid)
    assert country.conformity["iso_country_2"].ratio < 1
    assert country.max_length == max(len(v) for v in BAD_COUNTRIES)


def test_email_quality(rows, profile):
    email = _col(profile, "EMAIL")
    vals = _values(rows, "EMAIL")
    assert email.null_count > 0
    assert 0 < email.conformity["email"].matched < len(vals)
    assert "not-an-email" in {t.value for t in email.top_values}


def test_duplicate_detection(profile):
    assert profile.full_row_duplicate_rows == EXACT_DUPES
    assert profile.full_row_duplicate_ratio == pytest.approx(EXACT_DUPES / profile.total_rows, abs=1e-4)

    report = next(k for k in profile.key_duplicates if k.columns == ["CUST_ID"])  # inferred key
    assert report.duplicate_group_count == EXACT_DUPES + KEY_DUPES
    assert report.duplicate_row_count == EXACT_DUPES + KEY_DUPES
    assert len(report.sample_duplicate_keys) == 5


def test_explicit_composite_candidate_key(csv_path):
    result = ProfilerService().profile_file(csv_path, candidate_keys=[["CUST_ID", "CUST_NAME"]])
    assert [k.columns for k in result.key_duplicates] == [["CUST_ID", "CUST_NAME"]]
    # exact duplicates collide on (id, name); the key-duplicates differ in name
    assert result.key_duplicates[0].duplicate_row_count == EXACT_DUPES


def test_profiling_is_deterministic(csv_path, profile):
    again = ProfilerService().profile_file(csv_path)
    assert again.model_dump(exclude={"profiled_at"}) == profile.model_dump(exclude={"profiled_at"})


def test_parquet_matches_csv_string_metrics(csv_path, profile, tmp_path):
    parquet = tmp_path / "AR_CUSTOMERS.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT * FROM read_csv('{csv_path.as_posix()}', all_varchar=true)) "
        f"TO '{parquet.as_posix()}' (FORMAT parquet)"
    )
    con.close()

    result = ProfilerService().profile_file(parquet)
    assert result.file_format == "parquet"
    assert result.total_rows == profile.total_rows
    assert result.full_row_duplicate_rows == profile.full_row_duplicate_rows
    for csv_col, pq_col in zip(profile.columns, result.columns):
        assert (pq_col.null_count, pq_col.distinct_count, pq_col.min_length, pq_col.max_length) == (
            csv_col.null_count, csv_col.distinct_count, csv_col.min_length, csv_col.max_length,
        )
        assert pq_col.conformity == csv_col.conformity
        assert pq_col.detected_type == "VARCHAR"


def test_input_validation(csv_path, tmp_path):
    svc = ProfilerService()
    with pytest.raises(FileNotFoundError):
        svc.profile_file(tmp_path / "missing.csv")
    other = tmp_path / "data.xlsx"
    other.write_bytes(b"x")
    with pytest.raises(ValueError, match="unsupported"):
        svc.profile_file(other)
    with pytest.raises(ValueError, match="not found in source"):
        svc.profile_file(csv_path, candidate_keys=[["NOPE"]])


def test_empty_file_with_header_only(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text(",".join(HEADER) + "\n", encoding="utf-8")
    result = ProfilerService().profile_file(path)
    assert result.total_rows == 0 and result.full_row_duplicate_rows == 0
    assert all(c.null_ratio == 0 and c.distinct_ratio == 0 and c.top_values == [] for c in result.columns)


# ------------------------------------------------------------------ catalog persistence
@pytest.fixture()
def catalog(service, session_factory):
    """Module 1 objects for the legacy table: source, AR_CUSTOMERS and its columns."""
    source = service.register_data_source(
        DataSourceCreate(system_name="LEGACY_ORACLE_AR", system_type=SystemType.ORACLE,
                         target_type=TargetType.SOURCE)
    )
    table = service.register_table(TableCreate(source_id=source.id, table_name="AR_CUSTOMERS"))
    service.bulk_register_columns(BulkColumnRegistration(table_id=table.id, columns=[
        ColumnCreate(column_name="CUST_ID", data_type="NUMBER", numeric_precision=10, numeric_scale=0,
                     is_primary_key=True),
        ColumnCreate(column_name="CUST_NAME", data_type="VARCHAR2", char_max_length=100),
        ColumnCreate(column_name="EMAIL", data_type="VARCHAR2", char_max_length=100,
                     pii_classification=PiiClassification.RESTRICTED_PII),
        ColumnCreate(column_name="COUNTRY_CD", data_type="VARCHAR2", char_max_length=2),
        ColumnCreate(column_name="POSTAL_CODE", data_type="VARCHAR2", char_max_length=10),
        ColumnCreate(column_name="CUST_TYPE", data_type="VARCHAR2", char_max_length=1),
        ColumnCreate(column_name="LEGACY_FLAG", data_type="VARCHAR2", char_max_length=1),  # not in file
    ]))
    return table


def test_save_profile_updates_catalog(catalog, profile, service, session_factory):
    profiler = ProfilerService(session_factory)
    summary = profiler.save_profile_to_catalog(catalog.id, profile)

    assert summary.columns_updated == 6
    assert summary.unmatched_profile_columns == ["CREDIT_LIMIT"]
    assert summary.catalog_columns_not_profiled == ["LEGACY_FLAG"]
    assert summary.masked_sample_columns == ["EMAIL"]

    with session_factory() as s:
        table = s.get(CatTable, catalog.id)
        assert table.row_count_estimate == profile.total_rows
        assert table.last_profiled_at == profile.profiled_at

        cols = {c.column_name: c for c in s.scalars(sa.select(CatColumn).where(CatColumn.table_id == catalog.id))}
        for name in ("CUST_ID", "CUST_NAME", "COUNTRY_CD", "POSTAL_CODE", "CUST_TYPE"):
            prof = _col(profile, name)
            assert cols[name].null_ratio == Decimal(str(prof.null_ratio)).quantize(Decimal("0.0001"))
            assert cols[name].distinct_ratio == Decimal(str(prof.distinct_ratio)).quantize(Decimal("0.0001"))
            assert cols[name].sample_values == prof.sample_values
        assert cols["POSTAL_CODE"].null_ratio > Decimal("0.09")
        # PII: no raw e-mail address is stored, only its shape
        assert all("@" in v and "example" not in v for v in cols["EMAIL"].sample_values)
        # untouched column keeps no stats
        assert cols["LEGACY_FLAG"].null_ratio is None and cols["LEGACY_FLAG"].sample_values is None
        # structural metadata is not modified by profiling
        assert cols["CUST_ID"].data_type == "NUMBER" and cols["CUST_ID"].is_primary_key


def test_reprofiling_overwrites_and_is_idempotent(catalog, profile, session_factory):
    profiler = ProfilerService(session_factory)
    profiler.save_profile_to_catalog(catalog.id, profile)
    profiler.save_profile_to_catalog(catalog.id, profile)
    with session_factory() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(CatColumn).where(CatColumn.table_id == catalog.id)) == 7


def test_save_to_unknown_table_raises(profile, service, session_factory):
    with pytest.raises(NotFoundError):
        ProfilerService(session_factory).save_profile_to_catalog(999_999, profile)


def test_service_without_session_factory_cannot_save(profile):
    with pytest.raises(RuntimeError):
        ProfilerService().save_profile_to_catalog(1, profile)


def test_module1_behaviour_unchanged_after_profiling(catalog, profile, service, session_factory):
    """Non-regression: profiling must not disturb the seed catalog or the target dictionary."""
    ProfilerService(session_factory).save_profile_to_catalog(catalog.id, profile)
    seed_sap_catalog(service)
    again = seed_sap_catalog(service)
    assert again.tables == 6
    assert len(service.get_target_dictionary().tables) == 6
