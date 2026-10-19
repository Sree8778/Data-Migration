"""The live SAP adapter against a strict, stateful stand-in for the Gateway (tests/sap_gateway_fake.py), and the diagnostics that check a real system.

No real SAP system is reachable from the test environment, so these tests cannot prove the live path. What they do prove is everything a canned mock cannot:
the CSRF handshake with its session cookie, a token that expires mid-batch, a proxy that drops the cookie, SAP's error structure, retries, and that the
diagnostics catch the mismatches a real system would show (inactive service, wrong field names, shorter fields, no session cookie, wrong credentials).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.context import AppContext
from src.api.main import create_app
from src.api.settings import Settings
from src.schemas.sap import BusinessPartnerRecord
from src.seeds.seed_sap_catalog import seed_sap_catalog
from src.services.sap_client import (
    ENTITY_PROPERTIES, HttpSapClient, SapAuthError, SapConfig, SapUnavailableError, build_odata_payload)
from src.services.sap_diagnostics import SapDiagnostics, expected_odata_lengths, parse_metadata
from tests.sap_gateway_fake import FakeGateway
from tests.test_module7_api import U, scripted_llm

CONFIG = dict(base_url="https://sap.example.test", user="MIGUSER", password="pw", client="100")


def record(i: int, **over) -> BusinessPartnerRecord:
    base = dict(source_pk=str(i), bp_ext=f"{i:010d}", general={"TYPE": "2", "BU_GROUP": "BP01", "NAME_ORG1": f"Acme {i}"}, roles=["FLCU01"],
                address={"COUNTRY": "DE", "CITY1": "Berlin", "POST_CODE1": "10115"}, company_code={"BUKRS": "1000", "AKONT": "0000140000", "ZTERM": "0001"},
                sales_area={"VKORG": "1010", "VTWEG": "10", "SPART": "00", "WAERS": "EUR"})
    base.update(over)
    return BusinessPartnerRecord(**base)


def live(gateway: FakeGateway, **config) -> HttpSapClient:
    return HttpSapClient(SapConfig(**{**CONFIG, **config}), transport=gateway.transport(), retry_delay=0)


# ------------------------------------------------------------------ the adapter against a strict gateway
def test_a_batch_is_created_through_the_csrf_and_session_handshake():
    gw = FakeGateway()
    results = live(gw).create_business_partners([record(i) for i in range(1, 4)])
    assert [r.partner for r in results] == ["1000000001", "1000000002", "1000000003"] and all(r.messages[0].TYPE == "S" for r in results)
    assert gw.requests[0] == ("GET", "/sap/opu/odata/sap/API_BUSINESS_PARTNER/") and [m for m, _ in gw.requests].count("GET") == 1    # one token fetch for the whole batch
    assert len(gw.partners) == 3 and gw.partners["1000000001"]["OrganizationBPName1"] == "Acme 1"


def test_a_token_that_expires_in_the_middle_of_a_batch_is_fetched_again_without_losing_a_record():
    gw = FakeGateway(token_lifetime=2)
    results = live(gw).create_business_partners([record(i) for i in range(1, 6)])
    assert all(r.partner for r in results) and len(gw.partners) == 5                           # nothing lost, nothing created twice
    assert [m for m, _ in gw.requests].count("GET") >= 3                                       # the first fetch plus a refresh after each expiry


def test_a_proxy_that_drops_the_session_cookie_fails_every_write_with_the_reason_visible():
    gw = FakeGateway()
    gw.drop_cookie = True
    results = live(gw).create_business_partners([record(1), record(2)])
    assert not any(r.partner for r in results) and not gw.partners
    assert "CSRF token validation failed" in results[0].messages[0].MESSAGE and results[0].messages[0].TYPE == "E"


def test_credentials_the_client_and_the_service_are_each_reported_for_what_they_are():
    with pytest.raises(SapAuthError, match="401"):
        live(FakeGateway(), password="wrong").create_business_partners([record(1)])
    with pytest.raises(SapUnavailableError, match="CSRF token"):
        live(FakeGateway(client="200")).ping()                                                  # a client that does not exist: the system answers 404, there is no token
    with pytest.raises(SapUnavailableError):
        live(FakeGateway(service_active=False)).ping()


def test_a_temporary_failure_is_retried_once_and_a_second_one_stops_the_load():
    gw = FakeGateway()
    gw.fail_next(503, 1)
    assert live(gw).create_business_partners([record(1)])[0].partner == "1000000001"
    for status in (503, 429):
        broken = FakeGateway()
        broken.fail_next(status, 2)
        with pytest.raises(SapUnavailableError, match=str(status)):
            live(broken).create_business_partners([record(1)])


def test_sap_rejections_become_bapi_style_messages_with_their_details():
    gw = FakeGateway()
    bad = record(1, general={"TYPE": "9", "BU_GROUP": "", "NAME_ORG1": ""}, roles=["NOPE"], address={})
    result = live(gw).create_business_partners([bad])[0]
    assert result.partner is None and all(m.TYPE == "E" for m in result.messages)
    assert (result.messages[0].ID, result.messages[0].NUMBER) == ("R11", "007") and "category 9 is invalid" in result.messages[0].MESSAGE
    assert {m.NUMBER for m in result.messages} >= {"007", "008", "012"}                          # the first error and the details SAP lists with it
    assert not gw.partners


def test_the_external_key_is_sent_when_configured_and_a_duplicate_is_rejected_by_sap():
    gw = FakeGateway()
    client = live(gw, external_id_field="YY1_LegacyKey_bus")
    first, second = client.create_business_partners([record(1), record(2, bp_ext="0000000001")])
    assert first.partner and gw.partners[first.partner]["YY1_LegacyKey_bus"] == "0000000001"
    assert second.partner is None and "already exists" in second.messages[0].MESSAGE
    unknown = live(FakeGateway(), external_id_field="NoSuchField").create_business_partners([record(1)])[0]
    assert unknown.partner is None and "Property 'NoSuchField' is invalid" in unknown.messages[0].MESSAGE


# ------------------------------------------------------------------ the diagnostics
def run(gw: FakeGateway, **kw):
    cfg = SapConfig(**{**CONFIG, **kw.pop("config", {})})
    return {d.name: d for d in SapDiagnostics(cfg, transport=gw.transport()).run(**kw)}


def test_a_healthy_system_passes_every_read_only_check_and_writes_nothing():
    gw = FakeGateway()
    checks = run(gw)
    assert {n: c.status for n, c in checks.items()} == {"connection": "pass", "csrf_session": "pass", "metadata": "pass", "read_access": "pass",
                                                        "write_test": "skip", "error_mapping": "skip"}
    assert not gw.partners and not any(m == "POST" for m, _ in gw.requests)


def test_the_write_test_creates_one_marked_partner_reads_it_back_and_checks_the_error_mapping():
    gw = FakeGateway()
    checks = run(gw, write_test=True)
    assert checks["write_test"].status == "pass" and checks["error_mapping"].status == "pass" and len(gw.partners) == 1
    only = next(iter(gw.partners.values()))
    assert only["OrganizationBPName1"].startswith("MIGRATION TEST - SAFE TO ARCHIVE") and checks["write_test"].data["partner"] == only["BusinessPartner"]
    assert "R11/007" in checks["error_mapping"].detail and "category" in checks["error_mapping"].detail


def test_each_kind_of_misconfiguration_is_named_with_what_to_do_about_it():
    wrong = run(FakeGateway(), config={"password": "nope"})
    assert wrong["connection"].status == "fail" and "401" in wrong["connection"].detail and wrong["metadata"].status == "skip"
    inactive = run(FakeGateway(service_active=False))
    assert inactive["connection"].status == "fail" and "/IWFND/MAINT_SERVICE" in inactive["connection"].hint
    cookie = FakeGateway()
    cookie.drop_cookie = True
    assert run(cookie)["csrf_session"].status == "warn" and "load balancer" in run(cookie)["csrf_session"].hint

    missing = FakeGateway()
    missing.drop_property = {"PaymentMethodsList"}
    meta = run(missing)["metadata"]
    assert meta.status == "fail" and "A_CustomerCompany has no property PaymentMethodsList" in meta.detail
    renamed = FakeGateway()
    renamed.rename_property = {"StreetName": "Street"}
    assert "A_BusinessPartnerAddress has no property StreetName" in run(renamed)["metadata"].detail
    assert "external id field NoSuchField" in run(FakeGateway(), config={"external_id_field": "NoSuchField"})["metadata"].detail
    assert run(FakeGateway(), config={"external_id_field": "YY1_LegacyKey_bus"})["metadata"].status == "pass"


def test_a_field_that_is_shorter_in_the_system_than_our_validation_assumes_is_a_warning_with_the_numbers():
    gw = FakeGateway()
    gw.max_length_override = {"OrganizationBPName1": 35}
    meta = run(gw, expected_lengths={"OrganizationBPName1": 40, "CityName": 40})["metadata"]
    assert meta.status == "warn" and "OrganizationBPName1 holds 35 characters in this system, our validation allows 40" in meta.detail
    assert "CityName" not in meta.detail


def test_the_write_test_names_a_failing_partner_and_what_to_check():
    class Strict(FakeGateway):                                                                  # a system whose grouping BP01 does not exist
        def _post_partner(self, request):
            return self._error(400, "R1/401", "Grouping BP01 is not defined")
    checks = run(Strict(), write_test=True)
    assert checks["write_test"].status == "fail" and "Grouping BP01 is not defined" in checks["write_test"].detail and "external numbering" in checks["write_test"].hint


def test_parse_metadata_reads_sets_types_lengths_and_navigations():
    xml = FakeGateway()._metadata()
    sets, types = parse_metadata(xml)
    assert sets["A_BusinessPartner"] == "A_BusinessPartnerType" and types["A_BusinessPartnerType"]["OrganizationBPName1"]["max_length"] == 40
    assert "to_Customer" in types["A_BusinessPartnerType"]["__navigations__"]
    assert set(ENTITY_PROPERTIES) <= set(sets)
    for entity, props in ENTITY_PROPERTIES.items():                                             # every property our payload can send exists in the published model
        assert set(props) <= set(types[sets[entity]]), entity


def test_the_payload_uses_exactly_the_published_property_names():
    body = build_odata_payload(record(1), "YY1_LegacyKey_bus")
    assert set(body) >= {"BusinessPartnerCategory", "BusinessPartnerGrouping", "OrganizationBPName1", "YY1_LegacyKey_bus", "to_BusinessPartnerRole", "to_BusinessPartnerAddress", "to_Customer"}
    assert body["to_Customer"]["to_CustomerCompany"]["results"][0] == {"CompanyCode": "1000", "ReconciliationAccount": "0000140000", "PaymentTerms": "0001"}


def test_catalog_lengths_map_onto_odata_properties():
    assert expected_odata_lengths({"BUT000.NAME_ORG1": 40, "ADRC.CITY1": 40, "KNB1.ZTERM": 4, "KNVV.WAERS": 5, "ADRC.NOT_SENT": 9, "BUT000.TYPE": None}) == {
        "OrganizationBPName1": 40, "CityName": 40, "PaymentTerms": 4, "Currency": 5}


# ------------------------------------------------------------------ through the API
def make_client(session_factory, tmp_path, gateway: FakeGateway, monkeypatch, configured: bool = True) -> TestClient:
    for key, value in (("SAP_BASE_URL", CONFIG["base_url"]), ("SAP_USER", CONFIG["user"]), ("SAP_PASSWORD", CONFIG["password"]), ("SAP_CLIENT", CONFIG["client"])):
        (monkeypatch.setenv if configured else monkeypatch.delenv)(key, value) if configured else monkeypatch.delenv(key, raising=False)
    ctx = AppContext(Settings(workspace_dir=tmp_path / "ws", auto_seed=False), session_factory, llm_mapper=scripted_llm(), sap_transport=gateway.transport())
    seed_sap_catalog(ctx.metadata)
    return TestClient(create_app(ctx), headers=U("alice"))


def test_the_diagnostics_are_available_through_the_api_and_never_return_the_password(service, session_factory, tmp_path, monkeypatch):
    gw = FakeGateway()
    client = make_client(session_factory, tmp_path, gw, monkeypatch)
    r = client.post("/api/targets/diagnose-sap", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["wrote"] is False and [c["name"] for c in body["checks"]][:4] == ["connection", "csrf_session", "metadata", "read_access"]
    assert CONFIG["password"] not in r.text and not gw.partners
    wrote = client.post("/api/targets/diagnose-sap", json={"write_test": True}).json()
    assert wrote["wrote"] is True and wrote["ok"] is True and len(gw.partners) == 1
    assert client.post("/api/targets/test", json={"target": "live"}).json()["ok"] is True       # the existing connection test uses the same transport


def test_a_system_with_shorter_fields_shows_up_as_a_warning_through_the_api_using_our_catalog(service, session_factory, tmp_path, monkeypatch):
    gw = FakeGateway()
    gw.max_length_override = {"OrganizationBPName1": 35}
    client = make_client(session_factory, tmp_path, gw, monkeypatch)
    meta = next(c for c in client.post("/api/targets/diagnose-sap", json={}).json()["checks"] if c["name"] == "metadata")
    assert meta["status"] == "warn" and "OrganizationBPName1 holds 35" in meta["detail"]        # our catalog allows 40 for NAME_ORG1


def test_an_unconfigured_server_says_what_to_set(service, session_factory, tmp_path, monkeypatch):
    client = make_client(session_factory, tmp_path, FakeGateway(), monkeypatch, configured=False)
    r = client.post("/api/targets/diagnose-sap", json={})
    assert r.status_code == 400 and r.json()["code"] == "NOT_CONFIGURED" and "SAP_BASE_URL" in r.json()["detail"]


# ------------------------------------------------------------------ the script a person runs against their own system
def test_the_verification_script_reports_runs_the_write_test_only_when_confirmed_and_sets_the_exit_code(tmp_path, capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("verify_live_sap", Path(__file__).resolve().parent.parent / "scripts" / "verify_live_sap.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    env = {"SAP_BASE_URL": CONFIG["base_url"], "SAP_USER": CONFIG["user"], "SAP_PASSWORD": CONFIG["password"], "SAP_CLIENT": CONFIG["client"]}

    assert script.main([], env={}) == 2 and "Not configured" in capsys.readouterr().err                          # nothing configured
    gw = FakeGateway()
    report = tmp_path / "check.md"
    assert script.main(["--report", str(report)], env=env, transport=gw.transport()) == 0
    out = capsys.readouterr().out
    assert "[OK  ] connection" in out and "[SKIP] write_test" in out and "Nothing blocks a trial load" in out and not gw.partners
    assert report.read_text(encoding="utf-8").startswith("# SAP system check") and CONFIG["password"] not in out

    assert script.main(["--write-test"], env=env, transport=gw.transport()) == 2                                  # the write test needs its own explicit yes
    assert "--yes-create-one-test-partner" in capsys.readouterr().err and not gw.partners
    assert script.main(["--write-test", "--yes-create-one-test-partner"], env=env, transport=gw.transport()) == 0
    assert "[OK  ] write_test" in capsys.readouterr().out and len(gw.partners) == 1

    broken = FakeGateway()
    broken.drop_property = {"Currency"}
    assert script.main([], env=env, transport=broken.transport()) == 1                                           # a failed check is exit code 1
    assert "A_CustomerSalesArea has no property Currency" in capsys.readouterr().out
