"""The data quality rule library: planted problems are found at exactly the planted counts, personal data is masked in the examples, and the trend is kept."""

from __future__ import annotations

import csv
import io

from src.services.quality_rules import RULES, column_role, library
from tests.test_module7_api import client, ctx, dataset, encoder, env  # noqa: F401

HEADER = ["CUST_ID", "NAME1", "EMAIL", "PHONE", "VAT_ID", "COUNTRY", "POSTAL_CODE", "CREATED_DATE", "CREDIT_AMOUNT"]


def good(i):
    return {"CUST_ID": str(i), "NAME1": f"Firma {chr(65 + i)} GmbH", "EMAIL": f"user{i}@firma.de", "PHONE": f"+49 30 12345{i:02d}", "VAT_ID": f"DE1000000{i:02d}",
            "COUNTRY": "DE", "POSTAL_CODE": "10115", "CREATED_DATE": "2021-03-04", "CREDIT_AMOUNT": "1234.50"}


def messy():
    rows = [good(i) for i in range(1, 21)]
    def add(**kw):
        row = {k: "" for k in HEADER}
        row["CUST_ID"] = str(100 + len(rows))
        row.update(kw)
        rows.append(row)
    add(CUST_ID="5", NAME1="Dup Key GmbH")                                                      # the key 5 is used twice
    add(NAME1="TEST Kunde", EMAIL="a@b", PHONE="123")                                           # test customer, invalid e-mail, too few digits
    add(NAME1="Multi GmbH", EMAIL="x@y.com; z@y.com")                                           # two addresses in one field
    add(NAME1="Short Tax", VAT_ID="DE12", COUNTRY="DE")                                         # tax number too short / wrong shape for Germany
    add(NAME1="Same Tax", VAT_ID="DE100000001", COUNTRY="DE")                                   # the tax number of row 1
    add(NAME1="Bad Postal", COUNTRY="DE", POSTAL_CODE="ABC")
    add(NAME1="Atlantis Ltd", COUNTRY="Atlantis")
    add(NAME1="Name Country", COUNTRY="Germany")                                                # recognisable name, needs a lookup
    add(NAME1="MÃ¼ller GmbH")                                                         # garbled by a wrong code page
    add(NAME1="<b>Bold</b> Ltd")
    add(NAME1="Bell\x07Name")
    add(NAME1="Dates One", CREATED_DATE="03/04/2021")                                           # the 3rd of April or the 4th of March?
    add(NAME1="Dates Two", CREATED_DATE="31.12.2021")
    add(NAME1="Dates Three", CREATED_DATE="13/05/2021")
    add(NAME1="Dates Four", CREATED_DATE="someday")
    add(NAME1="Amounts One", CREDIT_AMOUNT="99,5")                                              # decimal comma among decimal points
    add(NAME1="Amounts Two", CREDIT_AMOUNT="1.234,56")
    add(NAME1="Amounts Three", CREDIT_AMOUNT="n/a")
    add(NAME1="   padded   ")
    add(NAME1="N/A")
    return rows


def as_csv(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HEADER)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def register(client, rows, table="Q_T", key=None):
    data = {"system_name": "L", "table_name": table}
    if key:
        data["primary_key_columns"] = key
    reg = client.post("/api/sources/register", data=data, files={"file": ("q.csv", as_csv(rows), "text/csv")})
    assert reg.status_code == 201, reg.text
    return reg.json()["table_id"]


def found(report, rule, column):
    hits = [f for f in report["findings"] if f["rule_id"] == rule and f["column"] == column]
    return hits[0] if hits else None


