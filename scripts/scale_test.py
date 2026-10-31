"""Scale test: push a large, dirty customer extract through the whole application and check what comes out against what was planted in it.

    python scripts/scale_test.py --base http://127.0.0.1:8768 --csv big.csv --truth big.truth.json --workspace <the app's workspace> [--pg-url ...]

Use a THROWAWAY application (own database and workspace). For every phase it prints the time; it samples the server's memory (and the machine's free memory) all the way through;
and it checks the results against the truth file: every planted duplicate that was found, every look-alike that was wrongly merged, every planted defect that slipped through to the
loadable file, and the counters against each other (nothing may go missing). Exit 1 when a result is wrong; slowness and memory are reported, not judged.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import duckdb
import httpx
import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

PROBLEMS: list[str] = []
TIMINGS: list[tuple[str, float]] = []


def fail(message: str) -> None:
    PROBLEMS.append(message)
    print("  PROBLEM:", message, flush=True)


class Meter:
    """Samples the server process tree's memory and the machine's available memory while a phase runs."""

    def __init__(self, port: int) -> None:
        self.pid = next((c.pid for c in psutil.net_connections("tcp") if c.status == "LISTEN" and c.laddr.port == port), None)
        self.peak_rss = 0
        self.min_available = psutil.virtual_memory().available
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _rss(self) -> int:
        if self.pid is None:
            return 0
        try:
            proc = psutil.Process(self.pid)
            return proc.memory_info().rss + sum(c.memory_info().rss for c in proc.children(recursive=True))
        except psutil.Error:
            return 0

    def _run(self) -> None:
        while not self._stop.wait(1.5):
            self.peak_rss = max(self.peak_rss, self._rss())
            self.min_available = min(self.min_available, psutil.virtual_memory().available)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()

    def report(self) -> str:
        return f"server peak {self.peak_rss / 2**30:.2f} GB, machine low-water mark {self.min_available / 2**30:.1f} GB available"


def phase(name: str):
    class _P:
        def __enter__(self_):
            self_.t = time.time()
            print(f"\n== {name} ==", flush=True)
            return self_

        def __exit__(self_, *exc):
            TIMINGS.append((name, time.time() - self_.t))
            print(f"  [{name}: {time.time() - self_.t:.1f} s]", flush=True)
    return _P()


METER: "Meter | None" = None


def poll(api, path: str, done=("succeeded", "failed", "cancelled"), every: float = 5.0, limit: float = 7200, timeline: bool = False) -> dict:
    t = time.time()
    last = None
    while time.time() - t < limit:
        j = api.get(path).json()
        if timeline and METER is not None:
            msg = (j.get("progress") or {}).get("message", "")
            now = f"{time.time() - t:7.0f} s  server {METER._rss() / 2**30:5.2f} GB  free {psutil.virtual_memory().available / 2**30:4.1f} GB  {msg}"
            if msg != last or int(time.time() - t) % 60 < every:
                print("   ", now, flush=True)
            last = msg
        if j["status"] in done:
            return j
        time.sleep(every)
    raise SystemExit(f"{path} did not finish in {limit} s")


COUNTRY_LK = {"DE": "DE", "GERMANY": "DE", "DEUTSCHLAND": "DE", "FR": "FR", "FRANCE": "FR", "US": "US", "USA": "US", "UNITED STATES": "US", "U.S.A": "US", "IT": "IT", "ITALY": "IT", "ITALIA": "IT",
              "GB": "GB", "UK": "GB", "UNITED KINGDOM": "GB", "ES": "ES", "SPAIN": "ES", "PT": "PT", "PORTUGAL": "PT", "AT": "AT", "AUSTRIA": "AT", "OSTERREICH": "AT", "ESPANA": "ES",
              "FRANKREICH": "FR", "SCHWEIZ": "CH", "BRASIL": "BR", "ENGLAND": "GB", "UNITED STATES OF AMERICA": "US", "CH": "CH", "SWITZERLAND": "CH", "JP": "JP", "JAPAN": "JP", "BR": "BR", "BRAZIL": "BR"}
TERMS_LK = {"NET30": "NT30", "NET60": "NT60", "COD": "CASH", "2/10 NET 30": "NT3D"}
RULES = [
    ("BUT000", "NAME_ORG1", "DIRECT_COPY", "NAME1", None, None), ("BUT000", "NAME_ORG2", "DIRECT_COPY", "NAME2", None, None), ("BUT000", "BP_EXT", "DIRECT_COPY", "CUST_ID", None, None),
    ("BUT000", "TYPE", "STATIC_VALUE", None, "'2'", None), ("BUT000", "BU_GROUP", "STATIC_VALUE", None, "'Z001'", None),
    ("BUT000", "BU_SORT1", "SQL_EXPRESSION", "NAME1", "upper(left(trim(NAME1), 20))", None), ("ADRC", "STREET", "DIRECT_COPY", "STREET", None, None),
    ("ADRC", "HOUSE_NUM1", "DIRECT_COPY", "HOUSE_NO", None, None), ("ADRC", "CITY1", "DIRECT_COPY", "CITY", None, None), ("ADRC", "POST_CODE1", "DIRECT_COPY", "POSTAL", None, None),
    ("ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY", None, COUNTRY_LK), ("ADRC", "LANGU", "DIRECT_COPY", "LANG", None, None), ("ADR6", "SMTP_ADDR", "DIRECT_COPY", "EMAIL", None, None),
    ("ADR2", "TEL_NUMBER", "DIRECT_COPY", "PHONE", None, None), ("KNB1", "ZTERM", "VALUE_MAPPING", "PAY_TERMS", None, TERMS_LK), ("KNVV", "WAERS", "DIRECT_COPY", "CURRENCY", None, None),
]


DEFAULT_PROFILE = {
    "table": "SCALE_CUSTOMERS", "key": "CUST_ID", "tax_col": "TAX_ID", "name_col": "NAME1", "delimiter": None,
    "rules": [list(r) for r in RULES],
    "exclusions": [{"column": "DEL_FLAG", "operator": "equals", "values": ["X"], "reason": "flagged deleted in legacy"},
                   {"column": "NAME1", "operator": "starts_with", "values": ["TEST "], "reason": "test customers"}],
    "defect_exclusions": [["NAME1", "is_blank", [], "no name: cannot be loaded"], ["COUNTRY", "is_blank", [], "no country: cannot be loaded"],
                          ["COUNTRY", "in", ["XX", "??", "Atlantis", "00", "N/A"], "not a country: cannot be loaded"]],
    "must_not_load": {
        "blank name (mandatory)": "trim(coalesce(NAME1, '')) = ''", "name longer than 40 characters": "length(NAME1) > 40", "TEST customer (rows to leave out)": "upper(NAME1) like 'TEST %'",
        "flagged deleted (rows to leave out)": "DEL_FLAG = 'X'", "junk country (no such country)": "upper(trim(COUNTRY)) in ('XX', '??', 'ATLANTIS', '00', 'N/A')",
        "blank country (mandatory)": "trim(coalesce(COUNTRY, '')) = ''"},
    "must_not_merge_kinds": ["diff_legal_entity", "branches_shared_phone"],
    "iso": ["DE", "FR", "US", "IT", "GB", "ES", "PT", "AT", "CH", "JP", "BR"],
    "spelled_sql": "upper(trim(COUNTRY)) in ('GERMANY','FRANCE','FRANKREICH','USA','U.S.A','ITALY','ITALIA','UK','ENGLAND','SPAIN','ESPANA','SCHWEIZ','BRASIL')",
}


def profile_of(truth: dict) -> dict:
    """The dataset's own description (columns, mapping, exclusions, what must never load) when the truth file carries one, else the first dataset's."""
    return {**DEFAULT_PROFILE, **truth.get("profile", {})}


def csv_reader(path: str, prof: dict) -> str:
    delim = f", delim='{prof['delimiter']}'" if prof["delimiter"] else ""
    return f"read_csv_auto('{Path(path).as_posix()}', all_varchar=true, sample_size=-1{delim})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8768")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--workspace", required=True, help="the throwaway application's workspace directory (to read the run's files)")
    ap.add_argument("--pg", default="", help="optional: psycopg2 DSN of the throwaway catalog database, to measure the lineage table")
    ap.add_argument("--skip-ai", action="store_true")
    ap.add_argument("--exclude-defects", action="store_true", help="leave out the rows with a critical defect (blank name, junk or blank country) so the gate opens and the load stages run")
    ap.add_argument("--lineage", default="ALL", choices=["ALL", "EXCEPTIONS", "NONE"])
    args = ap.parse_args()
    port = int(args.base.rsplit(":", 1)[1])
    api = httpx.Client(base_url=args.base + "/api", timeout=3600, headers={"X-User": "alice"})
    steward = httpx.Client(base_url=args.base + "/api", timeout=3600, headers={"X-User": "steward.bob"})
    truth = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    csv_path = Path(args.csv)
    prof = profile_of(truth)
    global METER
    meter = METER = Meter(port)
    print(f"file {csv_path.name}: {csv_path.stat().st_size / 2**20:.0f} MB; server pid {meter.pid}")

    with meter:
        api.post("/admin/seed-sap-catalog").raise_for_status()

        with phase("1. land the file") as _:
            with open(csv_path, "rb") as fh:
                r = api.post("/sources/register", data={"system_name": "SCALE", "system_type": "FLAT_FILE", "table_name": prof["table"], "primary_key_columns": prof["key"]},
                             files={"file": (csv_path.name, fh, "text/csv")})
            r.raise_for_status()
            land = r.json()
            tid = land["table_id"]
            print(f"  rows landed {land['rows_landed']:,}, columns {land['columns_registered']}, stored {land['bytes_stored'] / 2**20:.0f} MB, control {land['control_status']}")
            source_rows = land["rows_landed"]
        print("  memory:", meter.report())

        with phase("2. profile"):
            p = api.post(f"/profile/{tid}").json()
            print(f"  rows {p.get('total_rows'):,}, average health {p.get('average_health_score')}, inferred keys {len(p.get('key_duplicates', []))}")
            if p.get("total_rows") != source_rows:
                fail(f"the profile counts {p.get('total_rows')} rows, the landed file has {source_rows}")

        with phase("3. data quality assessment (179 checks)"):
            q = api.post(f"/quality/assess/{tid}")
            q.raise_for_status()
            qr = q.json()
            print(f"  score {qr['overall_score']}, findings {len(qr['findings'])}, rows {qr['rows']:,}")

        if not args.skip_ai:
            with phase("4. AI mapping (Gemini) on the large table"):
                import evaluate_ai as ev
                detail = ev.generate(api, tid)
                ev.score_mapping("large table", detail, ev.LEGACY_TRUTH)
            for item in ev.RESULTS:
                if not item["ok"]:
                    fail(f"AI mapping: {item['name']}: {item['detail']}")

        with phase("5. build the mapping, rows to leave out, approval"):
            mid = api.post("/mapping-sets", json={"source_table_id": tid}).json()["mapping_set_id"]
            for t, col, rt, src, logic, lk in prof["rules"]:
                api.post(f"/mapping/{mid}/rules", json={"target_table": t, "target_column": col, "rule_type": rt, "source_column": src, "transformation_logic": logic, "lookups": lk}).raise_for_status()
            for ex in prof["exclusions"]:
                e = api.post(f"/mapping/{mid}/exclusions", json=ex).json()
                print(f"  rows to leave out ({ex['reason']}): {e.get('excluded'):,}")
            if args.exclude_defects:
                for col, op, vals, why in prof["defect_exclusions"]:
                    r = api.post(f"/mapping/{mid}/exclusions", json={"column": col, "operator": op, "values": vals, "reason": why})
                    print(f"  left out ({why}): {r.json().get('excluded', r.text[:80]):,}" if r.status_code == 201 else f"  exclusion failed: {r.text[:160]}")
            pf = api.post(f"/mapping/{mid}/preflight").json()
            print(f"  pre-submission check: can submit {pf['can_submit']}, sample {pf['sample_rows']:,} of {pf['source_rows']:,}, valid {pf['valid_rate']:.1%}")
            api.post(f"/governance/submit-mapping/{mid}").raise_for_status()
            steward.post(f"/governance/approve-mapping/{mid}").raise_for_status()

        with phase(f"6. the run: transform, deduplicate, validate (lineage {args.lineage})"):
            job = api.post(f"/pipeline/run/{mid}/background", json={"wave_name": "SCALE", "lineage": args.lineage}).json()
            done = poll(api, f"/pipeline/jobs/{job['job_id']}", every=10, timeline=True)
            if done["status"] != "succeeded":
                fail(f"the run {done['status']}: {done.get('error') or done['progress'].get('message')}")
                print(json.dumps(done)[:600])
                return report(meter)
            run = done["result"]
            rid = run["run_id"]
            v = run["validation"]
            print(f"  run {rid}: input {run['run']['source_records']:,}, excluded {run['run']['excluded_records']:,}, duplicate children {v['duplicate_children_excluded']:,}, "
                  f"evaluated {v['evaluated_records']:,}, valid {v['valid_records']:,}, defective {v['defective_records']:,}, quality {v['quality_score']}")
            print("  dedup:", {k: run["dedup"][k] for k in ("duplicate_clusters", "duplicate_children", "exact_match_clusters", "fuzzy_match_clusters", "possible_duplicate_pairs", "fuzzy_pairs_evaluated", "fuzzy_blocks_limited")})
        print("  memory:", meter.report())

        check_counters(run, source_rows)
        check_against_truth(args, truth, rid, run, prof)

        if args.pg:
            import psycopg2
            conn = psycopg2.connect(args.pg)
            cur = conn.cursor()
            cur.execute("select count(*), pg_size_pretty(pg_total_relation_size('mig_cell_lineage')) from mig_cell_lineage where run_id = %s", (rid,))
            n, size = cur.fetchone()
            print(f"  lineage rows in the catalog for this run: {n:,} ({size} for the table)")
            conn.close()

        with phase("7. questions to the assistant about the large run"):
            ask_checks(api, rid, mid, run, truth, args)

        with phase("8. sign-off, Cockpit workbook, sandbox SAP load"):
            so = steward.post(f"/governance/sign-off-load/{rid}")
            if so.status_code != 200:
                gate = so.json().get("detail", {})
                print("  the gate is closed, as it should be:", gate.get("message", so.text)[:300])
                print("  (rerun with --exclude-defects to open it and test the load stages)")
                continue_load = False
            else:
                continue_load = True
            if not continue_load:
                return report(meter)
            t = time.time()
            ck = api.post(f"/pipeline/load/{rid}", json={"mode": "cockpit"})
            ck.raise_for_status()
            c = ck.json()["cockpit"]
            print(f"  workbook: {c['business_partners']:,} partners, {sum(c['sheet_rows'].values()):,} rows in {time.time() - t:.0f} s")
            if c["business_partners"] != v["valid_records"]:
                fail(f"the workbook holds {c['business_partners']:,} partners, the run has {v['valid_records']:,} valid records")
            t = time.time()
            ld = api.post(f"/pipeline/load/{rid}", json={"mode": "sap", "target": "sandbox", "batch_size": 1000})
            ld.raise_for_status()
            load = ld.json()["load"]
            print(f"  sandbox load: attempted {load['attempted']:,}, loaded {load['loaded']:,}, failed {load['failed']:,} in {time.time() - t:.0f} s")
            if load["loaded"] + load["failed"] != load["attempted"] or load["attempted"] != v["valid_records"]:
                fail("the load counters do not add up to the valid records")

        with phase("9. reconciliation certificate"):
            cert = api.get(f"/reconciliation/{rid}")
            if cert.status_code == 200:
                body = cert.json()
                print(f"  verdict: {body.get('verdict') or body.get('passed')}, accounted rows {body.get('accounted_rows', 'n/a')}")
            else:
                print("  certificate status", cert.status_code)

    return report(meter)


def check_counters(run: dict, source_rows: int) -> None:
    r, v = run["run"], run["validation"]
    print("\n== counters must add up ==")
    accounted = r["excluded_records"] + v["duplicate_children_excluded"] + v["defective_records"] + v["valid_records"]
    print(f"  excluded {r['excluded_records']:,} + duplicate children {v['duplicate_children_excluded']:,} + rejected {v['defective_records']:,} + valid {v['valid_records']:,} = {accounted:,} (source {source_rows:,})")
    if accounted != source_rows:
        fail(f"rows went missing or were counted twice: {accounted:,} accounted for, {source_rows:,} in the source")


def check_against_truth(args, truth: dict, rid: int, run: dict, prof: dict) -> None:
    print("\n== against what was planted ==")
    base = Path(args.workspace) / "runs" / f"run_{rid}"
    con = duckdb.connect()
    dedup = base / "deduplicated.parquet"
    valid = Path(run["validation"]["valid_path"])
    rows = con.execute(f"select _SRC_PK, dedup_cluster_id, is_duplicate_child from read_parquet('{dedup.as_posix()}')").fetchall()
    by_pk = {r[0]: r for r in rows}
    same = lambda a, b: (None if a not in by_pk or b not in by_pk else (by_pk[a][1] is not None and by_pk[a][1] == by_pk[b][1]))  # noqa: E731
    per: dict[str, list[int]] = {}
    for a, b, kind in truth["dup_pairs"]:
        s = same(a, b)
        c = per.setdefault(kind, [0, 0, 0])
        c[2 if s is None else (0 if s else 1)] += 1
    missed_total = 0
    for kind, (caught, missed, excl) in per.items():
        print(f"  planted duplicates {kind:22s} caught {caught:4d} missed {missed:3d} left out of the run {excl:3d}")
        missed_total += missed
    if missed_total:
        fail(f"{missed_total} planted duplicate pair(s) were not merged")
    wrong = {}
    for a, b, kind in truth["traps"]:
        s = same(a, b)
        if s:
            wrong[kind] = wrong.get(kind, 0) + 1
    print("  look-alikes wrongly merged:", wrong or "none")
    for kind in prof["must_not_merge_kinds"]:
        if wrong.get(kind):
            fail(f"{wrong[kind]} look-alike pair(s) of kind {kind} were merged: two legal entities lost")
    planted_children = {b for a, b, _ in truth["dup_pairs"]}
    others = [pk for pk, r in by_pk.items() if r[2] and pk not in planted_children]
    print(f"  records merged away that were not planted duplicates: {len(others):,} of {len(by_pk):,} (random look-alikes: judge by the tax number rule below)")
    kinds = con.execute(f"select dedup_match_type, dedup_match_strength, count(*) from read_parquet('{dedup.as_posix()}') where is_duplicate_child group by 1, 2 order by 3 desc").fetchall()
    print("  merged-away records by evidence:", [(k[0][:40] if k[0] else None, k[1], k[2]) for k in kinds[:8]])
    # a merge of two records that both carry a different tax number is two legal entities: judged on the CSV's own tax column, whether or not the application recognised it
    con.execute(f"""create view csvtax as select "{prof['key']}" as pk, regexp_replace(upper(trim("{prof['tax_col']}")), '[^A-Z0-9]', '', 'g') as t from {csv_reader(args.csv, prof)}""")
    clash = con.execute(f"""select count(*) from (select d.dedup_cluster_id from read_parquet('{dedup.as_posix()}') d join csvtax c on c.pk = d._SRC_PK
                             where d.dedup_cluster_id is not null and c.t <> '' group by 1 having count(distinct c.t) > 1)""").fetchone()[0]
    print(f"  merged groups that hold two different tax numbers (compared without spaces and dashes): {clash:,}")
    if clash:
        fail(f"{clash:,} merged group(s) contain records with different tax numbers (the veto failed)")

    # what must never reach the loadable file: worked out from the CSV itself (the generator's own list can be overwritten by a later step)
    vp = valid.as_posix()
    keys = {r[0] for r in con.execute(f"select _SRC_PK from read_parquet('{vp}')").fetchall()}
    con.execute(f"create view s as select * from {csv_reader(args.csv, prof)}")
    must_not_load = prof["must_not_load"]
    for label, cond in must_not_load.items():
        planted = {r[0] for r in con.execute(f'select "{prof["key"]}" from s where {cond}').fetchall()}
        leaked = planted & keys
        print(f"  {label}: {len(planted):,} in the file, reached the loadable file: {len(leaked):,}")
        if leaked:
            fail(f"{len(leaked)} record(s) with a {label} reached the loadable file, e.g. {sorted(leaked)[:3]}")
    dup_children_in_valid = {pk for pk, r in by_pk.items() if r[2]} & keys
    if dup_children_in_valid:
        fail(f"{len(dup_children_in_valid)} duplicate children reached the loadable file")
    spelled = con.execute(f"select count(*) from s where {prof['spelled_sql']}").fetchone()[0]
    iso = ", ".join(f"'{c}'" for c in prof["iso"])
    ok_countries = con.execute(f"select count(*) from read_parquet('{vp}') where ADRC_COUNTRY in ({iso})").fetchone()[0]
    print(f"  country spellings translated: {spelled:,} spelled-out names in the file; every loadable record has an ISO country: {ok_countries == len(keys)}")
    if ok_countries != len(keys):
        fail(f"{len(keys) - ok_countries:,} loadable record(s) do not have an ISO country code")


def ask_checks(api, rid: int, mid: int, run: dict, truth: dict, args) -> None:
    v = run["validation"]
    for question, need in (("how many records are valid and how many were rejected?", [v["valid_records"], v["defective_records"]]), ("why were records rejected?", [])):
        t = time.time()
        a = api.post("/assistant/ask", json={"question": question, "run_id": rid, "mapping_set_id": mid}).json()
        print(f"  '{question}' -> {time.time() - t:.1f} s, verified={a['verified']}, source={a['source']}")
        print("     ", a["answer"][:260].replace("\n", " "))
        for n in need:
            if f"{n:,}" not in a["answer"] and str(n) not in a["answer"]:
                fail(f"the answer to '{question}' does not contain {n:,}")
        if not a["verified"]:
            fail(f"the answer to '{question}' was not verified against the data")
    t = time.time()
    a = api.post("/assistant/ask", json={"question": "how to fix the errors", "run_id": rid, "mapping_set_id": mid}).json()
    print(f"  'how to fix the errors' -> {time.time() - t:.1f} s, {len(a['fixes'])} proposal(s): {[f['title'] for f in a['fixes']][:4]}")
    t = time.time()
    a = api.post("/assistant/ask", json={"question": "which rejected records have no country?", "run_id": rid}).json()
    shown = sum(len(tb["rows"]) for tb in a["tables"])
    print(f"  'which rejected records have no country?' -> {time.time() - t:.1f} s, matching {[tb['total'] for tb in a['tables']]}, showing {shown}")


def report(meter: Meter) -> int:
    print("\n== timings ==")
    for name, seconds in TIMINGS:
        print(f"  {seconds:8.1f} s  {name}")
    print("\n" + meter.report())
    print(f"\n== {len(PROBLEMS)} problem(s) ==")
    for p in PROBLEMS:
        print("  -", p)
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    sys.exit(main())
