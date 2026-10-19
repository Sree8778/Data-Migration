"""Generate a COMPLEX legacy SAP-ECC-style customer master (KNA1-like) with a precise answer key.

    python scripts/generate_complex_data.py --rows 15000 --out mock_data/complex --seed 42

Writes customers_complex.csv (cp1252, semicolon-delimited, quoted multi-line free text, 27 cryptic columns) and
answer_key.json. The mess is deliberate and mixed: persons and organisations, 13 countries written in several
languages, 5 date formats, 4 number locales, leading zeros, non-ASCII names, over-long names, deleted/test records,
invalid e-mails, unknown lookup values, blank mandatory fields, look-alike entities that must NOT merge, and
about 1,000 duplicates of 8 different kinds. Every defect is recomputed from the FINAL row values, so the answer
key can never drift from the file.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
from pathlib import Path

from rapidfuzz import fuzz, process

HEADER = ["KUNNR", "KTOKD", "NAME1", "NAME2", "SORTL", "STRAS", "ORT01", "PSTLZ", "LAND1", "REGIO", "SPRAS", "STCD1", "STCEG", "SMTP_ADDR",
          "TELF1", "TELF2", "ZTERM", "WAERS", "INCO1", "VKORG", "VTWEG", "SPART", "AKONT", "KLIMK", "ERDAT", "LOEVM", "NOTES"]

# ISO -> (written variants that the migration team HAS lookups for, cities, language key variants, currency, postal kind, street word)
COUNTRIES = {
    "DE": (["DE", "Germany", "Deutschland", "GERMANY", "de"], ["Berlin", "München", "Hamburg", "Köln", "Dresden", "Düsseldorf"], ["D", "DE", "Deutsch", "German"], "EUR", "5", "Straße"),
    "US": (["US", "USA", "United States", "U.S.A", "usa"], ["Boston", "Austin", "Denver", "Seattle", "Chicago"], ["E", "EN", "English", "Englisch"], "USD", "us", "Street"),
    "FR": (["FR", "France", "FRANCE", "Frankreich"], ["Paris", "Lyon", "Marseille", "Toulouse"], ["F", "FR", "Français", "French"], "EUR", "5", "Rue"),
    "GB": (["GB", "UK", "United Kingdom", "Great Britain", "Grossbritannien"], ["London", "Leeds", "Bristol", "Glasgow"], ["E", "EN", "English"], "GBP", "gb", "Road"),
    "ES": (["ES", "Spain", "España", "Spanien"], ["Madrid", "Valencia", "Sevilla", "Bilbao"], ["S", "ES", "Español", "Spanish"], "EUR", "5", "Calle"),
    "IT": (["IT", "Italy", "Italia", "Italien"], ["Roma", "Milano", "Torino", "Napoli"], ["I", "IT", "Italiano", "Italian"], "EUR", "5", "Via"),
    "NL": (["NL", "Netherlands", "Nederland", "Niederlande"], ["Amsterdam", "Rotterdam", "Utrecht"], ["N", "NL", "Nederlands", "Dutch"], "EUR", "nl", "Straat"),
    "CH": (["CH", "Switzerland", "Schweiz", "Suisse"], ["Zürich", "Bern", "Basel", "Genève"], ["D", "DE", "F", "FR"], "CHF", "4", "Strasse"),
    "AT": (["AT", "Austria", "Österreich", "Autriche"], ["Wien", "Graz", "Linz", "Salzburg"], ["D", "DE", "Deutsch"], "EUR", "4", "Gasse"),
    "IN": (["IN", "India", "INDIA", "Indien"], ["Pune", "Mumbai", "Chennai", "Delhi"], ["E", "EN", "English", "H", "Hindi"], "INR", "6", "Road"),
    "CA": (["CA", "Canada", "CANADA", "Kanada"], ["Toronto", "Calgary", "Ottawa", "Montréal"], ["E", "EN", "F", "FR", "English"], "CAD", "ca", "Avenue"),
    "BR": (["BR", "Brazil", "Brasil", "Brasilien"], ["São Paulo", "Rio de Janeiro", "Curitiba"], ["P", "PT", "Português", "Portuguese"], "BRL", "br", "Rua"),
    "JP": (["JP", "Japan", "JAPAN", "Japón"], ["Tokyo", "Osaka", "Nagoya"], ["J", "JA", "Japanese"], "JPY", "jp", "Dori"),
}
UNKNOWN_COUNTRY = ["Deutschlnd", "Germany (DE)", "??", "EU", "Europe", "Atlantis", "UNK", "N/A", "Gemany", "Amerika"]
TERMS_KNOWN = {"NET30": "NT30", "Net 30": "NT30", "30 DAYS NET": "NT30", "60 Tage netto": "NT60", "NET60": "NT60", "NET 60": "NT60", "COD": "CASH", "Immediate": "CASH",
               "2/10 NET 30": "NT3D", "NET90": "NT90"}
TERMS_UNKNOWN = ["WHEN-RECEIVED", "NET 45", "PREPAID", "Z999", "End of month +5"]
CURRENCY_VARIANTS = {"EUR": ["EUR", "EURO", "€"], "USD": ["USD", "US$", "$"], "GBP": ["GBP", "£", "STG"], "CHF": ["CHF", "SFR"], "INR": ["INR", "Rs"],
                     "CAD": ["CAD", "C$"], "BRL": ["BRL", "R$"], "JPY": ["JPY", "YEN", "¥"]}
CURRENCY_UNKNOWN = ["DM", "FRF", "ECU"]
SURNAMES = ["Müller", "Schröder", "Schäfer", "Böhm", "Núñez", "Çelik", "Åberg", "Søren", "Dupont", "Fernández", "Rossi", "Jansen", "Weiß", "Köhler", "O'Brien"]
SYL = ["ka", "lo", "mi", "ra", "ve", "tu", "zo", "pe", "xi", "da", "no", "su", "be", "fa", "gi", "ho", "ju", "li", "ma", "ne", "qua", "bro", "sty", "gre"]
INDUSTRY = ["Logistics", "Systems", "Foods", "Motors", "Textiles", "Analytics", "Pharma", "Energy", "Labs", "Industries", "Trading", "Software", "Maschinenbau", "Chemie"]
LEGAL = {"DE": ["GmbH", "AG", "KG", "GmbH & Co. KG"], "US": ["Inc", "LLC", "Corp"], "FR": ["SA", "SAS", "SARL"], "GB": ["Ltd", "PLC"], "ES": ["SL", "SA"], "IT": ["SpA", "Srl"],
         "NL": ["BV", "NV"], "CH": ["AG", "GmbH", "SA"], "AT": ["GmbH", "AG"], "IN": ["Pvt Ltd", "Ltd"], "CA": ["Inc", "Ltd"], "BR": ["Ltda", "SA"], "JP": ["KK", "Co Ltd"]}
FIRST = ["Hans", "Anna", "Peter", "Maria", "Klaus", "Sofia", "Luca", "Emma", "Jonas", "Chloé", "Mateo", "Zoë"]
STEMS = ["Haupt", "Bahnhof", "Garten", "Schul", "Kirch", "Berg", "Linden", "Oak", "Maple", "Cedar", "Elm", "Market", "Station", "Park", "Mill", "Rose"]
BU_GROUPS = {"Z001": "BP01", "Z002": "BP02", "ZPER": "BP03"}
UNKNOWN_KTOKD = ["Z999"]
TEST_NAMES = ["TEST CUSTOMER", "DUMMY", "ZZ_DO NOT USE", "TESTFIRMA", "DELETE ME"]


def postal(rng, kind, leading_zero=False):
    if kind == "5":
        return f"0{rng.randrange(1000, 9999)}" if leading_zero else f"{rng.randrange(10000, 99999)}"
    if kind == "us":
        return f"0{rng.randrange(1000, 9999)}" if leading_zero else (f"{rng.randrange(10000, 99999)}" if rng.random() < .8 else f"{rng.randrange(10000, 99999)}-{rng.randrange(1000, 9999)}")
    return {"4": lambda: f"{rng.randrange(1000, 9999)}", "6": lambda: f"{rng.randrange(100000, 999999)}",
            "gb": lambda: f"{rng.choice(['SW1A', 'LS1', 'BS1', 'G1'])} {rng.randrange(1, 9)}{rng.choice('ABDEF')}{rng.choice('ABDEF')}",
            "nl": lambda: f"{rng.randrange(1000, 9999)} {rng.choice('ABCDEF')}{rng.choice('ABCDEF')}",
            "ca": lambda: f"{rng.choice('MKLV')}{rng.randrange(1, 9)}{rng.choice('ABCEG')} {rng.randrange(1, 9)}{rng.choice('ABCEG')}{rng.randrange(1, 9)}",
            "br": lambda: f"{rng.randrange(10000, 99999)}-{rng.randrange(100, 999)}", "jp": lambda: f"{rng.randrange(100, 999)}-{rng.randrange(1000, 9999)}"}[kind]()


class Maker:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.buckets: dict[str, list[str]] = {}
        self.n = 0
        self.dup_n = 0

    def _distinct(self, name):
        bucket = self.buckets.setdefault(name[:2].lower(), [])
        if bucket and process.extractOne(name, bucket, scorer=fuzz.ratio, score_cutoff=82) is not None:   # same test as '>= 82', but rapidfuzz can stop early
            return False
        bucket.append(name)
        return True

    def company_name(self, iso):
        r = self.rng
        while True:
            if r.random() < .35:
                base = f"{r.choice(SURNAMES)}-{''.join(r.choice(SYL) for _ in range(2)).capitalize()} {r.choice(INDUSTRY)}"
            else:
                base = "".join(r.choice(SYL) for _ in range(r.choice((3, 3, 4)))).capitalize() + " " + r.choice(INDUSTRY)
            if self._distinct(base):
                return f"{base} {r.choice(LEGAL[iso])}"

    def person_name(self):
        r = self.rng
        while True:
            last = "".join(r.choice(SYL) for _ in range(r.choice((3, 4)))).capitalize()
            if r.random() < .35:
                last = last.replace("a", "ä", 1) if r.random() < .5 else last.replace("o", "ö", 1)
            last, first = last + r.choice(["", "", "mann", "er", "sen"]), r.choice(FIRST)
            name = f"{last}, {first}"
            if self._distinct(name):
                return name

    def entity(self):
        r = self.rng
        self.n += 1
        iso = r.choices(list(COUNTRIES), weights=[22, 18, 9, 9, 5, 5, 4, 4, 3, 6, 5, 6, 4])[0]
        variants, cities, langs, cur, pkind, street_word = COUNTRIES[iso]
        person = r.random() < .10
        name = self.person_name() if person else self.company_name(iso)
        slug = re.sub(r"[^a-z0-9]", "", name.lower().split()[0].split(",")[0]) or "x"
        fmt = r.choice(["{c}{n}", "{c} {n}", "{c}-{n}"])
        vat = f"{iso}{r.randrange(10**8, 10**9)}"
        row = {
            "KUNNR": f"{100000 + self.n:010d}", "KTOKD": "ZPER" if person else ("Z001" if iso == "DE" else "Z002"),
            "NAME1": name, "NAME2": r.choice(["", "", "", "c/o Finance Dept", "Attn: Accounts Payable", "Zentraleinkauf", "Div. 4"]),
            "SORTL": re.sub(r"[^A-Z0-9]", "", name.upper())[:10],
            "STRAS": f"{r.choice(STEMS)} {street_word} {r.randrange(1, 300)}{r.choice(['', '', 'a', 'b'])}",
            "ORT01": r.choice(cities), "PSTLZ": postal(r, pkind, leading_zero=r.random() < .12 and pkind in ("5", "us")),
            "LAND1": r.choice(variants), "REGIO": r.choice(["", "", "NW", "BY", "CA", "TX", "ON", "MH"]), "SPRAS": r.choice(langs),
            "STCD1": "" if r.random() < .30 else fmt.format(c=iso, n=vat[2:]),
            "STCEG": "" if r.random() < .45 else vat,
            "SMTP_ADDR": "" if r.random() < .15 else f"accounts@{slug}{self.n}.example.com",
            "TELF1": "" if r.random() < .20 else f"+{r.randrange(1, 99)} {r.randrange(100, 999)} {r.randrange(1000000, 9999999)}",
            "TELF2": "" if r.random() < .65 else f"0{r.randrange(100, 999)}/{r.randrange(10000, 99999)}",
            "ZTERM": "" if r.random() < .08 else r.choice(list(TERMS_KNOWN)), "WAERS": r.choice(CURRENCY_VARIANTS[cur]),
            "INCO1": r.choice(["", "", "EXW", "FOB", "CIF", "DAP"]), "VKORG": r.choice(["1000", "2000"]), "VTWEG": r.choice(["10", "20"]),
            "SPART": r.choice(["00", "01"]), "AKONT": r.choice(["0000140000", "0000141000"]),
            "KLIMK": self.money(), "ERDAT": self.date(), "LOEVM": "", "NOTES": "",
        }
        if r.random() < .08:
            row["NOTES"] = r.choice(['Payment via "SEPA"; contact: accounts', "Line one\nLine two (do not call)", 'Customer says "pay on 1st"; ask Hans', "Migrated; see ticket #4711"])
        return row

    def money(self):
        r = self.rng
        if r.random() < .15:
            return ""
        v = r.randrange(500, 900000) + r.choice([0, .5, .25, .99])
        kind = r.randrange(4)
        return [f"{v:,.2f}", f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), f"{int(v)}", f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")][kind]

    def date(self):
        r = self.rng
        y, m, d = r.randrange(1998, 2024), r.randrange(1, 13), r.randrange(1, 29)
        return [f"{y}-{m:02d}-{d:02d}", f"{d:02d}.{m:02d}.{y}", f"{m:02d}/{d:02d}/{y}", f"{y}{m:02d}{d:02d}", f"{d}-{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][m-1]}-{y}"][r.randrange(5)]

    def new_id(self):
        self.dup_n += 1
        return f"{900000 + self.dup_n:010d}"


# ---- duplicate variants
def d_exact(mk, e):
    return {**e, "KUNNR": mk.new_id()}


def d_tax(mk, e):  # a different-looking record (renamed, moved) that carries the same VAT number written differently
    other = mk.entity(); other["KUNNR"] = mk.new_id()
    other.update(SMTP_ADDR="", TELF1="", TELF2="", STCD1="", STCEG=e["STCEG"][:2].lower() + " " + e["STCEG"][2:5] + "." + e["STCEG"][5:])
    return other


def d_suffix(mk, e):
    head, _, legal = e["NAME1"].rpartition(" ")
    return {**e, "KUNNR": mk.new_id(), "NAME1": f"{head}, {legal}."}


def d_case(mk, e):
    return {**e, "KUNNR": mk.new_id(), "NAME1": "  " + e["NAME1"].upper().replace(" ", "  ") + " "}


def d_email(mk, e):
    other = mk.entity(); other["KUNNR"] = mk.new_id(); other.update(STCD1="", STCEG="", TELF1="", TELF2="", SMTP_ADDR=" " + e["SMTP_ADDR"].upper())
    return other


def d_phone(mk, e):
    d = re.sub(r"\D", "", e["TELF1"]); other = mk.entity(); other["KUNNR"] = mk.new_id()
    other.update(STCD1="", STCEG="", SMTP_ADDR="", TELF2="", TELF1=f"({d[:2]}) {d[2:5]}-{d[5:]}")
    return other


def d_typo_postal(mk, e):
    n = e["NAME1"]
    return {**e, "KUNNR": mk.new_id(), "NAME1": n[:4] + n[5:], "PSTLZ": ""}


def d_street(mk, e):
    s = e["STRAS"].replace("Street", "St").replace("Straße", "Str.").replace("Strasse", "Str.").replace("Road", "Rd").replace("Avenue", "Ave")
    return {**e, "KUNNR": mk.new_id(), "STRAS": s}


def defect_tags(row):
    """Recompute from the final values what the migration should flag. (Key = what a correct pipeline does.)"""
    t = []
    known_country = {v.strip().upper() for vs in COUNTRIES.values() for v in vs[0]}
    if not row["ORT01"].strip():
        t.append("blank_city")
    if not row["LAND1"].strip():
        t.append("blank_country")
    elif row["LAND1"].strip().upper() not in known_country:
        t.append("unknown_country")
    if row["ZTERM"].strip() and row["ZTERM"].strip().upper() not in {k.upper() for k in TERMS_KNOWN}:
        t.append("unknown_terms")
    if len(row["NAME1"].strip()) > 40:
        t.append("name_over_40")
    if not row["NAME1"].strip():
        t.append("blank_name")
    if row["LOEVM"].strip():
        t.append("deleted_flag")
    if any(w in row["NAME1"].upper() for w in ("TEST", "DUMMY", "DO NOT USE", "DELETE ME")):
        t.append("test_record")
    if row["SMTP_ADDR"].strip() and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", row["SMTP_ADDR"].strip()):
        t.append("invalid_email")
    if row["KTOKD"] == "ZPER":
        t.append("person")
    if row["PSTLZ"].strip().startswith("0") and row["PSTLZ"].strip().isdigit():
        t.append("postal_leading_zero")
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=15000)
    ap.add_argument("--out", default="mock_data/complex")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    mk = Maker(args.seed)
    r = mk.rng
    rows = [mk.entity() for _ in range(args.rows)]
    base = list(rows)

    # defects injected on top of the natural mess (applied before duplicates are cloned, so clones inherit them)
    def pick(p):
        return [e for e in base if r.random() < p]
    for e in pick(.03): e["ORT01"] = ""
    for e in pick(.02): e["LAND1"] = ""
    for e in pick(.02): e["LAND1"] = r.choice(UNKNOWN_COUNTRY)
    for e in pick(.025): e["ZTERM"] = r.choice(TERMS_UNKNOWN)
    for e in pick(.02): e["NAME1"] = e["NAME1"] + " " + r.choice(["Gesellschaft für Industrielle Beteiligungen", "International Holdings and Services", "Verwaltungs- und Beteiligungsgesellschaft"])
    for e in pick(.03): e["LOEVM"] = "X"
    for e in pick(.005): e["NAME1"] = r.choice(TEST_NAMES) + f" {r.randrange(1, 99)}"
    for e in pick(.02): e["SMTP_ADDR"] = r.choice(["john.doe@", "no email", "a@b", "accounts at firm.com", "x@@y.com"])
    for e in pick(.02): e["WAERS"] = r.choice(CURRENCY_UNKNOWN)
    for e in pick(.04): e["PSTLZ"] = ""
    for e in pick(.01): e["KTOKD"] = "Z999"

    groups, distinct_pairs = [], []
    pool = [e for e in base if e["KTOKD"] != "ZPER" and "test_record" not in defect_tags(e)]
    r.shuffle(pool)
    for cat, fn, count, need in [("exact-copy", d_exact, 150, None), ("tax-id-reformatted", d_tax, 150, "STCEG"), ("legal-suffix-punctuation", d_suffix, 200, None),
                                 ("case-and-spacing", d_case, 100, None), ("email-only", d_email, 100, "SMTP_ADDR"), ("phone-only", d_phone, 100, "TELF1"),
                                 ("typo-plus-postal-blank", d_typo_postal, 100, None), ("street-abbreviated", d_street, 100, None)]:
        made = 0
        while made < count:
            o = pool.pop()
            if need and not o[need].strip():
                continue
            if cat in ("email-only",) and "invalid_email" in defect_tags(o):
                continue
            dup = fn(mk, o)
            rows.append(dup)
            groups.append({"orig": o["KUNNR"], "dup": dup["KUNNR"], "category": cat})
            made += 1
    # distinct look-alikes that must never merge
    for kind in ("plant", "branch", "house"):
        for _ in range(100):
            a = mk.entity(); a.update(STCD1="", STCEG="", SMTP_ADDR="", TELF1="", TELF2="", ORT01=a["ORT01"] or "Berlin", PSTLZ=a["PSTLZ"] or "10115", ZTERM="NET30", LAND1="DE")
            a["NAME1"] = re.sub(r"[^\w ,&.-]", "", a["NAME1"])
            if a["KTOKD"] == "ZPER":
                a["KTOKD"] = "Z001"; a["NAME1"] = mk.company_name("DE")
            b = {**a, "KUNNR": mk.new_id()}
            if kind == "plant":
                head, _, legal = a["NAME1"].rpartition(" "); a["NAME1"], b["NAME1"] = f"{head} Plant 1 {legal}", f"{head} Plant 2 {legal}"
            elif kind == "branch":
                o = mk.entity(); b.update(STRAS=o["STRAS"], ORT01=o["ORT01"], PSTLZ=o["PSTLZ"] or "80331", LAND1=o["LAND1"], REGIO=o["REGIO"])
            else:
                num = re.search(r"\d+", a["STRAS"]); b["STRAS"] = a["STRAS"].replace(num.group(), str(int(num.group()) + 2), 1)
            rows += [a, b]
            distinct_pairs.append([a["KUNNR"], b["KUNNR"]])
    r.shuffle(rows)

    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER, delimiter=";", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writeheader(); w.writerows(rows)
    (out / "customers_complex.csv").write_bytes(buf.getvalue().encode("cp1252"))

    tags: dict[str, list[str]] = {}
    for row in rows:
        for t in defect_tags(row):
            tags.setdefault(t, []).append(row["KUNNR"])
    blanks = {c: sum(1 for row in rows if not row[c].strip()) for c in HEADER}
    key = {
        "rows": len(rows), "base_entities": len(base), "columns": HEADER, "blank_counts": blanks,
        "tag_counts": {t: len(v) for t, v in tags.items()}, "tags": tags, "duplicate_groups": groups, "distinct_pairs": distinct_pairs,
        "expected_mapping": {"KUNNR": "BUT000.BP_EXT", "KTOKD": "BUT000.BU_GROUP", "NAME1": "BUT000.NAME_ORG1", "NAME2": "BUT000.NAME_ORG2", "SORTL": "BUT000.BU_SORT1",
                             "STRAS": "ADRC.STREET", "ORT01": "ADRC.CITY1", "PSTLZ": "ADRC.POST_CODE1", "LAND1": "ADRC.COUNTRY", "REGIO": "ADRC.REGION", "SPRAS": "ADRC.LANGU",
                             "ZTERM": "KNB1.ZTERM", "AKONT": "KNB1.AKONT", "WAERS": "KNVV.WAERS", "INCO1": "KNVV.INCO1", "VKORG": "KNVV.VKORG", "VTWEG": "KNVV.VTWEG", "SPART": "KNVV.SPART"},
        "no_target_in_catalog": ["STCD1", "STCEG", "SMTP_ADDR", "TELF1", "TELF2", "KLIMK", "ERDAT", "LOEVM", "NOTES"],
        "lookups": {
            "country": {v: iso for iso, (vs, *_rest) in COUNTRIES.items() for v in vs},
            "terms": TERMS_KNOWN, "bu_group": BU_GROUPS,
            "currency": {v: iso for iso, vs in CURRENCY_VARIANTS.items() for v in vs},
        },
    }
    (out / "answer_key.json").write_text(json.dumps(key, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(rows)} rows ({len(base)} base + {len(groups)} duplicates + {2 * len(distinct_pairs)} look-alike rows) -> {out}")
    print({t: len(v) for t, v in tags.items()})
    print("injected duplicates:", {c: sum(1 for g in groups if g["category"] == c) for c in {g["category"] for g in groups}})


if __name__ == "__main__":
    main()
