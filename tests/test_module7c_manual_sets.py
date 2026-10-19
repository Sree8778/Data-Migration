"""Manual (no-AI) mapping sets and the profiler's name-aware pattern flags."""

from __future__ import annotations

from src.api.schemas import ColumnHealth
from src.api.views import column_health
from src.schemas.profiler import ColumnProfile, PatternConformity, TopValue
from tests.test_module7_api import (  # noqa: F401  (fixtures + helpers)
    SCRIPT,
    api_env,
    client,
    csv_path,
    ctx,
    dataset,
    encoder,
    env,
    subset,
    upload,
)


def test_empty_mapping_set_can_be_created_by_hand_and_enforces_segregation_of_duties(client, dataset):
    client.post("/api/admin/seed-sap-catalog")
    table_id = upload(client, subset(dataset)).json()["table_id"]

    created = client.post("/api/mapping-sets", json={"source_table_id": table_id})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "DRAFT" and body["rule_count"] == 0 and body["version"] == 1
    assert body["created_by"] == "alice" and body["target_table"] == "BUT000"
    assert client.post("/api/mapping-sets", json={"source_table_id": table_id}).json()["version"] == 2

    mid = body["mapping_set_id"]
    rule = client.post(f"/api/mapping/{mid}/rules", json={"target_table": "BUT000", "target_column": "NAME_ORG1",
                                                          "rule_type": "DIRECT_COPY", "source_column": "CUST_NAME"})
    assert rule.status_code == 201
    assert client.get(f"/api/mapping/{mid}").json()["mapping"]["rule_count"] == 1

    client.post(f"/api/governance/submit-mapping/{mid}")
    own = client.post(f"/api/governance/approve-mapping/{mid}")  # the creator cannot approve their own mapping
    assert own.status_code == 403 and own.json()["code"] == "SEGREGATION_OF_DUTIES"
    assert client.post(f"/api/governance/approve-mapping/{mid}", headers={"X-User": "data.steward"}).status_code == 200


def test_empty_mapping_set_validation(client, dataset):
    client.post("/api/admin/seed-sap-catalog")
    table_id = upload(client, subset(dataset)).json()["table_id"]
    assert client.post("/api/mapping-sets", json={"source_table_id": 999999}).status_code == 404
    assert client.post("/api/mapping-sets", json={"source_table_id": table_id, "target_object": "MATERIAL"}).status_code == 404
    assert client.post("/api/mapping-sets", json={"source_table_id": table_id, "bogus": 1}).status_code == 422
    assert client.post("/api/mapping-sets", json={}).status_code == 422
    target = client.get("/api/mapping-sets").json()
    assert target == []  # nothing half-created
    tid = client.post("/api/mapping-sets", json={"source_table_id": table_id}).json()["mapping_set_id"]
    placeholders = client.post(f"/api/construction/placeholder-rules/{tid}").json()
    assert {p["target"] for p in placeholders} >= {"BUT000.BU_GROUP", "KNB1.AKONT"}


def _profile(name: str, matched: float) -> ColumnProfile:
    pattern = PatternConformity(matched=int(matched * 100), checked=100, ratio=matched)
    zero = PatternConformity(matched=0, checked=100, ratio=0.0)
    return ColumnProfile(column_name=name, position=1, detected_type="VARCHAR", total_rows=100, null_count=0, null_ratio=0.0,
                         distinct_count=7, distinct_ratio=0.07, min_length=3, max_length=13,
                         top_values=[TopValue(value="x", count=1)], sample_values=["x"], leading_zero_risk=False,
                         conformity={"email": zero, "iso_country_2": zero, "numeric_string": zero, "leading_zero_numeric": zero,
                                     "postal_code": pattern})


def test_pattern_flags_only_apply_to_columns_that_look_like_what_they_are_tested_for():
    payment: ColumnHealth = column_health(_profile("PAYMENT_TERMS", 0.9), pii_masked=False)
    assert not any("POSTAL" in f for f in payment.flags) and payment.health_score == 100.0  # was a false positive

    postal = column_health(_profile("POSTAL_CODE", 0.9), pii_masked=False)
    assert "POSTAL_PATTERN_VIOLATIONS" in postal.flags and postal.health_score == 98.0
    assert "POSTAL_PATTERN_VIOLATIONS" in column_health(_profile("zip", 0.9), False).flags
    assert "POSTAL_PATTERN_VIOLATIONS" not in column_health(_profile("POSTAL_CODE", 1.0), False).flags


def test_default_pipeline_deduplicates_on_tax_id_without_any_configuration(api_env, client):
    """The mock-data run showed tax-ID duplicates went unseen: the tax column was never loaded into staging."""
    run = client.post(f"/api/pipeline/run/{api_env['mapping'].id}", json={}).json()
    dedup = run["dedup"]
    assert dedup["exact_edges_by_key"].get("tax_tax_id", 0) > 0
    exact = [s for s in dedup["samples"] if s["match_type"].startswith("EXACT:tax_tax_id")]
    assert exact and all(len(s["member_pks"]) >= 2 for s in exact)
    # an explicit dedup config still wins, and no tax key is added behind its back
    explicit = client.post(f"/api/pipeline/run/{api_env['mapping'].id}",
                           json={"dedup": {"name_column": "BUT000_NAME_ORG1", "address_columns": ["ADRC_STREET"]}}).json()
    assert explicit["dedup"]["exact_edges_by_key"] == {}
