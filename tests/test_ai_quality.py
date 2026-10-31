"""Faults found by evaluating the AI against answers known to be right (scripts/evaluate_ai.py), pinned down so they cannot come back.

  * the setup guide acted on a question ("what port does oracle use?" chose Oracle), took a verb for a user name ("which account should I ..."), and read "db1" as "db" + "1"
  * a customer extract was offered supplier fields (a supplier currency, a supplier payment term): the supplier tables are now left out when the source is a customer table
  * a payment term with a cash discount ("2/10 net 30") was pre-ticked as certain, though what a key means is defined in each SAP system
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb
import pytest

from src.api.guide import extract_fields
from src.services.mapping_orchestrator import OTHER_SIDE_TABLES, infer_partner_side
from tests.test_ai_assist import AssistLLM, add_rule, landed  # noqa: F401 - fixtures
from tests.test_module3_mapper import embeddings  # noqa: F401 - fixtures
from tests.test_module7_api import encoder  # noqa: F401
from tests.test_module4_transform_dedupe import csv_path, dataset, env  # noqa: F401


def harness():
    spec = importlib.util.spec_from_file_location("evaluate_ai", Path(__file__).resolve().parent.parent / "scripts" / "evaluate_ai.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ the guide reads what was said, and only what was said
def test_every_phrasing_the_evaluation_uses_is_read_correctly():
    cases = harness().GUIDE_CASES
    assert len(cases) >= 24
    wrong = [(m, extract_fields(m, kind), want) for m, kind, want in cases if extract_fields(m, kind) != want]
    assert wrong == []


@pytest.mark.parametrize("message", ["what port does oracle use?", "how do I connect to mysql", "is postgres supported", "which account should I use for SQL Server?", "can I use the sa login?"])
def test_a_question_is_not_an_instruction(message):
    assert extract_fields(message, "mssql") == {}


def test_a_question_that_also_states_a_detail_still_gives_the_detail():
    assert extract_fields("which port? host=db1 port=1433", "mssql")["host"] == "db1"                    # explicit key=value is an instruction even inside a question


def test_a_keyword_inside_another_word_is_not_a_keyword():
    assert extract_fields("host=db1 db=sales", "mssql") == {"host": "db1", "database": "sales"}           # db1 is not "db" + "1"
    assert "user" not in extract_fields("host 10.0.0.1; DROP TABLE users", "mssql")                       # users is not "user" + "s"
    assert extract_fields("my database is called ERP_PROD", "mssql") == {"database": "ERP_PROD"}
    assert extract_fields("mysql on localhost", "")["host"] == "localhost"


# ------------------------------------------------------------------ a customer extract is not offered supplier fields
@pytest.mark.parametrize("table,columns,expected", [
    ("AR_CUSTOMERS", ["CUST_ID", "NAME1"], "customer"),
    ("KNA1_EXPORT", ["KUNNR", "NAME1"], "customer"),
    ("LEGACY_VENDORS", ["LIFNR", "NAME1"], "supplier"),
    ("AP_SUPPLIER_MASTER", ["VENDOR_ID", "NAME"], "supplier"),
    ("PARTNERS", ["ID", "NAME"], "customer"),                       # nothing to go on: customer
    ("BP_MIXED", ["CUST_ID", "VENDOR_ID"], "both"),
])
def test_the_side_of_a_source_is_read_from_its_names(table, columns, expected):
    assert infer_partner_side(table, columns) == expected


def test_the_other_sides_tables_are_not_offered_so_they_cannot_be_chosen(embeddings):
    labels = lambda found: [f"{c.table_name}.{c.column_name}" for c in found]  # noqa: E731
    customer = labels(embeddings.get_top_k_candidates("CURRENCY", "VARCHAR", 12, ["EUR", "USD"], exclude_tables=OTHER_SIDE_TABLES["customer"]))
    assert customer and not any(l.startswith(("LFB1.", "LFM1.")) for l in customer) and "KNVV.WAERS" in customer
    supplier = labels(embeddings.get_top_k_candidates("PO_CURRENCY", "VARCHAR", 12, ["EUR", "USD"], exclude_tables=OTHER_SIDE_TABLES["supplier"]))
    assert supplier and not any(l.startswith(("KNB1.", "KNVV.")) for l in supplier)
    everything = labels(embeddings.get_top_k_candidates("ZTERM", "VARCHAR", 40, ["NT30"]))
    assert any(l.startswith("LFB1.") for l in everything) and any(l.startswith("KNB1.") for l in everything)      # without a side, both are there


def test_a_cryptic_customer_column_gets_the_customer_field(embeddings):
    found = embeddings.get_top_k_candidates("ZTERM", "VARCHAR", 3, ["NT30", "NT60"], exclude_tables=OTHER_SIDE_TABLES["customer"])
    assert (found[0].table_name, found[0].column_name) == ("KNB1", "ZTERM")


# ------------------------------------------------------------------ what a key means in the target system is the person's to confirm
def with_values(ctx, table, values):
    path = ctx.source_file(table).as_posix()
    con = duckdb.connect()
    con.execute(f"CREATE TABLE s AS SELECT *, row_number() OVER () AS rn FROM read_parquet('{path}')")
    for i, v in enumerate(values, start=1):
        con.execute(f"UPDATE s SET COUNTRY = ? WHERE rn = {i}", [v])
    con.execute(f"COPY (SELECT * EXCLUDE (rn) FROM s) TO '{path}' (FORMAT parquet)")


def test_a_payment_term_with_a_cash_discount_is_proposed_but_never_pre_ticked(landed):
    client, ctx, table, mset = landed(AssistLLM({}, {}))
    with_values(ctx, table, ["2/10 NET 30"] * 6 + ["NET30"] * 5 + ["NET60"] * 4 + ["COD"] * 3)
    rid = add_rule(client, mset, "KNB1", "ZTERM", "VALUE_MAPPING", "COUNTRY")["rule_id"]          # any text column will do for the test
    entries = {e["legacy_value"]: e for e in client.post(f"/api/mapping/rules/{rid}/lookup-draft", json={}).json()["entries"]}
    discount = entries["2/10 NET 30"]
    assert discount["proposed"] == "NT3D" and discount["suggested_accept"] is False and "cash discount" in discount["reason"] and discount["confidence"] <= 0.6
    assert entries["NET30"]["suggested_accept"] is True and entries["NET30"]["proposed"] == "NT30"          # the plain terms are still certain
    assert entries["NET60"]["suggested_accept"] is True and entries["COD"]["suggested_accept"] is True


@pytest.mark.parametrize("term,flagged", [("2/10 NET 30", True), ("1.5% 10 days", True), ("Skonto 2%", True), ("NET30", False), ("30 days", False), ("Net 45", False), ("COD", False)])
def test_which_payment_terms_need_the_persons_confirmation(term, flagged):
    from src.services.ai_assist import _depends_on_configuration
    assert bool(_depends_on_configuration("T052", term)) is flagged
    assert _depends_on_configuration("T005", term) is None                                              # only payment terms: a country key does not depend on a discount


# ------------------------------------------------------------------ abbreviations in legacy names
def test_abbreviations_are_spelled_out_beside_the_name_not_instead_of_it():
    from src.services.embedding_service import expand_abbreviations
    assert expand_abbreviations("cust id") == "cust id customer identifier number"
    assert expand_abbreviations("postal") == "postal"                                                    # nothing to expand
    assert expand_abbreviations("cust nr") == "cust nr customer number"


@pytest.mark.parametrize("column", ["CUST_ID", "CUST_NO", "CUSTOMER_ID", "CUST_NR", "KUNNR", "CustomerNumber", "ID"])
def test_the_partner_key_is_among_the_candidates_for_every_way_of_writing_a_customer_number(embeddings, column):
    from src.services.mapping_orchestrator import pinned_targets
    found = embeddings.get_top_k_candidates(column, "VARCHAR", 8, ["100000", "100001", "100002"], exclude_tables=OTHER_SIDE_TABLES["customer"], must_include=pinned_targets(column))
    assert "BUT000.BP_EXT" in [f"{c.table_name}.{c.column_name}" for c in found], [f"{c.table_name}.{c.column_name}" for c in found]


@pytest.mark.parametrize("column,pinned", [
    ("CUST_ID", True), ("CUSTOMER_NO", True), ("KUNNR", True), ("LIFNR", True), ("ID", True), ("CustomerNumber", True), ("VENDOR_ID", True),
    ("HOUSE_NO", False), ("TAX_ID", False), ("PHONE_NO", False), ("CUSTOMER_NAME", False), ("BANK_ID", False), ("COUNTRY_CODE", False), ("ORDER_NO", False), ("PAY_TERMS", False),
])
def test_only_a_column_that_is_plainly_the_partners_own_key_gets_the_partner_key_pinned(column, pinned):
    from src.services.mapping_orchestrator import pinned_targets
    assert bool(pinned_targets(column)) is pinned


# ------------------------------------------------------------------ German SAP-style names: the right field must be among the candidates the model is shown
# Found by comparing the AI with a person's mapping on a German file: for FIRMENNAME, ZUSATZ and UST_IDNR the shortlist did not contain NAME_ORG1, NAME_ORG2 or TAXNUM, so the model
# could only answer "no match" and the company name, SAP's most important mandatory field, was left unmapped.
@pytest.mark.parametrize("column,samples,wanted", [
    ("FIRMENNAME", ["Müller GmbH", "Schmidt AG", "Weber KG"], "BUT000.NAME_ORG1"),
    ("ZUSATZ", ["Abt. Einkauf", "z.Hd. Herrn Koch", "Buchhaltung"], "BUT000.NAME_ORG2"),
    ("UST_IDNR", ["DE123456789", "AT987654321", "FR12345678901"], "DFKKBPTAXNUM.TAXNUM"),
    ("KUNDE_NR", ["1000000", "1000001", "1000002"], "BUT000.BP_EXT"),
    ("STRASSE", ["Hauptstraße", "Bahnhofstr.", "Gartenweg"], "ADRC.STREET"),
    ("HAUSNR", ["12", "7a", "103"], "ADRC.HOUSE_NUM1"),
    ("PLZ", ["10115", "20095", "1010"], "ADRC.POST_CODE1"),
    ("ORT", ["Berlin", "Hamburg", "Wien"], "ADRC.CITY1"),
    ("LAND", ["Deutschland", "Österreich", "DE"], "ADRC.COUNTRY"),
    ("ZAHLUNGSBED", ["30 Tage netto", "sofort netto", "Vorkasse"], "KNB1.ZTERM"),
    ("WAEHRUNG", ["EUR", "CHF", "EUR"], "KNVV.WAERS"),
    ("TELEFON", ["+49 30 1234567", "+49 40 7654321", "+43 1 555"], "ADR2.TEL_NUMBER"),
])
def test_the_right_field_is_among_the_candidates_for_a_german_column_name(embeddings, column, samples, wanted):
    from src.services.mapping_orchestrator import pinned_targets
    found = embeddings.get_top_k_candidates(column, "VARCHAR", 8, samples, exclude_tables=OTHER_SIDE_TABLES["customer"], must_include=pinned_targets(column))
    labels = [f"{c.table_name}.{c.column_name}" for c in found]
    assert wanted in labels, labels


def test_german_words_are_spelled_out_beside_the_name():
    from src.services.embedding_service import expand_abbreviations
    assert "vat" in expand_abbreviations("ust idnr") and "identification number" in expand_abbreviations("ust idnr")
    assert "company name" in expand_abbreviations("firmenname")


# ------------------------------------------------------------------ an expression that rewrites the values is a decision for a person
@pytest.mark.parametrize("sql,changes", [
    ("LPAD(CAST(PLZ AS VARCHAR), 5, '0')", True), ("RPAD(NAME, 20, ' ')", True), ("REPLACE(PHONE, ' ', '')", True), ("REGEXP_REPLACE(PHONE, '[^0-9+]', '')", True),
    ("TRANSLATE(NAME, 'ae', 'AE')", True),
    ("CAST(KUNDE_NR AS VARCHAR(20))", False), ("UPPER(LEFT(TRIM(NAME), 20))", False), ("TRIM(NAME)", False), ("COALESCE(NAME, 'n/a')", False), ("SUBSTRING(NAME, 1, 40)", False),
])
def test_which_expressions_change_the_values_themselves(sql, changes):
    from src.services.rule_validator import content_changing_functions
    assert bool(content_changing_functions(sql)) is changes


def test_an_ai_expression_that_pads_a_postal_code_is_flagged_but_a_cast_is_not(session_factory, embeddings):
    from src.schemas.mapping_ai import FieldMappingRecommendation
    from src.services.rule_validator import RuleValidator
    from tests.test_ai_safeguards import _source
    from tests.test_module3_mapper import rec
    validator = RuleValidator(session_factory)
    padded = validator.validate(FieldMappingRecommendation(**rec("ADRC", "POST_CODE1", 0.95, "SQL_EXPRESSION", "LPAD(CAST(PLZ AS VARCHAR), 5, '0')")), source=_source("PLZ", 5, ["10115", "1010"]),
                                allowed_source_columns=["PLZ"])
    assert padded.persistable and any("changes the values themselves" in r and "padding" in r for r in padded.reasons)
    cast = validator.validate(FieldMappingRecommendation(**rec("ADRC", "POST_CODE1", 0.95, "SQL_EXPRESSION", "CAST(PLZ AS VARCHAR(10))")), source=_source("PLZ", 5, ["10115", "1010"]),
                              allowed_source_columns=["PLZ"])
    assert not any("changes the values themselves" in r for r in cast.reasons)
