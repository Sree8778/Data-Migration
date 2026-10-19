"""Run the FULL pipeline (AI mapping -> transform -> dedupe -> validate -> reconcile) on every source format and compare.

    python scripts/compare_pipeline_formats.py [--api http://localhost:8000] [--ai-formats excel,fixed-width,json-array]

Prerequisites: scripts/compare_formats.py has been run (it lands the FMT_* tables) and the server has a working
Gemini key. Part A: Gemini maps the reference table (CSV). Part B: that mapping is replicated onto every other
format's table and the pipeline runs, so any difference is caused by the format and nothing else. Part C: Gemini
maps a few other formats independently, to measure how repeatable the AI mapping is. Report:
mock_data/pipeline_comparison.md. Exit code 1 if any format's pipeline result differs from the reference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import duckdb
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.services.lookup_service import COUNTRY_SEED, PAYMENT_TERMS_SEED  # noqa: E402

LABELS = ["csv-utf8", "csv-lf-endings", "csv-utf8-bom", "semicolon-cp1252", "pipe-gzip", "tsv-utf16", "csv-in-zip", "excel",
          "excel-banner-rows", "csv-banner-rows", "json-array", "ndjson", "fixed-width", "parquet"]
STATIC = (("BUT000", "BU_GROUP", "'BP01'"), ("BUT000", "TYPE", "'2'"))
# the application's own standard lookups (what a steward gets from "load standard lookups"), so results mean something
COUNTRY_LOOKUP = dict(COUNTRY_SEED)
TERMS_LOOKUP = dict(PAYMENT_TERMS_SEED)


def table_name(label: str) -> str:
    return "FMT_" + label.replace("-", "_").upper()


class Api:
    def __init__(self, base: str):
        self.c = httpx.Client(base_url=base, headers={"X-User": "format.tester"}, timeout=600)

    def call(self, method, url, expect=(200, 201), **kw):
        r = self.c.request(method, url, **kw)
        if r.status_code not in expect:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def ai_mapping(self, table_id: int) -> dict:
        job = self.call("POST", f"/api/mapping/generate/{table_id}?background=true", expect=(200, 202))
        while True:
            s = self.call("GET", f"/api/mapping/jobs/{job['job_id']}")
            if s["status"] not in ("queued", "running"):
                break
            time.sleep(3)
        if s["status"] != "succeeded":
            raise RuntimeError(f"AI mapping job {s['status']}: {s.get('error')}")
        return s["result"]


def mapping_signature(detail: dict) -> list[tuple]:
    return sorted((r["source_column"] or "", r["target_table"], r["target_column"], r["rule_type"], r["transformation_logic"] or "")
                  for r in detail["rules"])


def fingerprint(path: str) -> str:
    con = duckdb.connect()
    cols = [r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path]).fetchall()]
    order = ", ".join(f'"{c}"' for c in cols)
    h = hashlib.sha256()
    for row in con.execute(f"SELECT * FROM read_parquet(?) ORDER BY {order}", [path]).fetchall():
        h.update(repr(row).encode())
    return h.hexdigest()


def resolve(path: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else ROOT / p)


def collect(api: Api, run_id: int) -> dict:
    run = api.call("GET", f"/api/runs/{run_id}")
    recon = api.call("GET", f"/api/reconciliation/{run_id}")
    rep = recon["report"]
    defects = api.call("GET", f"/api/defects/{run_id}")
    run_dir = ROOT / "workspace" / "runs" / f"run_{run_id}"
    validation = json.loads((run_dir / "validation.json").read_text(encoding="utf-8"))
    final = validation.get("valid_path") or validation["staging_path"]
    return {
        "run_id": run_id, "status": run["status"], "extracted": run["records_extracted"], "transformed": run["records_transformed"],
        "failed": run["records_failed"], "quality": run["quality_score"],
        "valid": validation["valid_records"], "defective": validation["defective_records"], "dup_children": validation["duplicate_children_excluded"],
        "failures": {f"{f['target']}|{f['kind']}|{f['severity']}": f["violation_count"] for f in validation["failures"]},
        "defect_count": len(defects.get("defects", defects if isinstance(defects, list) else [])),
        "recon_balanced": rep["balanced"], "recon_passed": recon["passed"], "recon_variance": rep["variance"],
        "checks": {c["name"]: c["passed"] for c in rep["checks"]},
        "valid_fp": fingerprint(resolve(final)) if final else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--ai-formats", default="excel,fixed-width,json-array")
    args = ap.parse_args()
    api = Api(args.api)
    tables = {t["table_name"]: t["table_id"] for t in api.call("GET", "/api/tables")}
    if api.call("GET", "/api/keys")["active"] < 1:
        raise SystemExit("no active Gemini key in the server's pool")

    # ---------------------------------------------------------------- A: AI maps the reference table
    ref_label = "csv-utf8"
    t0 = time.time()
    ai = api.ai_mapping(tables[table_name(ref_label)])
    ref_set = ai["mapping_set_id"]
    proposals = {p["source_column"]: p for p in ai["report"]["proposals"]}
    print(f"A. Gemini mapped {ref_label} in {time.time() - t0:.0f}s -> mapping set {ref_set}")
    for rule in api.call("GET", f"/api/mapping/{ref_set}")["rules"]:
        if rule["rule_type"] == "VALUE_MAPPING":
            lookups = COUNTRY_LOOKUP if rule["target_column"] == "COUNTRY" else TERMS_LOOKUP
            api.call("PUT", f"/api/mapping/rules/{rule['rule_id']}", json={"lookups": lookups, "confirm": True, "note": "lookups supplied by compare script"})
    for tt, col, logic in STATIC:
        api.call("POST", f"/api/mapping/{ref_set}/rules", json={"target_table": tt, "target_column": col, "rule_type": "STATIC_VALUE", "transformation_logic": logic})
    ref_detail = api.call("GET", f"/api/mapping/{ref_set}")
    ref_rules = [r for r in ref_detail["rules"]]
    # value-mapping rules were given lookups above; replicate them with the same lookups
    lookup_for = {"COUNTRY": COUNTRY_LOOKUP, "ZTERM": TERMS_LOOKUP}

    # ---------------------------------------------------------------- B: same mapping, every format, full pipeline
    results = {}
    for label in LABELS:
        tid = tables[table_name(label)]
        if label == ref_label:
            mset = ref_set
        else:
            mset = api.call("POST", "/api/mapping-sets", json={"source_table_id": tid})["mapping_set_id"]
            for r in ref_rules:
                body = {k: r[k] for k in ("target_table", "target_column", "rule_type", "source_column", "transformation_logic")}
                if r["rule_type"] == "VALUE_MAPPING":
                    body["lookups"] = lookup_for.get(r["target_column"])
                api.call("POST", f"/api/mapping/{mset}/rules", json=body)
        started = time.time()
        run = api.call("POST", f"/api/pipeline/run/{mset}", json={"wave_name": "FMT_" + label})
        info = collect(api, run["run_id"])
        info.update(label=label, mapping_set=mset, seconds=round(time.time() - started, 1), dedup=run.get("dedup") and {
            k: run["dedup"][k] for k in ("duplicate_clusters", "duplicate_children", "surviving_records", "exact_match_clusters", "fuzzy_match_clusters")},
            readiness=run["readiness"].get("state") or run["readiness"].get("status") or str(run["readiness"])[:60],
            mapping_sig=mapping_signature(api.call("GET", f"/api/mapping/{mset}")))
        results[label] = info
        print(f"B. {label:18s} run {info['run_id']:>3}  extracted {info['extracted']}  valid {info['valid']}  failed {info['failed']}  {info['seconds']}s")

    ref = results[ref_label]
    problems = []
    comparable = ("extracted", "transformed", "failed", "valid", "defective", "dup_children", "failures", "defect_count", "recon_balanced",
                  "recon_passed", "recon_variance", "checks", "dedup", "mapping_sig", "valid_fp", "quality", "readiness")
    for label, r in results.items():
        r["diffs"] = [k for k in comparable if label != ref_label and r[k] != ref[k]
                      and not (k == "checks" and set(r[k]) - {"extract_matches_landed_snapshot"} == set(ref[k]) - {"extract_matches_landed_snapshot"}
                               and {a: b for a, b in r[k].items() if a != "extract_matches_landed_snapshot"} == {a: b for a, b in ref[k].items() if a != "extract_matches_landed_snapshot"})]
        if r["diffs"]:
            problems.append(f"{label}: pipeline result differs in {r['diffs']}")

    # ---------------------------------------------------------------- C: is the AI mapping repeatable across formats?
    ai_lines = []
    ref_sig = {(p["source_column"], p["recommendation"] and p["recommendation"]["target_table"], p["recommendation"] and p["recommendation"]["target_column"],
                p["recommendation"] and p["recommendation"]["rule_type"]) for p in ai["report"]["proposals"]}
    for label in [x for x in args.ai_formats.split(",") if x]:
        again = api.ai_mapping(tables[table_name(label)])
        sig = {(p["source_column"], p["recommendation"] and p["recommendation"]["target_table"], p["recommendation"] and p["recommendation"]["target_column"],
                p["recommendation"] and p["recommendation"]["rule_type"]) for p in again["report"]["proposals"]}
        same = sig == ref_sig
        ai_lines.append((label, same, sorted(sig ^ ref_sig, key=str)))
        print(f"C. Gemini on {label}: {'same mapping as the reference' if same else 'DIFFERENT: ' + str(sorted(sig ^ ref_sig, key=str))}")

    # ---------------------------------------------------------------- report
    out = ["# Full-pipeline format comparison", "",
           f"AI mapping by Gemini (`{api.call('GET', '/api/keys')['model']}`) on the reference table; the same mapping replicated onto each format's table.", "",
           "## Mapping proposed by Gemini for the reference", "", "| Source column | Outcome | Target | Rule | Review |", "|---|---|---|---|---|"]
    for col, p in proposals.items():
        rec = p["recommendation"] or {}
        out.append(f"| {col} | {p['outcome']} | {rec.get('target_table', '')}.{rec.get('target_column', '')} | {rec.get('rule_type', '')} | {p['status'] or ''} {'; '.join(p['reasons'])[:60]} |")
    out += ["", "## Pipeline result per format", "",
            "| Format | Run | Extracted | Valid | Defective | Dup children | Quality % | Recon balanced | Recon passed | Valid-output fingerprint | Seconds | vs reference |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for label, r in results.items():
        out.append(f"| {label} | {r['run_id']} | {r['extracted']} | {r['valid']} | {r['defective']} | {r['dup_children']} | {r['quality']} | {r['recon_balanced']} | "
                   f"{r['recon_passed']} | `{(r['valid_fp'] or '')[:12]}` | {r['seconds']} | {'reference' if label == ref_label else ('IDENTICAL' if not r['diffs'] else 'DIFFERENT: ' + ', '.join(r['diffs']))} |")
    out += ["", "## Reference run detail", "", f"- Dedup: {ref['dedup']}", f"- Readiness: {ref['readiness']}", f"- Failures by check: {json.dumps(ref['failures'], indent=None)}",
            f"- Reconciliation checks: {json.dumps(ref['checks'])}", "", "## Is the AI mapping repeatable across formats?", ""]
    out += [f"- {label}: {'same as reference' if same else 'DIFFERENT - ' + str(d)}" for label, same, d in ai_lines]
    (ROOT / "mock_data" / "pipeline_comparison.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out))
    print("PROBLEMS:" if problems else "ALL FORMATS PRODUCE THE SAME PIPELINE RESULT")
    for p in problems:
        print(" -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
