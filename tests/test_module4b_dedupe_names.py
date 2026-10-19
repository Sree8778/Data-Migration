"""Deduplication of company names written with dotted / abbreviated legal suffixes (found by the mock-data run)."""

from __future__ import annotations

import duckdb

from src.schemas.execution import DedupConfig
from src.services.deduplication_engine import DeduplicationEngine


def run(tmp_path, rows):
    staging = tmp_path / "s.parquet"
    values = ", ".join("('{}', '{}', '{}', 'OK')".format(pk, name.replace("'", "''"), street) for pk, name, street in rows)
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES {values}) t(_SRC_PK, name, street, _TRANSFORM_STATUS)) TO '{staging.as_posix()}' (FORMAT parquet)")
    report = DeduplicationEngine().deduplicate(staging, tmp_path / "o.parquet", DedupConfig(name_column="name", address_columns=["street"]))
    out = duckdb.connect().execute("SELECT _SRC_PK, is_duplicate_child, surviving_master_pk FROM read_parquet(?) ORDER BY 1", [str(tmp_path / "o.parquet")]).fetchall()
    return report, {pk: (child, master) for pk, child, master in out}


def test_dotted_and_spelled_out_legal_suffixes_are_equivalent(tmp_path):
    rows = [
        ("1", "Kalomi Labs LLC", "1 Main Street"), ("2", "KALOMI LABS L.L.C.", "1 Main Street"),
        ("3", "Nomili Foods SAS", "2 Oak Road"), ("4", "NOMILI FOODS S.A.S.", "2 Oak Road"),
        ("5", "Zotu Motors KK", "3 Pine Lane"), ("6", "ZOTU MOTORS K.K.", "3 Pine Lane"),
        ("7", "Peli Pharma Pvt Ltd", "4 Elm Avenue"), ("8", "PELI PHARMA PRIVATE LIMITED", "4 Elm Avenue"),
        ("9", "Raxi Energy SL", "5 Lake Road"), ("10", "RAXI ENERGY S.L.", "5 Lake Road"),
        ("11", "Vemi Trading PLC", "6 Hill Road"), ("12", "VEMI TRADING P.L.C.", "6 Hill Road"),
        ("13", "Duno Systems AG", "7 Park Lane"), ("14", "DUNO SYSTEMS A.G.", "7 Park Lane"),
    ]
    report, result = run(tmp_path, rows)
    assert report.duplicate_clusters == 7 and report.duplicate_children == 7
    for first in range(1, 14, 2):
        assert result[str(first + 1)][0] or result[str(first)][0]  # exactly one record of each pair is the child
        assert len({result[str(first)][1], result[str(first + 1)][1]}) == 1


def test_different_companies_and_different_sites_are_still_kept_apart(tmp_path):
    rows = [
        ("1", "Kalomi Labs LLC", "1 Main Street"), ("2", "Kalomi Systems LLC", "1 Main Street"),  # different business, same address
        ("3", "Nomili Foods S.A.S.", "2 Oak Road"), ("4", "Nomili Foods S.A.S.", "9 Elsewhere Road"),  # same name, different site
        ("5", "Plant 1 Logistics Inc", "7 Dock Road"), ("6", "Plant 2 Logistics Inc.", "7 Dock Road"),  # numbered entities
    ]
    report, result = run(tmp_path, rows)
    assert report.duplicate_clusters == 0 and not any(child for child, _ in result.values())


def test_a_number_missing_from_one_address_is_missing_data_not_a_conflict(tmp_path):
    rows = [
        ("1", "Kalomi Labs LLC", "12 Main Street Boston 02111"), ("2", "Kalomi Labs LLC", "12 Main Street Boston"),  # postal code missing
        ("3", "Nomili Foods Inc", "9 Oak Road Austin 78701"), ("4", "Nomili Foods Inc", "Austin 78701"),  # street missing
        ("5", "Zotu Motors KK", "5 Pine Lane Leeds 12345"), ("6", "Zotu Motors KK", "7 Pine Lane Leeds 12345"),  # house number differs
        ("7", "Peli Pharma Ltd", "4 Elm Avenue Pune 40001"), ("8", "Peli Pharma Ltd", "4 Elm Avenue Pune 40002"),  # postal code differs
    ]
    report, result = run(tmp_path, rows)
    assert result["1"][1] == result["2"][1] and result["3"][1] == result["4"][1]  # merged
    assert not result["5"][0] and not result["6"][0] and result["5"][1] != result["6"][1]  # still two sites
    assert not result["7"][0] and not result["8"][0] and result["7"][1] != result["8"][1]
    assert report.duplicate_clusters == 2


