"""'Fix it': what the application does about a problem when asked, not only what it says about it.

Each fix is a named, bounded action. These tests pin down that it does what its text said before the button, that it never guesses (what it could not decide is handed back),
that it never offers or accepts a constant for a field that holds customer data, and that on an approved mapping it makes an editable copy for somebody else to approve instead
of changing what an earlier run was made with.
"""

from __future__ import annotations

import pytest

from tests.test_ai_safeguards import add_rule, landed  # noqa: F401 - fixtures
from tests.test_ai_safeguards import ctx, dataset, encoder, env  # noqa: F401
from tests.test_assistant import ask
from tests.test_module7_api import STEWARD, U


def fix(client, approved=True, **body):
    """What the screen sends after the person has said yes (or, with approved=False, no)."""
    return client.post("/api/mapping/fix", json={**body, "approved": approved})


def rule_of(client, mset, target):
    return next(r for r in client.get(f"/api/mapping/{mset}").json()["rules"] if f"{r['target_table']}.{r['target_column']}" == target)


def codes(report):
    return {f["code"] for f in report["findings"]}


# ------------------------------------------------------------------ the lookup fix
def test_the_lookup_fix_says_what_it_will_do_then_fills_the_table_and_the_blocker_goes(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    country = add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")
    assert "EMPTY_LOOKUP" in codes(client.post(f"/api/mapping/{mset}/preflight").json())

    buttons = client.get(f"/api/mapping/{mset}/fixes").json()
    lookup = next(b for b in buttons if b["kind"] == "apply_lookups")
    assert lookup["rule_id"] == country["rule_id"] and lookup["target"] == "ADRC.COUNTRY" and lookup["editable_now"] is True
    assert any("without the AI" in line for line in lookup["what_happens"]) and any("certain" in line for line in lookup["what_happens"])

    result = fix(client, kind="apply_lookups", mapping_set_id=mset, rule_id=country["rule_id"]).json()
    assert result["ok"] and result["mapping_set_id"] == mset and result["copied_from"] is None and result["changes"]
    assert "rows are now covered" in result["message"] and "Check rules on sample" in result["next_step"]
    assert rule_of(client, mset, "ADRC.COUNTRY")["lookup_count"] == len(result["changes"]) or rule_of(client, mset, "ADRC.COUNTRY")["lookup_count"] > 0
    assert "EMPTY_LOOKUP" not in codes(client.post(f"/api/mapping/{mset}/preflight").json())             # the blocker is gone


def test_a_lookup_fix_on_a_rule_that_is_not_a_value_mapping_is_refused_with_the_reason(landed):
    client, ctx, table, mset = landed
    copy = add_rule(client, mset, "ADRC", "CITY1", "DIRECT_COPY", "CITY")
    refused = fix(client, kind="apply_lookups", mapping_set_id=mset, rule_id=copy["rule_id"])
    assert refused.status_code == 422 and "VALUE_MAPPING" in refused.json()["detail"]
    assert fix(client, kind="apply_lookups", mapping_set_id=mset, rule_id=999999).status_code == 404


# ------------------------------------------------------------------ the fixed-value fix
def test_a_fixed_value_fix_sets_the_field_checks_the_value_and_refuses_fields_that_hold_customer_data(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    buttons = client.get(f"/api/mapping/{mset}/fixes").json()
    statics = {b["target"]: b for b in buttons if b["kind"] == "set_static_value"}
    assert {"BUT000.TYPE", "BUT000.BU_GROUP", "KNB1.BUKRS"} <= set(statics)
    assert "ADRC.CITY1" not in statics and "ADRC.COUNTRY" not in statics                                    # a constant city or country would corrupt the data: no button
    type_button = statics["BUT000.TYPE"]
    assert type_button["inputs"][0]["name"] == "value" and any("every record" in line for line in type_button["what_happens"])

    too_long = fix(client, kind="set_static_value", mapping_set_id=mset, target="BUT000.BU_GROUP", values={"value": "TOOLONGVALUE"})
    assert too_long.status_code == 422 and "at most 4" in too_long.json()["detail"]
    blank = fix(client, kind="set_static_value", mapping_set_id=mset, target="BUT000.BU_GROUP", values={"value": " "})
    assert blank.status_code == 422
    city = fix(client, kind="set_static_value", mapping_set_id=mset, target="ADRC.CITY1", values={"value": "Berlin"})
    assert city.status_code == 422 and "holds data about each customer" in city.json()["detail"]             # refused on the server too, not only hidden in the screen

    done = fix(client, kind="set_static_value", mapping_set_id=mset, target="BUT000.BU_GROUP", values={"value": "BP01"}).json()
    assert done["ok"] and done["copied_from"] is None and "BP01" in done["message"]
    rule = rule_of(client, mset, "BUT000.BU_GROUP")
    assert rule["rule_type"] == "STATIC_VALUE" and rule["transformation_logic"] == "'BP01'" and rule["source_column"] is None
    again = fix(client, kind="set_static_value", mapping_set_id=mset, target="BUT000.BU_GROUP", values={"value": "BP02"}).json()   # replaces the earlier fixed value
    assert again["ok"] and rule_of(client, mset, "BUT000.BU_GROUP")["transformation_logic"] == "'BP02'"
    assert "BUT000.BU_GROUP" not in client.get(f"/api/mapping/{mset}").json()["unmapped_mandatory_targets"]


def test_the_placeholder_fix_adds_one_rule_per_missing_field_and_a_second_press_adds_nothing(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    missing = client.get(f"/api/mapping/{mset}").json()["unmapped_mandatory_targets"]
    first = fix(client, kind="add_placeholders", mapping_set_id=mset).json()
    assert first["ok"] and len(first["changes"]) == len(missing) > 3
    assert client.get(f"/api/mapping/{mset}").json()["unmapped_mandatory_targets"] == []
    second = fix(client, kind="add_placeholders", mapping_set_id=mset).json()
    assert second["ok"] is False and second["changes"] == [] and "already has a rule" in second["message"]


# ------------------------------------------------------------------ an approved mapping is never changed: a copy is
def approved(client, mset):
    assert client.post(f"/api/governance/submit-mapping/{mset}").status_code == 200
    assert client.post(f"/api/governance/approve-mapping/{mset}", headers=U(STEWARD)).status_code == 200


def test_a_fix_on_an_approved_mapping_makes_an_editable_copy_and_leaves_the_approved_one_alone(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    approved(client, mset)
    before = client.get(f"/api/mapping/{mset}").json()
    button = next(b for b in client.get(f"/api/mapping/{mset}/fixes").json() if b["target"] == "BUT000.BU_GROUP")
    assert button["editable_now"] is False and any("editable DRAFT copy" in line for line in button["what_happens"])

    done = fix(client, kind="set_static_value", mapping_set_id=mset, target="BUT000.BU_GROUP", values={"value": "BP01"}).json()
    new = done["mapping_set_id"]
    assert done["ok"] and done["copied_from"] == mset and new != mset and f"copy of #{mset}" in done["message"] and "somebody else" in done["next_step"]

    after = client.get(f"/api/mapping/{mset}").json()
    assert after["mapping"]["status"] == "APPROVED" and len(after["rules"]) == len(before["rules"])           # what the earlier run was made with is untouched
    copy = client.get(f"/api/mapping/{new}").json()
    assert copy["mapping"]["status"] == "DRAFT" and copy["mapping"]["created_by"] == "alice" and copy["mapping"]["version"] > before["mapping"]["version"]
    assert len(copy["rules"]) == len(before["rules"]) + 1 and rule_of(client, new, "BUT000.BU_GROUP")["transformation_logic"] == "'BP01'"
    assert rule_of(client, new, "BUT000.NAME_ORG1")["source_column"] == "CUST_NAME"                          # the rest of the mapping came along
    assert client.post(f"/api/governance/submit-mapping/{new}").status_code in (200, 422)                    # it goes through the same check and approval as any mapping


def test_a_copy_carries_lookups_and_rows_to_leave_out(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    country = add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")
    ctx.lookups.seed_standard_lookups(country["rule_id"], "country")
    lookups = rule_of(client, mset, "ADRC.COUNTRY")["lookup_count"]
    assert lookups > 5
    assert client.post(f"/api/mapping/{mset}/exclusions", json={"column": "CUST_NAME", "operator": "starts_with", "values": ["TEST "], "reason": "test customers"}).status_code == 201
    approved(client, mset)
    new = fix(client, kind="copy_to_draft", mapping_set_id=mset).json()
    assert new["ok"] and new["copied_from"] == mset
    copy_rule = rule_of(client, new["mapping_set_id"], "ADRC.COUNTRY")
    assert copy_rule["lookup_count"] == lookups and copy_rule["rule_id"] != country["rule_id"]
    assert [r["reason"] for r in client.get(f"/api/mapping/{new['mapping_set_id']}/exclusions").json()["rules"]] == ["test customers"]


# ------------------------------------------------------------------ the run: what to do about its errors, and the buttons
@pytest.fixture()
def run_with_missing_fields(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    add_rule(client, mset, "ADRC", "CITY1", "DIRECT_COPY", "CITY")
    assert client.post(f"/api/construction/placeholder-rules/{mset}").status_code == 201                      # what the screen's placeholder button does
    approved(client, mset)
    run = client.post(f"/api/pipeline/run/{mset}", json={"wave_name": "FIX"})
    assert run.status_code == 201, run.text
    return client, ctx, mset, run.json()["run_id"]


def test_the_assistant_explains_a_runs_errors_and_offers_buttons_only_where_a_constant_is_sensible(run_with_missing_fields):
    client, ctx, mset, run_id = run_with_missing_fields
    answer = ask(client, "how to fix the errors", run_id=run_id, mapping_set_id=mset)
    assert [t["tool"] for t in answer["tool_runs"]] == ["fix_plan"]                                           # about the run's errors, not a mapping check that would mix two things
    facts = answer["facts"]["fix_plan"]
    assert facts["defect_groups"] >= 4 and facts["mapping_status"] == "APPROVED" and facts["mapping_can_change_now"] is False
    assert answer["tables"][0]["title"] == "What to do about each defect"
    assert all(row[3] for row in answer["tables"][0]["rows"])                                                  # every defect group says what to do
    titles = [f["title"] for f in answer["fixes"]]
    assert "Set KNB1.BUKRS to one value for every record" in titles and not any("ADRC.COUNTRY" in t or "CITY1" in t for t in titles)
    assert all(f["editable_now"] is False and any("editable DRAFT copy" in line for line in f["what_happens"]) for f in answer["fixes"])
    assert "approval" in answer["answer"].lower() or "approve" in answer["answer"].lower()                    # it asks, it does not announce


def test_pressing_a_runs_fix_lands_in_a_copy_and_the_runs_mapping_is_unchanged(run_with_missing_fields):
    client, ctx, mset, run_id = run_with_missing_fields
    answer = ask(client, "what should I do about the errors?", run_id=run_id, mapping_set_id=mset)
    button = next(f for f in answer["fixes"] if f["target"] == "KNB1.BUKRS")
    done = fix(client, kind=button["kind"], mapping_set_id=button["mapping_set_id"], target=button["target"], values={"value": "1000"}).json()
    assert done["ok"] and done["copied_from"] == mset
    assert rule_of(client, done["mapping_set_id"], "KNB1.BUKRS")["transformation_logic"] == "'1000'"
    assert "Construction placeholder" in rule_of(client, mset, "KNB1.BUKRS")["reasoning"] if "reasoning" in rule_of(client, mset, "KNB1.BUKRS") else True
    assert client.get(f"/api/mapping/{mset}").json()["mapping"]["status"] == "APPROVED"


def test_a_mapping_without_a_run_gets_next_steps_and_buttons_from_the_assistant(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")
    answer = ask(client, "What should I do next?", mapping_set_id=mset)
    assert [t["tool"] for t in answer["tool_runs"]] == ["next_steps"]
    kinds = {f["kind"] for f in answer["fixes"]}
    assert {"apply_lookups", "set_static_value", "add_placeholders"} <= kinds and all(f["editable_now"] for f in answer["fixes"])


def test_only_a_mapper_may_press_a_fix():
    from src.api.security import required_roles
    assert required_roles("POST", "/api/mapping/fix") == frozenset({"mapper"})                              # a viewer reads, a loader loads: neither rewrites a mapping
    assert required_roles("GET", "/api/mapping/5/fixes") is None


def test_several_fixes_from_one_set_of_buttons_go_into_the_same_copy(landed):
    """The buttons were made for the approved set. After the first fix has made the copy, the screen sends the next ones to the copy: a rule named by the screen is the one of the
    copy with the same target, so a lookup fix still finds its rule."""
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    country = add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")
    ctx.lookups.seed_standard_lookups(country["rule_id"], "country")
    approved(client, mset)
    first = fix(client, kind="set_static_value", mapping_set_id=mset, target="BUT000.BU_GROUP", values={"value": "BP01"}).json()
    copy = first["mapping_set_id"]
    second = fix(client, kind="set_static_value", mapping_set_id=copy, target="BUT000.TYPE", values={"value": "2"}).json()           # what the screen sends next
    assert second["ok"] and second["mapping_set_id"] == copy and second["copied_from"] is None
    assert rule_of(client, copy, "BUT000.TYPE")["transformation_logic"] == "'2'" and rule_of(client, copy, "BUT000.BU_GROUP")["transformation_logic"] == "'BP01'"
    placeholders = fix(client, kind="add_placeholders", mapping_set_id=copy).json()
    assert placeholders["ok"] and placeholders["mapping_set_id"] == copy
    named_from_old = fix(client, kind="apply_lookups", mapping_set_id=copy, rule_id=country["rule_id"]).json()                      # the rule id is the approved set's: found by its target
    assert named_from_old["mapping_set_id"] == copy and named_from_old["copied_from"] is None and named_from_old["message"]
    assert len([s for s in client.get("/api/mapping-sets").json() if s["mapping_set_id"] != mset]) == 1                              # one copy, not one per fix


# ------------------------------------------------------------------ nothing is done without a yes, and every answer is on the audit trail
def decisions(ctx):
    return [e for e in ctx.audit.entries() if e.get("phase") == "decision"]


def test_without_an_explicit_approval_nothing_changes_and_the_refusal_is_recorded(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    body = {"kind": "set_static_value", "mapping_set_id": mset, "target": "BUT000.BU_GROUP", "values": {"value": "BP01"}}
    silent = client.post("/api/mapping/fix", json=body).json()                                                   # no answer given at all counts as no
    no = client.post("/api/mapping/fix", json={**body, "approved": False, "shown": "Set BUT000.BU_GROUP to one value for every record"}).json()
    for result in (silent, no):
        assert result["ok"] is False and result["declined"] is True and result["changes"] == [] and "not approved" in result["message"]
    assert "BUT000.BU_GROUP" in client.get(f"/api/mapping/{mset}").json()["unmapped_mandatory_targets"]              # still unmapped: nothing was written
    assert [d["decision"] for d in decisions(ctx)] == ["declined", "declined"] and decisions(ctx)[1]["shown"].startswith("Set BUT000.BU_GROUP")

    yes = fix(client, **body).json()
    assert yes["ok"] and yes["declined"] is False
    last = decisions(ctx)[-1]
    assert last["decision"] == "approved" and last["user"] == "alice" and last["target"] == "BUT000.BU_GROUP" and last["values"] == {"value": "BP01"}
    assert ctx.audit.verify()["ok"]                                                                                # the chain of decisions is intact


def test_a_fix_whose_approval_cannot_be_recorded_is_not_made(landed, monkeypatch):
    from src.services.audit_log import AuditUnavailable
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")

    def broken(event):
        raise AuditUnavailable("disk full")
    monkeypatch.setattr(ctx.audit, "append", broken)
    refused = fix(client, kind="set_static_value", mapping_set_id=mset, target="BUT000.BU_GROUP", values={"value": "BP01"})
    assert refused.status_code in (503, 500)
    assert "BUT000.BU_GROUP" in client.get(f"/api/mapping/{mset}").json()["unmapped_mandatory_targets"]


def test_every_proposal_explains_the_situation_asks_a_question_and_says_how_to_undo_it(run_with_missing_fields):
    client, ctx, mset, run_id = run_with_missing_fields
    proposals = ask(client, "how to fix the errors", run_id=run_id, mapping_set_id=mset)["fixes"]
    assert proposals
    for f in proposals:
        assert f["situation"] and f["proposal"] and f["undo"] and f["unchanged"] and f["question"].endswith("?"), f["title"]
        assert f["what_happens"]
    bukrs = next(f for f in proposals if f["target"] == "KNB1.BUKRS")
    assert "records of run" in bukrs["situation"] and "KNB1.BUKRS is empty" in bukrs["situation"] and bukrs["inputs"]
    assert "Which value" in bukrs["question"] and "approve" in bukrs["question"]
    assert any("APPROVED" in line and "run that was made with it" in line for line in bukrs["unchanged"])           # the approved mapping and its run stay as they were


# ------------------------------------------------------------------ the application disagrees before it writes
def test_a_category_that_contradicts_the_mapping_is_refused_with_the_reason_and_what_to_use_instead(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    wrong = fix(client, kind="set_static_value", mapping_set_id=mset, target="BUT000.TYPE", values={"value": "1"}, override=True).json()      # even "I insist" does not make it right
    assert wrong["ok"] is False and wrong["challenge"]["level"] == "block" and "organisation names" in wrong["challenge"]["reason"] and "Use 2" in wrong["challenge"]["suggestion"]
    assert "BUT000.TYPE" in client.get(f"/api/mapping/{mset}").json()["unmapped_mandatory_targets"]                                           # nothing was written
    right = fix(client, kind="set_static_value", mapping_set_id=mset, target="BUT000.TYPE", values={"value": "2"}).json()
    assert right["ok"] is True and right["challenge"] is None


def test_an_unusual_choice_is_questioned_and_done_only_when_the_person_insists_and_both_answers_are_on_the_audit_trail(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    asked = fix(client, kind="set_static_value", mapping_set_id=mset, target="BUT000.BU_GROUP", values={"value": "TEST"}).json()
    assert asked["ok"] is False and asked["challenge"]["level"] == "warn" and "placeholder" in asked["challenge"]["reason"]
    assert "BUT000.BU_GROUP" in client.get(f"/api/mapping/{mset}").json()["unmapped_mandatory_targets"]                                       # not done: it was questioned first
    insisted = fix(client, kind="set_static_value", mapping_set_id=mset, target="BUT000.BU_GROUP", values={"value": "TEST"}, override=True).json()
    assert insisted["ok"] is True and rule_of(client, mset, "BUT000.BU_GROUP")["transformation_logic"] == "'TEST'"
    trail = [(d["decision"], d.get("overridden")) for d in decisions(ctx)]
    assert trail == [("challenged", None), ("approved", True)]
    assert "placeholder" in decisions(ctx)[0]["why"]


def test_replacing_a_rule_that_takes_real_legacy_values_is_questioned(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    add_rule(client, mset, "KNB1", "ZTERM", "DIRECT_COPY", "PAYMENT_TERMS")
    asked = fix(client, kind="set_static_value", mapping_set_id=mset, target="KNB1.ZTERM", values={"value": "NT30"}).json()
    assert asked["challenge"]["level"] == "warn" and "PAYMENT_TERMS" in asked["challenge"]["reason"] and "thrown away" in asked["challenge"]["why"]
    assert rule_of(client, mset, "KNB1.ZTERM")["rule_type"] == "DIRECT_COPY"                                                                  # the rule is as it was
    assert fix(client, kind="set_static_value", mapping_set_id=mset, target="KNB1.ZTERM", values={"value": "NT30"}, override=True).json()["ok"] is True


def test_a_reconciliation_account_the_application_cannot_verify_is_questioned(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    asked = fix(client, kind="set_static_value", mapping_set_id=mset, target="KNB1.AKONT", values={"value": "113100"}).json()
    assert asked["challenge"]["level"] == "warn" and "cannot check" in asked["challenge"]["reason"] and "chart of accounts" in asked["challenge"]["suggestion"]


def test_a_lookup_for_a_free_text_column_is_questioned(landed):
    client, ctx, table, mset = landed
    rule = add_rule(client, mset, "BUT000", "NAME_ORG1", "VALUE_MAPPING", "CUST_NAME")                                                         # names: nearly every row differs
    asked = fix(client, kind="apply_lookups", mapping_set_id=mset, rule_id=rule["rule_id"]).json()
    assert asked["ok"] is False and asked["challenge"]["level"] == "warn" and "free text" in asked["challenge"]["reason"]
