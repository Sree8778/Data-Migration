"""Extended Business Partner data: person names, e-mail / telephone / fax, tax numbers, bank details and supplier (vendor) data.

They have to travel the whole way, not just exist in the catalog: validated target fields -> shared record model -> Cockpit workbook -> SAP OData deep insert
(checked by the strict stand-in gateway and the diagnostics) -> database delivery. The SAP side is verified against the stand-in only, never a real system.
"""

from __future__ import annotations

import duckdb
import httpx
import pandas as pd
import pytest

from src.schemas.sap import BusinessPartnerRecord
from src.services.bp_model import BpConfig, assemble_record, iter_bp_records
from src.services.cockpit_generator import (
    BANK, CORE_SHEETS, EMAIL, FAX, GENERAL, OPTIONAL_SHEETS, PHONE, TAX, VENDOR_COMPANY, VENDOR_PURCHASING, CockpitConsistencyError, CockpitGenerator,
    iter_sheet_rows, list_sheets, validate_workbook)
from src.services.sap_client import ENTITY_PROPERTIES, HttpSapClient, SandboxSapClient, SapConfig, build_odata_payload
from src.services.sap_diagnostics import SapDiagnostics
from tests.sap_gateway_fake import FakeGateway

CONFIG = dict(base_url="https://sap.example.test", user="MIGUSER", password="pw", client="100")

BASE = {
    "_SRC_PK": "1", "BUT000_BP_EXT": "77", "BUT000_TYPE": "2", "BUT000_BU_GROUP": "BP01", "BUT000_NAME_ORG1": "Nordwind GmbH",
    "ADRC_COUNTRY": "DE", "ADRC_CITY1": "Berlin", "ADRC_POST_CODE1": "10115",
}
EXTENDED = {
    **BASE, "BUT100_RLTYP": "FLCU01 FLVN00",
    "ADR6_SMTP_ADDR": "info@nordwind.example", "ADR2_TEL_NUMBER": "+49 30 123456", "ADR2_TEL_EXTENS": "12", "ADR3_FAX_NUMBER": "+49 30 123457",
    "DFKKBPTAXNUM_TAXTYPE": "DE0", "DFKKBPTAXNUM_TAXNUM": "DE123456789",
    "BUT0BK_BKVID": "0001", "BUT0BK_BANKS": "DE", "BUT0BK_BANKL": "10020030", "BUT0BK_BANKN": "123456789", "BUT0BK_KOINH": "Nordwind GmbH", "BUT0BK_IBAN": "DE02100200300123456789",
    "LFB1_BUKRS": "1000", "LFB1_AKONT": "160000", "LFB1_ZTERM": "NT30", "LFB1_ZWELS": "UT",
    "LFM1_EKORG": "1000", "LFM1_WAERS": "EUR", "LFM1_ZTERM": "NT30", "LFM1_INCO1": "EXW",
    "KNB1_BUKRS": "1000", "KNB1_AKONT": "113100", "KNVV_VKORG": "1010", "KNVV_VTWEG": "10", "KNVV_SPART": "00", "KNVV_WAERS": "EUR",
}


def staging(path, rows):
    frame = pd.DataFrame(rows)
    con = duckdb.connect()
    con.register("frame", frame)
    con.execute(f"COPY (SELECT * FROM frame) TO '{path.as_posix()}' (FORMAT parquet)")
    return path


def record(row: dict, config: BpConfig = BpConfig(default_roles=("FLCU00",))) -> BusinessPartnerRecord:
    return assemble_record(row, config, "BUT100_RLTYP" in row)


def live(gateway: FakeGateway) -> HttpSapClient:
    return HttpSapClient(SapConfig(**CONFIG), transport=gateway.transport(), retry_delay=0)


# ------------------------------------------------------------------ the shared record model
def test_every_extended_segment_reaches_the_record_with_the_alpha_conversion_on_the_reconciliation_account():
    rec = record(EXTENDED)
    assert not rec.blocking_issues and not rec.warnings
    assert rec.email == {"SMTP_ADDR": "info@nordwind.example"} and rec.phone == {"TEL_NUMBER": "+49 30 123456", "TEL_EXTENS": "12"} and rec.fax == {"FAX_NUMBER": "+49 30 123457"}
    assert rec.tax == {"TAXTYPE": "DE0", "TAXNUM": "DE123456789"} and rec.bank["IBAN"] == "DE02100200300123456789" and rec.bank["BANKL"] == "10020030"
    assert rec.vendor_company["AKONT"] == "0000160000" and rec.vendor_purchasing == {"EKORG": "1000", "WAERS": "EUR", "ZTERM": "NT30", "INCO1": "EXW"}
    assert rec.company_code["AKONT"] == "0000113100" and rec.roles == ["FLCU01", "FLVN00"]


def test_a_customer_without_any_extended_data_is_untouched():
    rec = record(BASE)
    assert not (rec.email or rec.phone or rec.fax or rec.tax or rec.bank or rec.vendor_company or rec.vendor_purchasing) and not rec.blocking_issues and not rec.warnings


