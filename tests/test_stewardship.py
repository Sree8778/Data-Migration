"""Stewardship tasks (who fixes what, by when), the review of possible duplicates, and content packs."""

from __future__ import annotations

import csv
import io
import json
from datetime import date

import duckdb
import httpx
import pytest

from src.api.routes.packs import _digest
from src.schemas.execution import DedupConfig
from src.services.deduplication_engine import DeduplicationEngine
from src.services.duplicate_review import DuplicateReview, ReviewError, pair_key
from src.services.stewardship import Notifier, TaskError, TaskStore
from tests.test_module7_api import SCRIPT, api_env, client, csv_path, ctx, dataset, encoder, env, make_ctx, subset, upload  # noqa: F401

TODAY = date(2026, 6, 15)


# ------------------------------------------------------------------ tasks: the store
def store(tmp_path, notifier=None):
    return TaskStore(tmp_path, notifier, today=lambda: TODAY)


def test_a_task_has_an_owner_a_due_date_a_status_and_a_history(tmp_path):
    tasks = store(tmp_path)
    t = tasks.create("alice", "Fix the payment terms", owner="finance.owner", due="2026-06-30", priority="HIGH", ref={"run_id": 5, "signature": "ERR_LOOKUP_FAILED_ZTERM"})
    assert t["id"] == 1 and t["status"] == "OPEN" and t["owner"] == "finance.owner" and t["created_by"] == "alice"
    moved = tasks.update(1, "finance.owner", status="IN_PROGRESS", note="started")
    done = tasks.update(1, "finance.owner", status="DONE", owner="bob", due="", priority="LOW")
    assert moved["status"] == "IN_PROGRESS" and done["status"] == "DONE" and done["owner"] == "bob" and done["due"] is None and done["priority"] == "LOW"
    notes = tasks.get(1)["notes"]
    assert [n["by"] for n in notes] == ["finance.owner", "finance.owner"] and "status OPEN -> IN_PROGRESS" in notes[0]["changes"]
    assert any("owner finance.owner -> bob" in c for c in notes[1]["changes"]) and notes[0]["text"] == "started"
    for bad in (dict(title="x"), dict(title="Fine title", kind="NOPE"), dict(title="Fine title", priority="URGENT"), dict(title="Fine title", due="31/12/2026")):
        with pytest.raises(TaskError):
            tasks.create("alice", **bad)
    with pytest.raises(TaskError):
        tasks.update(99, "alice", status="DONE")
    with pytest.raises(TaskError):
        tasks.update(1, "alice", status="FINISHED")


def test_overdue_is_computed_from_the_due_date_and_open_status(tmp_path):
    tasks = store(tmp_path)
    tasks.create("a", "Late and open", owner="o1", due="2026-06-01")
    tasks.create("a", "Late but done", owner="o1", due="2026-06-01")
    tasks.create("a", "Due today", owner="o2", due="2026-06-15")
    tasks.create("a", "No owner, no date")
    tasks.update(2, "o1", status="DONE")
    by_title = {t["title"]: t for t in tasks.list()}
    assert by_title["Late and open"]["overdue"] and not by_title["Late but done"]["overdue"] and not by_title["Due today"]["overdue"] and not by_title["No owner, no date"]["overdue"]
    s = tasks.summary()
    assert s["open"] == 3 and s["overdue"] == 1 and s["unassigned"] == 1 and s["by_status"]["DONE"] == 1
    assert s["by_owner"] == {"o1": {"open": 1, "overdue": 1}, "o2": {"open": 1, "overdue": 0}, "(unassigned)": {"open": 1, "overdue": 0}}
    assert [t["title"] for t in tasks.list(owner="O1")] == ["Late and open", "Late but done"] and len(tasks.list(open_only=True)) == 3
    assert [t["title"] for t in tasks.list(overdue=True)] == ["Late and open"]
    assert tasks.list()[0]["title"] == "Late and open" and tasks.list()[-1]["status"] == "DONE"          # late and open first, finished last


