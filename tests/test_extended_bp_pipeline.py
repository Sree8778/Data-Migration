"""Extended Business Partner data through the whole pipeline: mapping -> transform -> validation -> gate -> Cockpit workbook / SAP sandbox / database delivery.

A mixed file (customers, one supplier, one person) shows that the optional segments never stop a record that does not use them, that a bad e-mail address or
IBAN is a defect on that row only, and that everything mapped really arrives in each of the three outputs.
"""

from __future__ import annotations

import csv
import io

import pytest

from src.services.cockpit_generator import BANK, EMAIL, PHONE, TAX, VENDOR_COMPANY, VENDOR_PURCHASING, iter_sheet_rows, list_sheets
from tests.test_db_targets import pg_query, pg_target, save_target, sign_off  # noqa: F401
from tests.test_module7_api import STEWARD, U, client, ctx, dataset, encoder, env  # noqa: F401

HEADER = ["PARTNER_NO", "TYPE", "NAME", "FIRST", "LAST", "COUNTRY", "CITY", "STREET", "MAIL", "PHONE", "VAT_TYPE", "VAT_NO", "BANK_CTRY", "BANK_KEY", "ACCOUNT", "IBAN",
          "ROLE", "RECON", "COMPANY", "ORG", "CURR"]
ROWS = [
    # a plain customer: none of the optional segments
    dict(PARTNER_NO="1001", TYPE="2", NAME="Alderwick Tools Ltd", COUNTRY="GB", CITY="Leeds", STREET="Mill Road 4", ROLE="FLCU01"),
    # a customer with everything customer-side
    dict(PARTNER_NO="1002", TYPE="2", NAME="Brightwater Foods GmbH", COUNTRY="DE", CITY="Kiel", STREET="Hafenstrasse 1", MAIL="office@brightwater.example",
         PHONE="+49 431 99887", VAT_TYPE="DE0", VAT_NO="DE811223344", BANK_CTRY="DE", BANK_KEY="21050170", ACCOUNT="0012345678", IBAN="DE89370400440532013000", ROLE="FLCU01"),
    # a supplier with vendor data
    dict(PARTNER_NO="2001", TYPE="2", NAME="Copperfield Supplies SA", COUNTRY="FR", CITY="Lyon", STREET="Rue Neuve 3", MAIL="sales@copperfield.example", ROLE="FLVN00",
         RECON="160000", COMPANY="1000", ORG="1000", CURR="EUR"),
    # a person
    dict(PARTNER_NO="3001", TYPE="1", NAME="", FIRST="Ada", LAST="Lovelace", COUNTRY="GB", CITY="London", STREET="Park Lane 9", MAIL="ada@lovelace.example", ROLE="FLCU01"),
    # two rows with a single bad value each
    dict(PARTNER_NO="4001", TYPE="2", NAME="Dunmore Freight BV", COUNTRY="NL", CITY="Utrecht", STREET="Havenweg 4", MAIL="not-an-address", ROLE="FLCU01"),
    dict(PARTNER_NO="4002", TYPE="2", NAME="Eastgate Optics AB", COUNTRY="SE", CITY="Malmo", STREET="Torget 2", BANK_CTRY="SE", BANK_KEY="5000", IBAN="12-34", ROLE="FLCU01"),
]
BAD = {"4001", "4002"}


def csv_bytes() -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HEADER)
    writer.writeheader()
    writer.writerows(ROWS)
    return buf.getvalue().encode()


RULES = [  # (table, column, source column or None, static logic)
    ("BUT000", "BP_EXT", "PARTNER_NO", None), ("BUT000", "TYPE", "TYPE", None), ("BUT000", "NAME_ORG1", "NAME", None), ("BUT000", "NAME_FIRST", "FIRST", None),
    ("BUT000", "NAME_LAST", "LAST", None), ("BUT000", "BU_GROUP", None, "'BP01'"),
    ("ADRC", "COUNTRY", "COUNTRY", None), ("ADRC", "CITY1", "CITY", None), ("ADRC", "STREET", "STREET", None),
    ("ADR6", "SMTP_ADDR", "MAIL", None), ("ADR2", "TEL_NUMBER", "PHONE", None),
    ("DFKKBPTAXNUM", "TAXTYPE", "VAT_TYPE", None), ("DFKKBPTAXNUM", "TAXNUM", "VAT_NO", None),
    ("BUT0BK", "BANKS", "BANK_CTRY", None), ("BUT0BK", "BANKL", "BANK_KEY", None), ("BUT0BK", "BANKN", "ACCOUNT", None), ("BUT0BK", "IBAN", "IBAN", None),
    ("BUT100", "RLTYP", "ROLE", None),
    ("LFB1", "BUKRS", "COMPANY", None), ("LFB1", "AKONT", "RECON", None), ("LFM1", "EKORG", "ORG", None), ("LFM1", "WAERS", "CURR", None),
]


