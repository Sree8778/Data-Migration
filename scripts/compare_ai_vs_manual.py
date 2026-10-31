"""Compare what the AI proposes with the mapping a person would write, on three differently shaped customer files, and say where the AI is right, wrong, or missing.

    python scripts/compare_ai_vs_manual.py --base http://127.0.0.1:8773 [--only english,german,nested] [--runs 2]

Needs a THROWAWAY application (own database and workspace) with the AI you want to judge switched on (for Gemini: LLM_PROVIDER=gemini and the key in GEMINI_API_KEYS, which never
touches the disk). The files: the practice file mock_data/legacy_ar_customers.csv (English names), a German SAP-style file (KUNDE_NR, STRASSE, PLZ ...) and a Firestore-style file whose
columns are nested field paths (address.city, contact.email). The answer key is a person's judgement written below; where the right answer is arguable the column is listed but not scored.

What is scored per column: did the AI choose the same SAP field; did it choose the same KIND of rule (a copy where a copy is right, a lookup where the values need translating); and was a wrong
choice flagged "needs review" (a wrong rule that is flagged is a nuisance; a wrong rule that is not flagged is the dangerous kind). It also reports whether the confidence separated right from wrong.
"""

from __future__ import annotations

import argparse
import csv
import io
import random
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:8773")
ap.add_argument("--only", default="english,german,nested")
ap.add_argument("--runs", type=int, default=2, help="how many times the AI is asked about each column")
args = ap.parse_args()
api = httpx.Client(base_url=args.base + "/api", timeout=600, headers={"X-User": "eval.user"})

LOOKUP = "VALUE_MAPPING"   # a column whose values must be translated: the right kind of rule is a lookup (a plain copy would load US/usa/United States unchanged)

# column -> (SAP field, right kind of rule or None when a copy is just as good); None as the field means "arguable: listed, not scored"
KEYS = {
    "english": {"CUST_ID": ("BUT000.BP_EXT", None), "CUST_NAME": ("BUT000.NAME_ORG1", None), "TAX_ID": ("DFKKBPTAXNUM.TAXNUM", None), "EMAIL": ("ADR6.SMTP_ADDR", None),
                "PHONE": ("ADR2.TEL_NUMBER", None), "ADDR_STREET": ("ADRC.STREET", None), "CITY": ("ADRC.CITY1", None), "COUNTRY": ("ADRC.COUNTRY", LOOKUP),
                "POSTAL_CODE": ("ADRC.POST_CODE1", None), "PAYMENT_TERMS": ("KNB1.ZTERM", LOOKUP)},
    "german": {"KUNDE_NR": ("BUT000.BP_EXT", None), "FIRMENNAME": ("BUT000.NAME_ORG1", None), "ZUSATZ": ("BUT000.NAME_ORG2", None), "STRASSE": ("ADRC.STREET", None),
               "HAUSNR": ("ADRC.HOUSE_NUM1", None), "ORT": ("ADRC.CITY1", None), "PLZ": ("ADRC.POST_CODE1", None), "LAND": ("ADRC.COUNTRY", LOOKUP), "SPRACHE": ("ADRC.LANGU", None),
               "MAIL": ("ADR6.SMTP_ADDR", None), "TELEFON": ("ADR2.TEL_NUMBER", None), "ZAHLUNGSBED": ("KNB1.ZTERM", LOOKUP), "WAEHRUNG": ("KNVV.WAERS", None),
               "LOESCH_KZ": (None, None), "UST_IDNR": ("DFKKBPTAXNUM.TAXNUM", None)},
    "nested": {"_id": ("BUT000.BP_EXT", None), "name": ("BUT000.NAME_ORG1", None), "address.street": ("ADRC.STREET", None), "address.city": ("ADRC.CITY1", None),
               "address.zip": ("ADRC.POST_CODE1", None), "address.country": ("ADRC.COUNTRY", LOOKUP), "contact.email": ("ADR6.SMTP_ADDR", None), "contact.phone": ("ADR2.TEL_NUMBER", None),
               "terms": ("KNB1.ZTERM", LOOKUP), "tax_id": ("DFKKBPTAXNUM.TAXNUM", None)},
}
KEYCOL = {"english": "CUST_ID", "german": "KUNDE_NR", "nested": "_id"}

