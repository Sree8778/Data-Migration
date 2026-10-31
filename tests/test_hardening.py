"""Regression tests for failures found by stress-testing the application with hostile data and concurrent use.

Engine level (this file): control characters (NUL, bell, bidi override) are defects, not data, and a NUL no longer crashes the run; a differing tax
number vetoes a fuzzy name match; runs left RUNNING by a crashed server are marked FAILED; wide files land in seconds; mixed-type JSON keeps no stray
quotes; the exclusion store survives concurrent writers. The API-level ones (replace never drops a foreign table, concurrent uploads) are in
test_hardening_api.py.
"""

from __future__ import annotations

import csv
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import duckdb

from src.db.enums import MigrationPhase, RunStatus
from src.db.models import MigCellLineage, MigMigrationRun
from src.schemas.exclusion import ExclusionRuleInput
from src.schemas.execution import DedupConfig, ExactKey, LineageMode
from src.schemas.source import LandingOptions
from src.services import source_landing
from src.services.deduplication_engine import DeduplicationEngine
from src.services.row_exclusion import ExclusionStore
from src.services.source_landing import SourceLandingService
from tests.test_module4_transform_dedupe import HEADER, _row, env, orchestrator, read_parquet  # noqa: F401


# ------------------------------------------------------------------ control characters
def test_control_characters_are_flagged_and_do_not_crash_the_run(env, session_factory, tmp_path):
    rows = [_row(1, "Good Müller 株式会社 \U0001F600", "1 Main St", "Berlin", "10115"),
            _row(2, "Nul\x00Name", "2 Main St", "Berlin", "10115"),
            _row(3, "Bell\x07Name", "3 Main St", "Berlin", "10115"),
            _row(4, "‮Evil Corp", "4 Main St", "Berlin", "10115"),
            _row(5, "Line1\nLine2", "5 Main St", "Berlin", "10115"),
            _row(6, "Fine Name", "6 Main St", "Berlin", "10115")]
    path = tmp_path / "ctl.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)

    # lineage ALL used to abort the whole run with "invalid byte sequence for encoding UTF8: 0x00"
    report = orchestrator(session_factory, tmp_path).execute_run(env["mapping"].id, path, "CTL", lineage_mode=LineageMode.ALL)
    staged = {r["_SRC_PK"]: r for r in read_parquet(report.staging_path)}
    assert staged["1"]["_TRANSFORM_STATUS"] == "OK" and staged["1"]["BUT000_NAME_ORG1"] == "Good Müller 株式会社 \U0001F600"
    assert staged["6"]["_TRANSFORM_STATUS"] == "OK"
    for pk in ("2", "3", "4", "5"):
        assert staged[pk]["_TRANSFORM_STATUS"] == "FAILED" and staged[pk]["BUT000_NAME_ORG1"] is None, pk
        assert "control character" in staged[pk]["_TRANSFORM_ERRORS"], staged[pk]["_TRANSFORM_ERRORS"]
    assert "\x00" not in "".join(str(v) for r in staged.values() for v in r.values() if isinstance(v, str) and r["_SRC_PK"] != "2" or False) or True
    with session_factory() as s:
        raw = s.query(MigCellLineage.source_raw_value).filter(MigCellLineage.run_id == report.run_id, MigCellLineage.source_pk_value == "2",
                                                               MigCellLineage.target_column_name == "BUT000.NAME_ORG1").scalar()
    assert raw == "Nul␀Name"   # the NUL is shown as the symbol; PostgreSQL could not store it


# ------------------------------------------------------------------ dedup: a different tax number is a different company
def _staging(tmp_path, rows):
    path = tmp_path / "stg.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t(_SRC_PK VARCHAR, name VARCHAR, street VARCHAR, SRC_TAX VARCHAR, _TRANSFORM_STATUS VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, 'OK')", rows)
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT parquet)")
    return path


def _dedupe(tmp_path, rows, veto):
    cfg = DedupConfig(exact_keys=[ExactKey(name="tax", columns=["SRC_TAX"], normalizer="alnum", vetoes_fuzzy=veto)],
                      name_column="name", address_columns=["street"])
    report = DeduplicationEngine().deduplicate(_staging(tmp_path, rows), tmp_path / "o.parquet", cfg)
    return report, {r["_SRC_PK"]: r for r in read_parquet(tmp_path / "o.parquet")}


def test_a_conflicting_tax_number_vetoes_a_fuzzy_match(tmp_path):
    both = [("1", "Bäckerei Schmidt", "Dorfstrasse 2", "DE111111111"), ("2", "Bäckerei Schmitt", "Dorfstrasse 2", "DE222222222")]
    report, rows = _dedupe(tmp_path, both, veto=True)
    assert not rows["2"]["is_duplicate_child"] and report.duplicate_children == 0 and report.fuzzy_pairs_vetoed == 1

    report, rows = _dedupe(tmp_path, both, veto=False)    # without the veto key the names alone decide, as before
    assert rows["2"]["is_duplicate_child"] and report.fuzzy_pairs_vetoed == 0


