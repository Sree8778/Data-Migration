"""Generate a realistic, deliberately dirty legacy Oracle customer extract (AR_CUSTOMERS) for testing.

    python scripts/generate_mock_data.py --rows 2000 --out mock_data

Writes <out>/legacy_ar_customers.csv and <out>/answer_key.json. The answer key lists exactly what was
injected (blank cities, unknown countries, duplicates...), so the app's profiling / validation /
deduplication results can be compared with the truth. Deterministic for a given --seed.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path

HEADER = ["CUST_ID", "CUST_NAME", "TAX_ID", "EMAIL", "PHONE", "ADDR_STREET", "CITY", "COUNTRY", "POSTAL_CODE", "PAYMENT_TERMS"]

SYLLABLES = ["ka", "lo", "mi", "ra", "ve", "tu", "zo", "pe", "xi", "da", "no", "su", "be", "fa", "gi", "ho", "ju", "li", "ma", "ne"]
INDUSTRY = ["Logistics", "Systems", "Foods", "Motors", "Textiles", "Analytics", "Pharma", "Energy", "Labs", "Industries", "Trading", "Software"]
LEGAL = {"US": ["Inc", "LLC", "Corp"], "DE": ["GmbH", "AG"], "FR": ["SA", "SAS"], "GB": ["Ltd", "PLC"], "IN": ["Pvt Ltd", "Ltd"],
         "CA": ["Inc", "Ltd"], "JP": ["KK", "Co Ltd"], "ES": ["SL", "SA"]}
# country -> (written variants, cities, postal generator)
COUNTRIES = {
    "US": (["US", "USA", "United States", "usa"], ["Boston", "Austin", "Denver", "Seattle", "Chicago", "Miami", "Phoenix"], "us"),
    "DE": (["DE", "Germany", "Deutschland"], ["Berlin", "Munich", "Hamburg", "Cologne"], "5"),
    "FR": (["FR", "France"], ["Paris", "Lyon", "Marseille", "Toulouse"], "5"),
    "GB": (["GB", "UK", "United Kingdom", "Great Britain"], ["London", "Leeds", "Bristol", "Glasgow"], "gb"),
    "IN": (["IN", "India"], ["Pune", "Mumbai", "Chennai", "Delhi"], "6"),
    "CA": (["CA", "Canada"], ["Toronto", "Calgary", "Ottawa", "Montreal"], "ca"),
    "JP": (["Japan"], ["Tokyo", "Osaka", "Nagoya"], "jp"),   # not in the demo lookup seed -> LOOKUP_FAILED until a steward adds it
    "ES": (["Spain"], ["Madrid", "Valencia", "Seville"], "5"),
}
WEIGHTS = {"US": 38, "DE": 16, "FR": 9, "GB": 11, "IN": 9, "CA": 8, "JP": 5, "ES": 4}
STREETS = ["Oak", "Maple", "Cedar", "Pine", "Elm", "Birch", "Lake", "Hill", "River", "Sunset", "Market", "Station", "Park", "Mill"]
SUFFIX = ["Street", "Avenue", "Road", "Lane", "Boulevard"]
PAYMENT = ["NET30", "Net 30", "NET60", "NET 60", "COD", "NET90", ""]


def postal(rng: random.Random, kind: str, leading_zero: bool) -> str:
    if kind == "us":
        return f"0{rng.randint(1000, 9999)}" if leading_zero else str(rng.randint(10000, 99999))
    if kind == "5":
        return str(rng.randint(10000, 99999))
    if kind == "6":
        return str(rng.randint(100000, 999999))
    if kind == "gb":
        return f"{rng.choice(['SW', 'LS', 'BS', 'G'])}{rng.randint(1, 9)} {rng.randint(1, 9)}{rng.choice('ABDEFGH')}{rng.choice('ABDEFGH')}"
    if kind == "ca":
        return f"{rng.choice('KMTV')}{rng.randint(1, 9)}{rng.choice('ABCEGH')} {rng.randint(1, 9)}{rng.choice('ABCEGH')}{rng.randint(1, 9)}"
    return f"{rng.randint(100, 999)}-{rng.randint(1000, 9999)}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=2000)
    ap.add_argument("--out", default="mock_data")
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    names: set[str] = set()
    rows: list[dict] = []
    key = {"rows_requested": args.rows, "blank_city": 0, "blank_country": 0, "unknown_country_text": 0, "countries_missing_from_seed_lookup": 0,
           "name_over_40_chars": 0, "blank_postal": 0, "leading_zero_postal": 0, "unknown_payment_terms": 0, "blank_payment_terms": 0,
           "padded_names": 0, "exact_duplicate_rows": 0, "tax_id_duplicates": 0, "email_duplicates": 0, "fuzzy_name_pairs": 0}

    def make_company(country: str) -> tuple[str, str]:
        while True:
            stem = "".join(rng.choice(SYLLABLES) for _ in range(rng.choice([3, 4]))).capitalize()
            name = f"{stem} {rng.choice(INDUSTRY)} {rng.choice(LEGAL[country])}"
            if name not in names:
                names.add(name)
                return name, stem

    n_base = args.rows - 120
    for i in range(n_base):
        country = rng.choices(list(WEIGHTS), weights=list(WEIGHTS.values()))[0]
        variants, cities, pkind = COUNTRIES[country]
        name, stem = make_company(country)
        if rng.random() < 0.01:
            name = f"{stem} International Manufacturing and Distribution Holdings {rng.choice(LEGAL[country])}"
            key["name_over_40_chars"] += 1
        if rng.random() < 0.03:
            name = f"  {name} "
            key["padded_names"] += 1
        city = rng.choice(cities)
        if rng.random() < 0.03:
            city = ""
            key["blank_city"] += 1
        written = rng.choice(variants)
        if rng.random() < 0.02:
            written = ""
            key["blank_country"] += 1
        elif rng.random() < 0.02:
            written = rng.choice(["Narnia", "Atlantis", "Wakanda"])
            key["unknown_country_text"] += 1
        elif country in ("JP", "ES"):
            key["countries_missing_from_seed_lookup"] += 1
        code = ""
        if rng.random() < 0.05:
            key["blank_postal"] += 1
        else:
            lz = country == "US" and rng.random() < 0.08
            code = postal(rng, pkind, lz)
            key["leading_zero_postal"] += lz
        pay = rng.choice(PAYMENT)
        if rng.random() < 0.02:
            pay = "WHEN-RECEIVED"
            key["unknown_payment_terms"] += 1
        key["blank_payment_terms"] += pay == ""
        slug = stem.lower()
        rows.append({
            "CUST_ID": str(100000 + i), "CUST_NAME": name,
            "TAX_ID": "" if rng.random() < 0.25 else f"{country}{rng.randint(10**8, 10**9 - 1)}",
            "EMAIL": "" if rng.random() < 0.2 else f"accounts@{slug}{i}.example.com",
            "PHONE": "" if rng.random() < 0.15 else f"+{rng.choice([1, 49, 33, 44, 91])}-{rng.randint(100, 999)}-{rng.randint(1000000, 9999999)}",
            "ADDR_STREET": f"{rng.randint(1, 999)} {rng.choice(STREETS)} {rng.choice(SUFFIX)} {i}", "CITY": city,
            "COUNTRY": written, "POSTAL_CODE": code, "PAYMENT_TERMS": pay,
        })

    # --- injected duplicates -------------------------------------------------------------------
    clean = [r for r in rows if r["TAX_ID"] and r["EMAIL"] and r["CITY"] and r["COUNTRY"].strip().lower() not in ("", "narnia", "atlantis", "wakanda")
             and len(r["CUST_NAME"].strip()) <= 40 and r["PHONE"] and r["POSTAL_CODE"]]
    rng.shuffle(clean)
    next_id = 100000 + n_base
    for orig in clean[:40]:  # same tax id written differently, thinner record at another address
        rows.append({**orig, "CUST_ID": str(next_id), "TAX_ID": orig["TAX_ID"].lower().replace(orig["TAX_ID"][:2], orig["TAX_ID"][:2] + "-", 1),
                     "EMAIL": "", "PHONE": "", "ADDR_STREET": f"{rng.randint(1, 99)} Other Lane {next_id}", "POSTAL_CODE": ""})
        next_id += 1
        key["tax_id_duplicates"] += 1
    for orig in clean[40:60]:  # same e-mail, different spelling of the company
        rows.append({**orig, "CUST_ID": str(next_id), "CUST_NAME": f"Zeta {next_id} Partners", "TAX_ID": "", "EMAIL": f" {orig['EMAIL'].upper()} ",
                     "PHONE": "", "ADDR_STREET": f"{rng.randint(1, 99)} Side Street {next_id}"})
        next_id += 1
        key["email_duplicates"] += 1
    for orig in clean[60:80]:  # near-identical company name at the same address (fuzzy duplicate)
        parts = orig["CUST_NAME"].strip().split(" ")
        variant = " ".join(parts[:-1] + [{"Inc": "Incorporated", "LLC": "L.L.C.", "Corp": "Corporation", "GmbH": "GMBH", "AG": "A.G.",
                                         "SA": "S.A.", "SAS": "S.A.S.", "Ltd": "Limited", "PLC": "P.L.C.", "Pvt Ltd": "Private Limited",
                                         "KK": "K.K.", "Co Ltd": "Company Limited", "SL": "S.L."}.get(parts[-1], parts[-1])])
        rows.append({**orig, "CUST_ID": str(next_id), "CUST_NAME": variant.upper(), "TAX_ID": "", "EMAIL": "", "PHONE": ""})
        next_id += 1
        key["fuzzy_name_pairs"] += 1
    for orig in clean[80:100]:  # exact duplicate record under a new key
        rows.append({**orig, "CUST_ID": str(next_id)})
        next_id += 1
        key["exact_duplicate_rows"] += 1
    rng.shuffle(rows)
    # keep legacy ids ascending after the shuffle, like a real extract ordered by key
    rows.sort(key=lambda r: int(r["CUST_ID"]))

    csv_path = out / "legacy_ar_customers.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)
    key["rows_written"] = len(rows)
    key["note"] = ("duplicate counts are injected pairs; blank/unknown counts are for the base rows. BU_GROUP, AKONT and KNVV.WAERS "
                   "do not exist in this source at all (mandatory SAP fields that must be constructed).")
    (out / "answer_key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    print(f"wrote {len(rows)} rows to {csv_path}")
    print(json.dumps(key, indent=2))


if __name__ == "__main__":
    main()
