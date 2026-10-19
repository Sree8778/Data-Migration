"""Multi-format source registration over HTTP: preview, options, control totals, snapshots, audit evidence."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from src.db.models import CatColumn, CatDataSource, CatTable
from tests.test_module7_api import HEADER, client, ctx, dataset, encoder, env, subset, upload  # noqa: F401


def table_rows(rows) -> list[list[str]]:
    return [[r[c] for c in HEADER] for r in rows]


def xlsx_bytes(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for row in table_rows(rows):
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def semicolon_cp1252(rows) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(HEADER)
    writer.writerows(table_rows(rows))
    return buf.getvalue().encode("cp1252", errors="replace")


def register(client, name, content, options=None, table="T1", **data):
    form = {"system_name": "SRC_SYS", "table_name": table, **data}
    if options is not None:
        form["options"] = json.dumps(options)
    return client.post("/api/sources/register", data=form, files={"file": (name, content, "application/octet-stream")})


@pytest.fixture()
def clean(dataset):
    rows = [dict(r) for r in subset(dataset, 300, clean=True)]
    rows[0]["CUST_NAME"] = "Müller Söhne"  # non-ASCII, so a cp1252 file really needs its encoding detected
    return rows


def test_preview_detects_and_stores_nothing(client, ctx, clean, session_factory):
    r = client.post("/api/sources/preview", files={"file": ("x.txt", semicolon_cp1252(clean), "text/plain")})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["format"] == "delimited" and p["delimiter"] == ";" and p["encoding"] == "cp1252"
    assert p["columns"] == HEADER and len(p["rows"]) == 20 and p["options"]["delimiter"] == ";"

    wb = client.post("/api/sources/preview", files={"file": ("x.xlsx", xlsx_bytes(clean), "application/octet-stream")}).json()
    assert wb["format"] == "excel" and wb["sheets"] == ["Sheet"] and wb["columns"] == HEADER

    bad = client.post("/api/sources/preview", data={"options": '{"delimiter": 5, "bogus": 1}'},
                      files={"file": ("x.csv", b"A\n1\n", "text/csv")})
    assert bad.status_code == 422 and "invalid options" in bad.json()["detail"]
    assert client.post("/api/sources/preview", files={"file": ("x.docx", b"abc", "text/plain")}).status_code == 422
    assert client.post("/api/sources/preview").status_code == 422

    assert not list((ctx.workspace / "tmp").glob("*"))  # scratch is always cleaned
    with session_factory() as s:
        assert s.query(CatTable).count() == 0 and s.query(CatDataSource).count() == 0


def test_register_excel_and_semicolon_files_and_inspect_the_snapshot(client, ctx, clean):
    xl = register(client, "customers.xlsx", xlsx_bytes(clean), table="XL", primary_key_columns="CUST_ID")
    assert xl.status_code == 201, xl.text
    body = xl.json()
    assert body["format"] == "excel" and body["rows_landed"] == len(clean) and body["columns_registered"] == len(HEADER)
    assert body["control_status"] == "NOT_PROVIDED"

    sc = register(client, "customers.txt", semicolon_cp1252(clean), table="SC", options={"delimiter": ";", "encoding": "cp1252"})
    assert sc.status_code == 201 and sc.json()["format"] == "delimited"

    listed = {t["table_name"]: t for t in client.get("/api/tables").json()}
    assert listed["XL"]["source_format"] == "excel" and listed["XL"]["rows_landed"] == len(clean) and listed["XL"]["has_source_file"]
    assert listed["SC"]["source_format"] == "delimited"

    snap = client.get(f"/api/tables/{xl.json()['table_id']}/snapshot")
    assert snap.status_code == 200
    s = snap.json()
    assert s["original_filename"] == "customers.xlsx" and len(s["original_sha256"]) == 64 and s["landed_by"] == "alice"
    assert s["rows_landed"] == len(clean) and {c["name"] for c in s["columns"]} == set(HEADER)
    assert client.get("/api/tables/9999/snapshot").status_code == 404

    # both land as the same Parquet snapshot and profile the same way
    px, ps = (client.post(f"/api/profile/{register_id}") for register_id in (xl.json()["table_id"], sc.json()["table_id"]))
    assert px.status_code == 200 and ps.status_code == 200
    assert px.json()["total_rows"] == ps.json()["total_rows"] == len(clean)
    types = lambda p: {c["column_name"]: c["detected_type"] for c in p["columns"]}  # noqa: E731
    assert types(px.json()) == types(ps.json())
    assert any(f for f in ctx.table_dir(xl.json()["table_id"]).iterdir() if f.name == "source.parquet")


def test_a_rejected_file_leaves_nothing_behind(client, ctx, clean, session_factory):
    # control total says 5 rows, the file has 300
    r = register(client, "c.xlsx", xlsx_bytes(clean), table="CTL", expected_rows="5")
    assert r.status_code == 422 and "control total mismatch" in r.json()["detail"]
    # a key column that is not in the file
    r2 = register(client, "c.xlsx", xlsx_bytes(clean), table="CTL", primary_key_columns="NOPE")
    assert r2.status_code == 422 and "primary_key_columns" in r2.json()["detail"]
    # malformed rows
    r3 = register(client, "m.csv", b"A,B\n1,2\n3\n", table="CTL")
    assert r3.status_code == 422 and "malformed" in r3.json()["detail"]
    with session_factory() as s:
        assert s.query(CatTable).count() == 0 and s.query(CatColumn).count() == 0 and s.query(CatDataSource).count() == 0
    assert not list((ctx.workspace / "tmp").glob("*"))

    ok = register(client, "c.xlsx", xlsx_bytes(clean), table="CTL", expected_rows=str(len(clean)))
    assert ok.status_code == 201 and ok.json()["control_status"] == "MATCH"
    assert client.get(f"/api/tables/{ok.json()['table_id']}/snapshot").json()["expected_rows"] == len(clean)


def test_reupload_in_a_different_format_replaces_the_snapshot(client, ctx, clean):
    first = upload(client, clean, table="SWAP")  # legacy-style csv upload, now landed as Parquet
    tid = first.json()["table_id"]
    directory = ctx.table_dir(tid)
    assert first.json()["format"] == "delimited" and (directory / "source.parquet").is_file()
    assert not (directory / "source.csv").exists()
    again = register(client, "swap.xlsx", xlsx_bytes(clean[:100]), table="SWAP", system_name="LEGACY_ORACLE_AR")  # same table => upsert
    assert again.json()["table_id"] == tid and again.json()["rows_landed"] == 100
    assert client.get(f"/api/tables/{tid}/snapshot").json()["format"] == "excel"
    assert len(list((directory / "original").iterdir())) == 1  # only the current original is kept
    assert ctx.source_file(tid) == directory / "source.parquet"


def test_pipeline_from_an_excel_source_carries_extract_evidence_into_the_certificate(client, ctx, clean):
    assert client.post("/api/admin/seed-sap-catalog").status_code == 200
    reg = register(client, "ar.xlsx", xlsx_bytes(clean), table="AR_CUSTOMERS", expected_rows=str(len(clean)), primary_key_columns="CUST_ID")
    assert reg.status_code == 201, reg.text
    table_id = reg.json()["table_id"]
    assert client.post(f"/api/profile/{table_id}").status_code == 200
    gen = client.post(f"/api/mapping/generate/{table_id}")
    assert gen.status_code == 201, gen.text
    mapping_id = gen.json()["mapping_set_id"]
    for column, logic in (("BU_GROUP", "'BP01'"), ("TYPE", "'2'")):
        assert client.post(f"/api/mapping/{mapping_id}/rules", json={
            "target_table": "BUT000", "target_column": column, "rule_type": "STATIC_VALUE", "transformation_logic": logic}).status_code == 201

    run = client.post(f"/api/pipeline/run/{mapping_id}", json={"wave_name": "XL_WAVE"})
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]
    assert run.json()["run"]["source_records"] == len(clean)
    evidence = ctx.run_dir(run_id) / "source_snapshot.json"
    assert evidence.is_file() and json.loads(evidence.read_text())["format"] == "excel"

    report = client.get(f"/api/reconciliation/{run_id}").json()["report"]
    checks = {c["name"]: c for c in report["checks"]}
    assert checks["extract_matches_landed_snapshot"]["passed"] and "ar.xlsx" in checks["extract_matches_landed_snapshot"]["detail"]
    assert checks["source_control_total_verified"]["passed"]


def test_workspace_listing_and_ui_expose_the_new_controls(client):
    html = client.get("/ui/sources").text
    for element in ('id="reg-file"', 'id="read-options"', 'id="opt-format"', 'id="opt-delimiter"', 'id="opt-encoding"', 'id="opt-sheet"',
                    'id="opt-header-row"', 'id="opt-member"', 'id="opt-fixed"', 'id="opt-expected"', 'id="refresh-preview"', 'id="preview-table"'):
        assert element in html, element
    js = client.get("/static/js/index.js").text
    assert "/api/sources/preview" in js and "readOptions" in js and "expected_rows" in js
    assert ".xlsx" in html and ".parquet" in html
    assert not Path("src/api/templates/index.html").read_text(encoding="utf-8").count("style=")  # strict CSP: no inline styles


def test_a_primary_key_that_does_not_identify_rows_is_rejected_with_the_offending_values(client, ctx, clean, session_factory):
    repeated = [dict(r) for r in clean]
    repeated[5]["CUST_ID"] = repeated[6]["CUST_ID"]
    repeated[50]["CUST_ID"] = repeated[51]["CUST_ID"]
    r = register(client, "k.xlsx", xlsx_bytes(repeated), table="KEYS", primary_key_columns="CUST_ID")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "primary key (CUST_ID)" in detail and "2 key value(s) are used by more than one row" in detail and repeated[6]["CUST_ID"] in detail

    blank = [dict(r) for r in clean]
    blank[3]["CUST_ID"] = "  "
    r2 = register(client, "k.xlsx", xlsx_bytes(blank), table="KEYS", primary_key_columns="CUST_ID")
    assert r2.status_code == 422 and "1 row(s) have a blank key" in r2.json()["detail"]

    # a composite key is judged on the combination, and without a declared key nothing is checked
    composite = register(client, "k.xlsx", xlsx_bytes(repeated), table="KEYS", primary_key_columns="CUST_ID,CUST_NAME")
    assert composite.status_code == 201, composite.text
    assert register(client, "k2.xlsx", xlsx_bytes(repeated), table="KEYS2").status_code == 201
    with session_factory() as s:
        assert {t.table_name for t in s.query(CatTable)} == {"KEYS", "KEYS2"}  # the rejected attempts left nothing behind


def test_default_deduplication_also_matches_on_email_and_phone(client, ctx, clean):
    assert client.post("/api/admin/seed-sap-catalog").status_code == 200
    rows = [dict(r) for r in clean[:60]]
    for r in rows:
        r["TAX_ID"] = ""
    rows[10].update(EMAIL="  Billing@Shared-Example.COM ", PHONE="")
    rows[20].update(EMAIL="billing@shared-example.com", PHONE="")      # same mailbox, nothing else in common
    rows[30].update(EMAIL="", PHONE="+49 (30) 555-0100")
    rows[40].update(EMAIL="", PHONE="4930-555 0100")                    # same number written differently
    reg = register(client, "d.xlsx", xlsx_bytes(rows), table="DEDUPE", primary_key_columns="CUST_ID")
    assert reg.status_code == 201, reg.text
    mapping = client.post("/api/mapping-sets", json={"source_table_id": reg.json()["table_id"]}).json()["mapping_set_id"]
    rules = (("BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME", None), ("BUT000", "BU_GROUP", "STATIC_VALUE", None, "'BP01'"), ("BUT000", "TYPE", "STATIC_VALUE", None, "'2'"))
    for table, column, rule_type, source, logic in rules:
        body = {"target_table": table, "target_column": column, "rule_type": rule_type, "source_column": source, "transformation_logic": logic}
        assert client.post(f"/api/mapping/{mapping}/rules", json=body).status_code == 201
    run = client.post(f"/api/pipeline/run/{mapping}", json={"wave_name": "EMAIL_PHONE"})
    assert run.status_code == 201, run.text
    dedup = run.json()["dedup"]
    assert set(dedup["exact_edges_by_key"]) >= {"email_email", "phone_phone"}
    assert dedup["exact_edges_by_key"]["email_email"] == 1 and dedup["exact_edges_by_key"]["phone_phone"] == 1
    assert dedup["duplicate_children"] >= 2


def test_key_columns_are_recognised_under_generic_and_sap_style_names():
    from src.api.routes.pipeline import _EMAIL_COLUMN, _PHONE_COLUMN, _TAX_COLUMN
    assert all(_TAX_COLUMN.search(n) for n in ("TAX_ID", "VAT_NO", "STCD1", "STCD2", "STCEG", "CUST_USTID", "GSTIN"))
    assert all(_EMAIL_COLUMN.search(n) for n in ("EMAIL", "E_MAIL", "SMTP_ADDR", "CONTACT_MAIL"))
    assert all(_PHONE_COLUMN.search(n) for n in ("PHONE", "TEL", "TELF1", "TELF2", "TEL_NUMBER", "MOBILE_NO"))
    assert not any(p.search(n) for p in (_TAX_COLUMN, _EMAIL_COLUMN, _PHONE_COLUMN) for n in ("STREET", "HOTEL", "ORT01", "CUST_NAME", "STATUS", "TELEX_X"))


def test_reuploading_a_different_layout_retires_the_old_columns_unless_a_rule_uses_them(client, ctx, session_factory):
    old = b"CUST_ID,NAME_1,STREET_ADDR,CITY_NAME,TAX_NUM\n1,Acme,1 Main St,Denver,X1\n2,Beta,2 Oak Rd,Austin,X2\n"
    new = b"CUST_ID,CUST_NAME,ADDR_STREET,CITY\n1,Acme,1 Main St,Denver\n2,Beta,2 Oak Rd,Austin\n"
    first = register(client, "a.csv", old, table="LAYOUT")
    assert first.status_code == 201, first.text
    tid = first.json()["table_id"]
    assert client.post("/api/admin/seed-sap-catalog").status_code == 200
    client.post(f"/api/profile/{tid}")
    mapping = client.post("/api/mapping-sets", json={"source_table_id": tid}).json()["mapping_set_id"]
    assert client.post(f"/api/mapping/{mapping}/rules", json={
        "target_table": "ADRC", "target_column": "CITY1", "rule_type": "DIRECT_COPY", "source_column": "CITY_NAME"}).status_code == 201

    again = register(client, "b.csv", new, table="LAYOUT")
    assert again.status_code == 201 and again.json()["table_id"] == tid
    with session_factory() as s:
        names = {c.column_name for c in s.query(CatColumn).filter(CatColumn.table_id == tid)}
    assert names == {"CUST_ID", "CUST_NAME", "ADDR_STREET", "CITY", "CITY_NAME"}  # NAME_1, STREET_ADDR, TAX_NUM retired; CITY_NAME kept for its rule
    assert any("CITY_NAME" in w and "remap" in w for w in again.json()["warnings"])


def _draft_with_two_rules(client, table="ARCH"):
    reg = register(client, "a.csv", b"CUST_ID,CUST_NAME,CITY\n1,Acme,Denver\n2,Beta,Austin\n", table=table)
    assert reg.status_code == 201, reg.text
    tid = reg.json()["table_id"]
    assert client.post("/api/admin/seed-sap-catalog").status_code == 200
    assert client.post(f"/api/profile/{tid}").status_code == 200
    mset = client.post("/api/mapping-sets", json={"source_table_id": tid}).json()["mapping_set_id"]
    ids = {}
    for source, tc in (("CUST_NAME", "NAME_ORG1"), ("CITY", "CITY1")):
        tt = "BUT000" if tc == "NAME_ORG1" else "ADRC"
        r = client.post(f"/api/mapping/{mset}/rules", json={"target_table": tt, "target_column": tc, "rule_type": "DIRECT_COPY", "source_column": source})
        assert r.status_code == 201, r.text
        ids[tc] = r.json()["rule_id"]
    return tid, mset, ids


def test_rules_can_be_deleted_only_from_draft_sets_and_the_deletion_is_audited(client, ctx):
    tid, mset, ids = _draft_with_two_rules(client)
    gone = client.delete(f"/api/mapping/rules/{ids['CITY1']}")
    assert gone.status_code == 200 and gone.json() == {"deleted": ids["CITY1"], "mapping_set_id": mset}
    assert [r["target_column"] for r in client.get(f"/api/mapping/{mset}").json()["rules"]] == ["NAME_ORG1"]
    assert client.delete(f"/api/mapping/rules/{ids['CITY1']}").status_code == 404
    log = [json.loads(line) for line in (ctx.workspace / "rule_deletions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["rule_id"] == ids["CITY1"] and log[-1]["target"] == "ADRC.CITY1" and log[-1]["deleted_by"] == "alice"

    # a SUBMITTED / APPROVED set is immutable
    assert client.post(f"/api/governance/submit-mapping/{mset}").json()["status"] == "SUBMITTED"
    frozen = client.delete(f"/api/mapping/rules/{ids['NAME_ORG1']}")
    assert frozen.status_code == 409 and "only DRAFT" in frozen.json()["detail"]
    assert len(client.get(f"/api/mapping/{mset}").json()["rules"]) == 1


def test_archiving_hides_a_mapping_set_without_touching_its_status_and_blocks_runs(client, ctx):
    tid, mset, ids = _draft_with_two_rules(client, table="ARCH2")
    other = client.post("/api/mapping-sets", json={"source_table_id": tid}).json()["mapping_set_id"]
    assert [s["mapping_set_id"] for s in client.get(f"/api/mapping-sets?source_table_id={tid}").json()] == [other, mset]

    assert client.post(f"/api/mapping/{mset}/archive", json={"reason": "x"}).status_code == 422  # a reason is required
    archived = client.post(f"/api/mapping/{mset}/archive", json={"reason": "superseded by the new layout"})
    assert archived.status_code == 200
    body = archived.json()
    assert body["archived"] and body["status"] == "DRAFT" and body["archived_by"] == "alice" and "superseded" in body["archive_reason"]
    assert [s["mapping_set_id"] for s in client.get(f"/api/mapping-sets?source_table_id={tid}").json()] == [other]  # default view hides it
    both = client.get(f"/api/mapping-sets?source_table_id={tid}&include_archived=true").json()
    assert {s["mapping_set_id"]: s["archived"] for s in both} == {other: False, mset: True}
    assert client.get(f"/api/mapping/{mset}").json()["mapping"]["archived"] is True

    blocked = client.post(f"/api/pipeline/run/{mset}", json={})
    assert blocked.status_code == 409 and "archived" in blocked.json()["detail"]

    assert client.post(f"/api/mapping/{mset}/unarchive").json()["archived"] is False
    assert client.post(f"/api/mapping/{mset}/unarchive").status_code == 409  # not archived any more
    assert len(client.get(f"/api/mapping-sets?source_table_id={tid}").json()) == 2

    assert client.post(f"/api/governance/submit-mapping/{mset}").status_code == 200
    waiting = client.post(f"/api/mapping/{mset}/archive", json={"reason": "not while it awaits review"})
    assert waiting.status_code == 409 and "waiting for approval" in waiting.json()["detail"]
    assert client.post("/api/mapping/9999/archive", json={"reason": "whatever"}).status_code == 404


def test_the_studio_only_offers_columns_that_are_in_the_current_file(client, session_factory):
    tid, mset, ids = _draft_with_two_rules(client, table="STALE")
    with session_factory.begin() as s:  # a column left over from an earlier layout, used by no rule / used by a rule
        s.add(CatColumn(table_id=tid, column_name="OLD_UNUSED", data_type="VARCHAR", ordinal_position=90))
        s.add(CatColumn(table_id=tid, column_name="OLD_USED", data_type="VARCHAR", ordinal_position=91))
    client.post(f"/api/mapping/{mset}/rules", json={"target_table": "ADRC", "target_column": "STREET", "rule_type": "DIRECT_COPY", "source_column": "OLD_USED"})
    cols = {c["column_name"]: c["missing_from_file"] for c in client.get(f"/api/mapping/{mset}").json()["source_columns"]}
    assert "OLD_UNUSED" not in cols  # phantom column no longer offered
    assert cols["OLD_USED"] is True and cols["CUST_NAME"] is False and cols["CITY"] is False  # a broken rule stays visible and flagged


def test_the_console_hosts_pages_as_tabs_and_embedded_pages_drop_their_own_chrome(client):
    shell = client.get("/")
    assert shell.status_code == 200
    for element in ('class="menubar"', 'class="iconbar"', 'id="tree"', 'id="tabstrip"', 'id="tabbody"', 'class="statusbar"', 'id="key-indicator"',
                    'id="actor"', 'data-act="open-profiler"', 'data-act="open-governance"'):
        assert element in shell.text, element
    assert "console.js" in shell.text and "style=" not in shell.text  # strict CSP: no inline styles
    # the console is framed by its own origin only; nobody else may frame it
    assert shell.headers["x-frame-options"] == "SAMEORIGIN" and "frame-ancestors 'self'" in shell.headers["content-security-policy"]

    full = client.get("/ui/sources").text
    embedded = client.get("/ui/sources?embed=1").text
    assert 'class="topbar"' in full and "<h1>" in full and 'id="key-modal"' in full
    assert 'class="topbar"' not in embedded and "<h1>" not in embedded and 'id="key-modal"' not in embedded and 'class="embedded"' in embedded
    assert 'id="register-form"' in embedded  # the page itself is intact
    assert 'data-table-id="7"' in client.get("/ui/studio/7?embed=1").text
    start = client.get("/ui/start?embed=1").text
    assert "Welcome" in start and "Support Center" in start and "console.css" in start

    js = client.get("/static/js/console.js").text
    assert "/api/tables" in js and "/api/mapping-sets" in js and "/api/runs" in js
    assert r"^\/ui\/[a-z]+(\/\d+)?$" in js  # the console only ever hosts its own /ui pages
    assert "e.origin !== window.location.origin" in js  # and only trusts messages from its own origin


class _FakeLive:
    def __init__(self, outcome):
        self.outcome = outcome

    def ping(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_targets_report_what_is_ready_and_the_connection_test_never_leaks_credentials(client, ctx, monkeypatch):
    from src.services.sap_client import SapAuthError, SapUnavailableError
    for key in ("SAP_BASE_URL", "SAP_USER", "SAP_PASSWORD", "SAP_CLIENT"):
        monkeypatch.delenv(key, raising=False)
    assert client.post("/api/admin/seed-sap-catalog").status_code == 200
    body = client.get("/api/targets").json()
    kinds = {t["kind"]: t for t in body["targets"]}
    assert body["catalog_ready"] and set(kinds) == {"sandbox", "cockpit", "live"}
    assert kinds["sandbox"]["available"] and kinds["sandbox"]["simulated"] and kinds["cockpit"]["available"] and not kinds["cockpit"]["testable"]
    assert not kinds["live"]["available"] and "SAP_BASE_URL" in kinds["live"]["detail"] and "SAP_PASSWORD" in kinds["live"]["detail"]

    monkeypatch.setenv("SAP_BASE_URL", "https://s4.example.com:44300/sap")
    monkeypatch.setenv("SAP_USER", "MIG_USER")
    monkeypatch.setenv("SAP_PASSWORD", "s3cret-value")
    monkeypatch.setenv("SAP_CLIENT", "100")
    live = {t["kind"]: t for t in client.get("/api/targets").json()["targets"]}["live"]
    assert live["available"] and live["testable"] and "s4.example.com:44300" in live["detail"] and "MIG_USER" in live["detail"] and "client 100" in live["detail"]
    assert "s3cret-value" not in client.get("/api/targets").text  # the password is never reported

    assert client.post("/api/targets/test", json={"target": "sandbox"}).json()["ok"] is True
    ctx._live_sap_client = lambda: _FakeLive("authenticated against https://s4.example.com:44300/sap")
    ok = client.post("/api/targets/test", json={"target": "live"}).json()
    assert ok["ok"] and "authenticated" in ok["detail"] and ok["latency_ms"] >= 0
    ctx._live_sap_client = lambda: _FakeLive(SapAuthError("SAP rejected the credentials (HTTP 401)"))
    bad = client.post("/api/targets/test", json={"target": "live"}).json()
    assert not bad["ok"] and "credentials rejected" in bad["detail"]
    ctx._live_sap_client = lambda: _FakeLive(SapUnavailableError("cannot reach SAP"))
    down = client.post("/api/targets/test", json={"target": "live"}).json()
    assert not down["ok"] and "cannot reach SAP" in down["detail"]
    ctx._live_sap_client = lambda: object()  # a client without a connection test
    assert "does not support" in client.post("/api/targets/test", json={"target": "live"}).json()["detail"]
    assert client.post("/api/targets/test", json={"target": "bogus"}).status_code == 422
    assert client.post("/api/targets/test", json={"target": "sandbox"}, headers={"X-User": ""}).status_code == 401  # identity is required


def test_the_live_client_connection_test_authenticates_and_creates_nothing():
    import httpx
    from src.services.sap_client import HttpSapClient, SapAuthError, SapConfig
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("x-csrf-token")))
        if request.headers.get("authorization", "").endswith("YmFkOmJhZA=="):  # bad:bad
            return httpx.Response(401)
        return httpx.Response(200, headers={"x-csrf-token": "tok-123"})

    config = SapConfig(base_url="https://s4.example.com/sap", user="MIG", password="pw")
    client = HttpSapClient(config, transport=httpx.MockTransport(handler), retry_delay=0)
    assert "authenticated against https://s4.example.com/sap" in client.ping()
    assert seen == [("GET", seen[0][1], "Fetch")] and all(m == "GET" for m, _, _ in seen)  # one read, nothing created
    bad = HttpSapClient(SapConfig(base_url="https://s4.example.com/sap", user="bad", password="bad"), transport=httpx.MockTransport(handler), retry_delay=0)
    with pytest.raises(SapAuthError):
        bad.ping()


def test_the_console_exposes_the_wizards(client):
    html = client.get("/").text
    for element in ('id="wizard"', 'id="wz-steps"', 'id="wz-next"', 'data-act="new-source"', 'data-act="new-target"', "wizard.js"):
        assert element in html, element
    js = client.get("/static/js/wizard.js").text
    for needle in ("Add Source Connection Wizard", "Add Target Connection Wizard", "/api/sources/preview", "/api/sources/register", "/api/targets/test",
                   "Select provider", "Set connection", "Select table", "Actions", "Summary", "/api/sources/import-db", "/api/connections/test", "Databases (read-only)"):
        assert needle in js, needle
    assert "style=" not in html and "localStorage" not in js  # strict CSP, and no secrets or state kept in the browser by the wizard