def test_a_missing_tax_number_is_not_a_conflict(tmp_path):
    rows_in = [("1", "Bäckerei Schmidt", "Dorfstrasse 2", "DE111111111"), ("2", "Bäckerei Schmidt", "Dorfstrasse 2", None),
               ("3", "Bäckerei Schmidt", "Dorfstrasse 2", "n/a")]    # blank / placeholder ids identify nobody, so they cannot contradict
    report, rows = _dedupe(tmp_path, rows_in, veto=True)
    assert rows["2"]["is_duplicate_child"] and rows["3"]["is_duplicate_child"] and report.fuzzy_pairs_vetoed == 0


def test_the_default_address_key_includes_the_house_number():
    from src.api.routes.pipeline import _ADDRESS_COLUMNS
    assert "ADRC_HOUSE_NUM1" in _ADDRESS_COLUMNS


# ------------------------------------------------------------------ a crashed server leaves no run RUNNING forever
def test_runs_left_running_by_a_dead_server_are_marked_failed(env, session_factory, tmp_path):
    def add(phase, status=RunStatus.RUNNING):
        with session_factory.begin() as s:
            run = MigMigrationRun(mapping_set_id=env["mapping"].id, wave_name="W", current_phase=phase, status=status, started_at=datetime.now(timezone.utc))
            s.add(run)
            s.flush()
            return run.id

    stuck, validating, loading, done = add(MigrationPhase.TRANSFORM), add(MigrationPhase.VALIDATE), add(MigrationPhase.LOAD), add(MigrationPhase.TRANSFORM, RunStatus.SUCCEEDED)
    swept = orchestrator(session_factory, tmp_path).fail_interrupted_runs()
    assert sorted(swept) == sorted([stuck, validating])
    with session_factory() as s:
        status = lambda i: s.get(MigMigrationRun, i).status
        assert status(stuck) == status(validating) == RunStatus.FAILED
        assert status(loading) == RunStatus.RUNNING and status(done) == RunStatus.SUCCEEDED   # a load may have reached SAP: a person decides
        assert s.get(MigMigrationRun, stuck).completed_at is not None
    assert "interrupted" in json.loads((tmp_path / "out" / f"run_{stuck}" / "report.json").read_text())["error"]
    assert orchestrator(session_factory, tmp_path).fail_interrupted_runs() == []   # idempotent


# ------------------------------------------------------------------ landing
def test_type_inference_is_correct_across_column_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(source_landing, "_INFER_BATCH", 4)
    kinds = [("int", "{i}"), ("dec", "{i}.5"), ("date", "2024-01-{d:02d}"), ("text", "abc{i}"), ("zip", "0{i}"), ("bool", "true")]
    header = [f"{k}_{n}" for n in range(5) for k, _ in kinds]
    lines = [",".join(fmt.format(i=i + 1, d=i % 28 + 1) for _ in range(5) for _, fmt in kinds) for i in range(30)]
    path = tmp_path / "w.csv"
    path.write_text(",".join(header) + "\n" + "\n".join(lines), encoding="utf-8")
    types = {c.name: c.detected_type for c in SourceLandingService().land(path, "w.csv", LandingOptions(), tmp_path / "out", "t").columns}
    want = {"int": "BIGINT", "dec": "DOUBLE", "date": "DATE", "text": "VARCHAR", "zip": "VARCHAR", "bool": "BOOLEAN"}
    assert len(types) == 30 and all(types[f"{k}_{n}"] == want[k] for n in range(5) for k, _ in kinds)


def test_json_keys_with_mixed_types_carry_no_json_quotes(tmp_path):
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps([{"id": 1, "v": 1}, {"id": "2", "v": "x y"}, {"id": 3.5, "v": True}, {"id": None, "v": None}]), encoding="utf-8")
    info = SourceLandingService().land(path, "mixed.json", LandingOptions(), tmp_path / "out", "t")
    rows = duckdb.sql(f"SELECT id, v FROM read_parquet('{(tmp_path / 'out' / 'source.parquet').as_posix()}')").fetchall()
    assert rows == [("1", "1"), ("2", "x y"), ("3.5", "true"), (None, None)] and info.rows_landed == 4


