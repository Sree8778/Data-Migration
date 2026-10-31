"""Faults the million-row scale test found in the duplicate matching, pinned down on small data.

  * "Maria Garcia" in the same city, with no street, phone or tax number, was merged and labelled as backed by an address: a very common name plus a city proves nothing. It is
    now sent to review (both records load); a RARE name with the same evidence still merges, and a common name WITH a street still merges.
  * a shared phone number linked two companies with different tax numbers, although "two tax numbers are two legal entities" was already the rule for name matches. The rule now
    holds for shared e-mail and phone too; a record without a tax number can still be linked.
"""

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from src.schemas.execution import DedupConfig, ExactKey
from src.services.deduplication_engine import DeduplicationEngine


def staged(tmp_path, rows: list[dict]):
    path = tmp_path / "staging.parquet"
    frame = pd.DataFrame(rows)
    con = duckdb.connect()
    con.register("f", frame)
    con.execute(f"COPY (SELECT * FROM f) TO '{path.as_posix()}' (FORMAT parquet)")
    return path


def row(pk, name, city="Rome", street="", tax="", phone="", email=""):
    return {"_SRC_PK": str(pk), "NAME": name, "STREET": street, "HOUSE_NUM1": "", "CITY1": city, "POST_CODE1": "", "SRC_TAX": tax, "SRC_PHONE": phone, "SRC_EMAIL": email}


CONFIG = DedupConfig(
    status_column=None, name_column="NAME", address_columns=["STREET", "HOUSE_NUM1", "CITY1", "POST_CODE1"],
    exact_keys=[ExactKey(name="tax", columns=["SRC_TAX"], normalizer="alnum", vetoes_fuzzy=True), ExactKey(name="phone", columns=["SRC_PHONE"], normalizer="digits"),
                ExactKey(name="email", columns=["SRC_EMAIL"], normalizer="email")])


def run(tmp_path, rows):
    out = tmp_path / "out.parquet"
    report = DeduplicationEngine().deduplicate(staged(tmp_path, rows), out, CONFIG)
    frame = duckdb.connect().execute(f"SELECT _SRC_PK, is_duplicate_child, dedup_match_strength FROM read_parquet('{out.as_posix()}') ORDER BY 1").fetchall()
    return report, {r[0]: (r[1], r[2]) for r in frame}


def test_a_common_name_with_nothing_but_a_city_is_not_merged_and_goes_to_review(tmp_path):
    rows = [row(i, "Maria Garcia", city="Rome", email=f"mg{i}@x.example") for i in range(1, 8)]          # seven of them, same city, nothing else: seven different people
    report, result = run(tmp_path, rows)
    assert report.duplicate_children == 0 and not any(child for child, _ in result.values())
    assert report.common_name_pairs_held_back >= 6 and report.possible_duplicate_pairs >= 6                  # not lost: a person decides
    assert any("COMMON name" in w for w in report.warnings)


def test_a_rare_name_with_the_same_evidence_still_merges_but_is_labelled_name_only(tmp_path):
    rows = [row(1, "Zyxwv Quokka GmbH", email="a@x.example"), row(2, "Zyxwv Quokka GmbH", email="b@x.example")] + [row(i, f"Other Company {i}", email=f"o{i}@x.example") for i in range(3, 9)]
    report, result = run(tmp_path, rows)
    assert report.duplicate_children == 1 and report.common_name_pairs_held_back == 0
    assert {result["1"][0], result["2"][0]} == {True, False} and "NAME_ONLY" in {result["1"][1], result["2"][1]}


def test_a_common_name_with_a_street_that_matches_is_still_one_customer(tmp_path):
    rows = [row(1, "Maria Garcia", street="Main Street 4", email="a@x.example"), row(2, "Maria Garcia", street="Main Street 4", email="b@x.example")]
    rows += [row(i, "Maria Garcia", city=f"City{i}", street=f"Other Road {i}", email=f"m{i}@x.example") for i in range(3, 9)]
    report, result = run(tmp_path, rows)
    assert result["1"][0] != result["2"][0] and report.duplicate_children == 1                                # exactly the pair that shares street and city


def test_a_shared_phone_does_not_make_two_legal_entities_one(tmp_path):
    rows = [row(1, "Alder Tools GmbH", tax="DE111111111", phone="+49 30 555 1234"), row(2, "Birch Foods SA", tax="FR222222222", phone="+49 30 555 1234")]
    report, result = run(tmp_path, rows)
    assert report.duplicate_children == 0 and report.exact_links_vetoed == 1
    assert any("different tax numbers" in w for w in report.warnings)


def test_a_shared_phone_still_links_when_one_record_has_no_tax_number_or_the_same_one(tmp_path):
    same = [row(1, "Alder Tools GmbH", tax="DE 111-111-111", phone="+49 30 555 1234"), row(2, "Alder Werkzeuge", tax="DE111111111", phone="+49 30 555 1234")]
    assert run(tmp_path, same)[0].duplicate_children == 1                                                   # the same number written two ways: one entity
    one_missing = [row(1, "Alder Tools GmbH", tax="DE111111111", phone="+49 30 555 9999"), row(2, "Alder Tools", tax="", phone="+49 30 555 9999")]
    assert run(tmp_path, one_missing)[0].duplicate_children == 1                                            # nothing to contradict it


