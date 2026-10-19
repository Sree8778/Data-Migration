"""Duplicate-detection test suite: many files with KNOWN, injected duplicates -> full pipeline -> score against the truth.

    python scripts/test_duplicates.py [--api http://localhost:8000] [--scale-rows 10000] [--seed 7]

Every scenario writes a CSV to mock_data/duplicates/, registers it through the API, builds a deterministic mapping
(no AI, so only deduplication is under test), runs the pipeline, and compares the engine's clusters with the answer
key: how many injected duplicates were found (recall) per duplicate type, whether any distinct entities were merged
by mistake, whether the surviving golden record is the right one, and how long it took. Report:
mock_data/duplicate_test_report.md. Exit 1 when a HARD expectation fails (exact/tax/email/phone duplicates must be
found, look-alikes must never merge, the most complete record must survive). Everything else is informational.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import httpx
from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parent.parent
HEADER = ["CUST_ID", "CUST_NAME", "TAX_ID", "EMAIL", "PHONE", "ADDR_STREET", "CITY", "COUNTRY", "POSTAL_CODE", "PAYMENT_TERMS"]
SYL = ["ka", "lo", "mi", "ra", "ve", "tu", "zo", "pe", "xi", "da", "no", "su", "be", "fa", "gi", "ho", "ju", "li", "ma", "ne", "qua", "bro", "sty", "gre"]
INDUSTRY = ["Logistics", "Systems", "Foods", "Motors", "Textiles", "Analytics", "Pharma", "Energy", "Labs", "Industries", "Trading", "Software"]
LEGAL = ["Inc", "LLC", "Corp", "GmbH", "Ltd", "SA", "AG", "Co Ltd"]
STREETS = ["Oak", "Maple", "Cedar", "Pine", "Elm", "Birch", "Lake", "Hill", "River", "Sunset", "Market", "Station", "Park", "Mill"]
CITIES = [("Boston", "US", "0211"), ("Austin", "US", "7870"), ("Berlin", "DE", "1011"), ("Munich", "DE", "8033"), ("Paris", "FR", "7500"),
          ("Lyon", "FR", "6900"), ("London", "GB", "EC1A"), ("Leeds", "GB", "LS1 "), ("Pune", "IN", "4110"), ("Toronto", "CA", "M5H "), ("Madrid", "ES", "2801")]
DASHES = "-"


# ----------------------------------------------------------------------------- entity generation
class World:
    """Generates distinct entities (no two company names within 82 Indel-similarity) and tracks ids."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.buckets: dict[str, list[str]] = {}
        self.next_id = 500000
        self.next_dup_id = 900000
        self.counter = 0

    def base_name(self) -> str:
        while True:
            word = "".join(self.rng.choice(SYL) for _ in range(self.rng.choice((3, 3, 4)))).capitalize()
            name = f"{word} {self.rng.choice(INDUSTRY)}"
            bucket = self.buckets.setdefault(name[:2], [])
            if not bucket or process.extractOne(name, bucket, scorer=fuzz.ratio)[1] < 82:
                bucket.append(name)
                return name

    def entity(self, with_tax=True, with_contacts=True) -> dict:
        self.counter += 1
        r = self.rng
        name = self.base_name()
        city, country, post = r.choice(CITIES)
        slug = name.split()[0].lower()
        return {
            "CUST_ID": str(self._id()), "CUST_NAME": f"{name} {r.choice(LEGAL)}",
            "TAX_ID": f"{country}{r.randrange(10**8, 10**9)}" if with_tax else "",
            "EMAIL": f"accounts@{slug}{self.counter}.example.com" if with_contacts else "",
            "PHONE": f"+{r.randrange(1, 99)}-{r.randrange(100, 999)}-{r.randrange(1000000, 9999999)}" if with_contacts else "",
            "ADDR_STREET": f"{r.randrange(1, 900)} {r.choice(STREETS)} {r.choice(['Street', 'Avenue', 'Road', 'Lane'])} {self.counter}",
            "CITY": city, "COUNTRY": country, "POSTAL_CODE": f"{post}{r.randrange(10, 99)}".strip(),
            "PAYMENT_TERMS": r.choice(["NET30", "NET60", "COD"]),
        }

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def dup_id(self) -> str:
        self.next_dup_id += 1
        return str(self.next_dup_id)


