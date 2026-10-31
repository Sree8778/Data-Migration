"""The AI assistant's helpers: a rule for one column the first pass missed, and a drafted lookup table.

The model is a scripted stand-in here (the real one is exercised by scripts/test_group2_real.py). What these tests pin down is everything AROUND the model: it
sees only the candidates that are still free, its hint is passed as data, repeated answers are voted, the answer is measured against the real file, a key
that is not on the SAP list is discarded however sure the model sounded, nothing is written until a person approves, and approval is recorded.
"""

from __future__ import annotations

import json
from collections import defaultdict

import duckdb
import pytest

from src.schemas.mapping_ai import FieldMappingRecommendation
from src.seeds.seed_sap_catalog import seed_sap_catalog
from src.services.llm_mapper_service import LLMOutputError
from tests.test_module3_mapper import embeddings, rec as rec_dict  # noqa: F401  (embeddings: a fixture)
from tests.test_module4_transform_dedupe import HEADER, csv_path, dataset, env, expected_errors  # noqa: F401
from tests.test_module7_api import encoder, make_client, make_ctx, subset, upload  # noqa: F401


class AssistLLM:
    """A model that answers as scripted and remembers what it was asked."""

    provider, deterministic, configured_model, model_name = "gemini", False, "fake-assist-1", "fake-assist-1"

    def __init__(self, recommendations: dict[str, list], keys: dict[str, list] | None = None) -> None:
        self.recommendations, self.keys = recommendations, keys or {}
        self.calls: dict[str, int] = defaultdict(int)
        self.recommend_log: list[tuple[str, list[str], str | None]] = []
        self.ask_log: list[dict] = []

    def recommend(self, ctx, candidates, hint=None):
        self.recommend_log.append((ctx.column_name, [f"{c.table_name}.{c.column_name}" for c in candidates], hint))
        seq = self.recommendations[ctx.column_name]
        item = seq[min(self.calls[ctx.column_name], len(seq) - 1)]
        self.calls[ctx.column_name] += 1
        if isinstance(item, Exception):
            raise item
        return None if item is None else FieldMappingRecommendation(**item)

    def ask(self, system, user, schema):
        payload = json.loads(user)
        self.ask_log.append({"system": system, "keys": len(payload["allowed_keys"]), "values": [v["value"] for v in payload["legacy_values"]]})
        items = []
        for entry in payload["legacy_values"]:
            value = entry["value"]
            seq = self.keys.get(value)
            if seq is None:
                items.append({"legacy_value": value, "sap_key": None, "confidence": 0.0, "reason": "do not know"})
                continue
            idx = self.calls[("ask", value)]
            self.calls[("ask", value)] += 1
            key, conf = seq[min(idx, len(seq) - 1)]
            items.append({"legacy_value": value, "sap_key": key, "confidence": conf, "reason": f"{value} is {key}"})
        return schema.model_validate({"items": items})


NAME_COPY = rec_dict("BUT000", "NAME_ORG1", 0.9)
COUNTRY_COPY = rec_dict("ADRC", "COUNTRY", 0.9)  # a plain copy of a column that holds country NAMES
COUNTRY_LOOKUP = rec_dict("ADRC", "COUNTRY", 0.9, "VALUE_MAPPING")


@pytest.fixture()
def landed(session_factory, tmp_path, encoder, dataset):
    def build(llm: AssistLLM, **settings):
        ctx = make_ctx(session_factory, tmp_path, encoder, llm=llm, **settings)
        seed_sap_catalog(ctx.metadata)
        client = make_client(ctx)
        table = upload(client, subset(dataset, 400)).json()["table_id"]
        assert client.post(f"/api/profile/{table}").status_code == 200
        mset = client.post("/api/mapping-sets", json={"source_table_id": table}).json()["mapping_set_id"]
        return client, ctx, table, mset
    return build