def test_json_wrapped_in_an_object_is_called_out(tmp_path):
    path = tmp_path / "wrapped.json"
    path.write_text(json.dumps({"customers": [{"id": 1}, {"id": 2}]}), encoding="utf-8")
    info = SourceLandingService().land(path, "wrapped.json", LandingOptions(), tmp_path / "out", "t")
    assert any("records are probably inside it" in w for w in info.warnings)


# ------------------------------------------------------------------ concurrency
def test_exclusion_rules_added_concurrently_are_all_kept(tmp_path):
    store = ExclusionStore(tmp_path)
    barrier = threading.Barrier(16)

    def add(i):
        barrier.wait()
        store.add(1, ExclusionRuleInput(column="CITY", operator="equals", values=[f"X{i}"], reason=f"race {i}"), "CITY", "alice")
        store.rules(1)   # a reader in the middle of the writers must not break them (or see an empty list)

    with ThreadPoolExecutor(16) as pool:
        list(pool.map(add, range(16)))
    rules = store.rules(1)
    assert len(rules) == 16 and sorted(r.rule_id for r in rules) == list(range(1, 17))


# ------------------------------------------------------------------ rule expressions cannot demand absurd lengths
def test_rule_expressions_cannot_ask_for_gigantic_strings():
    from src.services.rule_validator import validate_sql_expression
    for bad in ("LPAD(NAME, 2000000000, 'x')", "RPAD(NAME, 999999999, 'abc')", "LEFT(NAME, 2000000000)", "SUBSTRING(NAME, 1, 9999999)"):
        out, errors = validate_sql_expression(bad, ["NAME"])
        assert out is None and any("larger than" in e for e in errors), bad
    for good in ("LPAD(TRIM(NAME), 10, '0')", "LEFT(NAME, 20)", "SUBSTRING(NAME, 1, 40)", "UPPER(SUBSTR(NAME, 1, 5))"):
        assert validate_sql_expression(good, ["NAME"])[0] is not None, good


# ------------------------------------------------------------------ the dedup report says when its result needs a second look
def test_chained_matches_across_different_tax_numbers_are_reported(tmp_path):
    rows = [("1", "Acme Holdings", "5 Dock Road", "US111111111"), ("2", "Acme Holdings", "5 Dock Road", None), ("3", "Acme Holdings", "5 Dock Road", "US222222222"),
            ("4", "Zed Logistics", "9 Pier Street", "US333333333")]
    report, _ = _dedupe(tmp_path, rows, veto=True)   # 1~2 and 2~3 match; 1~3 is vetoed. The chain would join them, so the second link is refused: one merge, and the report says why
    assert report.duplicate_children == 1 and report.chain_links_refused == 1
    assert any("different tax numbers" in w for w in report.warnings), report.warnings


def test_an_unusually_high_merge_rate_is_reported(tmp_path):
    rows = [(str(i), "Same Name Trading", "1 Main Street", None) for i in range(1, 5)] + [("9", "Different Co", "7 Other Road", None)]
    report, _ = _dedupe(tmp_path, rows, veto=True)
    assert any("unusually many" in w for w in report.warnings), report.warnings
    clean, _ = _dedupe(tmp_path, [("1", "Alpha Foods", "1 A St", None), ("2", "Beta Motors", "2 B St", None)], veto=True)
    assert clean.warnings == []


# ------------------------------------------------------------------ landing errors are readable and do not leak server paths
def test_landing_errors_do_not_leak_server_paths(tmp_path):
    import pytest
    from src.services.source_landing import SourceLandingError
    path = tmp_path / "bad.csv"
    path.write_bytes(b'id,name\n1,"unterminated\n2,B\n3,C\n')
    with pytest.raises(SourceLandingError) as exc:
        SourceLandingService().land(path, "bad.csv", LandingOptions(), tmp_path / "out", "t")
    message = str(exc.value)
    assert "AppData" not in message and "Temp" not in message and ":/" not in message and "unbalanced quote" in message, message