def test_a_record_without_a_tax_number_cannot_join_two_companies_with_different_ones(tmp_path):
    """1 and 3 carry different tax numbers; 2 has none and shares the phone number with both. It may join ONE of them, never both: the second link is refused (and said so)."""
    rows = [row(1, "Alder Tools GmbH", tax="DE111111111", phone="+49 30 555 1234"), row(2, "Alder Tools", tax="", phone="+49 30 555 1234"),
            row(3, "Alder Tools Co", tax="DE999999999", phone="+49 30 555 1234")]
    report, result = run(tmp_path, rows)
    assert report.duplicate_children == 1 and report.chain_links_refused == 1 and report.exact_links_vetoed == 1
    assert result["1"][0] is False or result["3"][0] is False                                   # the two tax numbers did not end up in one group
    assert any("two different tax numbers into one group" in w for w in report.warnings)


def test_a_person_can_still_force_the_merge_of_two_tax_numbers(tmp_path):
    """A decision by a person is never refused (the group is then reported as one that holds two tax numbers)."""
    rows = [row(1, "Alder Tools GmbH", tax="DE111111111"), row(2, "Birch Foods SA", tax="DE999999999")]
    config = CONFIG.model_copy(update={"must_link": [("1", "2")]})
    out = tmp_path / "o.parquet"
    report = DeduplicationEngine().deduplicate(staged(tmp_path, rows), out, config)
    assert report.duplicate_children == 1 and report.chain_links_refused == 0


def test_the_cutoff_does_not_change_what_is_found(tmp_path):
    """score_cutoff makes the comparison faster, not different: a typo'd name with the same street is still the same customer, a different name is not."""
    rows = [row(1, "Kamori Foods GmbH", street="Hafen Str 4"), row(2, "Kamori Foods GmbH", street="Hafen Str 4", email="z@x.example"),
            row(3, "Kamoir Foods GmbH", street="Hafen Str 4", email="y@x.example"), row(4, "Totally Different Ltd", street="Hafen Str 4", email="w@x.example")]
    report, result = run(tmp_path, rows)
    assert report.duplicate_children == 2 and result["4"][0] is False


# ---- found by the second million-row dataset (a German-style export) ---------------------------------------------------------------------------------------------------------------

def test_a_name_written_the_other_way_round_is_the_same_customer(tmp_path):
    """"Müller, Hans" and "Hans Müller": the comparison ignores word order, but the first blocking key (the start of the name) used to put them in different blocks."""
    rows = [row(1, "Hans Müller", street="Hauptstraße 12", city="Köln"), row(2, "Müller, Hans", street="Hauptstraße 12", city="Köln")]
    rows += [row(i, f"Firma Nummer{i}", street=f"Nebenweg {i}", city="Köln") for i in range(3, 12)]
    report, result = run(tmp_path, rows)
    assert report.duplicate_children == 1 and result["1"][0] != result["2"][0]


def test_the_same_site_is_found_in_a_street_so_common_that_its_block_is_cut_down(tmp_path):
    """Hundreds of records on "Hauptstraße" make the address block huge, and an oversized block is only compared window-wise by name (records far apart in the alphabet never meet).
    The house number + postal code block finds the pair anyway."""
    config = CONFIG.model_copy(update={"max_block_size": 8})
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    filler = []
    for k in range(900):
        r = row(100 + k, f"{letters[k % 26]}{letters[(k // 26) % 26].lower()}{letters[k // 676].lower()}tal Handel", street="Hauptstraße", city="Köln")
        r["HOUSE_NUM1"], r["POST_CODE1"] = str(k + 1), "50667"   # every filler on its own house: different sites
        filler.append(r)
    a, b = row(1, "Bamburger Zorro", street="Hauptstraße", city="Köln"), row(2, "Wamburger Zorro", street="Hauptstr.", city="Köln")   # a typo in the first letter: the name blocks differ
    a["HOUSE_NUM1"] = b["HOUSE_NUM1"] = "777"
    a["POST_CODE1"] = b["POST_CODE1"] = "50667"
    out = tmp_path / "out.parquet"
    report = DeduplicationEngine().deduplicate(staged(tmp_path, filler + [a, b]), out, config)
    assert report.duplicate_children == 1


def test_a_german_vat_id_column_is_recognised_as_a_tax_number():
    from src.api.routes.pipeline import _TAX_COLUMN
    for name in ("UST_IDNR", "USTIDNR", "UST_ID", "STEUERNR", "STEUER_NR", "TAX_ID", "VAT_NO", "STCEG", "SIRET", "PIVA"):
        assert _TAX_COLUMN.search(name), name
    for name in ("TAXI_ZONE", "VATERNAME", "AUSTRIA", "INDUSTRY_CODE", "STATUS", "FIRMENNAME", "KUNDE_NR"):
        assert not _TAX_COLUMN.search(name), name


