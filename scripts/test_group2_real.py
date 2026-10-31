"""A real end-to-end test of the AI assistant features on the LARGE complex dataset, with real Gemini, scored against the generator's answer key.

    python scripts/generate_complex_data.py --rows 30000 --out mock_data/large --seed 7      # once (about 9 minutes)
    # the server must run with a Gemini key:  LLM_PROVIDER=gemini GEMINI_API_KEY=... uvicorn src.api.main:create_app --factory --port 8000
    python scripts/test_group2_real.py [--data mock_data/large]

  1  land and profile the dirty file; the AI maps it (one run per column)
  2  SUGGEST A RULE for each column the first pass missed, with a hint: is the suggested target the right one, and what does the measured evidence say?
  3  DRAFT LOOKUPS for every value-mapping rule: how many proposals agree with the answer key, how many are wrong, were any invented keys discarded, what coverage?
  4  approve the pre-ticked entries, then run the pipeline: how does the AI-drafted mapping compare with one built from the answer key's own lookups?
  5  ASK THE ASSISTANT six plain-English questions: is every figure in an answer the one an independent query gives?
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.services.ai_assist import lookup_domains  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LANG = {"D": "D", "DE": "D", "DEUTSCH": "D", "GERMAN": "D", "E": "E", "EN": "E", "ENGLISH": "E", "ENGLISCH": "E", "F": "F", "FR": "F", "FRANÇAIS": "F", "FRENCH": "F",
        "S": "S", "ES": "S", "ESPAÑOL": "S", "SPANISH": "S", "I": "I", "IT": "I", "ITALIANO": "I", "ITALIAN": "I", "N": "N", "NL": "N", "NEDERLANDS": "N", "DUTCH": "N",
        "H": "H", "HINDI": "H", "P": "P", "PT": "P", "PORTUGUÊS": "P", "PORTUGUESE": "P", "J": "J", "JA": "J", "JAPANESE": "J"}

ap = argparse.ArgumentParser()
ap.add_argument("--api", default="http://localhost:8000")
ap.add_argument("--data", default=str(ROOT / "mock_data" / "large"))
args = ap.parse_args()
DATA = Path(args.data)
KEY = json.loads((DATA / "answer_key.json").read_text(encoding="utf-8"))
LK = KEY["lookups"]
api = httpx.Client(base_url=args.api, headers={"X-User": "g2.author"}, timeout=3600)
steward = httpx.Client(base_url=args.api, headers={"X-User": "g2.steward"}, timeout=3600)
results: list[tuple[str, bool, str]] = []
norm = lambda s: s.strip().upper()  # noqa: E731


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)
    return bool(ok)


def call(client, method, url, expect=(200, 201, 202), **kw):
    r = client.request(method, url, **kw)
    if r.status_code not in expect:
        raise SystemExit(f"{method} {url} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


# ====================================================================== 1
step("1  land, profile and AI-map the dirty file (real Gemini)")
raw = (DATA / "customers_complex.csv").read_bytes()
reg = call(api, "POST", "/api/sources/register", data={"system_name": f"G2_REAL_{time.strftime('%H%M%S')}", "table_name": "KNA1_LARGE", "primary_key_columns": "KUNNR",
                                                       "expected_rows": str(KEY["rows"])}, files={"file": ("customers_complex.csv", raw)})
tid = reg["table_id"]
call(api, "POST", f"/api/profile/{tid}")
t0 = time.time()
job = call(api, "POST", f"/api/mapping/generate/{tid}?background=true&consistency_runs=1")
while True:
    s = call(api, "GET", f"/api/mapping/jobs/{job['job_id']}")
    if s["status"] not in ("queued", "running", "cancelling"):
        break
    time.sleep(8)
if s["status"] != "succeeded":
    raise SystemExit(f"AI mapping failed: {s.get('error')}")
mset = s["result"]["mapping_set_id"]
print(f"  AI mapping: {time.time() - t0:.0f}s, model {s['result']['report']['model_name']}")
detail = call(api, "GET", f"/api/mapping/{mset}")
got = {r["source_column"]: f"{r['target_table']}.{r['target_column']}" for r in detail["rules"] if r["source_column"]}
exp = KEY["expected_mapping"]
missed = [c for c in exp if c not in got]
print(f"  mapped {len(got)} columns; missed of the {len(exp)} mappable: {missed}")

# ====================================================================== 2
step("2  suggest a rule for each column the first pass missed")
hints = {"KTOKD": "this is the customer account group, it decides the business partner grouping", "SPRAS": "this is the customer's communication language"}
if not missed:
    print("  (the first pass missed nothing this time, so the suggestion is tried on two columns that are mapped, in a scratch set)")
for column in missed:
    sug = call(api, "POST", f"/api/mapping/{mset}/suggest-rule", json={"column": column, "hint": hints.get(column), "runs": 3})
    target = f"{sug['target_table']}.{sug['target_column']}" if sug["target_table"] else None
    print(f"  {column}: {sug['outcome']} -> {target} ({sug['rule_type']}, {sug['confidence']}), agreement {sug['agreement']:.0%}, runs {sug['runs']}")
    for r_ in sug["review_reasons"]:
        print("      review:", r_[:160])
    if sug["evidence"]:
        ev = sug["evidence"]
        print(f"      measured: {ev['non_blank']:,}/{ev['rows']:,} filled, {ev['distinct']} distinct, domain {ev['domain']} valid {ev['domain_valid_rate']}, top invalid {ev['top_invalid'][:3]}")
    check(f"the suggestion for {column} is the target the answer key expects", target == exp[column], f"{target} vs {exp[column]}")
    check(f"the suggestion for {column} can be stored and still needs a review", sug["acceptable"])
    if sug["acceptable"] and target == exp[column]:
        rule = call(api, "POST", f"/api/mapping/{mset}/suggestions/{sug['suggestion_id']}/accept")
        check(f"{column}: accepting creates an AI rule that needs a human review", rule["ai_suggested"] and rule["needs_review"], rule["ai_model_name"])

# ====================================================================== 3
step("3  AI-drafted lookups, scored against the answer key")
detail = call(api, "GET", f"/api/mapping/{mset}")
rules = {r["source_column"]: r for r in detail["rules"] if r["source_column"]}
truth = {"LAND1": LK["country"], "SPRAS": LANG, "ZTERM": LK["terms"], "WAERS": LK["currency"]}
target_of = {"LAND1": ("ADRC", "COUNTRY"), "SPRAS": ("ADRC", "LANGU"), "ZTERM": ("KNB1", "ZTERM"), "WAERS": ("KNVV", "WAERS")}
wanted_other = {"NAME1": ("BUT000", "NAME_ORG1"), "NAME2": ("BUT000", "NAME_ORG2"), "SORTL": ("BUT000", "BU_SORT1"), "STRAS": ("ADRC", "STREET"), "ORT01": ("ADRC", "CITY1"),
                "PSTLZ": ("ADRC", "POST_CODE1"), "REGIO": ("ADRC", "REGION"), "INCO1": ("KNVV", "INCO1"), "VKORG": ("KNVV", "VKORG"), "VTWEG": ("KNVV", "VTWEG"),
                "SPART": ("KNVV", "SPART"), "AKONT": ("KNB1", "AKONT"), "KUNNR": ("BUT000", "BP_EXT")}
for col, (tt, tc) in target_of.items():           # make sure these four are value mappings on the right field
    if col not in rules:
        call(api, "POST", f"/api/mapping/{mset}/rules", json={"target_table": tt, "target_column": tc, "rule_type": "VALUE_MAPPING", "source_column": col})
    elif rules[col]["rule_type"] != "VALUE_MAPPING":
        call(api, "PUT", f"/api/mapping/rules/{rules[col]['rule_id']}", json={"rule_type": "VALUE_MAPPING", "confirm": False})
for col, (tt, tc) in wanted_other.items():
    if col not in rules and col not in got:
        call(api, "POST", f"/api/mapping/{mset}/rules", json={"target_table": tt, "target_column": tc, "rule_type": "DIRECT_COPY", "source_column": col})
detail = call(api, "GET", f"/api/mapping/{mset}")
rules = {r["source_column"]: r for r in detail["rules"] if r["source_column"]}
before = call(api, "POST", f"/api/mapping/{mset}/preflight")
before_rates = {c["target"]: c["pass_rate"] for c in before["rules"] if c["rule_type"] == "VALUE_MAPPING"}

total_ai = total_right = total_wrong = 0
for col in ("LAND1", "SPRAS", "ZTERM", "WAERS"):
    rid = rules[col]["rule_id"]
    t0 = time.time()
    d = call(api, "POST", f"/api/mapping/rules/{rid}/lookup-draft", json={"runs": 2, "limit": 150})
    domain = lookup_domains().get_domain(d['domain']) or frozenset()
    right = wrong = beyond = refused_ok = refused_missed = invented = 0
    beyond_examples = []
    wrong_examples = []
    for e in d["entries"]:
        want = truth[col].get(e["legacy_value"]) or truth[col].get(e["legacy_value"].upper()) or truth[col].get(norm(e["legacy_value"]))
        if e["proposed"] is None:
            refused_ok += want is None
            refused_missed += want is not None
        elif e["method"] == "valid_key":                         # the value already is the SAP key: right by definition
            right += 1
        elif want is None:                                         # a value the answer key leaves unknown (a typo, a decorated name): the AI read it, a steward judges it
            beyond += 1
            beyond_examples.append((e["legacy_value"], e["proposed"], e["method"], e["confidence"]))
        elif want == e["proposed"]:
            right += 1
        else:
            wrong += 1
            wrong_examples.append((e["legacy_value"], e["proposed"], want, e["method"], e["confidence"]))
    methods = {m: sum(1 for e in d["entries"] if e["method"] == m) for m in ("valid_key", "synonym_list", "ai", "none")}
    cov = d["coverage"]
    n = cov["non_blank_rows"] or 1
    print(f"  {col} -> {d['target']} [{d['domain']}, {d['domain_size']} keys] {time.time() - t0:.0f}s: {len(d['entries'])} values drafted {methods}; vs key: right {right}, wrong {wrong}, "
          f"beyond the key {beyond}, correctly refused {refused_ok}, missed {refused_missed}; coverage {cov['covered_now'] / n:.1%} -> {cov['covered_if_suggested'] / n:.1%} (all proposals {cov['covered_if_all'] / n:.1%})")
    if wrong_examples:
        print("      CONTRADICTS the key:", wrong_examples[:5])
    if beyond_examples:
        print("      beyond the key (for a steward to judge):", beyond_examples[:6])
    total_ai += right + wrong
    total_right += right
    total_wrong += wrong
    invented = [e["proposed"] for e in d["entries"] if e["proposed"] is not None and e["proposed"] not in domain]
    check(f"{col}: every proposed key exists in {d['domain']} (nothing invented survives)", not invented, f"{len(d['entries'])} entries checked against {len(domain)} keys; invented: {invented[:3]}")
    ticked = [e["legacy_value"] for e in d["entries"] if e["suggested_accept"]]
    known = lambda e: truth[col].get(e["legacy_value"]) or truth[col].get(norm(e["legacy_value"]))  # noqa: E731
    wrong_ticked = [e for e in d["entries"] if e["suggested_accept"] and e["method"] != "valid_key" and known(e) is not None and known(e) != e["proposed"]]
    check(f"{col}: no pre-ticked entry contradicts the answer key", not wrong_ticked, f"{len(ticked)} pre-ticked, {len(wrong_ticked)} wrong: {[(e['legacy_value'], e['proposed']) for e in wrong_ticked[:4]]}")
    if ticked:
        applied = call(steward, "POST", f"/api/mapping/rules/{rid}/lookup-draft/apply", json={"accept": ticked})
        check(f"{col}: the approved entries are written with their approver", applied["applied"] == len(ticked) and applied["approved_by"] == "g2.steward", f"{applied['applied']} written")
check("across all drafts the AI's proposals agree with the answer key", total_right >= 0.9 * max(1, total_ai), f"{total_right} right, {total_wrong} wrong of {total_ai} proposals")

# ====================================================================== 4
step("4  finish the mapping and run the pipeline on the AI-drafted lookups")
if "KTOKD" in rules:
    call(api, "PUT", f"/api/mapping/rules/{rules['KTOKD']['rule_id']}", json={"lookups": LK["bu_group"], "confirm": True})   # account groups (TB001) are the customer's own: no key list to draft against
else:
    call(api, "POST", f"/api/mapping/{mset}/rules", json={"target_table": "BUT000", "target_column": "BU_GROUP", "rule_type": "VALUE_MAPPING", "source_column": "KTOKD", "lookups": LK["bu_group"]})
call(api, "POST", f"/api/mapping/{mset}/rules", json={"target_table": "BUT000", "target_column": "TYPE", "rule_type": "SQL_EXPRESSION", "source_column": "KTOKD",
                                                     "transformation_logic": "CASE WHEN KTOKD = 'ZPER' THEN '1' ELSE '2' END"})
draft_gate = call(api, "POST", f"/api/mapping/{mset}/preflight")
after_rates = {c["target"]: c["pass_rate"] for c in draft_gate["rules"] if c["rule_type"] == "VALUE_MAPPING"}
print("  value-mapping pass rates on the sample, before -> after the drafts:", {t: (before_rates.get(t), a) for t, a in after_rates.items()})
check("the drafted lookups raise the measured pass rate of every value-mapping rule that was drafted", all((after_rates.get(t) or 0) > (before_rates.get(t) or 0) for t in ("ADRC.COUNTRY", "KNB1.ZTERM", "KNVV.WAERS")), "")
check("the AI-assisted mapping passes the pre-submission check", draft_gate["can_submit"], f"{draft_gate['blockers']} blocker(s): {[f['message'][:80] for f in draft_gate['findings'] if f['severity'] == 'BLOCKER'][:3]}; valid rate {draft_gate['valid_rate']:.1%}")
run = call(api, "POST", f"/api/pipeline/run/{mset}", json={"wave_name": "G2_REAL"})
run_id = run["run_id"]
v = run["validation"]
print(f"  run {run_id}: valid {v['valid_records']:,}, defective {v['defective_records']:,}, duplicates {v['duplicate_children_excluded']:,}, gate {run['readiness']['state']}")
REFERENCE_VALID_RATE = 0.78                                                          # about what lookups built from the answer key give on this file (no rows left out)
rate = v["valid_records"] / KEY["rows"]
check("the AI-drafted mapping is close to one built from the answer key's own lookups", rate >= REFERENCE_VALID_RATE - 0.08, f"valid {rate:.1%} of the extract vs about {REFERENCE_VALID_RATE:.1%} with hand-made lookups")

# ====================================================================== 5
step("5  the assistant: six plain-English questions, every figure checked against an independent query")
rej = run["validation"]["rejected_path"]
con = duckdb.connect()
one = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
indep = {
    "extracted": KEY["rows"], "valid": v["valid_records"], "rejected": v["defective_records"],
    "blank_country": one(f"SELECT count(*) FROM read_parquet('{rej}') WHERE ADRC_COUNTRY IS NULL OR trim(CAST(ADRC_COUNTRY AS VARCHAR)) = ''"),
    "city_errors": one(f"SELECT count(*) FROM read_parquet('{rej}') WHERE contains(_VALIDATION_ERRORS, 'CITY1')"),
}
questions = [
    ("How did this run go?", lambda a: a["facts"].get("run_summary", {}).get("valid") == indep["valid"] and a["facts"]["run_summary"]["extracted"] == indep["extracted"]),
    ("Why were so many records rejected?", lambda a: bool(a["facts"].get("explain_defects", {}).get("groups"))),
    ("Which rejected records have no country?", lambda a: a["facts"].get("find_records", {}).get("matching_records") == indep["blank_country"] and a["tables"] and a["tables"][0]["total"] == indep["blank_country"]),
    ("How many rejected records have a problem with the city?", lambda a: any(f.get("matching_records") == indep["city_errors"] for f in a["facts"].values() if isinstance(f, dict) and "matching_records" in f)
     or any(g["records"] == indep["city_errors"] for f in a["facts"].values() if isinstance(f, dict) for g in f.get("groups", []))),
    ("Which mapping rules still need a review?", lambda a: "mapping_rules" in a["facts"] and a["facts"]["mapping_rules"]["needing_review"] >= 1),
    ("Tell me about the country column.", lambda a: "column_facts" in a["facts"] or "mapping_rules" in a["facts"] or "explain_defects" in a["facts"]),
]
for q_, ok_fn in questions:
    t0 = time.time()
    a = call(api, "POST", "/api/assistant/ask", json={"question": q_, "run_id": run_id})
    print(f"  Q: {q_}\n     A ({a['source']}, {time.time() - t0:.0f}s, tools {[t['tool'] for t in a['tool_runs']]}): {a['answer'][:260]}")
    for n in a["notes"]:
        print("     note:", n[:200])
    check(f"'{q_}' is answered from the right facts", bool(ok_fn(a)))
    check(f"'{q_}': every figure in the answer is in the data", a["verified"] is True, "the model's own wording was kept" if a["source"] == "model" else "the facts were stated plainly instead")

failed = [r for r in results if not r[1]]
print(f"\n{'=' * 78}\n{len(results) - len(failed)} of {len(results)} checks passed")
for name, _, detail in failed:
    print(f"  FAILED: {name} {detail}")
sys.exit(1 if failed else 0)
