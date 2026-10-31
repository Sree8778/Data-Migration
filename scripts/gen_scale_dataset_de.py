"""A second, differently shaped customer extract for the scale test (German-style ERP export): ';' delimited, other column names, accents, mixed companies and people,
Zipf-distributed surnames (huge name blocks), duplicates that carry NO contact data (only fuzzy matching can find them), and look-alike traps.

    python gen_data3.py out.csv out.truth.json [total_rows]
"""
import csv
import json
import random
import sys
import unicodedata

OUT, TRUTH = sys.argv[1], sys.argv[2]
TOTAL = int(sys.argv[3]) if len(sys.argv) > 3 else 1_000_000
rnd = random.Random(20260102)

SURN = ("Müller Schmidt Schneider Fischer Weber Meyer Wagner Becker Schulz Hoffmann Schäfer Koch Bauer Richter Klein Wolf Schröder Neumann Schwarz Zimmermann Braun Krüger Hofmann Hartmann Lange "
        "Schmitt Werner Krause Meier Lehmann Schmid Schulze Maier Köhler Herrmann König Walter Mayer Huber Kaiser Fuchs Peters Lang Scholz Möller Weiß Jung Hahn Schubert Vogel Friedrich Keller "
        "Günther Frank Berger Winkler Roth Beck Lorenz Baumann Franke Albrecht Schuster Simon Ludwig Böhm Winter Kraus Martin Schumacher Krämer Vogt Stein Jäger Otto Sommer Groß Seidel "
        "Çelik Yılmaz Kaya Demir Nguyễn Łukasik Nowak Kowalski García Martínez Rossi Ferrari Dubois Lefèvre Smith Johnson Williams Brown Jones Miller Davis Silva Santos Oliveira Tanaka Suzuki").split()
SYL = ["bach", "berg", "burg", "dorf", "feld", "hagen", "heim", "hof", "holz", "horn", "kamp", "kopf", "mann", "meyer", "mühl", "rath", "ried", "rode", "stein", "stedt", "thal", "wald", "wege", "witz",
       "al", "an", "ar", "el", "en", "er", "ia", "ik", "in", "or", "os", "ul", "ö", "ü", "ä"]
ONSET = ["Ba", "Be", "Bo", "Br", "Da", "De", "Do", "Fa", "Fe", "Ga", "Gr", "Ha", "He", "Ho", "Ja", "Ka", "Ke", "Kl", "Ko", "La", "Le", "Lo", "Ma", "Me", "Mo", "Na", "Ne", "Pa", "Pe", "Ra", "Re", "Ro", "Sa",
         "Sch", "Se", "So", "Ta", "Te", "To", "Ul", "Wa", "We", "Wi", "Za", "Ze"]
tail = set()
while len(tail) < 3000:
    tail.add(rnd.choice(ONSET) + rnd.choice(SYL) + rnd.choice(["", "", "s", "er", "mann"]))
SURNAMES = SURN + sorted(tail)
WEIGHTS = [1.0 / (i + 4) for i in range(len(SURNAMES))]          # Zipf-like: a handful of names carry a large share
FIRST = ("Hans Peter Michael Thomas Andreas Wolfgang Klaus Jürgen Günter Stefan Christian Markus Martin Frank Bernd Uwe Jörg Dieter Anna Maria Ursula Monika Petra Sabine Susanne Karin Renate Birgit Andrea "
         "Claudia Heike Ingrid Gisela Brigitte Helga Eva Katja Nicole José Juan Carlos Luis Pierre Jean Marco Luca Giuseppe Mehmet Ahmet Mustafa Ayşe Fatma Zoë Renée Åsa Łukasz Paweł John James Robert David Sarah Emma").split()
BRANCH = ("Maschinenbau Logistik Bau Handel Elektro Metallbau Software Consulting Gastronomie Pharma Textil Transport Immobilien Versicherung Druck Verlag Kunststoff Chemie Energie Medizintechnik Optik "
          "Sanitär Heizung Landtechnik Möbel Holzbau Stahlbau Automotive Spedition Catering Reinigung Sicherheit Werbung Elektronik Feinmechanik Getränke Backwaren Fleischwaren Gartenbau Fahrzeugbau").split()