def test_a_webhook_hears_about_tasks_and_a_broken_one_blocks_nothing(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    tasks = store(tmp_path, Notifier("http://bridge.example/hook", httpx.MockTransport(handler), synchronous=True))
    tasks.create("alice", "Check the duplicates", owner="o1", due="2026-06-01")
    tasks.update(1, "alice", owner="o2")
    tasks.update(1, "o2", status="DONE")
    events = [e["event"] for e in seen]
    assert events == ["task_created", "task_assigned", "task_closed"]
    assert set(seen[0]["task"]) == {"id", "title", "kind", "owner", "due", "priority", "status", "ref"}          # references and counts only
    tasks.create("alice", "Still late", owner="o1", due="2026-06-01")
    assert len(tasks.remind()) == 1 and seen[-1]["event"] == "task_overdue"

    def down(request):
        raise httpx.ConnectError("bridge is down")

    broken = store(tmp_path / "b", Notifier("http://bridge.example/hook", httpx.MockTransport(down), synchronous=True))
    assert broken.create("alice", "Created anyway")["id"] == 1 and Notifier(None).enabled is False


def test_defect_tasks_are_created_once_per_group_routed_by_table_and_close_themselves(tmp_path):
    class D:
        def __init__(self, sig, target, sev, n, open_=True):
            self.rule_signature, self.target, self.violation_count, self.is_open, self.root_cause = sig, target, n, open_, "why"
            self.severity = type("S", (), {"value": sev})()

    defects = [D("ERR_LOOKUP_FAILED_ZTERM", "KNB1.ZTERM", "CRITICAL", 7000), D("ERR_LENGTH_CITY1", "ADRC.CITY1", "HIGH", 12), D("ERR_FIXED", "BUT000.NAME_ORG1", "HIGH", 0, open_=False)]
    tasks = store(tmp_path)
    made = tasks.from_defects("alice", 5, defects, owner="default.owner", due="2026-07-01", owners_by_table={"ADRC": "address.owner"})
    assert {t["ref"]["signature"]: (t["owner"], t["priority"]) for t in made} == {"ERR_LOOKUP_FAILED_ZTERM": ("default.owner", "HIGH"), "ERR_LENGTH_CITY1": ("address.owner", "MEDIUM")}
    assert tasks.from_defects("alice", 5, defects, owner="x", due=None) == []                                      # asking again creates nothing
    closed = tasks.sync_defects(5, {"ERR_LOOKUP_FAILED_ZTERM"})                                                      # the city defect is gone in the later validation
    assert [c["ref"]["signature"] for c in closed] == ["ERR_LENGTH_CITY1"] and closed[0]["status"] == "DONE" and "closed automatically" in closed[0]["notes"][-1]["text"]


# ------------------------------------------------------------------ tasks: the API
def test_tasks_through_the_api_with_roles_dashboard_and_defects(client, api_env, ctx):
    created = client.post("/api/tasks", json={"title": "Correct the French customers", "owner": "alice", "due": "2000-01-01", "priority": "HIGH"})
    assert created.status_code == 201 and created.json()["created_by"] == "alice"
    assert client.post("/api/tasks", json={"title": "no", "owner": "x"}).status_code == 422
    assert client.post("/api/tasks", json={"title": "A fine title", "due": "soon"}).status_code == 422
    assert client.post("/api/tasks", json={"title": "A fine title", "surprise": 1}).status_code == 422
    listed = client.get("/api/tasks", params={"owner": "alice"}).json()
    assert listed["total"] == 1 and listed["tasks"][0]["overdue"] is True
    assert client.get("/api/tasks/summary").json()["overdue"] == 1 and client.get("/api/tasks/999").status_code == 404
    updated = client.post("/api/tasks/1/update", json={"status": "IN_PROGRESS", "note": "on it"})
    assert updated.status_code == 200 and updated.json()["status"] == "IN_PROGRESS" and client.post("/api/tasks/999/update", json={"status": "DONE"}).status_code == 404
    dash = client.get("/api/dashboard").json()
    assert dash["tasks"]["overdue"] == 1 and any("overdue" in a for a in dash["attention"])

    run = client.post(f"/api/pipeline/run/{api_env['mapping'].id}", json={"wave_name": "T"}).json()
    run_id = run["run_id"]
    open_groups = [d for d in client.get(f"/api/defects/{run_id}").json()["defects"] if d["is_open"]]
    assert open_groups
    made = client.post(f"/api/tasks/from-defects/{run_id}", json={"owner": "steward", "due": "2099-01-01", "owners_by_table": {"ADRC": "address.owner"}}).json()
    assert made["created"] == len(open_groups) and {t["owner"] for t in made["tasks"]} <= {"steward", "address.owner"}
    assert client.post(f"/api/tasks/from-defects/{run_id}", json={"owner": "steward"}).json()["created"] == 0
    synced = client.post(f"/api/tasks/sync/{run_id}").json()
    assert synced["closed"] == 0                                                                                  # every defect is still open
    client.post("/api/tasks", json={"title": "About a defect that is gone", "kind": "DEFECT_GROUP", "ref": {"run_id": run_id, "signature": "ERR_NOT_THERE"}})
    assert client.post(f"/api/tasks/sync/{run_id}").json()["closed"] == 1
    assert client.get("/api/tasks/remind").status_code in (404, 405, 422) and client.post("/api/tasks/remind").json()["webhook"] == "not configured"


# ------------------------------------------------------------------ possible duplicates: decisions
def test_decisions_are_kept_per_table_replace_each_other_and_keep_their_history(tmp_path):
    review = DuplicateReview(tmp_path)
    assert review.decide(1, 7, [{"pk_a": "B", "pk_b": "A", "decision": "MERGE", "reason": "same VAT"}], "ann") == {"MERGE": 1, "DISTINCT": 0, "CLEAR": 0}
    review.decide(1, 8, [{"pk_a": "A", "pk_b": "B", "decision": "DISTINCT"}, {"pk_a": "C", "pk_b": "D", "decision": "MERGE"}], "bob")
    d = review.decisions(1)
    assert d[pair_key("A", "B")]["decision"] == "DISTINCT" and d[pair_key("A", "B")]["by"] == "bob" and d[pair_key("A", "B")]["history"][0]["decision"] == "MERGE"
    assert review.pairs(1) == ([("C", "D")], [("A", "B")]) and review.decisions(2) == {}                                       # another table has its own
    review.decide(1, 9, [{"pk_a": "B", "pk_b": "A", "decision": "CLEAR"}], "bob")
    assert pair_key("A", "B") not in review.decisions(1)
    for bad in ({"pk_a": "A", "pk_b": "A", "decision": "MERGE"}, {"pk_a": "A", "pk_b": "B", "decision": "MAYBE"}, {"pk_a": "", "pk_b": "B", "decision": "MERGE"}):
        with pytest.raises(ReviewError):
            review.decide(1, 9, [bad], "bob")


def _staging(tmp_path, rows):
    path = tmp_path / "stg.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t(_SRC_PK VARCHAR, name VARCHAR, street VARCHAR, _TRANSFORM_STATUS VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, 'OK')", rows)
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT parquet)")
    return path