CITIES = [("Berlin", "10115", "DE", "Deutschland"), ("Hamburg", "20095", "DE", "Germany"), ("Wien", "1010", "AT", "Österreich"), ("Zürich", "8001", "CH", "Schweiz"), ("Lyon", "69001", "FR", "France"),
          ("Milano", "20121", "IT", "Italia"), ("Madrid", "28001", "ES", "España"), ("Köln", "50667", "DE", "DE")]
NAMES = ["Müller", "Schmidt", "Weber", "Fischer", "Becker", "Hoffmann", "Schäfer", "Koch", "Richter", "Wolf"]
SUFFIX = ["GmbH", "AG", "KG", "SARL", "SpA", "SL"]
TERMS_DE = ["sofort netto", "14 Tage 2% Skonto", "30 Tage netto", "60 Tage netto", "Vorkasse"]


def german() -> bytes:
    rnd = random.Random(7)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["KUNDE_NR", "FIRMENNAME", "ZUSATZ", "STRASSE", "HAUSNR", "ORT", "PLZ", "LAND", "SPRACHE", "MAIL", "TELEFON", "ZAHLUNGSBED", "WAEHRUNG", "LOESCH_KZ", "UST_IDNR"])
    for i in range(400):
        city, plz, iso, land = rnd.choice(CITIES)
        name = f"{rnd.choice(NAMES)} {rnd.choice(SUFFIX)}"
        w.writerow([1000000 + i, name, rnd.choice(["", "", "Abt. Einkauf"]), rnd.choice(["Hauptstraße", "Bahnhofstr.", "Gartenweg"]), str(rnd.randint(1, 120)), city, plz, land, iso if iso != "CH" else "DE",
                    f"info{i}@{name.split()[0].lower()}.example", f"+49 30 {rnd.randint(1000000, 9999999)}", rnd.choice(TERMS_DE), rnd.choice(["EUR", "EUR", "CHF"]), rnd.choice(["", "", "", "X"]),
                    f"{iso}{rnd.randint(100000000, 999999999)}"])
    return buf.getvalue().encode("utf-8")


def nested() -> bytes:
    rnd = random.Random(11)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["_id", "name", "address.street", "address.city", "address.zip", "address.country", "contact.email", "contact.phone", "terms", "tax_id"])
    for i in range(400):
        city, plz, iso, land = rnd.choice(CITIES)
        name = f"{rnd.choice(NAMES)} {rnd.choice(SUFFIX)}"
        w.writerow([f"doc{i:05d}", name, f"{rnd.choice(['Hauptstraße', 'Gartenweg'])} {rnd.randint(1, 99)}", city, plz, rnd.choice([iso, land]), f"kontakt{i}@{name.split()[0].lower()}.example",
                    f"+49 30 {rnd.randint(1000000, 9999999)}", rnd.choice(["NET30", "NET60", "COD", "2/10 NET 30"]), f"{iso}{rnd.randint(100000000, 999999999)}"])
    return buf.getvalue().encode("utf-8")


def source(kind: str) -> bytes:
    return {"english": lambda: (ROOT / "mock_data" / "legacy_ar_customers.csv").read_bytes(), "german": german, "nested": nested}[kind]()


def land(kind: str) -> int:
    r = api.post("/sources/register", data={"system_name": f"CMP_{kind.upper()}", "system_type": "FLAT_FILE", "table_name": f"CUSTOMERS_{kind.upper()}", "primary_key_columns": KEYCOL[kind]},
                 files={"file": (f"{kind}.csv", source(kind), "text/csv")})
    r.raise_for_status()
    tid = r.json()["table_id"]
    api.post(f"/profile/{tid}").raise_for_status()
    return tid


def generate(tid: int) -> tuple[dict, float]:
    started = time.time()
    job = api.post(f"/mapping/generate/{tid}", params={"background": "true", "consistency_runs": args.runs}).json()
    while time.time() - started < 3600:
        j = api.get(f"/mapping/jobs/{job['job_id']}").json()
        if j["status"] not in ("queued", "running", "cancelling"):
            if j["status"] != "succeeded":
                raise SystemExit(f"AI mapping job {j['status']}: {j.get('error') or j['progress'].get('message')}")
            return api.get(f"/mapping/{j['result']['mapping_set_id']}").json(), time.time() - started
        time.sleep(3)
    raise SystemExit("timed out")