PREFIX = "Nord Süd Ost West Rhein Main Alpen Euro Inter Global Prima Neo Alt Kern Hanse Baltic Donau Elbe Ruhr Isar Spree".split()
LEGAL = [("GmbH", 40), ("AG", 8), ("KG", 6), ("GmbH & Co. KG", 12), ("e.K.", 5), ("UG", 6), ("SE", 1), ("Ltd", 4), ("Inc", 4), ("SA", 4), ("SARL", 3), ("S.r.l.", 3), ("BV", 2), ("OHG", 2)]
LEGAL_NAMES, LEGAL_W = [x for x, _ in LEGAL], [w for _, w in LEGAL]
STREET_WORDS = ("Haupt Bahnhof Garten Schul Kirch Wald Berg Linden Rosen Birken Eichen Mühlen Markt Burg Wiesen Feld Hafen Industrie Park Sonnen Lerchen Ahorn Tannen Fichten Buchen Kastanien Schloss Dorf "
                "Brunnen Gerber Post Bach Lessing Goethe Schiller Mozart Bismarck Kant Humboldt").split()
STREET_SUFFIX = ["straße"] * 6 + ["weg", "allee", "platz", "gasse", "ring"]
STREETS = sorted({w + s for w in STREET_WORDS for s in STREET_SUFFIX})
CITIES = []
for i in range(600):
    c = rnd.choices(["DE", "AT", "CH", "FR", "IT", "ES", "GB", "US", "PL", "TR"], [55, 6, 6, 8, 6, 5, 5, 5, 2, 2])[0]
    CITIES.append((rnd.choice(ONSET) + rnd.choice(SYL) + rnd.choice(["", "", "stadt", " am Main", " an der Ruhr", "ingen"]), c, 10000 + rnd.randrange(80) * 1000 + rnd.randrange(10) * 100))
CITY_W = [1.0 / (i + 6) for i in range(len(CITIES))]
CCODES = {"DE": ("DEU", "Germany", "Deutschland"), "AT": ("AUT", "Austria", "Österreich"), "CH": ("CHE", "Switzerland", "Schweiz"), "FR": ("FRA", "France", "Frankreich"), "IT": ("ITA", "Italy", "Italien"),
          "ES": ("ESP", "Spain", "Spanien"), "GB": ("GBR", "United Kingdom", "Großbritannien"), "US": ("USA", "United States", "USA"), "PL": ("POL", "Poland", "Polen"), "TR": ("TUR", "Turkey", "Türkei")}
LANG = {"DE": "DE", "AT": "DE", "CH": "DE", "FR": "FR", "IT": "IT", "ES": "ES", "GB": "EN", "US": "EN", "PL": "EN", "TR": "EN"}
CURR = {"DE": "EUR", "AT": "EUR", "CH": "CHF", "FR": "EUR", "IT": "EUR", "ES": "EUR", "GB": "GBP", "US": "USD", "PL": "PLN", "TR": "TRY"}
DIAL = {"DE": "49", "AT": "43", "CH": "41", "FR": "33", "IT": "39", "ES": "34", "GB": "44", "US": "1", "PL": "48", "TR": "90"}
TERMS = ["30 Tage netto", "60 Tage netto", "Vorkasse", "2% Skonto 10 Tage, 30 netto", ""]
TERMS_W = [40, 20, 10, 20, 10]

seq = [0]
used_email: set[str] = set()


def uniq_tax(cc: str) -> str:
    seq[0] += 1
    n = (seq[0] * 7919 + 100003) % 900_000_000 + 100_000_000      # a bijection: never the same number twice
    return f"{cc}{n}" if cc != "US" else f"{n // 10000000:02d}-{n % 10000000:07d}"


def uniq_phone(cc: str, fmt: int | None = None) -> str:
    seq[0] += 1
    n = (seq[0] * 104729 + 12345) % 9_000_000_000 + 1_000_000_000
    s = str(n)[: rnd.choice([8, 9, 10])] if fmt is None else str(n)[:9]
    return f"+{DIAL[cc]} {s[:2]} {s[2:]}"