def test_incomplete_optional_segments_are_dropped_with_a_warning_and_never_stop_the_partner():
    rec = record({**BASE, "ADR6_SMTP_ADDR": "a@b.example", "DFKKBPTAXNUM_TAXNUM": "DE1", "BUT0BK_BANKN": "123", "ADR2_TEL_EXTENS": "5"})
    assert not rec.blocking_issues and not rec.tax and not rec.bank and not rec.phone and rec.email
    text = " | ".join(rec.warnings)
    assert "TAXTYPE and TAXNUM" in text and "BANKS" in text and "extension without a telephone" in text


def test_communication_data_without_an_address_country_is_dropped_because_sap_attaches_it_to_the_address():
    rec = record({**BASE, "ADRC_COUNTRY": None, "ADR6_SMTP_ADDR": "a@b.example", "ADR3_FAX_NUMBER": "1"})
    assert not rec.email and not rec.fax and any("without an address country" in w for w in rec.warnings)


def test_vendor_data_needs_a_supplier_role_and_its_mandatory_fields():
    no_role = record({**EXTENDED, "BUT100_RLTYP": "FLCU01"})
    assert any("supplier role" in i for i in no_role.blocking_issues)
    no_account = record({**EXTENDED, "LFB1_AKONT": None, "LFM1_WAERS": None})
    assert any("AKONT" in i for i in no_account.blocking_issues) and any("WAERS" in i for i in no_account.blocking_issues)
    ok_via_default = record({**BASE, "LFB1_BUKRS": "1000", "LFB1_AKONT": "1"}, BpConfig(default_roles=("FLVN00",)))
    assert not ok_via_default.blocking_issues


def test_the_streaming_reader_picks_up_the_new_columns(tmp_path):
    path = staging(tmp_path / "v.parquet", [EXTENDED])
    rec = next(iter_bp_records(path))
    assert rec.tax["TAXNUM"] == "DE123456789" and rec.bank["BANKN"] == "123456789" and rec.vendor_purchasing["EKORG"] == "1000"


# ------------------------------------------------------------------ the Migration Cockpit workbook
def test_the_workbook_gets_an_optional_sheet_only_when_somebody_has_data_for_it(tmp_path):
    plain = CockpitGenerator().generate(staging(tmp_path / "a.parquet", [BASE]), tmp_path / "a.xlsx")
    assert list_sheets(plain.path) == CORE_SHEETS and set(plain.sheet_rows) == set(CORE_SHEETS)

    result = CockpitGenerator().generate(staging(tmp_path / "b.parquet", [EXTENDED, {**BASE, "_SRC_PK": "2", "BUT000_BP_EXT": "78", "BUT100_RLTYP": "FLCU00"}]), tmp_path / "b.xlsx")
    assert list_sheets(result.path) == CORE_SHEETS + OPTIONAL_SHEETS and validate_workbook(result.path) == []
    assert result.business_partners == 2 and all(result.sheet_rows[s] == 1 for s in OPTIONAL_SHEETS)             # only the partner that has the data gets a row
    assert list(iter_sheet_rows(result.path, TAX))[1] == ["0000000077", "DE0", "DE123456789"]
    assert list(iter_sheet_rows(result.path, BANK))[1][-1] == "DE02100200300123456789"
    assert list(iter_sheet_rows(result.path, VENDOR_COMPANY))[1] == ["0000000077", "1000", "0000160000", "NT30", "UT"]
    assert list(iter_sheet_rows(result.path, VENDOR_PURCHASING))[1][1] == "1000"
    assert list(iter_sheet_rows(result.path, EMAIL))[1] == ["0000000077", "info@nordwind.example"]
    assert list(iter_sheet_rows(result.path, PHONE))[1][2] == "12" and list(iter_sheet_rows(result.path, FAX))[1][1] == "+49 30 123457"
    assert list(iter_sheet_rows(result.path, GENERAL))[0][-2:] == ["NAME_FIRST", "NAME_LAST"]


def test_a_workbook_with_only_some_optional_sheets_is_valid_but_a_stray_or_misordered_sheet_is_not(tmp_path):
    only_tax = CockpitGenerator().generate(staging(tmp_path / "t.parquet", [{**BASE, "DFKKBPTAXNUM_TAXTYPE": "DE0", "DFKKBPTAXNUM_TAXNUM": "1"}]), tmp_path / "t.xlsx")
    assert list_sheets(only_tax.path) == CORE_SHEETS + [TAX] and validate_workbook(only_tax.path) == []


def test_the_workbook_refuses_a_partner_with_vendor_data_and_no_supplier_role(tmp_path):
    bad = staging(tmp_path / "bad.parquet", [{**EXTENDED, "BUT100_RLTYP": "FLCU01"}])
    with pytest.raises(CockpitConsistencyError, match="supplier role"):
        CockpitGenerator().generate(bad, tmp_path / "bad.xlsx")
    assert not (tmp_path / "bad.xlsx").exists()


