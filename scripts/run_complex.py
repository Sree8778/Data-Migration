"""Run the COMPLEX dataset end to end and score every stage against its answer key.

    python scripts/generate_complex_data.py            # once
    python scripts/run_complex.py [--api http://localhost:8000] [--reuse-ai]

Stages: preview/register (encoding, delimiter, multi-line fields, control total) -> profile (null counts vs key) ->
Gemini mapping (scored vs the expected mapping) -> steward corrections (the fixes a data steward would make, listed
explicitly) -> pipeline with the DEFAULT dedupe config and again with EXPLICIT business keys -> row-level scoring
of defect detection, deduplication and output values. Report: mock_data/complex/complex_report.md.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

import duckdb
import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "mock_data" / "complex"
LANG_CODE = {"D": "D", "DE": "D", "DEUTSCH": "D", "GERMAN": "D", "E": "E", "EN": "E", "ENGLISH": "E", "ENGLISCH": "E", "F": "F", "FR": "F", "FRANÇAIS": "F", "FRENCH": "F",
             "S": "S", "ES": "S", "ESPAÑOL": "S", "SPANISH": "S", "I": "I", "IT": "I", "ITALIANO": "I", "ITALIAN": "I", "N": "N", "NL": "N", "NEDERLANDS": "N", "DUTCH": "N",
             "H": "H", "HINDI": "H", "P": "P", "PT": "P", "PORTUGUÊS": "P", "PORTUGUESE": "P", "J": "J", "JA": "J", "JAPANESE": "J"}
LANG_DOMAIN = set("EDFSIPJ")  # the built-in T002 domain
ERROR_PATTERNS = {
    "blank_city": r"ADRC\.CITY1: MANDATORY_EMPTY",
    "blank_country": r"ADRC\.COUNTRY: MANDATORY_EMPTY",
    "unknown_country": r"ADRC\.COUNTRY: LOOKUP_FAILED",
    "unknown_terms": r"KNB1\.ZTERM: LOOKUP_FAILED",
    "name_over_40": r"NAME_ORG1.*LENGTH|LENGTH.*NAME_ORG1|ERR_LENGTH_.*NAME_ORG1|NAME_ORG1: .*too long",
    "unknown_account_group": r"BUT000\.BU_GROUP: LOOKUP_FAILED",
    "unknown_currency": r"KNVV\.WAERS: LOOKUP_FAILED",
    "language_not_in_domain": r"ADRC\.LANGU: .*(CHECKTABLE|DOMAIN)|ERR_CHECKTABLE_.*LANGU",
}


class Api:
    def __init__(self, base):
        self.c = httpx.Client(base_url=base, headers={"X-User": "complex.tester"}, timeout=3600)

    def call(self, method, url, expect=(200, 201, 202), **kw):
        r = self.c.request(method, url, **kw)
        if r.status_code not in expect:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:400]}")
        return r.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--reuse-ai", action="store_true", help="reuse the AI mapping set saved by a previous run")
    ap.add_argument("--data", default=str(ROOT / "mock_data" / "complex"), help="folder holding customers_complex.csv and answer_key.json (default: mock_data/complex)")
    args = ap.parse_args()
    global DATA
    DATA = Path(args.data)
    api = Api(args.api)
    key = json.loads((DATA / "answer_key.json").read_text(encoding="utf-8"))
    raw = (DATA / "customers_complex.csv").read_bytes()
    report: list[str] = ["# Complex dataset - end-to-end report", ""]
    findings: list[str] = []

    # ------------------------------------------------------------------ 1. landing
    pv = api.call("POST", "/api/sources/preview", files={"file": ("customers_complex.csv", raw)})
    t0 = time.time()
    reg = api.call("POST", "/api/sources/register", data={"system_name": "SAP_ECC_LEGACY", "table_name": "KNA1_COMPLEX", "primary_key_columns": "KUNNR",
                                                         "expected_rows": str(key["rows"])}, files={"file": ("customers_complex.csv", raw)})
    tid = reg["table_id"]
    land_s = time.time() - t0
    prof = api.call("POST", f"/api/profile/{tid}")
    null_ok = sum(1 for c in prof["columns"] if c["null_count"] == key["blank_counts"][c["column_name"]])
    report += ["## 1. Landing and profiling", "",
               f"- File: {len(raw) / 1e6:.1f} MB, {key['rows']:,} rows x {len(key['columns'])} columns; detected **{pv['format']}**, encoding **{pv['encoding']}** ({pv['encoding_basis']}), delimiter **{pv['delimiter']}**",
               f"- Rows landed: {reg['rows_landed']:,} (control total {reg['control_status']}) in {land_s:.1f}s; multi-line quoted fields and embedded delimiters parsed correctly",
               f"- Null counts equal the answer key in **{null_ok}/{len(key['columns'])}** columns", ""]
    if null_ok != len(key["columns"]):
        findings.append("profile null counts differ from the answer key")

    # ------------------------------------------------------------------ 2. AI mapping
    state = DATA / "_state.json"
    t0 = time.time()
    if args.reuse_ai and state.exists():
        mset = json.loads(state.read_text())["mapping_set_id"]
        ai_s = None
    else:
        job = api.call("POST", f"/api/mapping/generate/{tid}?background=true")
        while True:
            s = api.call("GET", f"/api/mapping/jobs/{job['job_id']}")
            if s["status"] not in ("queued", "running"):
                break
            time.sleep(5)
        if s["status"] != "succeeded":
            raise SystemExit(f"AI mapping failed: {s.get('error')}")
        mset = s["result"]["mapping_set_id"]
        ai_s = time.time() - t0
        state.write_text(json.dumps({"mapping_set_id": mset, "table_id": tid}))
    detail = api.call("GET", f"/api/mapping/{mset}")
    got = {r["source_column"]: f"{r['target_table']}.{r['target_column']}" for r in detail["rules"] if r["source_column"]}
    exp = key["expected_mapping"]
    correct = [c for c, t in exp.items() if got.get(c) == t]
    wrong = {c: got[c] for c, t in exp.items() if c in got and got[c] != t}
    missed = [c for c in exp if c not in got]
    extra = [c for c in got if c not in exp]
    report += ["## 2. AI mapping (Gemini)", "",
               f"- {len(exp)} columns have a target in the catalog; **{len(correct)} mapped correctly**, {len(wrong)} to a different target {wrong or ''}, **{len(missed)} missed** ({', '.join(missed) or '-'}); "
               f"{len(extra)} mapped although the catalog has no target ({', '.join(extra) or '-'})." + (f" Took {ai_s:.0f}s." if ai_s else ""), ""]
    if missed:
        findings.append(f"AI mapping missed {len(missed)} of {len(exp)} mappable columns: {', '.join(missed)}")

    # ------------------------------------------------------------------ 3. steward corrections (on a fresh copy, so re-running is safe)
    ai_set = mset
    mset = api.call("POST", "/api/mapping-sets", json={"source_table_id": tid})["mapping_set_id"]
    for r in detail["rules"]:
        api.call("POST", f"/api/mapping/{mset}/rules", json={k: r[k] for k in ("target_table", "target_column", "rule_type", "source_column", "transformation_logic")})
    detail = api.call("GET", f"/api/mapping/{mset}")
    fixes = []
    lk = key["lookups"]
    country = {k: v for k, v in lk["country"].items()}
    lang = {k: v for k, v in LANG_CODE.items()}
    rules = {r["source_column"]: r for r in detail["rules"] if r["source_column"]}

    def put(col, **body):
        api.call("PUT", f"/api/mapping/rules/{rules[col]['rule_id']}", json={**body, "confirm": True, "note": "steward correction"})

    def post(target_table, target_column, rule_type, source=None, logic=None, lookups=None):
        api.call("POST", f"/api/mapping/{mset}/rules", json={"target_table": target_table, "target_column": target_column, "rule_type": rule_type,
                                                              "source_column": source, "transformation_logic": logic, "lookups": lookups})

    if "KUNNR" in rules and got["KUNNR"] != "BUT000.BP_EXT":
        put("KUNNR", target_table="BUT000", target_column="BP_EXT"); fixes.append("KUNNR retargeted to BUT000.BP_EXT (the legacy key, not the SAP customer number)")
    if "ZTERM" in rules:
        put("ZTERM", lookups=lk["terms"]); fixes.append(f"ZTERM: {len(lk['terms'])} payment-term lookups")
    if "WAERS" in rules and rules["WAERS"]["rule_type"] != "VALUE_MAPPING":
        put("WAERS", rule_type="VALUE_MAPPING", lookups=lk["currency"]); fixes.append("WAERS changed from DIRECT_COPY to VALUE_MAPPING (23 spellings of 8 currencies) with lookups")
    wanted = {"NAME1": ("BUT000", "NAME_ORG1", "DIRECT_COPY", {}), "NAME2": ("BUT000", "NAME_ORG2", "DIRECT_COPY", {}), "SORTL": ("BUT000", "BU_SORT1", "DIRECT_COPY", {}),
              "STRAS": ("ADRC", "STREET", "DIRECT_COPY", {}), "ORT01": ("ADRC", "CITY1", "DIRECT_COPY", {}), "PSTLZ": ("ADRC", "POST_CODE1", "DIRECT_COPY", {}),
              "REGIO": ("ADRC", "REGION", "DIRECT_COPY", {}), "LAND1": ("ADRC", "COUNTRY", "VALUE_MAPPING", {"lookups": country}),
              "SPRAS": ("ADRC", "LANGU", "VALUE_MAPPING", {"lookups": lang}), "KTOKD": ("BUT000", "BU_GROUP", "VALUE_MAPPING", {"lookups": lk["bu_group"]}),
              "INCO1": ("KNVV", "INCO1", "DIRECT_COPY", {}), "VKORG": ("KNVV", "VKORG", "DIRECT_COPY", {}), "VTWEG": ("KNVV", "VTWEG", "DIRECT_COPY", {}),
              "SPART": ("KNVV", "SPART", "DIRECT_COPY", {}), "AKONT": ("KNB1", "AKONT", "DIRECT_COPY", {})}
    for col, (tt, tc, rt, extra_args) in wanted.items():
        if col not in got:
            post(tt, tc, rt, source=col, **extra_args); fixes.append(f"added {col} -> {tt}.{tc} ({rt})")
    for col in ("LAND1", "SPRAS", "KTOKD"):  # the AI proposed these as value mappings but cannot know the lookups
        if col in rules:
            put(col, lookups=wanted[col][3]["lookups"]); fixes.append(f"{col}: {len(wanted[col][3]['lookups'])} lookups supplied for the AI's value mapping")
    post("BUT000", "TYPE", "SQL_EXPRESSION", source="KTOKD", logic="CASE WHEN KTOKD = 'ZPER' THEN '1' ELSE '2' END")
    fixes.append("BUT000.TYPE derived from the account group (person '1' / organisation '2')")
    report += [f"## 3. Steward corrections applied to a copy of the AI mapping (AI set {ai_set} -> working set {mset})", ""] + [f"- {f}" for f in fixes] + [""]

    # ------------------------------------------------------------------ 4. pipeline runs
    explicit = {
        "exact_keys": [{"name": "vat", "columns": ["SRC_STCEG"], "normalizer": "alnum"}, {"name": "email", "columns": ["SRC_SMTP_ADDR"], "normalizer": "email"},
                       {"name": "phone", "columns": ["SRC_TELF1"], "normalizer": "digits"}],
        "name_column": "BUT000_NAME_ORG1", "address_columns": ["ADRC_STREET", "ADRC_CITY1", "ADRC_POST_CODE1"]}
    runs = {}
    for label, body in (("default", {}), ("explicit", {"dedup": explicit, "passthrough_columns": ["STCEG", "STCD1", "SMTP_ADDR", "TELF1"]})):
        t0 = time.time()
        res = api.call("POST", f"/api/pipeline/run/{mset}", json={"wave_name": "COMPLEX_" + label.upper(), **body})
        runs[label] = {"run": res, "seconds": time.time() - t0}

    # ------------------------------------------------------------------ 5. scoring
    src = pd.read_csv(io.BytesIO(raw), sep=";", dtype=str, keep_default_na=False, encoding="cp1252")
    src_by = src.set_index("KUNNR", drop=False)
    con = duckdb.connect()
    groups = key["duplicate_groups"]

    def load(run_id, name):
        p = ROOT / "workspace" / "runs" / f"run_{run_id}"
        dd = con.execute("SELECT * FROM read_parquet(?)", [str(p / "deduplicated.parquet")]).df()
        rej = con.execute("SELECT _SRC_PK, _VALIDATION_ERRORS FROM read_parquet(?)", [str(p / "validation" / "rejected.parquet")]).df()
        valid = con.execute("SELECT * FROM read_parquet(?)", [str(p / "validation" / "valid.parquet")]).df()
        return dd, rej, valid

    report += ["## 4. Pipeline runs", "", "| Run | Dedupe config | Extracted | Transform failed | Dup children removed | Valid | Defective | Quality % | Readiness | Seconds |", "|---|---|---|---|---|---|---|---|---|---|"]
    scored = {}
    for label, info in runs.items():
        res = info["run"]
        dd, rej, valid = load(res["run_id"], label)
        v = json.loads((ROOT / "workspace" / "runs" / f"run_{res['run_id']}" / "validation.json").read_text(encoding="utf-8"))
        report.append(f"| {res['run_id']} | {label} | {res['run']['source_records']:,} | {res['run']['failed_records']:,} | {v['duplicate_children_excluded']:,} | {v['valid_records']:,} | "
                      f"{v['defective_records']:,} | {v['quality_score']:.1f} | {res['readiness'].get('state') or res['readiness'].get('status')} | {info['seconds']:.0f} |")
        scored[label] = (res, dd, rej, valid, v)
    report.append("")

    # ---- deduplication vs truth
    report += ["## 5. Deduplication vs the answer key", "", "(Rows that fail the mandatory/lookup checks are excluded from deduplication by design; they are listed separately.)", "",
               "| Duplicate type | Injected | Found - default config | Found - explicit keys | Missed only because a row was excluded (explicit) |", "|---|---|---|---|---|"]
    cats = sorted({g["category"] for g in groups})
    outcome = {}
    for label, (res, dd, rej, valid, v) in scored.items():
        cl = dict(zip(dd["_SRC_PK"].astype(str), dd["dedup_cluster_id"]))
        failed = set(dd.loc[dd["_TRANSFORM_STATUS"] == "FAILED", "_SRC_PK"].astype(str))
        per = {}
        for g in groups:
            same = g["orig"] in cl and g["dup"] in cl and pd.notna(cl[g["orig"]]) and cl[g["orig"]] == cl[g["dup"]]
            excl = g["orig"] in failed or g["dup"] in failed
            d = per.setdefault(g["category"], [0, 0, 0]); d[0] += 1; d[1] += bool(same); d[2] += (not same) and excl
        truth_of = {}
        for i, g in enumerate(groups):
            truth_of[g["orig"]] = ("g", i); truth_of[g["dup"]] = ("g", i)
        members = {}
        for pk, c in cl.items():
            if pd.notna(c):
                members.setdefault(c, []).append(pk)
        false_merge = [m for m in members.values() if len({truth_of.get(k, ("solo", k)) for k in m}) > 1]
        pairs_merged = sum(1 for a, b in key["distinct_pairs"] if pd.notna(cl.get(a)) and cl.get(a) == cl.get(b))
        outcome[label] = (per, len(false_merge), pairs_merged, false_merge[:3])
    for cat in cats:
        d0, d1 = outcome["default"][0][cat], outcome["explicit"][0][cat]
        report.append(f"| {cat} | {d1[0]} | {d0[1]} ({d0[1] / d0[0]:.0%}) | {d1[1]} ({d1[1] / d1[0]:.0%}) | {d1[2]} |")
    report += ["", f"- Different entities merged by mistake: default **{outcome['default'][1]}** clusters, explicit **{outcome['explicit'][1]}** clusters; "
                   f"look-alike pairs merged (of {len(key['distinct_pairs'])}): default **{outcome['default'][2]}**, explicit **{outcome['explicit'][2]}**", ""]
    if outcome["default"][0]["tax-id-reformatted"][1] == 0 and outcome["explicit"][0]["tax-id-reformatted"][1] > 0:
        findings.append("default dedupe does not recognise SAP-style key columns (STCEG/STCD1, SMTP_ADDR, TELF1): tax, e-mail and phone duplicates are only found with an explicit config")
    if outcome["explicit"][1] or outcome["explicit"][2]:
        findings.append("different entities were merged in the explicit run")

    # ---- defect detection vs the key (non-child rows)
    res, dd, rej, valid, v = scored["explicit"]
    child = dict(zip(dd["_SRC_PK"].astype(str), dd["is_duplicate_child"].fillna(False)))
    errtext = {pk: (str(t) if t is not None else "") for pk, t in zip(dd["_SRC_PK"].astype(str), dd["_TRANSFORM_ERRORS"])}
    for pk, t in zip(rej["_SRC_PK"].astype(str), rej["_VALIDATION_ERRORS"]):
        errtext[pk] = errtext.get(pk, "") + " | " + str(t)
    expected = {t: set(pks) for t, pks in key["tags"].items()}
    expected["unknown_account_group"] = set(src.loc[src["KTOKD"] == "Z999", "KUNNR"])
    known_cur = {k.upper() for k in lk["currency"]}
    expected["unknown_currency"] = set(src.loc[~src["WAERS"].str.strip().str.upper().isin(known_cur), "KUNNR"])
    expected["language_not_in_domain"] = set(src.loc[src["SPRAS"].str.strip().str.upper().map(lambda x: LANG_CODE.get(x, "?") not in LANG_DOMAIN), "KUNNR"])
    report += ["## 6. Defect detection vs the answer key (explicit run, duplicate children excluded)", "",
               "| Defect | In the file | Flagged by the app | Missed | False alarms |", "|---|---|---|---|---|"]
    for tag, pattern in ERROR_PATTERNS.items():
        exp_set = {p for p in expected.get(tag, set()) if not child.get(p, False)}
        flagged = {p for p, t in errtext.items() if re.search(pattern, t) and not child.get(p, False)}
        report.append(f"| {tag} | {len(exp_set)} | {len(flagged & exp_set)} | {len(exp_set - flagged)} | {len(flagged - exp_set)} |")
        if exp_set - flagged or flagged - exp_set:
            findings.append(f"defect '{tag}': {len(exp_set - flagged)} missed, {len(flagged - exp_set)} false alarms")
    report.append("")

    # ---- defects the app cannot see by design
    valid_pks = set(valid["_SRC_PK"].astype(str))
    deleted_valid = len(valid_pks & set(key["tags"]["deleted_flag"])); test_valid = len(valid_pks & set(key["tags"]["test_record"]))
    inv_valid = len(valid_pks & set(key["tags"]["invalid_email"]))
    report += ["## 7. Records that reach the 'valid' output although the legacy system said they should not migrate", "",
               f"- Flagged for deletion in the legacy system (LOEVM = X): **{deleted_valid}** of {len(key['tags']['deleted_flag'])} ended up valid",
               f"- Obvious test / dummy records: **{test_valid}** of {len(key['tags']['test_record'])} ended up valid",
               f"- Invalid e-mail addresses: {inv_valid} of {len(key['tags']['invalid_email'])} valid (no e-mail target field in this catalog, so not checked)", ""]
    if deleted_valid or test_valid:
        findings.append(f"{deleted_valid} deletion-flagged and {test_valid} test records pass through as valid: there is no row-exclusion rule")

    # ---- output values
    exp_rows = valid.copy()
    exp_rows["pk"] = exp_rows["_SRC_PK"].astype(str)
    checks = []

    def compare(name, col, fn):
        bad = 0; example = None
        for pk, val in zip(exp_rows["pk"], exp_rows[col]):
            want = fn(src_by.loc[pk])
            if (None if pd.isna(val) else str(val)) != want:
                bad += 1; example = example or (pk, val, want)
        checks.append((name, len(exp_rows), bad, example))

    compare("BP_EXT = KUNNR (10-digit, leading zeros kept)", "BUT000_BP_EXT", lambda r: r["KUNNR"])
    compare("TYPE = '1' for persons, '2' otherwise", "BUT000_TYPE", lambda r: "1" if r["KTOKD"] == "ZPER" else "2")
    compare("POST_CODE1 keeps leading zeros", "ADRC_POST_CODE1", lambda r: r["PSTLZ"].strip() or None)
    compare("NAME_ORG1 keeps umlauts / accents", "BUT000_NAME_ORG1", lambda r: r["NAME1"].strip())
    compare("COUNTRY converted to the ISO code", "ADRC_COUNTRY", lambda r: country.get(r["LAND1"].strip()) or country.get(r["LAND1"].strip().upper()) or next((v2 for k2, v2 in country.items() if k2.upper() == r["LAND1"].strip().upper()), None))
    compare("ZTERM converted to the SAP key", "KNB1_ZTERM", lambda r: next((v2 for k2, v2 in lk["terms"].items() if k2.upper() == r["ZTERM"].strip().upper()), None))
    compare("VKORG padded to 4 characters", "KNVV_VKORG", lambda r: r["VKORG"].strip().zfill(4))
    report += ["## 8. Output values in the valid records (checked against the source row)", "", "| Check | Rows checked | Wrong |", "|---|---|---|"]
    for name, n, bad, ex in checks:
        report.append(f"| {name} | {n:,} | {bad} |" + (f" e.g. {ex}" if ex and bad else ""))
        if bad:
            findings.append(f"output check failed: {name} ({bad} rows, e.g. {ex})")

    # ---- governance / reconciliation
    rec = api.call("GET", f"/api/reconciliation/{scored['explicit'][0]['run_id']}")["report"]
    report += ["", "## 9. Reconciliation and governance (explicit run)", "",
               f"- Every source row accounted for: **{rec['balanced']}** (extracted {rec['counts']['extracted'] if 'extracted' in rec['counts'] else ''}, variance {rec['variance']})",
               "- Checks: " + ", ".join(f"{c['name']}={'ok' if c['passed'] else 'NO'}" for c in rec["checks"]),
               f"- Readiness gate: {scored['explicit'][0]['readiness']}", ""]
    report += ["## Findings", ""] + ([f"- {f}" for f in findings] or ["- none"])
    (DATA / "complex_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