def test_the_library_is_documented_and_consistent():
    info = library()
    assert len(info) >= 30 and len({r.rule_id for r in info}) == len(info)
    assert all(r.hint and r.title and r.severity in ("HIGH", "MEDIUM", "LOW") for r in info)
    assert {"Completeness", "Validity", "Uniqueness", "Consistency", "Conformity", "Credibility", "Integrity"} <= {r.dimension for r in info}
    assert all(set(r.applies_to) <= {"any", "email", "phone", "tax", "postal", "country", "name", "date", "amount", "key"} for r in info)
    assert all((r.kind == "row") == (r.violation is not None) for r in RULES)


def test_columns_get_a_role_from_their_name_or_their_values():
    assert [column_role(n, []) for n in ("EMAIL", "TELF1", "STCD1", "VAT_ID", "PSTLZ", "LAND1", "NAME1", "ERDAT", "CREDIT_AMOUNT", "ZFIELD")] == \
        ["email", "phone", "tax", "tax", "postal", "country", "name", "date", "amount", "any"]
    assert column_role("ZFIELD", ["a@b.com", "c@d.org", "e@f.io", "g@h.net", "i@j.de"]) == "email" and column_role("CUST_ID", [], is_key=True) == "key"


def test_planted_problems_are_found_at_the_planted_counts(client):
    table_id = register(client, messy())
    assert client.get(f"/api/quality/{table_id}").status_code == 404                     # not assessed yet
    r = client.post(f"/api/quality/assess/{table_id}")
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["assessment_no"] == 1 and report["rows"] == len(messy()) and report["rules_run"] > 30 and report["previous_score"] is None
    roles = {c["column"]: c["role"] for c in report["columns"]}
    assert roles == {"CUST_ID": "any", "NAME1": "name", "EMAIL": "email", "PHONE": "phone", "VAT_ID": "tax", "COUNTRY": "country", "POSTAL_CODE": "postal",
                     "CREATED_DATE": "date", "CREDIT_AMOUNT": "amount"}

    expect = {("E01", "EMAIL"): 2, ("E02", "EMAIL"): 1, ("P01", "PHONE"): 1, ("T01", "VAT_ID"): 1, ("T02", "VAT_ID"): 1, ("T03", "VAT_ID"): 2, ("Z01", "POSTAL_CODE"): 1,
              ("C01", "COUNTRY"): 1, ("C02", "COUNTRY"): 1, ("G06", "NAME1"): 1, ("G07", "NAME1"): 1, ("G03", "NAME1"): 1, ("G01", "NAME1"): 1, ("G04", "NAME1"): 1,
              ("N04", "NAME1"): 1, ("D01", "CREATED_DATE"): 1, ("D02", "CREATED_DATE"): 1, ("D03", "CREATED_DATE"): 3, ("A01", "CREDIT_AMOUNT"): 1, ("A02", "CREDIT_AMOUNT"): 2}
    for (rule, column), count in expect.items():
        hit = found(report, rule, column)
        assert hit and hit["violations"] == count, (rule, column, hit and hit["violations"], count)
        assert hit["hint"] and hit["rate"] == round(count / hit["checked"], 4)
    # nothing is reported where nothing is wrong
    assert found(report, "E01", "NAME1") is None and found(report, "T02", "COUNTRY") is None
    assert [f["severity"] for f in report["findings"]][:3] == ["HIGH"] * 3                      # worst first
    assert 50 < report["overall_score"] < 100 and set(report["dimension_scores"]) >= {"Validity", "Uniqueness", "Consistency", "Integrity"}


def test_a_declared_key_is_checked_for_duplicates_and_blanks(tmp_path):
    import duckdb
    from src.services.quality_rules import QualityService
    path = tmp_path / "k.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t(ID VARCHAR, NAME1 VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [("1", "A"), ("2", "B"), ("2", "C"), ("3", "D"), (" ", "E"), ("4", "F")])
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT parquet)")
    report = QualityService(tmp_path / "ws").assess(1, "T", path, "u", ["ID"], {}).model_dump()
    assert found(report, "K01", "ID")["violations"] == 2 and found(report, "K02", "ID")["violations"] == 1
    assert next(c for c in report["columns"] if c["column"] == "ID")["role"] == "key" and found(report, "K01", "NAME1") is None