# ----------------------------------------------------------------------------- duplicate variants
def v_exact(w, e):
    return {**e, "CUST_ID": w.dup_id()}


def v_tax_reformatted(w, e):  # renamed company, moved, same tax id written differently
    t = e["TAX_ID"]
    other = w.entity(with_tax=False, with_contacts=False)
    return {**other, "CUST_ID": w.dup_id(), "TAX_ID": f"{t[:2]}-{t[2:5]} {t[5:]}".lower()}


def v_suffix_punct(w, e):  # same company, different legal-form punctuation / spacing
    name = e["CUST_NAME"]
    head, _, legal = name.rpartition(" ")
    variant = {"Inc": "Inc.", "LLC": "L.L.C.", "Corp": "Corp.", "GmbH": "G.m.b.H.", "Ltd": "Ltd.", "SA": "S.A.", "AG": "A.G.", "Co": "Co."}.get(legal, legal + ".")
    return {**e, "CUST_ID": w.dup_id(), "CUST_NAME": f"{head}, {variant}"}


def v_case_space(w, e):
    return {**e, "CUST_ID": w.dup_id(), "CUST_NAME": "  " + e["CUST_NAME"].upper().replace(" ", "  ") + " "}


def v_transposition(w, e):
    n = e["CUST_NAME"]; i = 3
    return {**e, "CUST_ID": w.dup_id(), "CUST_NAME": n[:i] + n[i + 1] + n[i] + n[i + 2:]}


def v_missing_char(w, e):
    n = e["CUST_NAME"]
    return {**e, "CUST_ID": w.dup_id(), "CUST_NAME": n[:4] + n[5:]}


def v_street_abbrev(w, e):
    s = e["ADDR_STREET"].replace("Street", "St").replace("Avenue", "Ave").replace("Road", "Rd").replace("Lane", "Ln")
    return {**e, "CUST_ID": w.dup_id(), "ADDR_STREET": s, "POSTAL_CODE": ""}


def v_typo_and_address(w, e):
    return v_street_abbrev(w, v_missing_char(w, e))


def v_email_only(w, e):
    other = w.entity(with_tax=False, with_contacts=False)
    return {**other, "CUST_ID": w.dup_id(), "EMAIL": "  " + e["EMAIL"].upper() + " "}


def v_phone_only(w, e):
    d = "".join(ch for ch in e["PHONE"] if ch.isdigit())
    other = w.entity(with_tax=False, with_contacts=False)
    return {**other, "CUST_ID": w.dup_id(), "PHONE": f"(+{d[:2]}) {d[2:5]} {d[5:]}"}


def STREET_ABBREV(e):
    return {"ADDR_STREET": e["ADDR_STREET"].replace("Street", "St").replace("Avenue", "Ave").replace("Road", "Rd").replace("Lane", "Ln")}


def address_variant(change):
    return lambda w, e: {**e, "CUST_ID": w.dup_id(), **change(e)}


_TYPOS = {"Oak": "Oal", "Maple": "Mapel", "Cedar": "Ceder", "Pine": "Pime", "Elm": "Elk", "Birch": "Birsh", "Lake": "Lage", "Hill": "Hall",
          "River": "Rivor", "Sunset": "Sunsat", "Market": "Marcet", "Station": "Statian", "Park": "Pork", "Mill": "Mall"}


def _street_typo(e):
    street = e["ADDR_STREET"]
    for good, bad in _TYPOS.items():
        street = street.replace(good, bad)
    return {"ADDR_STREET": street}


ADDRESS_VARIANTS = [
    ("street-abbreviated (St, Ave)", STREET_ABBREV),
    ("street-typo", _street_typo),
    ("postal-code-blank", lambda e: {"POSTAL_CODE": ""}),
    ("postal-code-differs (should NOT merge)", lambda e: {"POSTAL_CODE": e["POSTAL_CODE"][:-1] + ("1" if e["POSTAL_CODE"][-1] != "1" else "2")}),
    ("house-number-differs (should NOT merge)", lambda e: {"ADDR_STREET": str(int(e["ADDR_STREET"].split()[0]) + 1) + " " + e["ADDR_STREET"].split(" ", 1)[1]}),
    ("street-blank", lambda e: {"ADDR_STREET": ""}),
    ("city-blank (fails mandatory check)", lambda e: {"CITY": ""}),
    ("whole-address-blank (fails mandatory check)", lambda e: {"ADDR_STREET": "", "CITY": "", "POSTAL_CODE": ""}),
]