@pytest.fixture()
def extended_run(client, ctx):
    assert client.post("/api/admin/seed-sap-catalog").status_code == 200
    reg = client.post("/api/sources/register", data={"system_name": "LEGACY_EXT", "system_type": "ORACLE", "table_name": "EXT_PARTNERS", "primary_key_columns": "PARTNER_NO"},
                      files={"file": ("ext.csv", csv_bytes(), "text/csv")})
    assert reg.status_code == 201, reg.text
    table_id = reg.json()["table_id"]
    mapping = client.post("/api/mapping-sets", json={"source_table_id": table_id}).json()["mapping_set_id"]
    for table, column, source, logic in RULES:
        body = {"target_table": table, "target_column": column, "rule_type": "STATIC_VALUE" if logic else "DIRECT_COPY", "source_column": source, "transformation_logic": logic}
        created = client.post(f"/api/mapping/{mapping}/rules", json=body)
        assert created.status_code == 201, (table, column, created.text)
    return {"mapping": mapping, "table_id": table_id}


def run_pipeline(client, mapping):
    run = client.post(f"/api/pipeline/run/{mapping}", json={"wave_name": "EXT"})
    assert run.status_code == 201, run.text
    return run.json()


def test_optional_segments_are_not_demanded_of_a_mapping_that_does_not_use_them(client, ctx):
    assert client.post("/api/admin/seed-sap-catalog").status_code == 200
    reg = client.post("/api/sources/register", data={"system_name": "LEGACY_EXT", "system_type": "ORACLE", "table_name": "EXT_PARTNERS", "primary_key_columns": "PARTNER_NO"},
                      files={"file": ("ext.csv", csv_bytes(), "text/csv")})
    mapping = client.post("/api/mapping-sets", json={"source_table_id": reg.json()["table_id"]}).json()["mapping_set_id"]
    detail = client.get(f"/api/mapping/{mapping}").json()
    missing = detail["unmapped_mandatory_targets"]
    assert not [m for m in missing if m.split(".")[0] in {"ADR6", "ADR2", "ADR3", "DFKKBPTAXNUM", "BUT0BK", "LFB1", "LFM1"}], missing


def test_a_bad_email_or_iban_fails_that_row_only_and_everything_else_is_valid(client, extended_run):
    body = run_pipeline(client, extended_run["mapping"])
    validation = body["validation"]
    assert validation["evaluated_records"] == 6 and validation["valid_records"] == 4, validation
    kinds = {(f["kind"], f["target"]) for f in validation["failures"]}
    assert ("FORMAT", "ADR6.SMTP_ADDR") in kinds and ("FORMAT", "BUT0BK.IBAN") in kinds
    assert {f["violation_count"] for f in validation["failures"] if f["kind"] == "FORMAT"} == {1}
    assert body["readiness"]["open_critical_defects"] == 0 and body["defects"]["total_open"] == 2               # format defects are HIGH: they leave the row out, they do not block the load


def test_the_cockpit_workbook_holds_each_partner_s_extended_data(client, ctx, extended_run):
    body = run_pipeline(client, extended_run["mapping"])
    run_id = body["run_id"]
    assert client.post(f"/api/governance/submit-mapping/{extended_run['mapping']}").status_code == 200
    assert client.post(f"/api/governance/approve-mapping/{extended_run['mapping']}", headers=U(STEWARD)).status_code == 200
    sign_off(client, run_id)
    loaded = client.post(f"/api/pipeline/load/{run_id}", json={"mode": "cockpit"})
    assert loaded.status_code == 200, loaded.text
    path = ctx.run_dir(run_id) / "cockpit.xlsx"
    sheets = list_sheets(path)
    assert {EMAIL, PHONE, TAX, BANK, VENDOR_COMPANY, VENDOR_PURCHASING} <= set(sheets)
    emails = {r[0]: r[1] for r in list(iter_sheet_rows(path, EMAIL))[1:]}
    assert emails == {"0000001002": "office@brightwater.example", "0000002001": "sales@copperfield.example", "0000003001": "ada@lovelace.example"}   # the bad address row is not here
    assert list(iter_sheet_rows(path, TAX))[1] == ["0000001002", "DE0", "DE811223344"]
    assert [r[0] for r in list(iter_sheet_rows(path, BANK))[1:]] == ["0000001002"]                              # the row with the bad IBAN is not valid, so not delivered
    assert list(iter_sheet_rows(path, VENDOR_COMPANY))[1][:3] == ["0000002001", "1000", "0000160000"]