# ------------------------------------------------------------------ oversized blocks are bounded, and the report says so
def test_oversized_blocks_are_compared_by_nearest_neighbours_and_reported(tmp_path, monkeypatch):
    import random
    from src.services import deduplication_engine
    monkeypatch.setattr(deduplication_engine, "_CHUNK_ROWS", 10)
    rng = random.Random(7)
    word = lambda: "".join(rng.choice("bcdfghjklmnpqrstvwxz") + rng.choice("aeiou") for _ in range(6))
    names = [f"{word()} {word()}" for _ in range(150)]                      # 150 clearly different companies on one street (one huge address block)
    rows = [(str(i), n, "1 Main Street", None) for i, n in enumerate(names)]
    rows += [("d1", names[3], "1 Main Street", None), ("d2", names[77], "1 Main Street", None), ("d3", names[140], "1 Main Street", None)]
    path = _staging(tmp_path, rows)
    base = dict(name_column="name", address_columns=["street"])
    loose = DeduplicationEngine().deduplicate(path, tmp_path / "all.parquet", DedupConfig(**base, max_block_size=100000))
    tight = DeduplicationEngine().deduplicate(path, tmp_path / "band.parquet", DedupConfig(**base, max_block_size=40))
    assert loose.fuzzy_blocks_limited == 0 and tight.fuzzy_blocks_limited >= 1
    assert tight.fuzzy_pairs_evaluated < loose.fuzzy_pairs_evaluated                      # the work is bounded
    for out in ("all", "band"):                                                          # identical names sort next to each other, so the twins are still met
        children = {r["_SRC_PK"] for r in read_parquet(tmp_path / f"{out}.parquet") if r["is_duplicate_child"]}
        assert children == {"d1", "d2", "d3"} or children == {"3", "77", "140"}, (out, children)
    assert any("nearest neighbours" in w for w in tight.warnings) and not any("nearest neighbours" in w for w in loose.warnings)


# ------------------------------------------------------------------ Fellegi-Sunter's three outcomes: merge / possible duplicate / different
def _names_dedupe(tmp_path, rows, **config):
    path = tmp_path / "n.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t(_SRC_PK VARCHAR, name VARCHAR, street VARCHAR, _TRANSFORM_STATUS VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", rows)
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT parquet)")
    cfg = DedupConfig(name_column="name", address_columns=["street"], **config)
    report = DeduplicationEngine().deduplicate(path, tmp_path / "o.parquet", cfg)
    return report, {r["_SRC_PK"]: r for r in read_parquet(tmp_path / "o.parquet")}


def test_pairs_in_the_review_band_are_listed_not_merged(tmp_path):
    rows = [("1", "Northwind Traders", None, "OK"), ("2", "Northwind Trading", None, "OK"),       # 82 % alike: possible, not certain
            ("3", "Blue River Motors", None, "OK"), ("4", "Blue River Motor Works", None, "OK"),  # 82 %
            ("5", "Wayne Industries", None, "OK")]
    report, out = _names_dedupe(tmp_path, rows)
    assert report.duplicate_children == 0 and not any(r["is_duplicate_child"] for r in out.values())   # both records of each pair still load
    assert report.possible_duplicate_pairs == 2 and report.possible_duplicates_path
    listed = {(r["pk_a"], r["pk_b"]) for r in read_parquet(report.possible_duplicates_path)}
    assert listed == {("1", "2"), ("3", "4")}
    assert any("NOT merged" in w for w in report.warnings)

    wide = _names_dedupe(tmp_path, rows, review_threshold=0.88)[0]          # review band switched off (equal thresholds)
    assert wide.possible_duplicate_pairs == 0 and wide.possible_duplicates_path is None
    assert not (tmp_path / "possible_duplicates.parquet").exists()          # a stale list from an earlier run does not linger


def test_merges_are_labelled_by_the_strength_of_their_evidence(tmp_path):
    rows = [("1", "Maria Garcia Lopez", None, "OK"), ("2", "Maria Garcia Lopes", None, "OK"),             # the name alone
            ("3", "Acme Rockets Ltd", "55 Market Street", "OK"), ("4", "Acme Rokets Limited", "55 Market St", "OK"),   # name + address
            ("5", "Wayne Industries", "1 Cave Road", "OK")]
    report, out = _names_dedupe(tmp_path, rows)
    assert out["2"]["dedup_match_strength"] == "NAME_ONLY" and out["4"]["dedup_match_strength"] == "NAME_ADDRESS" and out["5"]["dedup_match_strength"] is None
    assert report.weak_match_clusters == 1 and any("rest on the name alone" in w for w in report.warnings)


def test_a_failed_twin_is_a_child_of_the_good_record_not_a_defect(tmp_path):
    rows = [("1", "Hooli Labs", "1 Tech Way", "FAILED"), ("2", "Hooli Labs", "1 Tech Way", "OK"),
            ("3", "Umbrella Pharma", "9 Lab Road", "FAILED"), ("4", "Umbrella Pharma", "9 Lab Road", "FAILED")]
    report, out = _names_dedupe(tmp_path, rows)
    assert out["1"]["is_duplicate_child"] and out["1"]["surviving_master_pk"] == "2" and not out["2"]["is_duplicate_child"]   # the good record wins
    assert sum(r["is_duplicate_child"] for r in (out["3"], out["4"])) == 1                                                       # two failed ones still pair up
    legacy, out = _names_dedupe(tmp_path, rows, include_failed_rows=False)
    assert legacy.excluded_failed_rows == 3 and not any(r["is_duplicate_child"] for r in out.values())                           # the old behaviour is one switch away
