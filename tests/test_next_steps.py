"""The pre-submission check says what to do about each problem, not only what is wrong, and the assistant can say it in words.

A finding without a next step is a complaint. Every finding carries a plain-words step and the name of the Studio button that does it; the report carries the whole
way to a submitted mapping, in order; and the assistant answers "what should I do next?" from a fresh check of the rules as they are now.
"""

from __future__ import annotations

import pytest

from src.services.mapping_preflight import ADVICE
from tests.test_ai_safeguards import add_rule, landed  # noqa: F401 - fixtures
from tests.test_ai_safeguards import ctx, dataset, encoder, env  # noqa: F401
from tests.test_assistant import ask


def findings_by_code(report):
    out: dict[str, list] = {}
    for f in report["findings"]:
        out.setdefault(f["code"], []).append(f)
    return out


def test_an_empty_lookup_says_to_draft_the_lookups_with_the_ai_and_points_at_the_rule(landed):
    client, ctx, table, mset = landed
    country = add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")
    report = client.post(f"/api/mapping/{mset}/preflight").json()
    empty = findings_by_code(report)["EMPTY_LOOKUP"][0]
    assert empty["action"] == "draft_lookups" and empty["rule_id"] == country["rule_id"] and empty["rule_ids"] == [country["rule_id"]]
    assert "Draft lookups with AI" in empty["next_step"] and "DIRECT_COPY" in empty["next_step"]          # and the other way out, when the values are already keys
    failing = findings_by_code(report)["MOSTLY_FAILING"][0]
    assert failing["action"] == "draft_lookups" and "values it misses" in failing["next_step"]            # the failure kind decides the advice, not only the rule type


def test_every_finding_has_a_next_step_and_the_report_lists_the_way_to_a_submitted_mapping(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")
    report = client.post(f"/api/mapping/{mset}/preflight").json()
    assert report["findings"] and all(f["next_step"] for f in report["findings"]), [f["code"] for f in report["findings"] if not f["next_step"]]
    steps = report["next_steps"]
    assert steps[0].startswith("Fix the ") and "BLOCKER" in steps[0]
    assert any(s.startswith("ADRC.COUNTRY: ") and "Draft lookups with AI" in s for s in steps)
    assert steps[-3].startswith("Press 'Check rules on sample' again") and "Submit for approval" in steps[-2] and "Run pipeline" in steps[-1]
    assert len([s for s in steps if s.startswith("ADRC.COUNTRY: ")]) == 1                                  # two findings on one rule give one step, not two


def test_a_mandatory_field_without_a_rule_points_at_the_placeholders_and_a_static_value(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    report = client.post(f"/api/mapping/{mset}/preflight").json()
    mandatory = findings_by_code(report)["MANDATORY_UNMAPPED"][0]
    assert mandatory["severity"] == "WARNING" and mandatory["action"] == "add_placeholders"
    assert "Add placeholders" in mandatory["next_step"] and "STATIC_VALUE" in mandatory["next_step"] and "Data Construction" in mandatory["next_step"]


def test_a_value_that_is_too_long_says_to_shorten_it_and_opens_the_rule(landed):
    client, ctx, table, mset = landed
    body = {"target_table": "BUT000", "target_column": "NAME_ORG1", "rule_type": "SQL_EXPRESSION", "source_column": "CUST_NAME",
            "transformation_logic": "CONCAT(CUST_NAME, CUST_NAME, CUST_NAME)"}
    rule = client.post(f"/api/mapping/{mset}/rules", json=body)
    assert rule.status_code == 201, rule.text
    report = client.post(f"/api/mapping/{mset}/preflight").json()
    failing = findings_by_code(report)["MOSTLY_FAILING"][0]
    assert failing["action"] == "edit_rule" and "LEFT(TRIM(col), N)" in failing["next_step"] and failing["rule_id"] == rule.json()["rule_id"]


def test_a_passing_mapping_still_says_how_to_go_on(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME")
    country = add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")
    ctx.lookups.seed_standard_lookups(country["rule_id"], "country")
    report = client.post(f"/api/mapping/{mset}/preflight").json()
    assert report["can_submit"] is True
    assert report["next_steps"][-2].startswith("Press 'Submit for approval'") and "different person" in report["next_steps"][-2]
    assert not any(s.startswith("Fix the ") for s in report["next_steps"])


def test_every_kind_of_finding_has_advice():
    """A finding raised by the service with no entry here would show a complaint and no next step."""
    expected = {"NO_RULES", "EMPTY_LOOKUP", "SOURCE_MISSING", "REVIEW_PENDING", "MANDATORY_UNMAPPED", "UNPINNED_MODEL", "NOT_SAMPLED", "DRY_RUN_FAILED", "AWAITING_CONSTRUCTION", "LOW_YIELD"}
    assert expected <= set(ADVICE) and all(step and action for step, action in ADVICE.values())


def test_the_assistant_answers_what_should_i_do_next_from_a_fresh_check(landed):
    client, ctx, table, mset = landed
    add_rule(client, mset, "ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY")
    answer = ask(client, "What should I do next?", mapping_set_id=mset)
    assert [r["tool"] for r in answer["tool_runs"]] == ["next_steps"] and answer["tool_runs"][0]["ok"]
    assert "cannot be submitted" in answer["answer"] and "Draft lookups with AI" in answer["answer"]
    facts = answer["facts"]["next_steps"]
    assert facts["verdict"].startswith("cannot be submitted yet") and facts["blockers"] >= 2 and facts["steps"][0].startswith("Fix the ") and "can_submit" not in facts
    assert answer["tables"][0]["title"] == "What to do" and answer["tables"][0]["rows"][0][0] == "BLOCKER"

    fixed = ctx.lookups.seed_standard_lookups(client.get(f"/api/mapping/{mset}").json()["rules"][0]["rule_id"], "country")  # fix it, then ask again
    assert fixed is not None
    again = ask(client, "how do I fix this?", mapping_set_id=mset)
    assert "can be submitted" in again["answer"]                                                          # the answer follows the rules as they are now, not as they were