def test_the_engine_applies_what_people_decided(tmp_path):
    rows = [("1", "Northwind Traders", None), ("2", "Northwind Trading", None),          # possible duplicates (82 %)
            ("3", "Acme Rockets", "5 Main St"), ("4", "Acme Rokets", "5 Main St"),       # merged on name + address
            ("5", "Maria Garcia Lopez", None), ("6", "Maria Garcia Lopes", None),        # merged on the name alone
            ("7", "Wayne Industries", "1 Cave Rd")]
    path = _staging(tmp_path, rows)

    def run(**kw):
        report = DeduplicationEngine().deduplicate(path, tmp_path / "o.parquet", DedupConfig(name_column="name", address_columns=["street"], **kw))
        out = {r[0]: r for r in duckdb.sql(f"SELECT _SRC_PK, is_duplicate_child, surviving_master_pk, dedup_match_strength FROM read_parquet('{(tmp_path / 'o.parquet').as_posix()}')").fetchall()}
        return report, {k for k, v in out.items() if v[1]}, out

    report, children, _ = run()
    assert children == {"4", "6"} and report.possible_duplicate_pairs == 1 and report.reviewed_merges == 0
    report, children, out = run(must_link=[("1", "2")], cannot_link=[("5", "6"), ("3", "4")])
    assert children == {"2"} and out["2"][3] == "REVIEWED" and out["2"][2] == "1"                                      # the two Northwinds are one, the others are kept apart
    assert report.reviewed_merges == 1 and report.reviewed_separations == 2 and report.possible_duplicate_pairs == 0   # a decided pair is no longer a question
    _, children, _ = run(must_link=[("5", "6"), ("5", "999")], cannot_link=[("5", "6")])                                 # a contradiction: 'different' wins, an unknown record is ignored
    assert "6" not in children