def phone_variant(p: str) -> str:
    d = "".join(ch for ch in p if ch.isdigit())
    return rnd.choice([f"+{d}", f"+{d[:2]} ({d[2:4]}) {d[4:]}", f"+{d[:2]}-{d[2:4]}-{d[4:]}", f"{d[:2]} {d[2:]}"] if False else [f"+{d[:2]}-{d[2:4]}-{d[4:]}", f"+{d[:2]} ({d[2:4]}) {d[4:]}"])


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return "".join(ch if ch.isalnum() else "" for ch in s)[:18] or "firma"


def email_for(name: str, person: bool) -> str:
    for _ in range(20):
        local = f"{slug(name)}{rnd.randrange(1000)}" if person else f"info{rnd.randrange(100)}"
        dom = rnd.choice(["web.de", "gmx.net", "t-online.de", "gmail.com", "outlook.com"]) if person else f"{slug(name)}{rnd.randrange(100)}.{rnd.choice(['de', 'com', 'eu', 'at', 'ch'])}"
        e = f"{local}@{dom}"
        if e not in used_email:
            used_email.add(e)
            return e
    seq[0] += 1
    e = f"x{seq[0]}@mail.example"
    used_email.add(e)
    return e


def country_text(cc: str) -> str:
    iso3, en, de = CCODES[cc]
    kind = rnd.choices(["iso2", "iso3", "en", "de"], [40, 20, 20, 20])[0]
    v = {"iso2": cc, "iso3": iso3, "en": en, "de": de}[kind]
    return v.upper() if rnd.random() < 0.15 else v


def address():
    city, cc, plz0 = rnd.choices(CITIES, CITY_W)[0]
    street = rnd.choice(STREETS)
    return {"city": city, "cc": cc, "plz": str(plz0 + rnd.randrange(100)), "street": street, "house": str(rnd.randrange(1, 200)) + (rnd.choice("abc") if rnd.random() < 0.05 else "")}


def make_base(kind: str | None = None) -> dict:
    person = rnd.random() < 0.4 if kind is None else kind == "person"
    a = address()
    if person:
        first, last = rnd.choice(FIRST), rnd.choices(SURNAMES, WEIGHTS)[0]
        name, name2 = f"{first} {last}", ""
    else:
        last = rnd.choices(SURNAMES, WEIGHTS)[0]
        legal = rnd.choices(LEGAL_NAMES, LEGAL_W)[0]
        name = f"{last} {rnd.choice(BRANCH)} {legal}" if rnd.random() < 0.7 else f"{rnd.choice(PREFIX)}{rnd.choice(BRANCH).lower()} {legal}"
        name2 = rnd.choice(["", "", "", "Zentrale", "Verwaltung", "Vertrieb"])
    cc = a["cc"]
    tax = uniq_tax(cc) if (not person and rnd.random() < 0.85) or (person and rnd.random() < 0.05) else ""
    return {"person": person, "name": name, "name2": name2, "street": a["street"], "house": a["house"], "city": a["city"], "plz": a["plz"], "cc": cc, "country": country_text(cc), "lang": LANG[cc],
            "email": email_for(name, person) if rnd.random() < 0.75 else "", "phone": uniq_phone(cc) if rnd.random() < 0.8 else "", "terms": rnd.choices(TERMS, TERMS_W)[0], "curr": CURR[cc],
            "del": "", "tax": tax}


COLS = ["KUNDE_NR", "FIRMENNAME", "ZUSATZ", "STRASSE", "HAUSNR", "ORT", "PLZ", "LAND", "SPRACHE", "MAIL", "TELEFON", "ZAHLUNGSBED", "WAEHRUNG", "LOESCH_KZ", "UST_IDNR"]
rows: list[dict] = []     # each a base dict plus "id"
counter = [1000000]


def add(d: dict) -> str:
    counter[0] += 1
    d["id"] = f"KD-{counter[0]}"
    rows.append(d)
    return d["id"]