def test_a_house_number_that_reappears_in_the_postal_code_does_not_hide_the_conflict(tmp_path):
    rows = [("1", "Sudasty Energy Inc", "78 Cedar Street 2315 Leeds LS1 78"), ("2", "Sudasty Energy Inc", "80 Cedar Street 2315 Leeds LS1 78")]
    report, result = run(tmp_path, rows)
    assert report.duplicate_clusters == 0 and result["1"][1] != result["2"][1]


def run_keys(tmp_path, rows, keys, **config):
    staging = tmp_path / "k.parquet"
    values = ", ".join("('{}', '{}', '{}', '{}', 'OK')".format(*r) for r in rows)
    duckdb.connect().execute(f"COPY (SELECT * FROM (VALUES {values}) t(_SRC_PK, name, email, phone, _TRANSFORM_STATUS)) TO '{staging.as_posix()}' (FORMAT parquet)")
    report = DeduplicationEngine().deduplicate(staging, tmp_path / "o.parquet", DedupConfig(exact_keys=keys, **config))
    out = duckdb.connect().execute("SELECT _SRC_PK, is_duplicate_child FROM read_parquet(?)", [str(tmp_path / "o.parquet")]).fetchall()
    return report, dict(out)


def test_placeholder_keys_never_link_unrelated_records(tmp_path):
    from src.schemas.execution import ExactKey
    junk = [(str(i), f"Company {i}", "john.doe@", "0000000", "") for i in range(1, 9)]  # 8 records sharing junk e-mail / phone
    junk += [(str(i), f"Other {i}", "no email", "123", "") for i in range(20, 24)] + [("30", "Third", "a@b", "5555555", ""), ("31", "Fourth", "a@b", "5555555", "")]
    real = [("40", "Real One", "Sales@Real.Example.com ", "+49 30 5550100", ""), ("41", "Different Name", "sales@real.example.com", "(49) 30-5550100", "")]
    rows = [(a, b, c, d, e) for a, b, c, d, e in junk + real]
    keys = [ExactKey(name="email", columns=["email"], normalizer="email"), ExactKey(name="phone", columns=["phone"], normalizer="digits")]
    report, result = run_keys(tmp_path, rows, keys)
    assert not any(result[str(i)] for i in list(range(1, 9)) + list(range(20, 24)))  # junk e-mails / short or repeated-digit phones link nobody
    assert report.duplicate_clusters == 1 and {result["40"], result["41"]} == {True, False}  # a genuine shared mailbox + phone still links
    assert result["30"] != result["31"] or not result["30"]  # "5555555" is a repeated digit => not a key, "a@b" is not an address


def test_a_value_shared_by_too_many_records_is_treated_as_a_placeholder(tmp_path):
    from src.schemas.execution import ExactKey
    rows = [(str(i), f"Company {i}", "accounts@holding.example.com", "", "") for i in range(1, 31)]  # one group mailbox on 30 customers
    rows += [("90", "Pair A", "pair@x.example.com", "", ""), ("91", "Pair B", "pair@x.example.com", "", "")]
    keys = [ExactKey(name="email", columns=["email"], normalizer="email")]
    report, result = run_keys(tmp_path, rows, keys, max_key_group=25)
    assert report.duplicate_clusters == 1 and report.ignored_shared_keys == {"email": 1}
    assert not any(result[str(i)] for i in range(1, 31)) and {result["90"], result["91"]} == {True, False}
    report2, result2 = run_keys(tmp_path, rows, keys, max_key_group=40)  # raising the cap makes the group link (explicit choice)
    assert report2.duplicate_clusters == 2 and report2.ignored_shared_keys == {}
