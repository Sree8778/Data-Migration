"""Cycle comparison and the programme dashboard."""

from __future__ import annotations

from tests.test_module7_api import SCRIPT, api_env, client, csv_path, ctx, dataset, encoder, env, subset, upload  # noqa: F401


def _run(client, mapping_id, wave, **body):
    r = client.post(f"/api/pipeline/run/{mapping_id}", json={"wave_name": wave, **body})
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def test_two_cycles_are_compared_defect_group_by_defect_group(client, api_env, ctx):
    mid = api_env["mapping"].id
    first = _run(client, mid, "MOCK_1")
    second = _run(client, mid, "MOCK_2", lookup_fallback="PASS_THROUGH")           # a different treatment of unknown lookup values: the defects change
    r = client.get("/api/cycles/compare", params={"base": first, "other": second})
    assert r.status_code == 200, r.text
    c = r.json()
    a, b = ctx.load_validation_report(first), ctx.load_validation_report(second)
    before, after = {f.signature: f.violation_count for f in a.failures}, {f.signature: f.violation_count for f in b.failures}
    assert c["base"]["run_id"] == first and c["other"]["run_id"] == second and c["base"]["wave_name"] == "MOCK_1" and c["other"]["wave_name"] == "MOCK_2"
    assert c["quality_change"] == round(b.quality_score - a.quality_score, 2) and c["valid_change"] == b.valid_records - a.valid_records
    assert {d["signature"] for d in c["resolved"]} == set(before) - set(after) and {d["signature"] for d in c["new"]} == set(after) - set(before)
    assert {d["signature"] for d in c["improved"]} == {k for k in before if k in after and after[k] < before[k]}
    assert {d["signature"] for d in c["worsened"]} == {k for k in before if k in after and after[k] > before[k]}
    assert c["unchanged"] + len(c["improved"]) + len(c["worsened"]) == len(set(before) & set(after)) and c["verdict"]
    assert all(d["change"] == d["after"] - d["before"] for d in c["resolved"] + c["new"] + c["improved"] + c["worsened"])
    same = client.get("/api/cycles/compare", params={"base": first, "other": first}).json()
    assert same["quality_change"] == 0 and not same["resolved"] and not same["new"] and same["verdict"].startswith("No change")
    assert client.get("/api/cycles/compare", params={"base": first, "other": 999999}).status_code == 404


def test_the_dashboard_shows_waves_trends_quality_and_what_needs_attention(client, api_env, ctx):
    assert client.get("/api/dashboard").json()["waves"] == []                      # nothing yet
    mid = api_env["mapping"].id
    _run(client, mid, "WAVE_A")
    last = _run(client, mid, "WAVE_A", lookup_fallback="PASS_THROUGH")
    _run(client, mid, "WAVE_B")
    table_id = api_env["table"].id
    assert client.post(f"/api/quality/assess/{table_id}").status_code == 200
    (ctx.run_dir(last) / "db_loads_pending.json").write_text("[]", encoding="utf-8")   # an interrupted delivery marker
    d = client.get("/api/dashboard").json()
    waves = {w["wave"]: w for w in d["waves"]}
    assert set(waves) == {"WAVE_A", "WAVE_B"} and waves["WAVE_A"]["runs"] == 2 and waves["WAVE_A"]["latest_run"] == last and len(waves["WAVE_A"]["trend"]) == 2
    assert waves["WAVE_B"]["runs"] == 1 and waves["WAVE_A"]["quality_score"] is not None and waves["WAVE_A"]["gate"] in ("BLOCKED", "READY_FOR_LOAD", "NOT_READY") or waves["WAVE_A"]["gate"]
    assert d["runs"]["SUCCEEDED"] == 3 and d["tables"]["registered"] >= 1 and d["tables"]["landed"] >= 1
    assert d["data_quality"] and d["data_quality"][0]["table_id"] == table_id and d["data_quality"][0]["score"] <= 100
    assert any("interrupted" in a for a in d["attention"])
    assert any("blocked at the gate" in a for a in d["attention"]) or all(w["gate"] != "BLOCKED" for w in d["waves"])