def test_examples_of_personal_data_are_masked_and_the_rest_is_shown(client):
    table_id = register(client, messy())
    report = client.post(f"/api/quality/assess/{table_id}").json()
    email, name = found(report, "E01", "EMAIL"), found(report, "G07", "NAME1")
    assert email["samples"] and all("y.com" not in s and "a@b" not in s for s in email["samples"]) and all(set(s) <= set("aA9@;. ") for s in email["samples"])
    assert name["samples"] and all("Bold" not in s for s in name["samples"])                      # a name is confidential: shown as a shape
    assert found(report, "D02", "CREATED_DATE")["samples"] == ["03/04/2021"]                      # a date is not personal: shown as it is
    assert found(report, "A01", "CREDIT_AMOUNT")["samples"] == ["a/a"]                              # credit data is confidential too, so it is shown as a shape
    assert next(c for c in report["columns"] if c["column"] == "EMAIL")["pii"] == "RESTRICTED_PII"


def test_each_assessment_is_kept_and_reports_the_change(client, ctx):
    table_id = register(client, messy())
    first = client.post(f"/api/quality/assess/{table_id}").json()
    again = client.post(f"/api/quality/assess/{table_id}").json()
    assert again["assessment_no"] == 2 and again["previous_score"] == first["overall_score"] and again["change"] == 0.0

    clean = [good(i) for i in range(1, 21)]                                                       # the business cleaned the data and it is landed again
    assert client.post("/api/sources/register", data={"system_name": "L", "table_name": "Q_T"}, files={"file": ("q.csv", as_csv(clean), "text/csv")}).status_code == 201
    better = client.post(f"/api/quality/assess/{table_id}").json()
    assert better["assessment_no"] == 3 and better["change"] > 0 and better["overall_score"] > first["overall_score"] and better["findings"] == []
    history = client.get(f"/api/quality/{table_id}/history").json()
    assert [h["assessment_no"] for h in history] == [1, 2, 3] and history[0]["findings"] > 0 and history[2]["findings"] == 0 and history[0]["high_findings"] > 0
    assert client.get(f"/api/quality/{table_id}").json()["assessment_no"] == 3
    assert client.get(f"/api/quality/{table_id}/assessment/1").json()["overall_score"] == first["overall_score"]
    assert client.get(f"/api/quality/{table_id}/assessment/9").status_code == 404


def test_rules_that_need_a_country_say_so_when_there_is_none(client):
    rows = [{"ID": str(i), "ZIP": "ABC", "VAT": "12"} for i in range(1, 8)]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["ID", "ZIP", "VAT"])
    writer.writeheader()
    writer.writerows(rows)
    table_id = client.post("/api/sources/register", data={"system_name": "L", "table_name": "Q_NC"}, files={"file": ("n.csv", buf.getvalue().encode(), "text/csv")}).json()["table_id"]
    report = client.post(f"/api/quality/assess/{table_id}").json()
    assert any("no country column" in n for n in report["notes"])
    assert found(report, "Z01", "ZIP") is None and found(report, "T02", "VAT") is None            # not judged per country
    assert found(report, "T01", "VAT")["violations"] == 7 and found(report, "Z02", "ZIP") is None  # but the country-free rules still run ("ABC" has a plausible shape)


def test_assessing_needs_a_landed_file_and_an_existing_table(client):
    assert client.post("/api/quality/assess/999999").status_code == 404
    nofile = client.post("/api/sources/register", data={"system_name": "L", "table_name": "NOFILE"})
    assert nofile.status_code == 201 and nofile.json()["table_id"] is not None
    assert client.post(f"/api/quality/assess/{nofile.json()['table_id']}").status_code == 409        # registered, but no file landed
    assert client.get("/api/quality/rules").json()[0]["rule_id"]