def add_rule(client, mset, table, column, rule_type, source, **extra):
    body = {"target_table": table, "target_column": column, "rule_type": rule_type, "source_column": source, **extra}
    r = client.post(f"/api/mapping/{mset}/rules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ suggest a rule
def test_a_suggestion_is_measured_against_the_file_and_nothing_is_saved_until_it_is_accepted(landed):
    llm = AssistLLM({"COUNTRY": [COUNTRY_COPY]})
    client, ctx, table, mset = landed(llm)
    before = client.get(f"/api/mapping/{mset}").json()["rules"]
    r = client.post(f"/api/mapping/{mset}/suggest-rule", json={"column": "country", "hint": "this holds the customer's country, written in many ways", "runs": 3})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["outcome"] == "SUGGESTED" and (s["target_table"], s["target_column"], s["rule_type"]) == ("ADRC", "COUNTRY", "DIRECT_COPY") and s["acceptable"]
    assert s["source_column"] == "COUNTRY" and s["runs"] == 3 and s["agreement"] == 1.0 and s["model_name"] == "fake-assist-1" and s["needs_lookups"] is False
    assert llm.calls["COUNTRY"] == 3 and all(h == "this holds the customer's country, written in many ways" for _, _, h in llm.recommend_log)
    ev = s["evidence"]
    assert ev["domain"] == "T005" and ev["domain_valid_rate"] < 0.95 and ev["top_invalid"] and ev["rows"] == 400
    assert any(x.startswith("measured: only") and "a value mapping is likely needed" in x for x in s["review_reasons"])        # the copy would fail most rows
    assert client.get(f"/api/mapping/{mset}").json()["rules"] == before                                                           # nothing was saved


def test_accepting_creates_an_ai_rule_that_still_needs_a_human_review(landed):
    client, ctx, table, mset = landed(AssistLLM({"COUNTRY": [COUNTRY_LOOKUP]}))
    s = client.post(f"/api/mapping/{mset}/suggest-rule", json={"column": "COUNTRY"}).json()
    assert s["needs_lookups"] is True and s["rule_type"] == "VALUE_MAPPING"
    rule = client.post(f"/api/mapping/{mset}/suggestions/{s['suggestion_id']}/accept")
    assert rule.status_code == 201, rule.text
    r = rule.json()
    assert r["ai_suggested"] and r["ai_model_name"] == "fake-assist-1" and r["needs_review"] and r["confidence"] == 0.9
    assert any("AI suggestion for a column the first pass did not map" in x for x in r["review_reasons"]) and "value lookups are not defined yet" in r["review_reasons"]
    assert "Suggested by AI at the request of alice" in client.get(f"/api/mapping/{mset}").json()["rules"][0]["reasoning"]
    assert client.post(f"/api/mapping/{mset}/suggestions/{s['suggestion_id']}/accept").status_code == 409        # that target now has a rule
    assert client.post(f"/api/mapping/{mset}/suggestions/nope/accept").status_code == 404


def test_targets_that_already_have_a_rule_are_not_offered_and_the_hint_is_only_sent_when_given(landed):
    llm = AssistLLM({"CITY": [rec_dict("ADRC", "CITY1", 0.9)], "CUST_NAME": [NAME_COPY]})
    client, ctx, table, mset = landed(llm)
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    client.post(f"/api/mapping/{mset}/suggest-rule", json={"column": "CITY"})
    column, offered, hint = llm.recommend_log[0]
    assert column == "CITY" and "BUT000.NAME_ORG1" not in offered and hint is None and len(offered) >= 8


def test_unstable_answers_are_flagged_and_a_missing_match_or_a_made_up_target_cannot_be_accepted(landed):
    flip = [COUNTRY_COPY, COUNTRY_LOOKUP, COUNTRY_COPY]
    llm = AssistLLM({"COUNTRY": flip, "CITY": [None], "TAX_ID": [rec_dict("KNB1", "NOPE_COLUMN", 0.95)], "PHONE": [LLMOutputError("garbage")],
                     "EMAIL": [rec_dict("BUT000", "NAME_ORG2", 0.9, "SQL_EXPRESSION", "UPPER(TRIM(EMAIL)); DROP TABLE cat_columns")]})
    client, ctx, table, mset = landed(llm)
    s = client.post(f"/api/mapping/{mset}/suggest-rule", json={"column": "COUNTRY", "runs": 3}).json()
    assert s["rule_type"] == "DIRECT_COPY" and s["agreement"] == pytest.approx(2 / 3) and any(x.startswith("unstable: 2 of 3 runs") for x in s["review_reasons"])
    none = client.post(f"/api/mapping/{mset}/suggest-rule", json={"column": "CITY", "runs": 2}).json()
    assert none["outcome"] == "NO_MATCH" and not none["acceptable"] and client.post(f"/api/mapping/{mset}/suggestions/{none['suggestion_id'] or 'x'}/accept").status_code == 404
    for column in ("TAX_ID", "EMAIL"):
        bad = client.post(f"/api/mapping/{mset}/suggest-rule", json={"column": column, "runs": 1}).json()
        assert bad["outcome"] == "REJECTED" and not bad["acceptable"] and bad["suggestion_id"]
        refused = client.post(f"/api/mapping/{mset}/suggestions/{bad['suggestion_id']}/accept")
        assert refused.status_code == 422 and "cannot be stored" in refused.json()["detail"]
    assert client.post(f"/api/mapping/{mset}/suggest-rule", json={"column": "PHONE", "runs": 1}).json()["outcome"] == "ERROR"
    assert not any(r["source_column"] in ("TAX_ID", "EMAIL", "CITY", "PHONE") for r in client.get(f"/api/mapping/{mset}").json()["rules"])


def test_a_suggestion_needs_a_draft_mapping_set_an_unmapped_column_and_a_real_column(landed):
    client, ctx, table, mset = landed(AssistLLM({"COUNTRY": [COUNTRY_COPY]}))
    add_rule(client, mset, "ADRC", "COUNTRY", "DIRECT_COPY", "COUNTRY")
    taken = client.post(f"/api/mapping/{mset}/suggest-rule", json={"column": "COUNTRY"})
    assert taken.status_code == 409 and "already has a rule" in taken.json()["detail"]
    assert client.post(f"/api/mapping/{mset}/suggest-rule", json={"column": "NOPE"}).status_code == 404
    assert client.post(f"/api/mapping/9999/suggest-rule", json={"column": "CITY"}).status_code == 404
    assert client.post(f"/api/governance/submit-mapping/{mset}", headers={"X-User": "alice"}).status_code in (200, 422)
    if client.get(f"/api/mapping/{mset}").json()["mapping"]["status"] == "SUBMITTED":
        late = client.post(f"/api/mapping/{mset}/suggest-rule", json={"column": "CITY"})
        assert late.status_code == 409 and "DRAFT" in late.json()["detail"]


# ------------------------------------------------------------------ drafted lookups
def with_values(ctx, table, values):
    """Overwrite the COUNTRY of the first rows of the landed file with the given values, so the draft has awkward values to translate."""
    path = ctx.source_file(table).as_posix()
    con = duckdb.connect()
    con.execute(f"CREATE TABLE s AS SELECT *, row_number() OVER () AS rn FROM read_parquet('{path}')")
    for i, v in enumerate(values, start=1):
        con.execute(f"UPDATE s SET COUNTRY = ? WHERE rn = {i}", [v])
    con.execute(f"COPY (SELECT * EXCLUDE (rn) FROM s) TO '{path}' (FORMAT parquet)")


COUNTRY_KEYS = {"Frankreich": [("FR", 0.95)], "Nederland": [("NL", 0.9)], "Amerika": [("US", 0.9)], "Bharat": [("IN", 0.9), ("BD", 0.9)],
                "Narnia": [("ZZ", 0.99)], "Wakanda": [("US", 0.4)]}
AWKWARD = ["Frankreich"] * 6 + ["Nederland"] * 5 + ["Amerika"] * 4 + ["Bharat"] * 3 + ["Narnia"] * 2 + ["Wakanda"] + ["Atlantis"] * 2


def lookup_rule(client, mset):
    return add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")["rule_id"]


def test_a_lookup_draft_translates_what_it_can_without_the_model_and_discards_keys_that_do_not_exist(landed):
    llm = AssistLLM({}, COUNTRY_KEYS)
    client, ctx, table, mset = landed(llm)
    with_values(ctx, table, AWKWARD)
    rid = lookup_rule(client, mset)
    d = client.post(f"/api/mapping/rules/{rid}/lookup-draft", json={"runs": 2}).json()
    by = {e["legacy_value"]: e for e in d["entries"]}
    assert d["domain"] == "T005" and "built-in" in d["domain_note"] and d["target"] == "ADRC.COUNTRY" and d["model_name"] == "fake-assist-1"
    assert by["US"]["method"] == "valid_key" and by["US"]["proposed"] == "US" and by["US"]["suggested_accept"]                 # no model needed
    assert by["Germany"]["method"] == "synonym_list" and by["Germany"]["proposed"] == "DE" and by["Germany"]["confidence"] == 0.95
    assert by["Frankreich"]["method"] == "ai" and by["Frankreich"]["proposed"] == "FR" and by["Frankreich"]["suggested_accept"] and by["Frankreich"]["rows"] >= 6
    assert by["Narnia"]["proposed"] is None and by["Narnia"]["method"] == "none"                                                # ZZ is not a country key: discarded
    assert by["Atlantis"]["proposed"] is None and by["Atlantis"]["method"] == "none"                                           # the model did not know
    assert by["Wakanda"]["method"] == "ai" and by["Wakanda"]["confidence"] == 0.4 and by["Wakanda"]["suggested_accept"] is False  # not sure enough to pre-tick
    assert by["Bharat"]["agreement"] is False and by["Bharat"]["suggested_accept"] is False and "disagreed" in by["Bharat"]["reason"]   # two runs, two answers
    asked = {v for call in llm.ask_log for v in call["values"]}
    assert "US" not in asked and "Germany" not in asked and {"Frankreich", "Narnia", "Atlantis"} <= asked                          # only the hard ones reached the model
    assert all(call["keys"] > 200 and "never guess" in call["system"] for call in llm.ask_log)
    assert client.get(f"/api/mapping/rules/{rid}/lookup-draft").json()["entries"] == d["entries"]                              # kept for the steward
    assert client.get(f"/api/mapping/{mset}").json()["rules"][0]["lookup_count"] == 0                                           # drafting wrote nothing


def test_only_approved_entries_are_written_with_their_approver_and_a_key_must_exist(landed):
    client, ctx, table, mset = landed(AssistLLM({}, COUNTRY_KEYS))
    with_values(ctx, table, AWKWARD)
    rid = lookup_rule(client, mset)
    d = client.post(f"/api/mapping/rules/{rid}/lookup-draft", json={}).json()
    before = d["coverage"]
    assert before["covered_now"] == 0 and before["covered_if_suggested"] > 0 and before["covered_if_all"] >= before["covered_if_suggested"]
    pre = [e["legacy_value"] for e in d["entries"] if e["suggested_accept"]]
    ok = client.post(f"/api/mapping/rules/{rid}/lookup-draft/apply", json={"accept": pre}, headers={"X-User": "steward.sam"})
    assert ok.status_code == 200, ok.text
    r = ok.json()
    assert r["applied"] == len(pre) == r["lookups_now"] and r["approved_by"] == "steward.sam" and r["coverage"]["covered_now"] == before["covered_if_suggested"]
    stored = ctx.lookups.get_lookups(rid)
    assert stored["Frankreich"] == "FR" and stored["US"] == "US" and "Narnia" not in stored and "Wakanda" not in stored                # exactly what was ticked
    rule = client.get(f"/api/mapping/{mset}").json()["rules"][0]
    assert "steward.sam" in rule["reasoning"] and "approved from an AI draft (fake-assist-1)" in rule["reasoning"]
    after = client.get(f"/api/mapping/rules/{rid}/lookup-draft").json()
    assert {e["legacy_value"] for e in after["entries"] if e["status"] == "approved"} == set(pre) and all(
        e["approved_by"] == "steward.sam" for e in after["entries"] if e["status"] == "approved")

    bad = client.post(f"/api/mapping/rules/{rid}/lookup-draft/apply", json={"accept": ["Wakanda"], "overrides": {"Wakanda": "ZZ"}})
    assert bad.status_code == 422 and "not a key in T005" in bad.json()["detail"]                                                  # the steward cannot invent a key either
    mine = client.post(f"/api/mapping/rules/{rid}/lookup-draft/apply", json={"accept": ["Wakanda"], "overrides": {"Wakanda": "KE"}})
    assert mine.status_code == 200 and ctx.lookups.get_lookups(rid)["Wakanda"] == "KE"                                             # but may choose another real one
    assert client.post(f"/api/mapping/rules/{rid}/lookup-draft/apply", json={"accept": ["Narnia"]}).status_code == 422             # no proposal and no override
    assert client.post(f"/api/mapping/rules/{rid}/lookup-draft/apply", json={"accept": ["not in the draft"]}).status_code == 422


def test_the_measured_pass_rate_improves_after_the_approved_lookups(landed):
    client, ctx, table, mset = landed(AssistLLM({}, COUNTRY_KEYS))
    with_values(ctx, table, AWKWARD)
    rid = lookup_rule(client, mset)
    ctx.lookups.register_lookups(rid, {"UK": "GB"})                                                                                  # one entry, so the rule is not empty
    empty = next(r for r in client.post(f"/api/mapping/{mset}/preflight").json()["rules"] if r["rule_id"] == rid)["pass_rate"]
    d = client.post(f"/api/mapping/rules/{rid}/lookup-draft", json={}).json()
    client.post(f"/api/mapping/rules/{rid}/lookup-draft/apply", json={"accept": [e["legacy_value"] for e in d["entries"] if e["suggested_accept"]]})
    better = next(r for r in client.post(f"/api/mapping/{mset}/preflight").json()["rules"] if r["rule_id"] == rid)["pass_rate"]
    assert better > empty + 0.5                                                                                                      # the drafted lookups cover most rows


def test_a_draft_needs_a_known_key_list_a_value_mapping_rule_and_a_draft_mapping_set(landed):
    client, ctx, table, mset = landed(AssistLLM({}, COUNTRY_KEYS))
    group = add_rule(client, mset, "BUT000", "BU_GROUP", "VALUE_MAPPING", "CUST_NAME")["rule_id"]                                    # account groups (TB001) are the customer's own
    unknown = client.post(f"/api/mapping/rules/{group}/lookup-draft", json={})
    assert unknown.status_code == 422 and "TB001" in unknown.json()["detail"] and "could not be checked" in unknown.json()["detail"]
    copy = add_rule(client, mset, "ADRC", "CITY1", "DIRECT_COPY", "CITY")["rule_id"]
    assert client.post(f"/api/mapping/rules/{copy}/lookup-draft", json={}).status_code == 422
    assert client.post("/api/mapping/rules/9999/lookup-draft", json={}).status_code == 404
    assert client.get(f"/api/mapping/rules/{group}/lookup-draft").json() is None
    no_draft = client.post(f"/api/mapping/rules/{group}/lookup-draft/apply", json={"accept": ["x"]})
    assert no_draft.status_code == 404 and "draft the lookups first" in no_draft.json()["detail"]


def test_currency_and_incoterms_can_be_drafted_because_their_key_lists_are_built_in(landed):
    llm = AssistLLM({}, {"Euro": [("EUR", 0.95)], "Dollar": [("USD", 0.9)], "Quatloo": [("QQQ", 0.99)]})
    client, ctx, table, mset = landed(llm)
    path = ctx.source_file(table).as_posix()
    con = duckdb.connect()
    con.execute(f"CREATE TABLE s AS SELECT *, row_number() OVER () AS rn FROM read_parquet('{path}')")
    for i, v in enumerate(["Euro"] * 5 + ["Dollar"] * 4 + ["EUR"] * 3 + ["Quatloo"] * 2, start=1):
        con.execute(f"UPDATE s SET COUNTRY = ? WHERE rn = {i}", [v])                                                                  # any text column will do for the test
    con.execute(f"COPY (SELECT * EXCLUDE (rn) FROM s) TO '{path}' (FORMAT parquet)")
    rid = add_rule(client, mset, "KNVV", "WAERS", "VALUE_MAPPING", "COUNTRY")["rule_id"]
    d = client.post(f"/api/mapping/rules/{rid}/lookup-draft", json={}).json()
    by = {e["legacy_value"]: e for e in d["entries"]}
    assert d["domain"] == "TCURC" and "ISO 4217" in d["domain_note"] and by["EUR"]["method"] == "valid_key" and by["Euro"]["proposed"] == "EUR"
    assert by["Quatloo"]["proposed"] is None                                                                                         # QQQ is not a currency


# ------------------------------------------------------------------ a person's hint steers which targets the model is shown
def test_a_hint_brings_the_right_target_into_the_candidates_a_cryptic_name_alone_does_not(embeddings):
    labels = lambda cands: [f"{c.table_name}.{c.column_name}" for c in cands]  # noqa: E731
    # a name that means nothing to anyone (KTOKD itself is now found without a hint: the SAP alias is in the field's description)
    plain = labels(embeddings.get_top_k_candidates("XQ7", "VARCHAR", 12, ["Z002", "ZPER", "Z003"]))
    hinted = labels(embeddings.get_top_k_candidates("XQ7", "VARCHAR", 12, ["Z002", "ZPER", "Z003"], hint="this is the customer account group, it decides the business partner grouping"))
    assert "BUT000.BU_GROUP" in hinted
    assert hinted.index("BUT000.BU_GROUP") <= (plain.index("BUT000.BU_GROUP") if "BUT000.BU_GROUP" in plain else 99)           # never worse than without the hint
    assert len(hinted) == 12 and len(set(hinted)) == 12                                                                     # still a full, duplicate-free list
    assert labels(embeddings.get_top_k_candidates("XQ7", "VARCHAR", 12, ["Z002", "ZPER"], hint="")) == labels(embeddings.get_top_k_candidates("XQ7", "VARCHAR", 12, ["Z002", "ZPER"]))
    assert "BUT000.BU_GROUP" in labels(embeddings.get_top_k_candidates("KTOKD", "VARCHAR", 3, ["Z002", "ZPER", "Z003"]))       # and the SAP name alone now finds it, with no hint