def judge(kind: str, detail: dict, seconds: float) -> dict:
    by_col: dict[str, list[dict]] = {}
    for r in detail["rules"]:
        if r["source_column"]:
            by_col.setdefault(r["source_column"], []).append(r)
    tally = dict(right=0, wrong=0, missed=0, wrong_flagged=0, wrong_unflagged=0, kind_ok=0, kind_wrong=0, kind_wrong_flagged=0)
    conf_right: list[float] = []
    conf_wrong: list[float] = []
    print(f"\n=== {kind}: {len(KEYS[kind])} columns, AI took {seconds:.0f} s ===", flush=True)
    print(f"  {'column':16} {'AI chose':26} {'conf':>5}  {'rule kind':14} verdict", flush=True)
    for col, (want, want_kind) in KEYS[kind].items():
        rules = by_col.get(col, [])
        if not rules:
            print(f"  {col:16} {'(nothing)':26} {'':>5}  {'':14} {'MISSED' if want else 'ok (left unmapped)'}", flush=True)
            if want:
                tally["missed"] += 1
            continue
        r = rules[0]
        have = f"{r['target_table']}.{r['target_column']}"
        conf = r["confidence"] if r["confidence"] is not None else 0.0
        flag = " [needs review]" if r["needs_review"] else ""
        if want is None:
            print(f"  {col:16} {have:26} {conf:>5.0%}  {r['rule_type']:14} arguable, not scored{flag}", flush=True)
            continue
        if have == want:
            tally["right"] += 1
            conf_right.append(conf)
            verdict = "right"
        else:
            tally["wrong"] += 1
            conf_wrong.append(conf)
            tally["wrong_flagged" if r["needs_review"] else "wrong_unflagged"] += 1
            verdict = f"WRONG (should be {want})"
        if have == want and want_kind == LOOKUP:
            if r["rule_type"] == LOOKUP:
                tally["kind_ok"] += 1
            else:
                tally["kind_wrong"] += 1
                tally["kind_wrong_flagged"] += 1 if r["needs_review"] else 0
                verdict += " but a plain COPY where a lookup is needed"
        print(f"  {col:16} {have:26} {conf:>5.0%}  {r['rule_type']:14} {verdict}{flag}", flush=True)
    scored = tally["right"] + tally["wrong"] + tally["missed"]
    print(f"  -> field right {tally['right']} of {scored}; wrong {tally['wrong']} (flagged for review {tally['wrong_flagged']}, NOT flagged {tally['wrong_unflagged']}); missed {tally['missed']}", flush=True)
    needs = sum(1 for v in KEYS[kind].values() if v[1] == LOOKUP)
    print(f"  -> columns that need a lookup: {needs}; proposed as a lookup {tally['kind_ok']}, as a plain copy {tally['kind_wrong']} (of which flagged {tally['kind_wrong_flagged']})", flush=True)
    if conf_right and conf_wrong:
        print(f"  -> confidence: right ones {min(conf_right):.0%}-{max(conf_right):.0%} (mean {sum(conf_right) / len(conf_right):.0%}); wrong ones {min(conf_wrong):.0%}-{max(conf_wrong):.0%}", flush=True)
    extra = [(c, f"{rs[0]['target_table']}.{rs[0]['target_column']}") for c, rs in by_col.items() if c not in KEYS[kind]]
    if extra:
        print(f"  -> mapped columns that are not in the answer key: {extra}", flush=True)
    return tally | {"scored": scored}


def main() -> None:
    health = api.get("/health").json()
    print(f"AI provider: {health['llm_provider']}; consistency runs: {args.runs}", flush=True)
    api.post("/admin/seed-sap-catalog")
    total: dict[str, int] = {}
    for kind in args.only.split(","):
        detail, seconds = generate(land(kind))
        for k, v in judge(kind, detail, seconds).items():
            total[k] = total.get(k, 0) + v
    print(f"\nALL: field right {total['right']} of {total['scored']}, wrong {total['wrong']} (flagged {total['wrong_flagged']}, not flagged {total['wrong_unflagged']}), missed {total['missed']}")
    sys.exit(0)


if __name__ == "__main__":
    main()