def test_the_review_lists_and_the_decisions_flow_through_the_api(client, ctx):
    client.post("/api/admin/seed-sap-catalog")
    rows = [("1001", "Northwind Traders", ""), ("1002", "Northwind Trading", ""), ("1003", "Maria Garcia Lopez", ""), ("1004", "Maria Garcia Lopes", ""),
            ("1005", "Acme Rockets Ltd", "Denver"), ("1006", "Blue Sky Foods", "Austin"), ("1007", "Delta Motors", "Boston"), ("1008", "Echo Labs", "Seattle")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["CUST_ID", "NAME", "CITY"])
    w.writerows(rows)
    table_id = client.post("/api/sources/register", data={"system_name": "L", "table_name": "DUP_T", "primary_key_columns": "CUST_ID"}, files={"file": ("d.csv", buf.getvalue().encode(), "text/csv")}).json()["table_id"]
    mapping = client.post("/api/mapping-sets", json={"source_table_id": table_id}).json()["mapping_set_id"]
    for target, column, source in (("BUT000", "NAME_ORG1", "NAME"), ("ADRC", "CITY1", "CITY"), ("BUT000", "BP_EXT", "CUST_ID")):
        assert client.post(f"/api/mapping/{mapping}/rules", json={"target_table": target, "target_column": column, "rule_type": "DIRECT_COPY", "source_column": source}).status_code == 201

    first = client.post(f"/api/pipeline/run/{mapping}", json={"wave_name": "R1"}).json()
    assert first["dedup"]["possible_duplicate_pairs"] == 1 and first["dedup"]["weak_match_clusters"] == 1 and first["dedup"]["duplicate_children"] == 1
    possible = client.get(f"/api/duplicates/{first['run_id']}/possible").json()
    assert possible["total"] == 1 and possible["undecided"] == 1 and {possible["items"][0]["pk_a"], possible["items"][0]["pk_b"]} == {"1001", "1002"}
    merges = client.get(f"/api/duplicates/{first['run_id']}/merges").json()
    assert merges["total"] == 1 and merges["items"][0]["child_pk"] == "1004" and merges["items"][0]["master_pk"] == "1003" and merges["items"][0]["strength"] == "NAME_ONLY"

    decided = client.post(f"/api/duplicates/{first['run_id']}/decide", json={"decisions": [
        {"pk_a": "1001", "pk_b": "1002", "decision": "MERGE", "reason": "same company, two spellings"}, {"pk_a": "1003", "pk_b": "1004", "decision": "DISTINCT", "reason": "two people"}]})
    assert decided.status_code == 200 and decided.json()["recorded"]["MERGE"] == 1 and decided.json()["applies_from"] == "the next run of this table"
    assert client.get(f"/api/duplicates/{first['run_id']}/possible").json()["items"][0]["decision"] == "MERGE"          # the first run's list now shows the decision
    assert client.post(f"/api/duplicates/{first['run_id']}/decide", json={"decisions": [{"pk_a": "1", "pk_b": "1", "decision": "MERGE"}]}).status_code == 422

    second = client.post(f"/api/pipeline/run/{mapping}", json={"wave_name": "R2"}).json()
    d = second["dedup"]
    assert d["reviewed_merges"] == 1 and d["reviewed_separations"] == 1 and d["possible_duplicate_pairs"] == 0 and d["duplicate_children"] == 1
    out = {r[0]: r for r in duckdb.sql(f"SELECT _SRC_PK, is_duplicate_child, dedup_match_strength FROM read_parquet('{ctx.run_dir(second['run_id']).as_posix()}/deduplicated.parquet')").fetchall()}
    assert out["1002"][1] and out["1002"][2] == "REVIEWED" and not out["1003"][1] and not out["1004"][1]                 # Northwind merged on the people's word; the two Marias both load
    assert client.get(f"/api/duplicates/{second['run_id']}/decisions").json()["total"] == 2
    assert client.get("/api/duplicates/999999/possible").status_code == 404