DEFECTS = 10000
PLANT = {"exact": 3000, "case_ws": 3000, "translit": 4000, "reordered": 3000, "typo": 4000, "legal_form": 3000, "street_abbrev": 3000, "same_tax_diff_name": 2000, "same_email_diff_name": 2000,
         "same_phone_diff_name": 1000}
TRAPS = {"diff_legal_entity": 3000, "branches_shared_phone": 2000, "homonym_people": 5000, "numbered": 1500, "same_address_diff_firm": 2000}
n_extra = sum(PLANT.values()) + sum(TRAPS.values()) * 2 + DEFECTS
n_base = TOTAL - n_extra
print(f"{n_base:,} base rows", flush=True)
for _ in range(n_base):
    add(make_base())
base = list(rows)
dup_pairs, traps = [], []


def umlaut_ascii(s: str) -> str:
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return s


def strip_accents(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def typo(s: str) -> str:
    i = rnd.randrange(4, max(5, len(s) - 3)) if len(s) > 8 else len(s) - 2
    return s[:i] + s[i + 1] + s[i] + s[i + 2:] if i + 1 < len(s) and s[i] != s[i + 1] else s[:i] + s[i + 1:]


def pick(pred):
    for _ in range(100000):
        r = rnd.choice(base)
        if pred(r):
            return r
    raise SystemExit("could not find an original for a duplicate kind")


def nocontact(d: dict) -> dict:
    d["email"] = d["phone"] = d["tax"] = ""
    return d


accents = lambda r: any(ch in r["name"] for ch in "äöüÄÖÜß")  # noqa: E731
for kind, count in PLANT.items():
    for _ in range(count):
        if kind == "translit":
            o = pick(lambda r: accents(r) or any(ord(ch) > 127 for ch in r["name"]))
        elif kind == "reordered":
            o = pick(lambda r: r["person"])
        elif kind == "legal_form":
            o = pick(lambda r: not r["person"] and r["name"].endswith(("GmbH", "AG", "KG", "UG", "Ltd", "Inc")))
        elif kind == "street_abbrev":
            o = pick(lambda r: r["street"].endswith("straße"))
        elif kind == "typo":
            o = pick(lambda r: len(r["name"]) > 12)
        else:
            o = pick(lambda r: True)
        c = dict(o)
        if kind == "exact":
            pass
        elif kind == "case_ws":
            c["name"] = "  " + o["name"].upper().replace(" ", "  ") + " "
            nocontact(c)
        elif kind == "translit":
            c["name"] = umlaut_ascii(o["name"]) if accents(o) and rnd.random() < 0.7 else strip_accents(o["name"])
            nocontact(c)
        elif kind == "reordered":
            first, last = o["name"].split(" ", 1)
            c["name"] = f"{last}, {first}"
            nocontact(c)
        elif kind == "typo":
            c["name"] = typo(o["name"])
            nocontact(c)
        elif kind == "legal_form":
            c["name"] = o["name"].replace("GmbH", "Gmbh.", 1) if "GmbH" in o["name"] else o["name"] + "."
            nocontact(c)
        elif kind == "street_abbrev":
            c["street"] = o["street"][:-6] + rnd.choice(["str.", "Str.", " Str."])
            nocontact(c)
        elif kind == "same_tax_diff_name":
            p = make_base("org")
            c.update(name=p["name"], name2="", email="", phone="")
            if not o["tax"]:
                o["tax"] = c["tax"] = uniq_tax(o["cc"])
        elif kind == "same_email_diff_name":
            if not o["email"]:
                o["email"] = c["email"] = email_for(o["name"], o["person"])
            p = make_base("org")
            c.update(name=p["name"], name2="", phone="", tax="")
        elif kind == "same_phone_diff_name":
            if not o["phone"]:
                o["phone"] = c["phone"] = uniq_phone(o["cc"])
            p = make_base("org")
            c.update(name=p["name"], name2="", email="", tax="", phone=phone_variant(o["phone"]))
        cid = add(c)
        dup_pairs.append([o["id"], cid, kind])

for kind, count in TRAPS.items():
    for _ in range(count):
        if kind == "diff_legal_entity":
            o = pick(lambda r: r["tax"] != "")
            c = dict(o)
            c["tax"] = uniq_tax(o["cc"])
            c["email"] = c["phone"] = ""
        elif kind == "branches_shared_phone":
            o = pick(lambda r: r["tax"] != "" and r["phone"] != "")
            c = dict(o)
            c.update(name=o["name"] + " Niederlassung", tax=uniq_tax(o["cc"]), email="", street=rnd.choice(STREETS), house=str(rnd.randrange(1, 200)))
        elif kind == "homonym_people":
            o = pick(lambda r: r["person"] and r["tax"] == "")
            c = dict(o)
            a = address()
            c.update(street=a["street"], house=a["house"], email=email_for(o["name"], True), phone=uniq_phone(o["cc"]), tax="")
        elif kind == "numbered":
            o = pick(lambda r: not r["person"] and r["tax"] != "" and r["name"].endswith("GmbH"))
            o["name"] = o["name"].replace(" GmbH", " Werk 1 GmbH")
            o["tax"] = ""
            c = dict(o)
            c.update(name=o["name"].replace("Werk 1", "Werk 2"), email="", phone="")   # two plants, same address, nothing but the NUMBER in the name to tell them apart
        else:  # same_address_diff_firm
            o = pick(lambda r: not r["person"])
            p = make_base("org")
            c = dict(p)
            c.update(street=o["street"], house=o["house"], city=o["city"], plz=o["plz"], cc=o["cc"], country=o["country"], lang=o["lang"], curr=o["curr"])
        cid = add(c)
        traps.append([o["id"], cid, kind])

# defects: separate rows, each wrong in one way
long_name = lambda: make_base("org") | {}   # noqa: E731
for kind, count in (("blank_name", 500), ("space_name", 200), ("long_name", 500), ("test", 300), ("zz", 200), ("deleted", 400), ("junk_country", 200), ("blank_country", 200), ("bad_email", 500), ("phone_letters", 300),
                    ("blank_postal", 400), ("quote_name", 300)):
    for _ in range(count):
        d = make_base()
        if kind == "blank_name":
            d["name"] = ""
        elif kind == "space_name":
            d["name"] = "   "
        elif kind == "long_name":
            d["name"] = d["name"] + " " + " ".join(rnd.choice(BRANCH) for _ in range(5))
        elif kind == "test":
            d["name"] = "TEST " + d["name"]
        elif kind == "zz":
            d["name"] = "ZZ_" + d["name"]
        elif kind == "deleted":
            d["del"] = "J"
        elif kind == "junk_country":
            d["country"] = rnd.choice(["XX", "??", "Atlantis", "00", "N/A"])
        elif kind == "blank_country":
            d["country"] = ""
        elif kind == "bad_email":
            d["email"] = rnd.choice(["not-an-email", "info.at.firma.de", "kontakt@", "x y z"])
        elif kind == "phone_letters":
            d["phone"] = "call me later"
        elif kind == "blank_postal":
            d["plz"] = ""
        elif kind == "quote_name":
            d["name"] = d["name"].replace(" ", ' "', 1) + '" ; Filiale'
        add(d)
while len(rows) < TOTAL:
    add(make_base())

rnd.shuffle(rows)
with open(OUT, "w", encoding="utf-8", newline="") as fh:
    w = csv.writer(fh, delimiter=";")
    w.writerow(COLS)
    for d in rows:
        w.writerow([d["id"], d["name"], d["name2"], d["street"], d["house"], d["city"], d["plz"], d["country"], d["lang"], d["email"], d["phone"], d["terms"], d["curr"], d["del"], d["tax"]])
print(f"{len(rows):,} rows written", flush=True)

COUNTRY_LK = {}
for cc, (iso3, en, de) in CCODES.items():
    for v in (cc, iso3, en, de):
        COUNTRY_LK[v.upper()] = cc
TERMS_LK = {"30 TAGE NETTO": "NT30", "60 TAGE NETTO": "NT60", "VORKASSE": "CASH", "2% SKONTO 10 TAGE, 30 NETTO": "NT3D"}
profile = {
    "table": "DS2_KUNDEN", "key": "KUNDE_NR", "tax_col": "UST_IDNR", "name_col": "FIRMENNAME", "delimiter": ";",
    "rules": [
        ["BUT000", "NAME_ORG1", "DIRECT_COPY", "FIRMENNAME", None, None], ["BUT000", "NAME_ORG2", "DIRECT_COPY", "ZUSATZ", None, None], ["BUT000", "BP_EXT", "DIRECT_COPY", "KUNDE_NR", None, None],
        ["BUT000", "TYPE", "STATIC_VALUE", None, "'2'", None], ["BUT000", "BU_GROUP", "STATIC_VALUE", None, "'Z001'", None],
        ["BUT000", "BU_SORT1", "SQL_EXPRESSION", "FIRMENNAME", "upper(left(trim(FIRMENNAME), 20))", None], ["ADRC", "STREET", "DIRECT_COPY", "STRASSE", None, None],
        ["ADRC", "HOUSE_NUM1", "DIRECT_COPY", "HAUSNR", None, None], ["ADRC", "CITY1", "DIRECT_COPY", "ORT", None, None], ["ADRC", "POST_CODE1", "DIRECT_COPY", "PLZ", None, None],
        ["ADRC", "COUNTRY", "VALUE_MAPPING", "LAND", None, COUNTRY_LK], ["ADRC", "LANGU", "VALUE_MAPPING", "SPRACHE", None, {"DE": "D", "EN": "E", "FR": "F", "IT": "I", "ES": "S"}], ["ADR6", "SMTP_ADDR", "DIRECT_COPY", "MAIL", None, None],
        ["ADR2", "TEL_NUMBER", "DIRECT_COPY", "TELEFON", None, None], ["KNB1", "ZTERM", "VALUE_MAPPING", "ZAHLUNGSBED", None, TERMS_LK], ["KNVV", "WAERS", "DIRECT_COPY", "WAEHRUNG", None, None],
    ],
    "exclusions": [{"column": "LOESCH_KZ", "operator": "equals", "values": ["J"], "reason": "flagged deleted in legacy"},
                   {"column": "FIRMENNAME", "operator": "starts_with", "values": ["TEST "], "reason": "test customers"},
                   {"column": "FIRMENNAME", "operator": "starts_with", "values": ["ZZ_"], "reason": "obsolete (ZZ_ prefix)"}],
    "defect_exclusions": [["FIRMENNAME", "is_blank", [], "no name: cannot be loaded"], ["LAND", "is_blank", [], "no country: cannot be loaded"],
                          ["LAND", "in", ["XX", "??", "Atlantis", "00", "N/A"], "not a country: cannot be loaded"]],
    "must_not_load": {
        "blank name (mandatory)": "trim(coalesce(FIRMENNAME, '')) = ''",
        "name longer than 40 characters": "length(trim(FIRMENNAME)) > 40",
        "TEST customer": "upper(FIRMENNAME) like 'TEST %'",
        "ZZ_ obsolete": "FIRMENNAME like 'ZZ\\_%' escape '\\'",
        "flagged deleted": "LOESCH_KZ = 'J'",
        "junk country": "upper(trim(LAND)) in ('XX', '??', 'ATLANTIS', '00', 'N/A')",
        "blank country": "trim(coalesce(LAND, '')) = ''",
        "e-mail without an @": "trim(coalesce(MAIL, '')) <> '' and position('@' in MAIL) = 0",
    },
    "must_not_merge_kinds": ["diff_legal_entity", "branches_shared_phone", "homonym_people", "numbered", "same_address_diff_firm"],
    "iso": sorted(CCODES), "spelled_sql": "upper(trim(LAND)) not in (" + ", ".join(f"'{c}'" for c in sorted(CCODES)) + ") and trim(coalesce(LAND, '')) <> '' and upper(trim(LAND)) in ("
                  + ", ".join("'" + k.replace("'", "''") + "'" for k in COUNTRY_LK) + ")",
}
json.dump({"dup_pairs": dup_pairs, "traps": traps, "profile": profile}, open(TRUTH, "w", encoding="utf-8"))
print("truth written:", len(dup_pairs), "planted duplicates,", len(traps), "traps", flush=True)
