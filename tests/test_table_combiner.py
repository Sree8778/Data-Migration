"""Combining landed tables: structured joins and filters, measured before they are created, refused when they would silently repeat records."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError

from src.schemas.combine import CombineSpec, FilterStep
from src.services.table_combiner import CombineError, TableCombiner


@pytest.fixture()
def tables(tmp_path):
    """customers (1 per id), contacts (0..n per customer), terms (a code table): all text, as landed snapshots are."""
    con = duckdb.connect()
    con.execute("CREATE TABLE customers AS SELECT * FROM (VALUES "
                "('1','Acme GmbH','DE',' NET30 '),('2','Beta Inc','US','net60'),('3','Gamma SA','FR',''),('4','Delta Ltd','GB','NET30'),('5','Echo AG','DE','COD')) t(CUST_ID, NAME, COUNTRY, TERMS)")
    con.execute("CREATE TABLE contacts AS SELECT * FROM (VALUES "
                "('c1','1','Ann'),('c2','1','Bob'),('c3','2','Cid'),('c4','9','Orphan'),('c5','','Blank')) t(CONTACT_ID, CUST_ID, CONTACT)")
    con.execute("CREATE TABLE terms AS SELECT * FROM (VALUES ('NET30','Net 30 days'),('NET60','Net 60 days'),('COD','Cash on delivery')) t(CODE, TEXT)")
    paths = {}
    for i, name in enumerate(("customers", "contacts", "terms"), start=1):
        p = tmp_path / f"{name}.parquet"
        con.execute(f"COPY {name} TO '{p.as_posix()}' (FORMAT parquet)")
        paths[i] = (name.upper(), p)
    return paths


def spec(**kw):
    base = {"base": {"table_id": 1, "alias": "c"}}
    base.update(kw)
    return CombineSpec.model_validate(base)


TERMS_JOIN = {"table_id": 3, "alias": "t", "type": "left", "on": [{"left": "c.TERMS", "right": "t.CODE"}]}
CONTACT_JOIN = {"table_id": 2, "alias": "k", "type": "left", "on": [{"left": "c.CUST_ID", "right": "k.CUST_ID"}]}


def run(s, tables, rows=50):
    return TableCombiner().preview(s, tables, sample_rows=rows)


# ------------------------------------------------------------------ a clean join
def test_a_lookup_join_keeps_every_customer_and_matches_blanks_and_case_by_the_chosen_rule(tables):
    p = run(spec(joins=[TERMS_JOIN]), tables)
    assert (p.base_rows, p.rows_after_joins, p.rows_out, p.blocked) == (5, 5, 5, None)
    by_id = {r[0]: dict(zip([c.name for c in p.columns], r)) for r in p.sample}
    assert by_id["1"]["TEXT"] == "Net 30 days" and by_id["2"]["TEXT"] == "Net 60 days" and by_id["5"]["TEXT"] == "Cash on delivery"        # ' NET30 ' and 'net60' match (trim, case)
    assert by_id["3"]["TEXT"] == "" and by_id["4"]["TEXT"] == "Net 30 days"                                                              # a blank key matches nothing
    j = p.joins[0]
    assert (j.left_rows, j.right_rows, j.left_rows_without_a_match, j.left_rows_with_several, j.most_partners) == (5, 3, 1, 0, 1)
    assert any("1 of 5 CUSTOMERS records find no partner in TERMS" in w and "they stay" in w for w in p.warnings)
    exact = run(spec(joins=[TERMS_JOIN], match="exact"), tables)                                                                         # exact: ' NET30 ' and 'net60' no longer match
    assert exact.joins[0].left_rows_without_a_match == 3
    trimmed = run(spec(joins=[TERMS_JOIN], match="trim"), tables)                                                                        # trim only: 'net60' still does not match 'NET60'
    assert trimmed.joins[0].left_rows_without_a_match == 2


def test_an_inner_join_drops_unmatched_records_and_says_so(tables):
    p = run(spec(joins=[{**TERMS_JOIN, "type": "inner"}]), tables)
    assert (p.base_rows, p.rows_out) == (5, 4) and any("DROPPED by the inner join" in w for w in p.warnings)


# ------------------------------------------------------------------ fan-out
def test_a_join_that_repeats_a_customer_is_refused_unless_allowed(tables):
    p = run(spec(joins=[CONTACT_JOIN]), tables)
    assert p.base_rows == 5 and p.rows_after_joins == 6 and p.blocked and "turn 5 CUSTOMERS records into 6 rows (1 repeated)" in p.blocked
    assert p.joins[0].left_rows_with_several == 1 and p.joins[0].most_partners == 2 and p.joins[0].left_rows_without_a_match == 3        # customers 3, 4 and 5 have no contact
    with pytest.raises(CombineError, match="repeated"):
        TableCombiner().materialise(spec(joins=[CONTACT_JOIN]), tables, Path("unused.csv"))
    allowed = run(spec(joins=[CONTACT_JOIN], allow_fanout=True), tables)
    assert allowed.blocked is None and allowed.rows_out == 6 and any("allowed, so records repeat" in w for w in allowed.warnings)
    flipped = run(spec(base={"table_id": 2, "alias": "k"}, joins=[{"table_id": 1, "alias": "c", "type": "left", "on": [{"left": "k.CUST_ID", "right": "c.CUST_ID"}]}]), tables)
    assert flipped.blocked is None and flipped.rows_out == 5 and flipped.joins[0].left_rows_without_a_match == 2                       # the many-side as base: no repeats (orphan and blank find no customer)


def test_a_blank_key_never_matches_another_blank_key(tables):
    p = run(spec(base={"table_id": 2, "alias": "k"}, joins=[{"table_id": 1, "alias": "c", "type": "left", "on": [{"left": "k.CUST_ID", "right": "c.CUST_ID"}]}]), tables)
    names = [c.name for c in p.columns]
    blank = next(r for r in p.sample if r[names.index("CONTACT")] == "Blank")
    assert blank[names.index("c_NAME") if "c_NAME" in names else names.index("NAME")] == ""                                              # CUST_ID '' on both sides matched nothing


# ------------------------------------------------------------------ filters
@pytest.mark.parametrize("flt, expected", [
    ({"column": "c.COUNTRY", "op": "equals", "values": ["de"]}, 2), ({"column": "c.COUNTRY", "op": "not_equals", "values": ["DE"]}, 3),
    ({"column": "c.COUNTRY", "op": "in", "values": ["us", "FR"]}, 2), ({"column": "c.NAME", "op": "contains", "values": ["inc"]}, 1),
    ({"column": "c.NAME", "op": "starts_with", "values": ["g"]}, 1), ({"column": "c.TERMS", "op": "is_blank"}, 1), ({"column": "c.TERMS", "op": "not_blank"}, 4),
    ({"column": "c.NAME", "op": "regex", "values": ["(GmbH|AG)$"]}, 2),
])
def test_filters(tables, flt, expected):
    p = run(spec(filters=[flt]), tables)
    assert p.rows_out == expected and p.removed_by_filters == 5 - expected


def test_filters_work_on_joined_columns_and_a_filter_that_removes_everything_blocks(tables):
    p = run(spec(joins=[TERMS_JOIN], filters=[{"column": "t.TEXT", "op": "contains", "values": ["net 30"]}]), tables)
    assert p.rows_out == 2 and p.removed_by_filters == 3 and any("filters remove 3 of 5" in w for w in p.warnings)
    none = run(spec(filters=[{"column": "c.NAME", "op": "equals", "values": ["nobody"]}]), tables)
    assert none.blocked == "the combination (with its filters) has no rows"
    with pytest.raises(CombineError, match="not a valid regular expression"):
        run(spec(filters=[{"column": "c.NAME", "op": "regex", "values": ["(unclosed"]}]), tables)


# ------------------------------------------------------------------ the columns that come out
def test_columns_with_the_same_name_get_their_alias_and_picks_can_rename(tables):
    p = run(spec(joins=[CONTACT_JOIN]), tables)
    assert [c.name for c in p.columns] == ["c_CUST_ID", "NAME", "COUNTRY", "TERMS", "CONTACT_ID", "k_CUST_ID", "CONTACT"]
    picked = run(spec(joins=[TERMS_JOIN], columns=[{"ref": "c.CUST_ID", "name": "KUNNR"}, {"ref": "c.NAME"}, {"ref": "t.TEXT", "name": "TERMS_TEXT"}]), tables)
    assert [c.name for c in picked.columns] == ["KUNNR", "NAME", "TERMS_TEXT"] and picked.sample[0] == ["1", "Acme GmbH", "Net 30 days"]
    with pytest.raises(CombineError, match="two output columns would be called"):
        run(spec(columns=[{"ref": "c.NAME"}, {"ref": "c.COUNTRY", "name": "name"}]), tables)


def test_output_is_in_the_base_tables_order_and_written_as_text(tables, tmp_path):
    rows, columns, preview = TableCombiner().materialise(spec(joins=[TERMS_JOIN], filters=[{"column": "c.COUNTRY", "op": "equals", "values": ["DE"]}]), tables, tmp_path / "out.csv")
    assert rows == 2 and columns == ["CUST_ID", "NAME", "COUNTRY", "TERMS", "CODE", "TEXT"]
    text = (tmp_path / "out.csv").read_text(encoding="utf-8").splitlines()
    assert text[0] == "CUST_ID,NAME,COUNTRY,TERMS,CODE,TEXT" and text[1].startswith("1,Acme GmbH,DE,") and text[2].startswith("5,Echo AG,DE,COD,COD,Cash")


# ------------------------------------------------------------------ what is refused
def test_unknown_aliases_columns_tables_and_misplaced_conditions_are_refused_with_the_names(tables):
    with pytest.raises(CombineError, match="has no column 'NOPE'"):
        run(spec(filters=[{"column": "c.NOPE", "op": "is_blank"}]), tables)
    with pytest.raises(CombineError, match="unknown alias 'x'"):
        run(spec(filters=[{"column": "x.NAME", "op": "is_blank"}]), tables)
    with pytest.raises(CombineError, match="not a column reference"):
        run(spec(filters=[{"column": "NAME", "op": "is_blank"}]), tables)
    with pytest.raises(CombineError, match="has no landed file"):
        run(spec(joins=[{"table_id": 99, "alias": "z", "on": [{"left": "c.CUST_ID", "right": "z.A"}]}]), tables)
    with pytest.raises(CombineError, match="must belong to t"):
        run(spec(joins=[{**TERMS_JOIN, "on": [{"left": "c.TERMS", "right": "c.TERMS"}]}]), tables)
    with pytest.raises(CombineError, match="not joined yet"):
        run(spec(joins=[{**TERMS_JOIN, "on": [{"left": "t.CODE", "right": "t.CODE"}]}]), tables)


def test_the_description_itself_is_validated():
    with pytest.raises(ValidationError):
        CombineSpec.model_validate({"base": {"table_id": 1, "alias": "1bad"}})                                                          # an alias is a name
    with pytest.raises(ValidationError, match="its own alias"):
        CombineSpec.model_validate({"base": {"table_id": 1, "alias": "c"}, "joins": [{**TERMS_JOIN, "alias": "C"}]})
    with pytest.raises(ValidationError):
        FilterStep(column="c.X", op="equals", values=[])
    with pytest.raises(ValidationError):
        CombineSpec.model_validate({"base": {"table_id": 1, "alias": "c"}, "joins": [{**TERMS_JOIN, "on": []}]})
    with pytest.raises(ValidationError):
        CombineSpec.model_validate({"base": {"table_id": 1, "alias": "c"}, "sql": "SELECT 1"})                                           # there is no free-text SQL


def test_hostile_values_are_data_not_sql(tables):
    p = run(spec(filters=[{"column": "c.NAME", "op": "equals", "values": ["x' OR '1'='1"]}]), tables)
    assert p.blocked == "the combination (with its filters) has no rows" and p.base_rows == 5
    q = run(spec(filters=[{"column": "c.NAME", "op": "equals", "values": ["Acme GmbH"]}]), tables)
    assert q.rows_out == 1


# ------------------------------------------------------------------ through the API: landing, evidence, rebuild
CUSTOMERS_CSV = "CUST_ID,NAME,COUNTRY,TERMS\n1,Acme GmbH,DE, NET30 \n2,Beta Inc,US,net60\n3,Gamma SA,FR,\n4,Delta Ltd,GB,NET30\n5,Echo AG,DE,COD\n"
CONTACTS_CSV = "CONTACT_ID,CUST_ID,CONTACT\nc1,1,Ann\nc2,1,Bob\nc3,2,Cid\nc4,9,Orphan\nc5,,Blank\n"
TERMS_CSV = "CODE,TEXT\nNET30,Net 30 days\nNET60,Net 60 days\nCOD,Cash on delivery\n"


@pytest.fixture()
def api(session_factory, service, tmp_path):
    from tests.test_module7_api import make_client, make_ctx
    ctx = make_ctx(session_factory, tmp_path)
    client = make_client(ctx)

    def land(name, text, system="LEGACY"):
        r = client.post("/api/sources/register", data={"system_name": system, "system_type": "FLAT_FILE", "table_name": name},
                        files={"file": (f"{name}.csv", text.encode(), "text/csv")})
        assert r.status_code == 201, r.text
        return r.json()["table_id"]

    ids = {"customers": land("CUSTOMERS", CUSTOMERS_CSV), "contacts": land("CONTACTS", CONTACTS_CSV), "terms": land("TERMS", TERMS_CSV)}
    return client, ctx, ids


def body(ids, **kw):
    base = {"base": {"table_id": ids["customers"], "alias": "c"},
            "joins": [{"table_id": ids["terms"], "alias": "t", "type": "left", "on": [{"left": "c.TERMS", "right": "t.CODE"}]}],
            "system_name": "COMBINED", "table_name": "CUSTOMER_TERMS", "primary_key_columns": ["CUST_ID"]}
    base.update(kw)
    return base


def test_preview_creates_nothing_and_creating_lands_a_normal_source_with_its_origin(api):
    client, ctx, ids = api
    r = client.post("/api/sources/combine/preview", json={k: v for k, v in body(ids).items() if k not in ("system_name", "table_name", "primary_key_columns")})
    assert r.status_code == 200 and r.json()["rows_out"] == 5 and r.json()["blocked"] is None
    names = lambda: sorted(t["table_name"] for t in client.get("/api/tables").json())
    assert names() == ["CONTACTS", "CUSTOMERS", "TERMS"]                                                                       # the preview created nothing
    created = client.post("/api/sources/combine", json=body(ids))
    assert created.status_code == 201, created.text
    reg = created.json()
    assert names() == ["CONTACTS", "CUSTOMERS", "CUSTOMER_TERMS", "TERMS"]
    assert reg["rows_landed"] == 5 and reg["control_status"] == "MATCH" and reg["origin"]["kind"] == "combined"
    origin = reg["origin"]
    assert {e["table_name"] for e in origin["inputs"]} == {"CUSTOMERS", "TERMS"} and all(len(e["snapshot_sha256"]) == 64 for e in origin["inputs"])
    assert origin["spec"]["joins"][0]["alias"] == "t" and origin["combined_by"] == "alice" and origin["joins"][0]["left_rows_without_a_match"] == 1
    landed = duckdb.connect().execute(f"SELECT CUST_ID, TEXT FROM read_parquet('{(ctx.table_dir(reg['table_id']) / 'source.parquet').as_posix()}') ORDER BY CUST_ID").fetchall()
    assert landed == [("1", "Net 30 days"), ("2", "Net 60 days"), ("3", None), ("4", "Net 30 days"), ("5", "Cash on delivery")]       # all text; the unmatched stay, with no value (a blank cell lands as empty, as in every source)
    assert client.post(f"/api/profile/{reg['table_id']}").status_code == 200                                                   # a combined table is a source like any other


def test_the_api_refuses_what_the_service_refuses(api):
    client, _, ids = api
    repeat = body(ids, joins=[{"table_id": ids["contacts"], "alias": "k", "type": "left", "on": [{"left": "c.CUST_ID", "right": "k.CUST_ID"}]}])
    r = client.post("/api/sources/combine", json=repeat)
    assert r.status_code == 422 and "repeated" in r.text
    assert client.post("/api/sources/combine", json={**repeat, "allow_fanout": True, "primary_key_columns": []}).status_code == 201
    assert client.post("/api/sources/combine", json=body(ids, primary_key_columns=["NOPE"])).status_code == 422
    assert client.post("/api/sources/combine", json=body(ids, joins=[{"table_id": 999, "alias": "t", "on": [{"left": "c.TERMS", "right": "t.CODE"}]}])).status_code == 404
    assert client.post("/api/sources/combine", json={**body(ids), "sql": "DROP TABLE x"}).status_code == 422


def test_a_primary_key_that_repeats_after_combining_is_rejected_and_nothing_is_registered(api):
    client, _, ids = api
    repeat = body(ids, joins=[{"table_id": ids["contacts"], "alias": "k", "type": "left", "on": [{"left": "c.CUST_ID", "right": "k.CUST_ID"}]}], allow_fanout=True, primary_key_columns=["c_CUST_ID"])
    r = client.post("/api/sources/combine", json={**repeat, "system_name": "COMBINED_BAD", "table_name": "BAD"})
    assert r.status_code == 422 and "c_CUST_ID" in r.text and "COMBINED_BAD" not in client.get("/api/sources").text


def test_rebuild_reports_which_input_changed_and_uses_the_current_data(api):
    client, ctx, ids = api
    first = client.post("/api/sources/combine", json=body(ids)).json()
    same = client.post(f"/api/sources/combine/{first['table_id']}/rebuild")
    assert same.status_code == 201 and same.json()["table_id"] == first["table_id"] and "same snapshots" in same.json()["warnings"][0]
    client.post("/api/sources/register", data={"system_name": "LEGACY", "system_type": "FLAT_FILE", "table_name": "TERMS"},
                files={"file": ("terms.csv", (TERMS_CSV + "NET90,Net 90 days\n").encode(), "text/csv")})
    changed = client.post(f"/api/sources/combine/{first['table_id']}/rebuild")
    assert changed.status_code == 201 and "changed since the last build: TERMS" in changed.json()["warnings"][0]
    assert client.post(f"/api/sources/combine/{ids['terms']}/rebuild").status_code == 409                                       # not a combined table