# ------------------------------------------------------------------ the SAP OData payload
def test_the_deep_insert_carries_every_segment_where_the_api_expects_it():
    body = build_odata_payload(record({**EXTENDED, "BUT000_TYPE": "1", "BUT000_NAME_FIRST": "Ada", "BUT000_NAME_LAST": "Lovelace"}))
    assert body["FirstName"] == "Ada" and body["LastName"] == "Lovelace"
    address = body["to_BusinessPartnerAddress"]["results"][0]
    assert address["to_EmailAddress"]["results"] == [{"EmailAddress": "info@nordwind.example"}]
    assert address["to_PhoneNumber"]["results"] == [{"PhoneNumber": "+49 30 123456", "PhoneNumberExtension": "12"}]
    assert address["to_FaxNumber"]["results"] == [{"FaxNumber": "+49 30 123457"}]
    assert body["to_BusinessPartnerTaxNumber"]["results"] == [{"BPTaxType": "DE0", "BPTaxNumber": "DE123456789"}]
    assert body["to_BusinessPartnerBank"]["results"][0] == {"BankIdentification": "0001", "BankCountryKey": "DE", "BankNumber": "10020030", "BankAccount": "123456789",
                                                           "BankAccountHolderName": "Nordwind GmbH", "IBAN": "DE02100200300123456789"}
    assert body["to_Supplier"]["to_SupplierCompany"]["results"][0]["ReconciliationAccount"] == "0000160000"
    assert body["to_Supplier"]["to_SupplierPurchasingOrg"]["results"][0] == {"PurchasingOrganization": "1000", "PurchaseOrderCurrency": "EUR", "PaymentTerms": "NT30",
                                                                             "IncotermsClassification": "EXW"}
    assert "to_Customer" in body and "to_Supplier" in body                                                          # a partner can be both


def test_a_plain_customer_payload_has_no_new_navigations():
    body = build_odata_payload(record(BASE))
    assert not {k for k in body if k.startswith("to_")} - {"to_BusinessPartnerRole", "to_BusinessPartnerAddress"}
    assert set(body["to_BusinessPartnerAddress"]["results"][0]) == {"Country", "CityName", "PostalCode"}


def test_every_property_we_send_is_declared_for_the_diagnostics():
    declared = {p for props in ENTITY_PROPERTIES.values() for p in props}
    assert {"EmailAddress", "BPTaxNumber", "IBAN", "ReconciliationAccount", "PurchasingOrganization", "FirstName"} <= declared


# ------------------------------------------------------------------ against the strict stand-in gateway
def test_the_gateway_accepts_the_full_deep_insert_and_keeps_every_child():
    gw = FakeGateway()
    result = live(gw).create_business_partners([record({**EXTENDED, "BUT000_TYPE": "1", "BUT000_NAME_FIRST": "Ada", "BUT000_NAME_LAST": "Lovelace"})])[0]
    assert result.partner, [m.MESSAGE for m in result.messages]
    assert gw.partners[result.partner]["FirstName"] == "Ada"
    children = gw.deep[result.partner]
    assert {"to_BusinessPartnerTaxNumber", "to_BusinessPartnerBank", "to_Supplier", "to_Customer", "to_BusinessPartnerAddress"} <= set(children)


def test_the_gateway_rejects_what_a_real_system_rejects():
    gw = FakeGateway()
    client = live(gw)
    cases = {
        "a person without a name": {**BASE, "BUT000_TYPE": "1"},
        "an invalid e-mail": {**BASE, "ADR6_SMTP_ADDR": "not-an-address"},
    }
    for label, row in cases.items():
        result = client.create_business_partners([record(row)])[0]
        assert not result.partner, label
    assert not gw.partners

    body = build_odata_payload(record(EXTENDED))
    body["to_BusinessPartnerBank"]["results"][0]["Swift"] = "X"                                                      # a property the entity does not have
    response = httpx.Client(transport=gw.transport(), base_url=CONFIG["base_url"], auth=("MIGUSER", "pw"), params={"sap-client": "100"})
    token = response.get("/sap/opu/odata/sap/API_BUSINESS_PARTNER/", headers={"x-csrf-token": "Fetch"})
    sent = response.post("/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_BusinessPartner", json=body, headers={"x-csrf-token": token.headers["x-csrf-token"]})
    assert sent.status_code == 400 and "Swift" in sent.json()["error"]["message"]["value"]


def test_the_sandbox_applies_the_same_new_checks():
    sandbox = SandboxSapClient()
    outcomes = sandbox.create_business_partners([record(BASE), record({**BASE, "BUT000_BP_EXT": "5", "BUT000_TYPE": "1"}), record({**BASE, "BUT000_BP_EXT": "6", "ADR6_SMTP_ADDR": "x"})])
    assert [bool(o.partner) for o in outcomes] == [True, False, False]


def test_the_diagnostics_see_the_new_entities_and_notice_when_a_release_lacks_one():
    gw = FakeGateway()
    checks = {d.name: d for d in SapDiagnostics(SapConfig(**CONFIG), transport=gw.transport()).run()}
    assert checks["metadata"].status == "pass", checks["metadata"].detail
    gw.drop_property = {"BPTaxNumber"}
    broken = {d.name: d for d in SapDiagnostics(SapConfig(**CONFIG), transport=gw.transport()).run()}
    assert broken["metadata"].status == "fail" and "BPTaxNumber" in broken["metadata"].detail