# ------------------------------------------------------------------ content packs
def _land(client, name, rows, header):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return client.post("/api/sources/register", data={"system_name": "L2", "table_name": name}, files={"file": (f"{name}.csv", buf.getvalue().encode(), "text/csv")}).json()["table_id"]


def test_a_mapping_can_be_exported_and_imported_as_a_checked_draft(client, api_env, ctx, dataset):
    mapping_id, table_id = api_env["mapping"].id, api_env["table"].id
    client.post(f"/api/mapping/{mapping_id}/exclusions", json={"column": "CUST_NAME", "operator": "starts_with", "values": ["TEST"], "reason": "test customers"})
    pack = client.get(f"/api/packs/export/{mapping_id}", params={"name": "AR customers v1"}).json()
    assert pack["format"] == "migration-content-pack" and pack["name"] == "AR customers v1" and len(pack["rules"]) == 10 and pack["source"]["table_name"] == "AR_CUSTOMERS"
    assert {r["rule_type"] for r in pack["rules"]} >= {"DIRECT_COPY", "VALUE_MAPPING", "SQL_EXPRESSION", "STATIC_VALUE"} and len(pack["exclusions"]) == 1
    lookups = {r["target_column"]: r["lookups"] for r in pack["rules"] if r["rule_type"] == "VALUE_MAPPING"}
    assert lookups["COUNTRY"]["GERMANY"] == "DE" and lookups["ZTERM"]["NET30"] == "NT30"
    body = {k: v for k, v in pack.items() if k != "sha256"}
    assert pack["sha256"] == _digest(body)
    assert "Firma" not in json.dumps(pack) and "@" not in json.dumps(pack)                                         # rules, never record values

    rows = [[r[h] for h in ("CUST_ID", "CUST_NAME", "TAX_ID", "EMAIL", "PHONE", "ADDR_STREET", "CITY", "COUNTRY", "POSTAL_CODE", "PAYMENT_TERMS")] for r in subset(dataset, 30)]
    header = ["CUST_ID", "CUST_NAME", "TAX_ID", "EMAIL", "PHONE", "ADDR_STREET", "CITY", "COUNTRY", "POSTAL_CODE", "PAYMENT_TERMS"]
    other = _land(client, "AR_CUSTOMERS_WAVE2", rows, header)

    dry = client.post(f"/api/packs/import/{other}", json={"pack": pack, "dry_run": True})
    assert dry.status_code == 201 and dry.json()["dry_run"] and dry.json()["mapping_set_id"] is None and dry.json()["rules_imported"] == 10 and dry.json()["rules_skipped"] == []
    assert client.get("/api/mapping-sets", params={"source_table_id": other}).json() == []                           # a dry run writes nothing
    done = client.post(f"/api/packs/import/{other}", json={"pack": pack})
    assert done.status_code == 201, done.text
    result = done.json()
    assert result["mapping_set_id"] and result["rules_imported"] == 10 and result["lookup_entries"] > 10 and result["exclusions_imported"] == 1
    detail = client.get(f"/api/mapping/{result['mapping_set_id']}").json()
    assert detail["mapping"]["status"] == "DRAFT" and detail["mapping"]["created_by"] == "alice" and len(detail["rules"]) == 10
    assert all(r["needs_review"] and "content pack" in " ".join(r["review_reasons"]) for r in detail["rules"])        # every imported rule waits for a human
    country = next(r for r in detail["rules"] if r["target_column"] == "COUNTRY")
    assert country["lookup_count"] == len(lookups["COUNTRY"]) and len(client.get(f"/api/mapping/{result['mapping_set_id']}/exclusions").json()["rules"]) == 1
    assert client.post(f"/api/governance/approve-mapping/{result['mapping_set_id']}", headers={"X-User": "data.steward"}).status_code in (409, 422)   # still a draft: it must be submitted first


