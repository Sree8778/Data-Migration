"""A real end-to-end test of the Group 1 controls on the LARGE complex dataset, with real Gemini and a real PostgreSQL target.

    python scripts/generate_complex_data.py --rows 30000 --out mock_data/large --seed 7      # once (about 9 minutes)
    python scripts/create_demo_source_db.py                                                   # once (legacy_demo)
    # the server must run with a Gemini key:  LLM_PROVIDER=gemini GEMINI_API_KEY=... uvicorn src.api.main:create_app --factory --port 8000
    python scripts/test_group1_real.py [--data mock_data/large] [--runs 2]

Every result is compared with the generator's answer key, not merely checked for a 200:

  PART A  the dirty file (all of it)
    A1 land and profile (control total)         A2 AI mapping with repeat runs (model, pinning, unstable columns, evidence flags)
    A3 the pre-submission check on the raw AI draft (it must find the problems), then on the steward's corrected mapping
    A4 row exclusion: the suggestions against the answer key's deleted and test records, then the run's accounting (variance 0)
  PART B  a clean slice, delivered into PostgreSQL
    B1 value-checked delivery   B2 tampering found   B3 a changed source: delta   B4 merge, then rollback restores the table exactly   B5 rollback of the first delivery
Exit code 0 = every check passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import duckdb
import httpx
import psycopg2

ROOT = Path(__file__).resolve().parent.parent
LANG = {"D": "D", "DE": "D", "DEUTSCH": "D", "GERMAN": "D", "E": "E", "EN": "E", "ENGLISH": "E", "ENGLISCH": "E", "F": "F", "FR": "F", "FRANÇAIS": "F", "FRENCH": "F",
        "S": "S", "ES": "S", "ESPAÑOL": "S", "SPANISH": "S", "I": "I", "IT": "I", "ITALIANO": "I", "ITALIAN": "I", "N": "N", "NL": "N", "NEDERLANDS": "N", "DUTCH": "N",
        "H": "H", "HINDI": "H", "P": "P", "PT": "P", "PORTUGUÊS": "P", "PORTUGUESE": "P", "J": "J", "JA": "J", "JAPANESE": "J"}

ap = argparse.ArgumentParser()
ap.add_argument("--api", default="http://localhost:8000")
ap.add_argument("--data", default=str(ROOT / "mock_data" / "large"))
ap.add_argument("--runs", type=int, default=2, help="AI runs per column")
ap.add_argument("--pg-port", default="5433")
args = ap.parse_args()
DATA = Path(args.data)
KEY = json.loads((DATA / "answer_key.json").read_text(encoding="utf-8"))
TAGS = {t: set(pks) for t, pks in KEY["tags"]} if isinstance(KEY["tags"], list) else {t: set(v) for t, v in KEY["tags"].items()}
STAMP = time.strftime("%H%M%S")
api = httpx.Client(base_url=args.api, headers={"X-User": "g1.author"}, timeout=3600)
steward = httpx.Client(base_url=args.api, headers={"X-User": "g1.steward"}, timeout=3600)
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)
    return bool(ok)


def call(client, method, url, expect=(200, 201, 202), **kw):
    r = client.request(method, url, **kw)
    if r.status_code not in expect:
        raise SystemExit(f"{method} {url} -> {r.status_code}: {r.text[:400]}")
    return r.json()


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


# ====================================================================== PART A
step("A1  land and profile the dirty file")
raw = (DATA / "customers_complex.csv").read_bytes()
t0 = time.time()
reg = call(api, "POST", "/api/sources/register", data={"system_name": f"G1_REAL_{STAMP}", "table_name": "KNA1_LARGE", "primary_key_columns": "KUNNR", "expected_rows": str(KEY["rows"])},
           files={"file": ("customers_complex.csv", raw)})
tid = reg["table_id"]
call(api, "POST", f"/api/profile/{tid}")
check("the file lands with its control total", reg["rows_landed"] == KEY["rows"] and reg["control_status"], f"{reg['rows_landed']:,} rows in {time.time() - t0:.0f}s, control {reg['control_status']}")

step(f"A2  AI mapping with {args.runs} runs per column (real Gemini)")
t0 = time.time()
job = call(api, "POST", f"/api/mapping/generate/{tid}?background=true&consistency_runs={args.runs}")
while True:
    s = call(api, "GET", f"/api/mapping/jobs/{job['job_id']}")
    if s["status"] not in ("queued", "running", "cancelling"):
        break
    time.sleep(8)
if s["status"] != "succeeded":
    raise SystemExit(f"AI mapping failed: {s.get('error')}")
report = s["result"]["report"]
ai_set = report["mapping_set_id"]
unstable = [p for p in report["proposals"] if any(r.startswith("unstable") for r in p["reasons"])]
evidence = [p for p in report["proposals"] if any("valid" in r and "keys" in r or "characters but" in r for r in p["reasons"])]
print(f"  model: {report['model_name']} | pinned: {report['model_pinned']} | runs per column: {report['consistency_runs']} | {time.time() - t0:.0f}s")
print(f"  unstable columns ({len(unstable)}): {[(p['source_column'], p['alternatives']) for p in unstable]}")
print(f"  evidence flags ({len(evidence)}): {[(p['source_column'], [r for r in p['reasons'] if 'keys' in r or 'characters' in r]) for p in evidence]}")
check("the model that answered is recorded", bool(report["model_name"]), report["model_name"])
check("every column was asked the requested number of times", all(p["runs"] == report["consistency_runs"] for p in report["proposals"]), f"{report['consistency_runs']} run(s)")
check("an alias is reported as not pinned", report["model_pinned"] is ("latest" not in report["model_name"].split(" -> ")[0]), f"pinned={report['model_pinned']}")
exp = KEY["expected_mapping"]
detail = call(api, "GET", f"/api/mapping/{ai_set}")
got = {r["source_column"]: f"{r['target_table']}.{r['target_column']}" for r in detail["rules"] if r["source_column"]}
correct = [c for c, t in exp.items() if got.get(c) == t]
check("the AI maps most of the mappable columns correctly", len(correct) >= len(exp) * 0.8, f"{len(correct)}/{len(exp)} correct; missed: {[c for c in exp if c not in got]}")

step("A3  the pre-submission check on the raw AI draft, then on the steward's mapping")
raw_check = call(api, "POST", f"/api/mapping/{ai_set}/preflight")
codes = {f["code"] for f in raw_check["findings"]}
blockers = [f["message"][:90] for f in raw_check["findings"] if f["severity"] == "BLOCKER"]
print(f"  raw draft: can_submit={raw_check['can_submit']} blockers={len(blockers)} warnings={raw_check['warnings']} valid rate={raw_check['valid_rate']}")
for b in blockers[:6]:
    print("    BLOCKER", b)
check("the raw draft is NOT submittable (lookups are empty or rules fail)", raw_check["can_submit"] is False and raw_check["blockers"] > 0, f"{raw_check['blockers']} blocker(s): {sorted(codes)}")
refused = api.post(f"/api/governance/submit-mapping/{ai_set}")
check("submitting the raw draft is refused with the report", refused.status_code == 422 and refused.json()["code"] == "PREFLIGHT_BLOCKED", f"HTTP {refused.status_code}")

mset = call(api, "POST", "/api/mapping-sets", json={"source_table_id": tid})["mapping_set_id"]
for r in detail["rules"]:
    call(api, "POST", f"/api/mapping/{mset}/rules", json={k: r[k] for k in ("target_table", "target_column", "rule_type", "source_column", "transformation_logic")})
lk = KEY["lookups"]
rules = {r["source_column"]: r for r in call(api, "GET", f"/api/mapping/{mset}")["rules"] if r["source_column"]}


def put(col, **body):
    call(api, "PUT", f"/api/mapping/rules/{rules[col]['rule_id']}", json={**body, "confirm": True, "note": "steward correction"})


def post(tt, tc, rt, source=None, logic=None, lookups=None):
    call(api, "POST", f"/api/mapping/{mset}/rules", json={"target_table": tt, "target_column": tc, "rule_type": rt, "source_column": source, "transformation_logic": logic, "lookups": lookups})


if "KUNNR" in rules and got.get("KUNNR") != "BUT000.BP_EXT":
    put("KUNNR", target_table="BUT000", target_column="BP_EXT")
if "ZTERM" in rules:
    put("ZTERM", lookups=lk["terms"])
if "WAERS" in rules:
    put("WAERS", rule_type="VALUE_MAPPING", lookups=lk["currency"])
wanted = {"NAME1": ("BUT000", "NAME_ORG1", "DIRECT_COPY", {}), "NAME2": ("BUT000", "NAME_ORG2", "DIRECT_COPY", {}), "SORTL": ("BUT000", "BU_SORT1", "DIRECT_COPY", {}),
          "STRAS": ("ADRC", "STREET", "DIRECT_COPY", {}), "ORT01": ("ADRC", "CITY1", "DIRECT_COPY", {}), "PSTLZ": ("ADRC", "POST_CODE1", "DIRECT_COPY", {}),
          "REGIO": ("ADRC", "REGION", "DIRECT_COPY", {}), "LAND1": ("ADRC", "COUNTRY", "VALUE_MAPPING", {"lookups": lk["country"]}),
          "SPRAS": ("ADRC", "LANGU", "VALUE_MAPPING", {"lookups": LANG}), "KTOKD": ("BUT000", "BU_GROUP", "VALUE_MAPPING", {"lookups": lk["bu_group"]}),
          "INCO1": ("KNVV", "INCO1", "DIRECT_COPY", {}), "VKORG": ("KNVV", "VKORG", "DIRECT_COPY", {}), "VTWEG": ("KNVV", "VTWEG", "DIRECT_COPY", {}),
          "SPART": ("KNVV", "SPART", "DIRECT_COPY", {}), "AKONT": ("KNB1", "AKONT", "DIRECT_COPY", {})}
for col, (tt, tc, rt, extra) in wanted.items():
    if col not in got:
        post(tt, tc, rt, source=col, **extra)
for col in ("LAND1", "SPRAS", "KTOKD"):
    if col in rules:
        put(col, lookups=wanted[col][3]["lookups"])
post("BUT000", "TYPE", "SQL_EXPRESSION", source="KTOKD", logic="CASE WHEN KTOKD = 'ZPER' THEN '1' ELSE '2' END")

fixed = call(api, "POST", f"/api/mapping/{mset}/preflight")
print(f"  steward mapping: can_submit={fixed['can_submit']} blockers={fixed['blockers']} warnings={fixed['warnings']} valid rate={fixed['valid_rate']}")
for f in fixed["findings"][:8]:
    print(f"    {f['severity']:7s} {f['code']:20s} {f['message'][:110]}")
worst = sorted((c for c in fixed["rules"] if c["pass_rate"] is not None), key=lambda c: c["pass_rate"])[:4]
print("  weakest rules on the sample:", [(c["target"], c["pass_rate"], c["failure_kinds"]) for c in worst])
check("the corrected mapping passes the check (no blockers)", fixed["can_submit"] and fixed["blockers"] == 0, f"valid rate {fixed['valid_rate']:.1%} on {fixed['sample_rows']:,} of {fixed['source_rows']:,} rows")
unmatched = {c["target"]: c["unmatched_values"][:3] for c in fixed["rules"] if c["unmatched_values"]}
check("lookup rules list the legacy values that have no entry", bool(unmatched), str({k: [(u['value'], u['count']) for u in v] for k, v in list(unmatched.items())[:2]}))

step("A4  rows to leave out: suggestions vs the answer key, then the run's accounting")
sug = call(api, "GET", f"/api/mapping/{mset}/exclusions/suggestions")
flag = next((s for s in sug if s["column"] == "LOEVM"), None)
names = next((s for s in sug if s["column"] == "NAME1"), None)
truth_deleted, truth_test = TAGS["deleted_flag"], TAGS["test_record"]
check("the deletion flag is suggested with exactly the deleted records' count", bool(flag) and flag["matches"] == len(truth_deleted), f"suggested {flag and flag['matches']} vs {len(truth_deleted)} tagged deleted")
check("the test-name suggestion catches every test record", bool(names) and names["matches"] >= len(truth_test), f"suggested {names and names['matches']} vs {len(truth_test)} tagged test records; examples {names and names['examples']}")
for s_ in (flag, names):
    if s_:
        call(api, "POST", f"/api/mapping/{mset}/exclusions", json={"column": s_["column"], "operator": s_["operator"], "values": s_["values"], "reason": s_["reason"]})
overview = call(api, "GET", f"/api/mapping/{mset}/exclusions")
union = truth_deleted | truth_test
check("the rules together remove the deleted-or-test records and no others (up to false positives of the name pattern)",
      overview["summary"]["excluded_rows"] >= len(union) * 0.99 and overview["summary"]["excluded_rows"] <= len(union) * 1.02,
      f"{overview['summary']['excluded_rows']} removed vs {len(union)} tagged deleted or test")

t0 = time.time()
run = call(api, "POST", f"/api/pipeline/run/{mset}", json={"wave_name": "G1_REAL_A"})
print(f"  pipeline: {time.time() - t0:.0f}s, gate {run['readiness']['state']}")
rec = call(api, "GET", f"/api/reconciliation/{run['run_id']}")["report"]
c = rec["counts"]
print(f"  extracted {c['extracted']:,} | excluded {c['excluded']:,} | transformed ok {c['transformed_ok']:,} | duplicates {c['duplicate_children']:,} | rejected {c['validation_rejected']:,} | valid {c['valid']:,}")
check("the run kept the whole file as 'extracted' and counted the excluded rows", c["extracted"] == KEY["rows"] and c["excluded"] == overview["summary"]["excluded_rows"], f"{c['extracted']:,} extracted, {c['excluded']:,} excluded")
check("every record is still accounted for (variance 0)", rec["variance"] == 0 and rec["balanced"], f"variance {rec['variance']}")
check("the excluded rows are kept with their reason", any(x["name"] == "rows_left_out_are_kept_with_their_reason" and x["passed"] for x in rec["checks"]))
check("the dirty file is still BLOCKED by the gate for its real defects", run["readiness"]["state"] == "BLOCKED", f"{run['readiness']['open_critical_defects']} critical defect group(s)")
valid_a = run["validation"]["valid_path"]

# ====================================================================== PART B
step("B1  a clean slice delivered into PostgreSQL with the value check")
con = duckdb.connect()
slice_keys = [r[0] for r in con.execute(f"SELECT CAST(_SRC_PK AS VARCHAR) FROM read_parquet('{valid_a}') ORDER BY 1 LIMIT 4000").fetchall()]
src_path = next((ROOT / "workspace" / "tables" / str(tid)).glob("source.*"))
con.execute(f"CREATE TABLE s AS SELECT * FROM read_parquet('{src_path.as_posix()}') WHERE KUNNR IN ({', '.join(repr(k) for k in slice_keys)})")
slice_csv = DATA / "_slice.csv"
con.execute(f"COPY s TO '{slice_csv.as_posix()}' (FORMAT csv, HEADER, DELIMITER ';')")
reg_b = call(api, "POST", "/api/sources/register", data={"system_name": f"G1_CLEAN_{STAMP}", "table_name": "KNA1_CLEAN", "primary_key_columns": "KUNNR"}, files={"file": ("slice.csv", slice_csv.read_bytes())})
tid_b = reg_b["table_id"]
call(api, "POST", f"/api/profile/{tid_b}")
slice_csv.unlink(missing_ok=True)
set_b = call(api, "POST", "/api/mapping-sets", json={"source_table_id": tid_b})["mapping_set_id"]
good = [("BUT000", "BP_EXT", "DIRECT_COPY", "KUNNR", None, None)] + [(tt, tc, rt, col, None, extra.get("lookups")) for col, (tt, tc, rt, extra) in wanted.items()]
good.append(("BUT000", "TYPE", "SQL_EXPRESSION", "KTOKD", "CASE WHEN KTOKD = 'ZPER' THEN '1' ELSE '2' END", None))
for tt, tc, rt, src, logic, lookups in good:
    call(api, "POST", f"/api/mapping/{set_b}/rules", json={"target_table": tt, "target_column": tc, "rule_type": rt, "source_column": src, "transformation_logic": logic, "lookups": lookups})
for rule in call(api, "GET", f"/api/mapping/{set_b}")["rules"]:
    if rule["rule_type"] == "VALUE_MAPPING" and rule["source_column"] == "WAERS":
        call(api, "PUT", f"/api/mapping/rules/{rule['rule_id']}", json={"lookups": lk["currency"], "confirm": True})
if "WAERS" not in {g[3] for g in good}:
    call(api, "POST", f"/api/mapping/{set_b}/rules", json={"target_table": "KNVV", "target_column": "WAERS", "rule_type": "VALUE_MAPPING", "source_column": "WAERS", "lookups": lk["currency"]})
call(api, "POST", f"/api/governance/submit-mapping/{set_b}")
call(steward, "POST", f"/api/governance/approve-mapping/{set_b}")
run_a = call(api, "POST", f"/api/pipeline/run/{set_b}", json={"wave_name": "G1_CLEAN_A"})
check("the clean slice is READY_FOR_LOAD", run_a["readiness"]["state"] == "READY_FOR_LOAD", f"{run_a['validation']['valid_records']:,} valid of {reg_b['rows_landed']:,}; gate {run_a['readiness']['state']} {run_a['readiness'].get('reasons')}")
if run_a["readiness"]["state"] != "READY_FOR_LOAD":
    print("\nCannot continue the delivery checks: the slice is not ready.")
    sys.exit(1)
a_id, valid_n = run_a["run_id"], run_a["validation"]["valid_records"]
call(steward, "POST", f"/api/governance/sign-off-load/{a_id}")
TARGET, TABLE = f"G1_PG_{STAMP}", "MIG_G1_REAL"
admin = psycopg2.connect(host="localhost", port=int(args.pg_port), dbname="legacy_demo", user="migration", password="migration_dev_pw")
admin.autocommit = True
admin.cursor().execute(f"CREATE SCHEMA IF NOT EXISTS staging; DROP TABLE IF EXISTS staging.{TABLE.lower()}")
call(api, "POST", "/api/connections", json={"name": TARGET, "dialect": "postgresql", "host": "localhost", "port": int(args.pg_port), "database": "legacy_demo", "user": "migration",
                                           "password": "migration_dev_pw", "role": "target", "default_schema": "staging"})
t0 = time.time()
delivered = call(steward, "POST", f"/api/pipeline/load-db/{a_id}", json={"connection": TARGET, "table_name": TABLE})
cols = len(call(steward, "GET", f"/api/pipeline/load-db/{a_id}/preview", params={"connection": TARGET, "table_name": TABLE + "_X"})["columns"])
check("the delivery arrived with every value verified before the commit", delivered["values_verified"] and delivered["cells_compared"] == valid_n * cols,
      f"{delivered['cells_compared']:,} cells = {valid_n:,} rows x {cols} columns, fingerprint {delivered['value_digest']}, {time.time() - t0:.0f}s")


def dump():
    cur = admin.cursor()
    cur.execute(f"SELECT * FROM staging.{TABLE.lower()} ORDER BY legacy_key")
    return cur.fetchall()


original = dump()
check("PostgreSQL itself holds exactly the validated records", len(original) == valid_n, f"{len(original):,} rows")
chk = {x["name"]: x for x in call(steward, "GET", f"/api/reconciliation/{a_id}")["report"]["checks"]}
check("the certificate records the count check and the value check", chk[f"db_delivery_postgresql_staging.{TABLE.lower()}"]["passed"] and chk[f"db_values_postgresql_staging.{TABLE.lower()}"]["passed"])

step("B2  tampering with the delivered table is found")
ask = {"connection": TARGET, "table_name": TABLE}
check("an untouched table verifies as identical", call(steward, "POST", f"/api/pipeline/verify-db/{a_id}", json=ask)["verified"])
cur = admin.cursor()
cur.execute(f"SELECT column_name FROM information_schema.columns WHERE table_schema = 'staging' AND table_name = '{TABLE.lower()}' ORDER BY ordinal_position")
names_pg = [r[0] for r in cur.fetchall()]
first, name_idx = original[0][0], names_pg.index("but000_name_org1")
cur.execute(f"UPDATE staging.{TABLE.lower()} SET but000_name_org1 = 'Edited by hand' WHERE legacy_key = %s", (first,))
bad = call(steward, "POST", f"/api/pipeline/verify-db/{a_id}", json=ask)
check("one hand-edited value is found and its column named", bad["verified"] is False and bad["differing_columns"] == ["but000_name_org1"], str(bad["differing_columns"]))
cur.execute(f"UPDATE staging.{TABLE.lower()} SET but000_name_org1 = %s WHERE legacy_key = %s", (original[0][name_idx], first))
check("putting the value back makes the table verify as identical again", call(steward, "POST", f"/api/pipeline/verify-db/{a_id}", json=ask)["verified"] and dump() == original)

step("B3  the source changes: delta against the first run")
edit = duckdb.connect()
landed_b = next((ROOT / "workspace" / "tables" / str(tid_b)).glob("source.*"))
edit.execute(f"CREATE TABLE src AS SELECT * FROM read_parquet('{landed_b.as_posix()}')")
ids = [r[0] for r in edit.execute("SELECT KUNNR FROM src ORDER BY KUNNR").fetchall()]
moved, gone = ids[100:120], ids[200:210]
q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
edit.execute(f"UPDATE src SET ORT01 = 'Changedville' WHERE KUNNR IN ({q(moved)})")
edit.execute(f"DELETE FROM src WHERE KUNNR IN ({q(gone)})")
template = ids[300]
for n in range(15):
    edit.execute(f"INSERT INTO src SELECT * REPLACE ('{'09' + str(900000 + n)}' AS KUNNR, 'Novel Customer {n} Holdings' AS NAME1, 'Newplace {n}' AS ORT01, 'Newroad {n}' AS STRAS, '' AS STCD1, '' AS STCEG, "
                 f"'' AS SMTP_ADDR, '' AS TELF1, '' AS TELF2, '' AS NAME2, 'NOVEL{n}' AS SORTL) FROM src WHERE KUNNR = '{template}'")
edit.execute(f"COPY src TO '{landed_b.as_posix()}' (FORMAT parquet)")
run_b = call(api, "POST", f"/api/pipeline/run/{set_b}", json={"wave_name": "G1_CLEAN_B"})
b_id = run_b["run_id"]
call(steward, "POST", f"/api/governance/sign-off-load/{b_id}")
delta = call(api, "POST", f"/api/runs/{b_id}/delta", json={})
dc = delta["counts"]
print(f"  delta vs run {delta['baseline_run_id']}: new {dc['new']}, changed {dc['changed']}, unchanged {dc['unchanged']}, removed {dc['removed']} ({delta['removed_reasons']})")
check("the delta finds the 15 new, 20 moved and 10 gone customers", (dc["new"], dc["changed"], dc["removed"]) == (15, 20, 10) and delta["baseline_run_id"] == a_id)
check("nothing is double counted", dc["new"] + dc["changed"] + dc["unchanged"] == dc["current_valid"] and dc["removed"] + dc["changed"] + dc["unchanged"] == dc["baseline_valid"])

step("B4  merge only the delta into the delivered table, then roll the merge back")
t0 = time.time()
merged = call(steward, "POST", f"/api/pipeline/load-db/{b_id}", json={**ask, "mode": "merge", "baseline_run": a_id, "delete_removed": True})
print(f"  merge: {merged['inserted_new']} new, {merged['updated']} replaced, {merged['deleted']} deleted; table {merged['rows_before']} -> {merged['rows_in_target']} ({time.time() - t0:.1f}s)")
check("the merge wrote only the delta", (merged["inserted_new"], merged["updated"], merged["deleted"]) == (15, 20, 10) and merged["rows_in_target"] == valid_n + 15 - 10)
check("the merge was value-verified and the certificate checks its arithmetic", merged["values_verified"] and any(
    x["name"].startswith("db_merge_") and x["passed"] for x in call(steward, "GET", f"/api/reconciliation/{b_id}")["report"]["checks"]))
cur.execute(f"SELECT count(*) FROM staging.{TABLE.lower()} WHERE adrc_city1 = 'Changedville'")
check("the changed customers carry their new value in the table", cur.fetchone()[0] == 20)
after_merge = dump()
undone = call(steward, "POST", f"/api/pipeline/rollback-db/{b_id}", json={**ask, "reason": "real test: undo the delta"})
check("rolling the merge back restores the table byte for byte", dump() == original and undone["rows_restored"] == merged["before_rows"], f"removed {undone['rows_removed']}, restored {undone['rows_restored']}")

step("B5  roll back the first delivery (tidy up)")
final = call(steward, "POST", f"/api/pipeline/rollback-db/{a_id}", json={**ask, "reason": "real test finished"})
cur.execute(f"SELECT count(*) FROM pg_tables WHERE schemaname = 'staging' AND tablename = '{TABLE.lower()}'")
check("the emptied table is dropped", final["table_dropped"] and cur.fetchone()[0] == 0, f"{final['rows_removed']:,} rows removed")
api.delete(f"/api/connections/{TARGET}")
admin.close()

# ====================================================================== summary
failed = [r for r in results if not r[1]]
print(f"\n{'=' * 78}\n{len(results) - len(failed)} of {len(results)} checks passed")
for name, _, detail in failed:
    print(f"  FAILED: {name} {detail}")
sys.exit(1 if failed else 0)