# ----------------------------------------------------------------------------- scenarios
@dataclass
class Scenario:
    key: str
    title: str
    rows: list[dict] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)       # {"orig": id, "dups": {dup_id: category}, "master": id | None}
    distinct_pairs: list[tuple[str, str]] = field(default_factory=list)  # must never share a cluster
    explicit_config: bool = False
    hard: dict[str, float] = field(default_factory=dict)  # category -> minimum recall
    notes: str = ""
    pk: str = "CUST_ID"


def inject(w, sc: Scenario, entities: list[dict], plan: list[tuple[str, callable, int]], tax_blank=False):
    """plan = [(category, variant function, how many)]; each duplicate gets a distinct original."""
    pool = list(entities)
    w.rng.shuffle(pool)
    for category, fn, count in plan:
        for _ in range(count):
            orig = pool.pop()
            dup = fn(w, orig)
            sc.rows.append(dup)
            sc.groups.append({"orig": orig["CUST_ID"], "dups": {dup["CUST_ID"]: category}, "master": None})


def build_scenarios(w: World, scale_rows: int) -> list[Scenario]:
    out = []

    sc = Scenario("exact_copies", "Exact copies (every field identical, new customer number)", hard={"exact-copy": 1.0})
    base = [w.entity() for _ in range(300)]; sc.rows += base
    inject(w, sc, base, [("exact-copy", v_exact, 40)]); out.append(sc)

    sc = Scenario("tax_id", "Same tax id in different formats, company renamed and moved", hard={"tax-id-reformatted": 1.0},
                  notes="Only the tax id links these; names and addresses are different.")
    base = [w.entity() for _ in range(300)]; sc.rows += base
    inject(w, sc, base, [("tax-id-reformatted", v_tax_reformatted, 40)]); out.append(sc)

    sc = Scenario("name_variants", "Fuzzy name duplicates (no tax id, same address)",
                  hard={"legal-suffix-punctuation": 1.0, "case-and-spacing": 1.0})
    base = [w.entity(with_tax=False, with_contacts=False) for _ in range(400)]; sc.rows += base
    inject(w, sc, base, [("legal-suffix-punctuation", v_suffix_punct, 30), ("case-and-spacing", v_case_space, 30),
                         ("transposed-letters", v_transposition, 30), ("missing-letter", v_missing_char, 30)]); out.append(sc)

    sc = Scenario("address_variants", "Same company, address written / recorded differently (name identical)",
                  hard={"street-abbreviated (St, Ave)": 1.0, "street-typo": 1.0, "postal-code-blank": 1.0, "street-blank": 1.0},
                  notes="A postal code or house number that one record lacks is missing data and does not block a merge; a DIFFERENT number does. "
                        "Blank-city records fail the mandatory-field check and are excluded from deduplication until fixed (by design).")
    base = [w.entity(with_tax=False, with_contacts=False) for _ in range(40 * len(ADDRESS_VARIANTS))]; sc.rows += base
    inject(w, sc, base, [(name, address_variant(fn), 40) for name, fn in ADDRESS_VARIANTS]); out.append(sc)
    sc = Scenario("name_typo_and_address", "Name typo together with an address difference", hard={"name-typo + street-abbreviated": 1.0, "name-typo + postal-blank": 1.0})
    base = [w.entity(with_tax=False, with_contacts=False) for _ in range(100)]; sc.rows += base
    inject(w, sc, base, [("name-typo + street-abbreviated", lambda w_, e: v_missing_char(w_, {**e, **STREET_ABBREV(e)}), 30),
                         ("name-typo + postal-blank", lambda w_, e: v_missing_char(w_, {**e, "POSTAL_CODE": ""}), 30)]); out.append(sc)

    for explicit in (False, True):
        sc = Scenario("email_phone_explicit" if explicit else "email_phone_default",
                      "E-mail / phone only duplicates - " + ("explicit dedupe config with e-mail + phone keys" if explicit else "DEFAULT dedupe config"),
                      explicit_config=explicit, hard={"email-only": 1.0, "phone-only": 1.0},
                      notes="The default config now also keys on e-mail and phone columns when the source has them.")
        base = [w.entity(with_tax=False) for _ in range(300)]; sc.rows += base
        inject(w, sc, base, [("email-only", v_email_only, 30), ("phone-only", v_phone_only, 30)]); out.append(sc)

    sc = Scenario("lookalikes", "Look-alikes that are DIFFERENT entities and must never merge")
    for i in range(25):  # Plant 1 / Plant 2, same address
        a = w.entity(with_tax=False, with_contacts=False); b = {**a, "CUST_ID": str(w._id()), "CUST_NAME": a["CUST_NAME"].rpartition(" ")[0] + " Plant 2 " + a["CUST_NAME"].rpartition(" ")[2]}
        a["CUST_NAME"] = a["CUST_NAME"].rpartition(" ")[0] + " Plant 1 " + a["CUST_NAME"].rpartition(" ")[2]
        sc.rows += [a, b]; sc.distinct_pairs.append((a["CUST_ID"], b["CUST_ID"]))
    for i in range(25):  # same name, different city / street (branches of a franchise)
        a = w.entity(with_tax=False, with_contacts=False); other = w.entity(with_tax=False, with_contacts=False)
        b = {**other, "CUST_ID": str(w._id()), "CUST_NAME": a["CUST_NAME"]}
        sc.rows += [a, b]; sc.distinct_pairs.append((a["CUST_ID"], b["CUST_ID"]))
    for i in range(25):  # one-word-different names at different addresses
        a = w.entity(with_tax=False, with_contacts=False); other = w.entity(with_tax=False, with_contacts=False)
        first, _, rest = a["CUST_NAME"].partition(" ")
        b = {**other, "CUST_ID": str(w._id()), "CUST_NAME": f"{first} {'Fuels' if 'Foods' in rest else 'Foods'} {rest.rpartition(' ')[2]}"}
        sc.rows += [a, b]; sc.distinct_pairs.append((a["CUST_ID"], b["CUST_ID"]))
    for i in range(25):  # same name, same street, different house number
        a = w.entity(with_tax=False, with_contacts=False)
        num, _, rest = a["ADDR_STREET"].partition(" ")
        b = {**a, "CUST_ID": str(w._id()), "ADDR_STREET": f"{int(num) + 2} {rest}"}
        sc.rows += [a, b]; sc.distinct_pairs.append((a["CUST_ID"], b["CUST_ID"]))
    sc.notes = "25 pairs each: 'Plant 1' vs 'Plant 2'; same name in two cities; one word different; same street, different house number."
    out.append(sc)

    sc = Scenario("chains_and_master", "Groups of three + golden-record choice (most complete record must survive)")
    for i in range(40):
        full = w.entity(with_tax=False)
        b = {**v_case_space(w, full), "EMAIL": "", "PHONE": ""}
        c = {**v_suffix_punct(w, full), "EMAIL": "", "PHONE": ""}
        full["CUST_ID"] = str(w._id() + 100000)  # the complete record has the LARGEST number, so "smallest pk" would pick the wrong one
        sc.rows += [b, c, full]
        sc.groups.append({"orig": full["CUST_ID"], "dups": {b["CUST_ID"]: "triple-member", c["CUST_ID"]: "triple-member"}, "master": full["CUST_ID"]})
    out.append(sc)
    sc.hard = {"triple-member": 1.0}

    sc = Scenario("doubled_file", "The whole extract delivered twice (every record appears again under a new number)", hard={"exact-copy": 1.0})
    base = [w.entity() for _ in range(200)]; sc.rows += base
    inject(w, sc, base, [("exact-copy", v_exact, 200)]); out.append(sc)

    sc = Scenario("scale", f"Scale: {scale_rows:,} distinct customers + mixed duplicates", hard={"exact-copy": 1.0, "tax-id-reformatted": 1.0})
    base = [w.entity() for _ in range(scale_rows)]; sc.rows += base
    n = max(10, scale_rows // 40)
    inject(w, sc, base, [("exact-copy", v_exact, n), ("tax-id-reformatted", v_tax_reformatted, n), ("legal-suffix-punctuation", v_suffix_punct, n),
                         ("case-and-spacing", v_case_space, n)]); out.append(sc)
    return out


# ----------------------------------------------------------------------------- running + scoring
def csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=HEADER, lineterminator="\r\n")
    wr.writeheader(); wr.writerows(rows)
    return buf.getvalue().encode("utf-8")