def test_a_pack_that_does_not_fit_says_so_and_a_changed_pack_is_refused(client, api_env, dataset):
    pack = client.get(f"/api/packs/export/{api_env['mapping'].id}").json()
    narrow = _land(client, "NARROW", [["1", "Acme", "Denver"]], ["CUST_ID", "CUST_NAME", "CITY"])                  # most columns are missing
    r = client.post(f"/api/packs/import/{narrow}", json={"pack": pack, "dry_run": True}).json()
    assert 0 < r["rules_imported"] < 10 and r["rules_skipped"] and all("not in table" in s["reason"] or "not in" in s["reason"] for s in r["rules_skipped"])
    assert any("did not fit" in step for step in r["next_steps"]) and {s["what"] for s in r["rules_skipped"]} >= {"ADRC.STREET"}

    changed = json.loads(json.dumps(pack))
    changed["rules"][0]["transformation_logic"] = "upper(CUST_NAME)"
    refused = client.post(f"/api/packs/import/{narrow}", json={"pack": changed})
    assert refused.status_code == 422 and "checksum" in refused.json()["detail"]
    assert client.post(f"/api/packs/import/{narrow}", json={"pack": {**pack, "version": 2}}).status_code == 422
    assert client.post("/api/packs/import/999999", json={"pack": pack}).status_code == 404

    evil = {k: v for k, v in pack.items() if k != "sha256"}
    evil["rules"] = [{"target_table": "BUT000", "target_column": "NAME_ORG1", "rule_type": "SQL_EXPRESSION", "source_column": "CUST_NAME", "transformation_logic": "read_text('C:/secrets.txt')", "lookups": {}, "provenance": {}}]
    evil["sha256"] = _digest(evil)                                                                                  # a consistent checksum does not make unsafe SQL safe
    none_fit = client.post(f"/api/packs/import/{narrow}", json={"pack": evil})
    assert none_fit.status_code == 422 and "function not allowed" in none_fit.json()["detail"]
    assert client.get("/api/mapping-sets", params={"source_table_id": narrow}).json() == []


# ------------------------------------------------------------------ survivorship
def test_a_merged_record_can_inherit_the_fields_it_lacks_from_its_best_duplicate(tmp_path):
    path = tmp_path / "s.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t(_SRC_PK VARCHAR, name VARCHAR, street VARCHAR, phone VARCHAR, city VARCHAR, _TRANSFORM_STATUS VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?)", [
        ("1", "Acme Rockets Ltd", "55 Market Street", "555-0101", None, "OK"),
        ("2", "Acme Rokets Limited", "55 Market St", None, "Denver", "OK"),
        ("3", "Acme Rockets", "55 Market Street", "999-FAILED", "FAILEDVILLE", "FAILED"),     # a failed record never supplies a value
        ("4", "Globex", "1 Tech Way", None, "Austin", "OK")])
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT parquet)")

    def run(mode):
        report = DeduplicationEngine().deduplicate(path, tmp_path / f"{mode}.parquet", DedupConfig(name_column="name", address_columns=["street"], survivorship=mode))
        rows = {r[0]: r for r in duckdb.sql(f"SELECT _SRC_PK, is_duplicate_child, phone, city, dedup_enriched_columns, name FROM read_parquet('{(tmp_path / f'{mode}.parquet').as_posix()}')").fetchall()}
        return report, rows

    report, rows = run("master_only")
    assert report.enriched_cells == 0 and rows["1"][3] is None and rows["1"][4] is None                              # today's behaviour is the default
    report, rows = run("fill_blanks")
    assert report.enriched_cells == 1 and rows["1"][3] == "Denver" and rows["1"][4] == "city" and rows["1"][2] == "555-0101"
    assert rows["1"][5] == "Acme Rockets Ltd" and rows["4"][3] == "Austin" and rows["4"][4] is None                    # the name is never borrowed; a record without duplicates is untouched
    assert any("filled from a duplicate" in w for w in report.warnings)