def test_a_table_without_any_tax_column_says_so(tmp_path):
    config = CONFIG.model_copy(update={"exact_keys": [ExactKey(name="email", columns=["SRC_EMAIL"], normalizer="email")]})
    report = DeduplicationEngine().deduplicate(staged(tmp_path, [row(1, "A Firma", street="X 1"), row(2, "B Firma", street="Y 2")]), tmp_path / "o.parquet", config)
    assert any("No tax / VAT number column" in w for w in report.warnings)
    assert not any("No tax / VAT number column" in w for w in run(tmp_path, [row(1, "A Firma", street="X 1"), row(2, "B Firma", street="Y 2")])[0].warnings)


def test_accents_do_not_change_which_words_come_first(tmp_path):
    """token_sort_ratio("EVA ÇELIK", "CELIK EVA") is 44: the cedilla sorts the word to the other end. Names are folded (Ü = UE, Ç = C) before they are compared."""
    pairs = [("Eva Çelik", "Eva Celik"), ("Müller, Hans", "Hans Mueller"), ("Südbau Handel GmbH", "Suedbau Handel GmbH"), ("Åsa Huber", "Asa Huber")]
    for n, (x, y) in enumerate(pairs):
        rows = [row(1, x, street="Ahornweg", city="Köln"), row(2, y, street="Ahornweg", city="Köln")]
        rows[0]["HOUSE_NUM1"] = rows[1]["HOUSE_NUM1"] = "166"
        rows[0]["POST_CODE1"] = rows[1]["POST_CODE1"] = "50667"
        sub = tmp_path / f"p{n}"
        sub.mkdir()
        assert run(sub, rows)[0].duplicate_children == 1, (x, y)


def test_a_misplaced_space_in_a_name_at_the_same_address_is_still_the_same_customer(tmp_path):
    rows = [row(1, "Claudia Huber", street="Dorfstraße"), row(2, "Claudi aHuber", street="Dorfstraße"), row(3, "Schmidt Holzbau GmbH", street="Ahornplatz"), row(4, "Schmid tHolzbau GmbH", street="Ahornplatz")]
    for r in rows[:2]:
        r["HOUSE_NUM1"], r["POST_CODE1"] = "93", "16989"
    for r in rows[2:]:
        r["HOUSE_NUM1"], r["POST_CODE1"] = "155", "75501"
    assert run(tmp_path, rows)[0].duplicate_children == 2


def test_same_name_city_postal_code_and_number_but_a_different_street_is_not_merged(tmp_path):
    """Found by the second dataset: "Peter Huber, Goethestraße 95, 50474" and "Peter Huber, Eichenring 95, 50474" were merged because the name carries most of the score."""
    rows = [row(1, "Peter Huber", street="Goethestraße", city="Hedorf"), row(2, "Peter Huber", street="Eichenring", city="Hedorf")]
    for r in rows:
        r["HOUSE_NUM1"], r["POST_CODE1"] = "95", "50474"
    report, result = run(tmp_path, rows)
    assert report.duplicate_children == 0 and report.street_conflict_pairs_held_back == 1 and report.possible_duplicate_pairs == 1       # both load; a person can still decide
    assert any("plainly different STREET names" in w for w in report.warnings)


def test_street_spellings_and_typos_are_still_the_same_street(tmp_path):
    cases = [("Hauptstraße", "Hauptstr."), ("Goethestraße", "Goethestrasse"), ("Goethestraße", "Goethestrase"), ("Ahornstraße", "Ahorn Str."), ("Bahnhofstraße", "Bahnhof Strasse")]
    for n, (x, y) in enumerate(cases):
        rows = [row(1, "Peter Huber", street=x, city="Hedorf"), row(2, "Peter Huber", street=y, city="Hedorf")]
        for r in rows:
            r["HOUSE_NUM1"], r["POST_CODE1"] = "95", "50474"
        sub = tmp_path / f"c{n}"
        sub.mkdir()
        report, _ = run(sub, rows)
        assert report.duplicate_children == 1 and report.street_conflict_pairs_held_back == 0, (x, y)


def test_the_key_columns_of_a_nested_source_are_recognised_by_their_dotted_names():
    """A document database flattens contact.email to a dotted column name; only '_' used to end a word, so the e-mail, phone and tax columns of Firebase / MongoDB were not keys."""
    from src.api.routes.pipeline import _EMAIL_COLUMN, _PHONE_COLUMN, _TAX_COLUMN
    for rx, good, bad in ((_EMAIL_COLUMN, ["contact.email", "EMAIL", "SMTP_ADDR", "billing.contact.mail"], ["EMAILER", "remailing", "contact.emailing"]),
                          (_PHONE_COLUMN, ["contact.phone", "TELEFON", "TELF1", "a.b.mobile"], ["telephoned", "headphones"]),
                          (_TAX_COLUMN, ["company.tax_id", "billing.vat", "UST_IDNR", "TAX_ID"], ["TAXI_ZONE", "company.taxi", "VATERNAME"])):
        assert all(rx.search(x) for x in good), [x for x in good if not rx.search(x)]
        assert not any(rx.search(x) for x in bad), [x for x in bad if rx.search(x)]