class Api:
    def __init__(self, base):
        self.c = httpx.Client(base_url=base, headers={"X-User": "dup.tester"}, timeout=1800)

    def call(self, method, url, expect=(200, 201), **kw):
        r = self.c.request(method, url, **kw)
        if r.status_code not in expect:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:300]}")
        return r.json()


RULES = [("BUT000", "NAME_ORG1", "CUST_NAME"), ("ADRC", "STREET", "ADDR_STREET"), ("ADRC", "CITY1", "CITY"), ("ADRC", "COUNTRY", "COUNTRY"), ("ADRC", "POST_CODE1", "POSTAL_CODE")]
EXPLICIT = {
    "exact_keys": [{"name": "tax", "columns": ["SRC_TAX_ID"], "normalizer": "alnum"}, {"name": "email", "columns": ["SRC_EMAIL"], "normalizer": "email"},
                   {"name": "phone", "columns": ["SRC_PHONE"], "normalizer": "digits"}],
    "name_column": "BUT000_NAME_ORG1", "address_columns": ["ADRC_STREET", "ADRC_CITY1", "ADRC_POST_CODE1"],
}


def run_scenario(api: Api, sc: Scenario, out_dir: Path) -> dict:
    path = out_dir / f"{sc.key}.csv"
    data = csv_bytes(sc.rows)
    path.write_bytes(data)
    res: dict = {"key": sc.key, "title": sc.title, "rows": len(sc.rows), "file": path.name, "notes": sc.notes}
    reg = api.call("POST", "/api/sources/register", data={"system_name": "DUP_TEST", "table_name": "DUP_" + sc.key.upper(), "primary_key_columns": sc.pk,
                                                         "expected_rows": str(len(sc.rows))}, files={"file": (path.name, data)})
    mset = api.call("POST", "/api/mapping-sets", json={"source_table_id": reg["table_id"]})["mapping_set_id"]
    for tt, tc, src in RULES:
        api.call("POST", f"/api/mapping/{mset}/rules", json={"target_table": tt, "target_column": tc, "rule_type": "DIRECT_COPY", "source_column": src})
    for col, logic in (("BU_GROUP", "'BP01'"), ("TYPE", "'2'")):
        api.call("POST", f"/api/mapping/{mset}/rules", json={"target_table": "BUT000", "target_column": col, "rule_type": "STATIC_VALUE", "transformation_logic": logic})
    body = {"wave_name": "DUP_" + sc.key}
    if sc.explicit_config:
        body["dedup"] = EXPLICIT
        body["passthrough_columns"] = ["EMAIL", "PHONE", "TAX_ID"]
    t0 = time.time()
    run = api.call("POST", f"/api/pipeline/run/{mset}", json=body)
    res["seconds"] = round(time.time() - t0, 1)
    d = run["run"]["deduplication"]
    res.update(run_id=run["run_id"], clusters=d["duplicate_clusters"], children=d["duplicate_children"], exact_clusters=d["exact_match_clusters"],
               fuzzy_clusters=d["fuzzy_match_clusters"], fuzzy_pairs_evaluated=d["fuzzy_pairs_evaluated"], exact_edges=d["exact_edges_by_key"])

    p = ROOT / run["run"]["deduplicated_path"] if not Path(run["run"]["deduplicated_path"]).is_absolute() else Path(run["run"]["deduplicated_path"])
    got = duckdb.connect().execute("SELECT CAST(_SRC_PK AS VARCHAR), dedup_cluster_id, is_duplicate_child, surviving_master_pk FROM read_parquet(?)", [str(p)]).fetchall()
    cluster = {pk: cid for pk, cid, _, _ in got if cid is not None}
    survivor = {pk: m for pk, _, _, m in got}
    is_child = {pk: bool(c) for pk, _, c, _ in got}

    cats: dict[str, list[int]] = {}
    for g in sc.groups:
        for dup, cat in g["dups"].items():
            found = g["orig"] in cluster and dup in cluster and cluster[g["orig"]] == cluster[dup]
            cats.setdefault(cat, [0, 0]); cats[cat][0] += 1; cats[cat][1] += found
    res["categories"] = {c: {"injected": n, "found": f, "recall": round(f / n, 3)} for c, (n, f) in cats.items()}

    truth: dict[str, int] = {}  # entity group number for every id that has an injected relationship
    for i, g in enumerate(sc.groups):
        for k in [g["orig"], *g["dups"]]:
            truth[k] = i
    members: dict[int, list[str]] = {}
    for pk, cid in cluster.items():
        members.setdefault(cid, []).append(pk)
    false_merges = []
    for cid, pks in members.items():
        groups = {truth.get(k, f"solo-{k}") for k in pks}
        if len(groups) > 1:
            false_merges.append(pks[:6])
    res["false_merges"] = len(false_merges)
    res["false_merge_examples"] = false_merges[:3]
    res["distinct_pairs_merged"] = sum(1 for a, b in sc.distinct_pairs if a in cluster and b in cluster and cluster[a] == cluster[b])
    res["distinct_pairs"] = len(sc.distinct_pairs)
    masters = [g for g in sc.groups if g["master"]]
    if masters:
        right = sum(1 for g in masters if all(survivor.get(k) == g["master"] for k in [g["orig"], *g["dups"]]))
        res["golden_correct"] = f"{right}/{len(masters)}"
        res["golden_ok"] = right == len(masters)
    expected_children = sum(len(g["dups"]) for g in sc.groups)
    res["expected_children"] = expected_children

    problems = []
    for cat, v in res["categories"].items():
        if "should NOT merge" in cat and v["found"]:
            problems.append(f"{sc.key}: {v['found']} '{cat}' record(s) were merged")
    for cat, minimum in sc.hard.items():
        if res["categories"].get(cat, {"recall": 0})["recall"] < minimum:
            problems.append(f"{sc.key}: recall for '{cat}' is {res['categories'].get(cat, {}).get('recall')} (required {minimum})")
    if res["distinct_pairs_merged"]:
        problems.append(f"{sc.key}: {res['distinct_pairs_merged']} look-alike pair(s) were merged")
    if res["false_merges"]:
        problems.append(f"{sc.key}: {res['false_merges']} cluster(s) contain different entities")
    if masters and not res["golden_ok"]:
        problems.append(f"{sc.key}: wrong golden record in {res['golden_correct']} groups")
    res["problems"] = problems
    return res