def test_survivorship_is_a_pipeline_option(client, ctx):
    client.post("/api/admin/seed-sap-catalog")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["CUST_ID", "NAME", "NAME2", "SORT", "CITY", "STREET"])
    # two good records of one customer: each fills one optional field the other lacks (equal completeness, so the smaller key is the master)
    w.writerows([("1", "Acme Rockets Ltd", "", "ACME", "Denver", "55 Market Street"), ("2", "Acme Rokets Ltd", "c/o Warehouse", "", "Denver", "55 Market Street"),
                 ("3", "Globex", "Attn: AP", "GLOB", "Austin", "1 Tech Way")])
    table_id = client.post("/api/sources/register", data={"system_name": "L", "table_name": "SURV_T", "primary_key_columns": "CUST_ID"}, files={"file": ("s.csv", buf.getvalue().encode(), "text/csv")}).json()["table_id"]
    mapping = client.post("/api/mapping-sets", json={"source_table_id": table_id}).json()["mapping_set_id"]
    for target, column, source in (("BUT000", "NAME_ORG1", "NAME"), ("BUT000", "NAME_ORG2", "NAME2"), ("BUT000", "BU_SORT1", "SORT"), ("ADRC", "CITY1", "CITY"),
                                   ("ADRC", "STREET", "STREET"), ("BUT000", "BP_EXT", "CUST_ID")):
        assert client.post(f"/api/mapping/{mapping}/rules", json={"target_table": target, "target_column": column, "rule_type": "DIRECT_COPY", "source_column": source}).status_code == 201
    plain = client.post(f"/api/pipeline/run/{mapping}", json={"wave_name": "A"}).json()
    filled = client.post(f"/api/pipeline/run/{mapping}", json={"wave_name": "B", "survivorship": "fill_blanks"}).json()
    assert plain["dedup"]["duplicate_children"] == 1 and plain["dedup"]["enriched_cells"] == 0 and filled["dedup"]["enriched_cells"] == 1
    survivors = {r[0]: r for r in duckdb.sql(f"SELECT _SRC_PK, BUT000_NAME_ORG2, dedup_enriched_columns FROM read_parquet('{ctx.run_dir(filled['run_id']).as_posix()}/deduplicated.parquet') WHERE NOT is_duplicate_child").fetchall()}
    assert survivors["1"][1] == "c/o Warehouse" and survivors["1"][2] == "BUT000_NAME_ORG2"                       # the surviving record has the second name line only its duplicate knew
    assert survivors["3"][2] is None
    assert client.post(f"/api/pipeline/run/{mapping}", json={"survivorship": "sometimes"}).status_code == 422


# ------------------------------------------------------------------ maintenance
def test_the_maintenance_job_erases_what_is_due_skips_holds_and_is_on_the_trail(client, api_env, ctx):
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    import sqlalchemy as sa
    from src.db.models import MigMigrationRun

    mid = api_env["mapping"].id
    old, held, fresh = (client.post(f"/api/pipeline/run/{mid}", json={"wave_name": w}).json()["run_id"] for w in ("OLD", "HELD", "FRESH"))
    when = datetime.now(timezone.utc) - timedelta(days=100)
    with ctx.session_factory.begin() as s:
        for rid in (old, held):
            s.execute(sa.update(MigMigrationRun).where(MigMigrationRun.id == rid).values(completed_at=when, started_at=when - timedelta(minutes=1)))
    client.post(f"/api/compliance/runs/{held}/hold", json={"reason": "dispute"})
    client.post("/api/tasks", json={"title": "Late task", "owner": "x", "due": "2000-01-01"})

    assert ctx.run_maintenance()["retention"] is None                                                                   # no retention period: nothing is erased
    ctx.settings = replace(ctx.settings, retention_days=30)
    result = client.post("/api/compliance/maintenance/run").json()
    assert result["retention"] == {"erased_runs": [old], "skipped_under_hold": [held]} and result["overdue_tasks"] == 1
    assert not (ctx.run_dir(old) / "staging.parquet").exists() and (ctx.run_dir(held) / "staging.parquet").is_file() and (ctx.run_dir(fresh) / "staging.parquet").is_file()
    assert any(e.get("method") == "JOB" and e.get("user", "").startswith("system:maintenance") for e in ctx.audit.entries()) and ctx.audit.verify()["ok"]

    ctx.settings = replace(ctx.settings, maintenance_hours=1)
    ctx.start_maintenance()
    ctx.start_maintenance()                                                                                              # starting twice does not start two
    ctx.stop_maintenance()