def test_the_sandbox_load_creates_every_valid_partner_with_its_children(client, ctx, extended_run):
    body = run_pipeline(client, extended_run["mapping"])
    run_id = body["run_id"]
    assert client.post(f"/api/governance/submit-mapping/{extended_run['mapping']}").status_code == 200
    assert client.post(f"/api/governance/approve-mapping/{extended_run['mapping']}", headers=U(STEWARD)).status_code == 200
    sign_off(client, run_id)
    loaded = client.post(f"/api/pipeline/load/{run_id}", json={"mode": "sap", "target": "sandbox"})
    assert loaded.status_code == 200, loaded.text
    load = loaded.json()["load"]
    assert load["attempted"] == 4 and load["loaded"] == 4 and load["failed"] == 0, load


def test_the_database_delivery_carries_the_extended_columns(client, ctx, pg_target, extended_run):
    p, conn = pg_target
    body = run_pipeline(client, extended_run["mapping"])
    run_id = body["run_id"]
    assert client.post(f"/api/governance/submit-mapping/{extended_run['mapping']}").status_code == 200
    assert client.post(f"/api/governance/approve-mapping/{extended_run['mapping']}", headers=U(STEWARD)).status_code == 200
    sign_off(client, run_id)
    assert save_target(client, p).status_code == 201
    delivered = client.post(f"/api/pipeline/load-db/{run_id}", json={"connection": "ora_like_target", "table_name": "MIG_BP_EXT"})
    assert delivered.status_code == 201, delivered.text
    rows = pg_query(conn, "SELECT legacy_key, adr6_smtp_addr, dfkkbptaxnum_taxnum, but0bk_iban, lfb1_akont, but000_name_last FROM mig_target.mig_bp_ext ORDER BY legacy_key")
    by_key = {r[0]: r for r in rows}
    assert len(rows) == 4 and by_key["1002"][1:4] == ("office@brightwater.example", "DE811223344", "DE89370400440532013000")
    assert by_key["2001"][4] == "160000" and by_key["3001"][5] == "Lovelace" and by_key["1001"][1] is None


def test_a_person_needs_a_last_name_and_an_organisation_needs_a_name_not_the_other_way_round(client, ctx):
    assert client.post("/api/admin/seed-sap-catalog").status_code == 200
    rows = [dict(PARTNER_NO="1", TYPE="1", NAME="", FIRST="Ada", LAST="Lovelace", COUNTRY="GB"),      # a person: the last name is the name
            dict(PARTNER_NO="2", TYPE="1", NAME="", FIRST="Grace", LAST="", COUNTRY="GB"),             # a person without a last name
            dict(PARTNER_NO="3", TYPE="2", NAME="Hopper Ltd", FIRST="", LAST="", COUNTRY="GB"),        # an organisation: NAME_ORG1 is its name
            dict(PARTNER_NO="4", TYPE="2", NAME="", FIRST="", LAST="Smith", COUNTRY="GB")]             # an organisation without a name
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HEADER)
    writer.writeheader()
    writer.writerows(rows)
    reg = client.post("/api/sources/register", data={"system_name": "LEGACY_NAMES", "system_type": "ORACLE", "table_name": "NAMES", "primary_key_columns": "PARTNER_NO"},
                      files={"file": ("n.csv", buf.getvalue().encode(), "text/csv")})
    mapping = client.post("/api/mapping-sets", json={"source_table_id": reg.json()["table_id"]}).json()["mapping_set_id"]
    for table, column, source, logic in [r for r in RULES if r[0] in ("BUT000", "ADRC") and r[1] in ("BP_EXT", "TYPE", "NAME_ORG1", "NAME_FIRST", "NAME_LAST", "BU_GROUP", "COUNTRY")]:
        body = {"target_table": table, "target_column": column, "rule_type": "STATIC_VALUE" if logic else "DIRECT_COPY", "source_column": source, "transformation_logic": logic}
        assert client.post(f"/api/mapping/{mapping}/rules", json=body).status_code == 201
    validation = run_pipeline(client, mapping)["validation"]
    failing = {(f["target"], tuple(f["sample_failing_pks"])) for f in validation["failures"]}
    assert failing == {("BUT000.NAME_LAST", ("2",)), ("BUT000.NAME_ORG1", ("4",))}, failing
    assert validation["valid_records"] == 2