PROBLEMS: list[str] = []


def file_level_checks(api: Api, out_dir: Path) -> list[str]:
    """Same file delivered twice, and the same primary key twice inside one file."""
    lines = []
    path = out_dir / "exact_copies.csv"
    data = path.read_bytes()
    form = {"system_name": "DUP_TEST", "table_name": "DUP_EXACT_COPIES", "primary_key_columns": "CUST_ID"}
    before = api.call("GET", "/api/tables")
    tid = next(t["table_id"] for t in before if t["table_name"] == "DUP_EXACT_COPIES")
    snap1 = api.call("GET", f"/api/tables/{tid}/snapshot")
    again = api.call("POST", "/api/sources/register", data=form, files={"file": ("exact_copies_renamed.csv", data)})
    snap2 = api.call("GET", f"/api/tables/{again['table_id']}/snapshot")
    after = api.call("GET", "/api/tables")
    lines.append(f"- Same file uploaded again (renamed): table id {'unchanged' if again['table_id'] == tid else 'CHANGED'} ({tid} -> {again['table_id']}), "
                 f"tables in catalog {len(before)} -> {len(after)}, SHA-256 {'identical' if snap1['original_sha256'] == snap2['original_sha256'] else 'different'}, "
                 f"rows {again['rows_landed']}, warnings: {again['warnings'] or 'none'}")

    rows = list(csv.DictReader(io.StringIO(data.decode())))
    rows[5]["CUST_ID"] = rows[6]["CUST_ID"]; rows[50]["CUST_ID"] = rows[51]["CUST_ID"]
    dup_pk = csv_bytes(rows)
    r = api.c.post("/api/sources/register", data={**form, "table_name": "DUP_SAME_KEY_TWICE"}, files={"file": ("same_key.csv", dup_pk)})
    verdict = f"HTTP {r.status_code}: " + (r.json().get("detail", "") if r.status_code >= 400 else f"landed, warnings: {r.json().get('warnings') or 'none'}")
    lines.append(f"- Same CUST_ID used twice in one file (2 keys repeated), primary key declared: {verdict[:400]}")
    if r.status_code != 422:
        PROBLEMS.append("a file whose declared primary key repeats was accepted")
    if r.status_code == 201:
        mset = api.call("POST", "/api/mapping-sets", json={"source_table_id": r.json()["table_id"]})["mapping_set_id"]
        api.call("POST", f"/api/mapping/{mset}/rules", json={"target_table": "BUT000", "target_column": "NAME_ORG1", "rule_type": "DIRECT_COPY", "source_column": "CUST_NAME"})
        rr = api.c.post(f"/api/pipeline/run/{mset}", json={"wave_name": "DUP_SAME_KEY"})
        lines.append(f"- Pipeline on that file: HTTP {rr.status_code} " + (rr.text[:200] if rr.status_code >= 400 else f"ran, extracted {rr.json()['run']['source_records']}"))
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--scale-rows", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    out_dir = ROOT / "mock_data" / "duplicates"
    out_dir.mkdir(parents=True, exist_ok=True)
    api = Api(args.api)
    w = World(args.seed)
    scenarios = build_scenarios(w, args.scale_rows)
    if args.only:
        scenarios = [s for s in scenarios if s.key in args.only.split(",")]
    results, problems = [], []
    for sc in scenarios:
        r = run_scenario(api, sc, out_dir)
        results.append(r)
        problems += r["problems"]
        cats = ", ".join(f"{c} {v['found']}/{v['injected']}" for c, v in r["categories"].items())
        print(f"{sc.key:22s} rows {r['rows']:>6}  clusters {r['clusters']:>4}  children {r['children']:>4} (truth {r['expected_children']})  "
              f"false merges {r['false_merges']}  look-alikes merged {r['distinct_pairs_merged']}/{r['distinct_pairs']}  {r['seconds']}s | {cats}")

    file_lines = file_level_checks(api, out_dir) if not args.only else []
    problems += PROBLEMS

    md = ["# Duplicate detection test report", "",
          f"{len(results)} files with known, injected duplicates (seed {args.seed}); full pipeline via the API with a deterministic mapping; engine clusters compared with the answer key.", "",
          "| Scenario | Rows | Injected dups | Clusters | Children removed | False merges | Look-alikes merged | Golden record | Seconds |", "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        md.append(f"| {r['title']} | {r['rows']:,} | {r['expected_children']} | {r['clusters']} | {r['children']} | {r['false_merges']} | "
                  f"{r['distinct_pairs_merged']}/{r['distinct_pairs']} | {r.get('golden_correct', '-')} | {r['seconds']} |")
    md += ["", "## Recall by duplicate type (duplicates found / injected)", "", "| Scenario | Duplicate type | Found | Injected | Recall |", "|---|---|---|---|---|"]
    for r in results:
        for c, v in r["categories"].items():
            md.append(f"| {r['key']} | {c} | {v['found']} | {v['injected']} | {v['recall']:.0%} |")
    md += ["", "## Notes", ""] + [f"- **{r['key']}**: {r['notes']}" for r in results if r["notes"]]
    md += ["", "## File-level checks", ""] + file_lines
    md += ["", "## Hard expectation failures", ""] + ([f"- {p}" for p in problems] or ["- none"])
    (ROOT / "mock_data" / "duplicate_test_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(file_lines))
    print("PROBLEMS:" if problems else "ALL HARD EXPECTATIONS MET")
    for p in problems:
        print(" -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
