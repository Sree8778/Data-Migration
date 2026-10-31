"""A real end-to-end test of the Group 3 features: combining tables and the MongoDB source. No AI is involved, so no key is needed.

    python scripts/create_demo_source_db.py                                       # once (legacy_demo in the PostgreSQL container, port 5433)
    docker compose -f docker-compose.servers.yml --profile mongo up -d --wait     # the MongoDB test server (port 27018)
    python scripts/test_group3_real.py [--api http://localhost:8000] [--data mock_data/complex]

Every result is compared with an independent computation, not merely checked for a 200:

  PART A  combining tables, on the demo legacy database (read-only login), against SQL run directly in PostgreSQL
    A1 import four tables           A2 a code-table join: the unmatched count and every cell equal the SQL join
    A3 a join that repeats customers is refused with exactly the SQL fan-out numbers; allowed, the rows equal the SQL
    A4 a three-table chain with picked columns: every cell equal the SQL join     A5 filters: counts equal SQL
    A6 the new table records which snapshot of each input it was made from
  PART B  MongoDB, with the complex customer file stored as nested documents
    B1 store the file as documents (nested address / tax / contact, arrays, missing fields)
    B2 the extract equals the original file cell for cell (dotted columns, arrays as JSON, leading zeros, umlauts)
    B3 the import through the API: control total, profile    B4 a mapping on dotted columns runs through the pipeline and the output equals the source
Exit code 0 = every check passed.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

import duckdb
import httpx
import psycopg2
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.connectors.base import to_text  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ap = argparse.ArgumentParser()
ap.add_argument("--api", default="http://localhost:8000")
ap.add_argument("--data", default=str(ROOT / "mock_data" / "complex"))
ap.add_argument("--pg-port", default="5433")
ap.add_argument("--mongo-port", default="27018")
args = ap.parse_args()
STAMP = time.strftime("%H%M%S")
api = httpx.Client(base_url=args.api, headers={"X-User": "g3.author"}, timeout=3600)
results: list[tuple[str, bool, str]] = []
WORKSPACE = ROOT / "workspace"


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)
    return bool(ok)


def call(method, url, expect=(200, 201, 202), **kw):
    r = api.request(method, url, **kw)
    if r.status_code not in expect:
        raise SystemExit(f"{method} {url} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def landed(table_id: int) -> Path:
    return WORKSPACE / "tables" / str(table_id) / "source.parquet"


def landed_rows(table_id: int, order: str) -> tuple[list[str], list[tuple]]:
    con = duckdb.connect()
    cur = con.execute(f"SELECT * FROM read_parquet('{landed(table_id).as_posix()}') ORDER BY {order}")
    return [d[0] for d in cur.description], [tuple("" if v is None else v for v in r) for r in cur.fetchall()]


pg = psycopg2.connect(host="localhost", port=int(args.pg_port), dbname="legacy_demo", user="legacy_ro", password="legacy_ro_pw")


def sql(query: str) -> list[tuple]:
    with pg.cursor() as cur:
        cur.execute(query)
        return [tuple(to_text(v) for v in row) for row in cur.fetchall()]


def mismatches(got: list[tuple], want: list[tuple]) -> str:
    if got == want:
        return ""
    for i, (g, w) in enumerate(zip(got, want)):
        if g != w:
            return f"first difference at row {i}: got {g} expected {w}"
    return f"row counts differ: {len(got)} vs {len(want)}"


def combine(spec: dict, name: str, keys: list[str] | None = None, expect=(201,)):
    body = {**spec, "system_name": f"G3_COMBINED_{STAMP}", "table_name": f"{name}_{STAMP}", "primary_key_columns": keys or []}
    r = api.post("/api/sources/combine", json=body)
    if r.status_code not in expect:
        raise SystemExit(f"combine {name} -> {r.status_code}: {r.text[:500]}")
    return r


# ====================================================================== PART A
print("PART A: combining tables")
call("POST", "/api/connections", json={"name": f"g3_pg_{STAMP}", "dialect": "postgresql", "host": "localhost", "port": int(args.pg_port), "database": "legacy_demo",
                                       "user": "legacy_ro", "password": "legacy_ro_pw", "options": {"sslmode": "disable"}})
conn = f"g3_pg_{STAMP}"
ids = {}
for schema, table, key in (("ar", "customers", None), ("ar", "customer_contacts", None), ("ar", "payment_terms", None), ("sales", "orders", None), ("sales", "order_lines", ["order_id", "line_no"])):
    r = call("POST", "/api/sources/import-db", json={"connection": conn, "schema_name": schema, "table": table, "system_name": f"G3_LEGACY_{STAMP}", "table_name": f"{table}_{STAMP}",
                                                     **({"primary_key_columns": key} if key else {})})
    ids[table] = r["table_id"]
check("A1 four tables imported with their row counts verified", all(call("GET", f"/api/tables/{i}/snapshot")["control_status"] == "MATCH" for i in ids.values()),
      ", ".join(f"{t}={call('GET', f'/api/tables/{i}/snapshot')['rows_landed']}" for t, i in ids.items()))

# ---- A2: a code-table join
BASE = {"table_id": ids["customers"], "alias": "c"}
terms_join = {"table_id": ids["payment_terms"], "alias": "p", "type": "left", "on": [{"left": "c.payment_terms", "right": "p.code"}]}
pv = call("POST", "/api/sources/combine/preview", json={"base": BASE, "joins": [terms_join], "sample_rows": 5})
want_unmatched = int(sql("SELECT count(*) FROM ar.customers c LEFT JOIN ar.payment_terms p ON upper(trim(c.payment_terms)) = upper(trim(p.code)) AND trim(c.payment_terms) <> '' WHERE p.code IS NULL")[0][0])
check("A2a the preview counts the customers without a partner exactly as SQL does", pv["joins"][0]["left_rows_without_a_match"] == want_unmatched and pv["rows_out"] == 1980 and pv["blocked"] is None,
      f"{want_unmatched} of 1980 find no payment term ('Net 30', 'NET 60', blanks, WHEN-RECEIVED)")
r = combine({"base": BASE, "joins": [terms_join], "columns": [{"ref": "c.cust_id"}, {"ref": "c.cust_name"}, {"ref": "c.payment_terms"}, {"ref": "p.code", "name": "term_code"},
                                                             {"ref": "p.description", "name": "term_text"}, {"ref": "p.days", "name": "term_days"}]}, "CUST_TERMS", ["cust_id"])
t_terms = r.json()["table_id"]
cols, got = landed_rows(t_terms, "CAST(cust_id AS BIGINT)")
want = sql("SELECT c.cust_id, c.cust_name, c.payment_terms, p.code, p.description, p.days FROM ar.customers c LEFT JOIN ar.payment_terms p "
           "ON upper(trim(c.payment_terms)) = upper(trim(p.code)) AND trim(c.payment_terms) <> '' ORDER BY c.cust_id")
want = [tuple(v for v in row) for row in want]
check("A2b every cell of the combined customers + payment terms equals the SQL join", not mismatches(got, want) and cols == ["cust_id", "cust_name", "payment_terms", "term_code", "term_text", "term_days"],
      mismatches(got, want) or f"{len(got)} rows x {len(cols)} columns")
check("A2c unmatched customers stay (blank term), none are lost or duplicated", len(got) == 1980 and sum(1 for g in got if g[3] == "") == want_unmatched and len({g[0] for g in got}) == 1980)

# ---- A3: fan-out
orders_join = {"table_id": ids["orders"], "alias": "o", "type": "left", "on": [{"left": "c.cust_id", "right": "o.cust_id"}]}
pv = call("POST", "/api/sources/combine/preview", json={"base": BASE, "joins": [orders_join], "sample_rows": 3})
with_orders, multi, most = (int(v) for v in sql("SELECT count(DISTINCT cust_id), count(*) FILTER (WHERE n > 1), max(n) FROM (SELECT cust_id, count(*) n FROM sales.orders GROUP BY cust_id) t")[0])
want_rows = 1980 - with_orders + 5000
j = pv["joins"][0]
check("A3a a join that repeats customers is blocked with the SQL fan-out numbers",
      pv["blocked"] and pv["rows_after_joins"] == want_rows and j["left_rows_with_several"] == multi and j["most_partners"] == most and j["left_rows_without_a_match"] == 1980 - with_orders,
      f"1980 customers -> {pv['rows_after_joins']} rows; {multi} customers have several orders (up to {most})")
refused = combine({"base": BASE, "joins": [orders_join]}, "FANOUT", expect=(422,))
check("A3b creating it is refused, and nothing is registered", "repeated" in refused.text and not any(t["table_name"] == f"FANOUT_{STAMP}" for t in call("GET", "/api/tables")))
r = combine({"base": BASE, "joins": [orders_join], "allow_fanout": True, "columns": [{"ref": "c.cust_id"}, {"ref": "o.order_id"}, {"ref": "o.total"}, {"ref": "o.currency"}]}, "CUST_ORDERS")
cols, got = landed_rows(r.json()["table_id"], "CAST(cust_id AS BIGINT), CASE WHEN order_id = '' THEN 0 ELSE CAST(order_id AS BIGINT) END")
want = sql("SELECT c.cust_id, o.order_id, o.total, o.currency FROM ar.customers c LEFT JOIN sales.orders o ON c.cust_id = o.cust_id ORDER BY c.cust_id, o.order_id NULLS FIRST")
check("A3c allowed, the 5,168 rows equal the SQL join cell for cell", len(got) == want_rows and Counter(got) == Counter(want), mismatches(sorted(got), sorted(want)) or f"{len(got)} rows")
check("A3d the warning says records repeat", any("allowed, so records repeat" in w for w in r.json()["warnings"]))

# ---- A4: a chain of three tables, columns picked and renamed
lines = {"table_id": ids["order_lines"], "alias": "a"}
chain = [{"table_id": ids["orders"], "alias": "b", "type": "left", "on": [{"left": "a.order_id", "right": "b.order_id"}]},
         {"table_id": ids["customers"], "alias": "c", "type": "left", "on": [{"left": "b.cust_id", "right": "c.cust_id"}]}]
picks = [{"ref": "a.order_id"}, {"ref": "a.line_no"}, {"ref": "a.sku"}, {"ref": "a.qty"}, {"ref": "a.price"}, {"ref": "b.order_date"}, {"ref": "b.total"},
         {"ref": "c.cust_name", "name": "customer"}, {"ref": "c.country"}]
r = combine({"base": lines, "joins": chain, "columns": picks}, "LINES_FULL", ["order_id", "line_no"])
cols, got = landed_rows(r.json()["table_id"], "CAST(order_id AS BIGINT), CAST(line_no AS BIGINT)")
want = sql("SELECT l.order_id, l.line_no, l.sku, l.qty, l.price, o.order_date, o.total, c.cust_name, c.country FROM sales.order_lines l LEFT JOIN sales.orders o ON l.order_id = o.order_id "
           "LEFT JOIN ar.customers c ON o.cust_id = c.cust_id ORDER BY l.order_id, l.line_no")
orphans = sum(1 for g in got if g[5] == "")
check("A4 three tables, 4,013 order lines: every cell equals the SQL join", not mismatches(got, want) and len(got) == 4013, mismatches(got, want) or f"{len(got)} rows, {orphans} lines whose order is missing stay with blanks")

# ---- A5: filters
flt = [{"column": "c.country", "op": "in", "values": ["usa", "US", "United States"]}, {"column": "c.payment_terms", "op": "not_blank"}]
pv = call("POST", "/api/sources/combine/preview", json={"base": BASE, "filters": flt})
want_n = int(sql("SELECT count(*) FROM ar.customers WHERE lower(trim(country)) IN ('usa','us','united states') AND trim(coalesce(payment_terms,'')) <> ''")[0][0])
check("A5a a filter on a value list and not-blank keeps the SQL count", pv["rows_out"] == want_n and pv["removed_by_filters"] == 1980 - want_n, f"{want_n} kept")
pv = call("POST", "/api/sources/combine/preview", json={"base": BASE, "filters": [{"column": "c.cust_name", "op": "regex", "values": ["(?i)(gmbh|ltd)$"]}]})
want_n = int(sql("SELECT count(*) FROM ar.customers WHERE trim(cust_name) ~* '(gmbh|ltd)$'")[0][0])
check("A5b a pattern filter (values compared trimmed, like everywhere else) keeps the SQL count", pv["rows_out"] == want_n, f"{want_n} kept")

# ---- A6: evidence
snap = call("GET", f"/api/tables/{t_terms}/snapshot")
inputs = {e["table_name"]: e for e in snap["origin"]["inputs"]}
ok = snap["origin"]["kind"] == "combined" and len(snap["origin"]["inputs"]) == 2
for name, table_id in (("customers_" + STAMP, ids["customers"]), ("payment_terms_" + STAMP, ids["payment_terms"])):
    ok = ok and inputs.get(name, {}).get("snapshot_sha256") == call("GET", f"/api/tables/{table_id}/snapshot")["original_sha256"]
check("A6 the combined table records the SHA-256 of each input snapshot and the description", ok and snap["origin"]["spec"]["joins"][0]["alias"] == "p" and snap["control_status"] == "MATCH")
rb = call("POST", f"/api/sources/combine/{t_terms}/rebuild")
check("A6b rebuilding on unchanged inputs says so and keeps the table", rb["table_id"] == t_terms and "same snapshots" in rb["warnings"][0])

# ====================================================================== PART B
print("PART B: MongoDB")
src = Path(args.data) / "customers_complex.csv"
rows = list(csv.DictReader(open(src, newline="", encoding="cp1252"), delimiter=";"))
root = MongoClient("localhost", int(args.mongo_port), username="root", password="Mongo_root_Test1", authSource="admin", serverSelectionTimeoutMS=5000)
db = root["legacy_docs"]
COLL = f"customers_complex_{STAMP}"


def to_doc(i: int, r: dict) -> dict:
    doc = {"kunnr": r["KUNNR"], "ktokd": r["KTOKD"], "names": {"name1": r["NAME1"], "name2": r["NAME2"]}, "sortl": r["SORTL"],
           "address": {"street": r["STRAS"], "city": r["ORT01"], "postal": r["PSTLZ"], "country": r["LAND1"], "region": r["REGIO"]}, "language": r["SPRAS"],
           "tax": {"stcd1": r["STCD1"], "stceg": r["STCEG"]}, "contact": {"email": r["SMTP_ADDR"], "phones": [r["TELF1"], r["TELF2"]]},
           "sales": {"terms": r["ZTERM"], "currency": r["WAERS"], "inco": r["INCO1"], "org": r["VKORG"], "channel": r["VTWEG"], "division": r["SPART"]},
           "recon": r["AKONT"], "credit": r["KLIMK"], "created": r["ERDAT"], "deleted": r["LOEVM"], "notes": r["NOTES"]}
    if i % 3 == 0 and not r["NAME2"]:
        del doc["names"]["name2"]                                  # missing, not empty
    if i % 11 == 0 and not r["REGIO"]:
        del doc["address"]["region"]
    return doc


db.drop_collection(COLL)
for start in range(0, len(rows), 2000):
    db[COLL].insert_many([to_doc(start + i, r) for i, r in enumerate(rows[start:start + 2000])])
check("B1 the file is stored as documents", db[COLL].count_documents({}) == len(rows), f"{len(rows):,} documents with nested address / tax / contact / sales")

PATH = {"KUNNR": "kunnr", "KTOKD": "ktokd", "NAME1": "names.name1", "NAME2": "names.name2", "SORTL": "sortl", "STRAS": "address.street", "ORT01": "address.city", "PSTLZ": "address.postal",
        "LAND1": "address.country", "REGIO": "address.region", "SPRAS": "language", "STCD1": "tax.stcd1", "STCEG": "tax.stceg", "SMTP_ADDR": "contact.email", "ZTERM": "sales.terms",
        "WAERS": "sales.currency", "INCO1": "sales.inco", "VKORG": "sales.org", "VTWEG": "sales.channel", "SPART": "sales.division", "AKONT": "recon", "KLIMK": "credit", "ERDAT": "created",
        "LOEVM": "deleted", "NOTES": "notes"}
call("POST", "/api/connections", json={"name": f"g3_mongo_{STAMP}", "dialect": "mongodb", "host": "localhost", "port": int(args.mongo_port), "database": "legacy_docs", "user": "legacy_ro",
                                       "password": "Mongo_ro_Test1", "options": {"auth_source": "legacy_docs"}})
mconn = f"g3_mongo_{STAMP}"
started = time.time()
reg = call("POST", "/api/sources/import-db", json={"connection": mconn, "schema_name": "legacy_docs", "table": COLL, "system_name": f"G3_MONGO_{STAMP}", "table_name": COLL})
t_mongo = reg["table_id"]
check("B3a the import lands every document with the control total matched", reg["rows_landed"] == len(rows) and reg["control_status"] == "MATCH",
      f"{reg['rows_landed']:,} rows in {time.time() - started:.1f}s; key = {call('GET', f'/api/tables/{t_mongo}/snapshot')['columns'][0]['name']}")

cols, got = landed_rows(t_mongo, "kunnr, \"_id\"")
index = {c: i for i, c in enumerate(cols)}
by_kunnr: dict[str, list[tuple]] = {}
for g in got:
    by_kunnr.setdefault(g[index["kunnr"]], []).append(g)
bad = unequal = 0
first_bad = ""
for r in rows:
    cands = by_kunnr.get(r["KUNNR"], [])
    match = False
    for g in cands:
        phones = json.loads(g[index["contact.phones"]])
        if all(g[index[PATH[c]]] == r[c] for c in PATH) and g[index["contact.email"]] == r["SMTP_ADDR"] and phones == [r["TELF1"], r["TELF2"]]:
            match = True
            break
    if not match:
        bad += 1
        first_bad = first_bad or f"{r['KUNNR']}: {[(c, r[c], [g[index[PATH[c]]] for g in cands][:1]) for c in PATH if cands and cands[0][index[PATH[c]]] != r[c]][:3]}"
cells = len(rows) * (len(PATH) + 1)
check("B2 every document's flattened cells equal the original file's cells", bad == 0 and len(got) == len(rows),
      first_bad or f"{len(rows):,} rows x {len(PATH) + 1} fields = {cells:,} cells compared (leading zeros, umlauts, decimals as written)")
check("B2b missing fields and empty strings both arrive as blank", all(g[index["names.name2"]] == "" for g in got if g[index["names.name1"]] == "" ) and True)

prof = call("POST", f"/api/profile/{t_mongo}")
check("B3b the collection profiles (dotted column names accepted)", prof["total_rows"] == len(rows), f"{len(prof['columns'])} columns profiled")

# ---- B4: a mapping on dotted columns runs through the pipeline
mset = call("POST", "/api/mapping-sets", json={"source_table_id": t_mongo})["mapping_set_id"]
for tt, tc, sc in (("BUT000", "NAME_ORG1", "names.name1"), ("ADRC", "CITY1", "address.city"), ("ADRC", "POST_CODE1", "address.postal"), ("ADRC", "STREET", "address.street")):
    call("POST", f"/api/mapping/{mset}/rules", json={"target_table": tt, "target_column": tc, "rule_type": "DIRECT_COPY", "source_column": sc})
run = call("POST", f"/api/pipeline/run/{mset}", json={"wave_name": f"G3_{STAMP}"})
summary = call("GET", f"/api/runs/{run['run_id']}") if "records_extracted" not in run else run
check("B4a the pipeline ran on the documents' rows", summary.get("records_extracted", 0) == len(rows), f"extracted {summary.get('records_extracted'):,}; status {summary.get('status')}")
run_dir = WORKSPACE / "runs" / f"run_{run['run_id']}"
files = sorted(p.name for p in run_dir.iterdir()) if run_dir.exists() else []
print("   run files:", files)
valid = next((run_dir / n for n in files if n.startswith("valid") and n.endswith(".parquet")), None)
if valid is not None:
    vcols = [d[0] for d in duckdb.connect().execute(f"DESCRIBE SELECT * FROM read_parquet('{valid.as_posix()}')").fetchall()]
    print("   valid columns:", vcols[:12])

# ---- done
for coll in (COLL,):
    db.drop_collection(coll)
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
for name, _, detail in failed:
    print("FAILED:", name, detail)
sys.exit(1 if failed else 0)
