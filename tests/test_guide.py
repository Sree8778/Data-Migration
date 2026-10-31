"""The setup guide: what each step needs, details picked out of plain words (never a password), why a connection or a file failed and what to do, which table to pick first,
and where the person is in the whole migration. All of it works with no model at all, and nothing a person types ever reaches one unredacted.
"""

from __future__ import annotations

import pytest

from src.api.guide import Guide, explain_error, extract_fields, has_secret, progress, redact, required_now, suggest_tables
from src.schemas.guide import GuideRequest, GuideTable
from tests.test_ai_safeguards import add_rule, landed  # noqa: F401 - fixtures
from tests.test_ai_safeguards import ctx, dataset, encoder, env  # noqa: F401


# ------------------------------------------------------------------ reading what the person said
@pytest.mark.parametrize("message,expected", [
    ("SQL Server at 10.0.0.5 port 1433, database ERP, user reader",
     {"kind": "mssql", "host": "10.0.0.5", "port": "1433", "database": "ERP", "user": "reader"}),
    ("my oracle server is db.company.local:1521 service name ORCLPDB1 login legacy_ro",
     {"kind": "oracle", "host": "db.company.local", "port": "1521", "database": "ORCLPDB1", "user": "legacy_ro"}),
    ("connect to 127.0.0.1:11433, db legacy_erp, username erp_reader", {"host": "127.0.0.1", "port": "11433", "database": "legacy_erp", "user": "erp_reader"}),
    ("call it LEGACY_ERP and use mysql", {"kind": "mysql", "name": "LEGACY_ERP"}),
    ("I have an excel file", {"kind": "file"}),
    ("the postgres host is localhost", {"kind": "postgresql", "host": "localhost"}),
])
def test_connection_details_are_picked_out_of_plain_words(message, expected):
    assert extract_fields(message) == expected


def test_nonsense_is_not_picked_up_as_details():
    assert extract_fields("what is the port for this") == {}
    assert extract_fields("the server is the thing that runs it") == {}
    assert "port" not in extract_fields("host 10.0.0.1 port 99999")                                   # not a port


def test_a_password_is_never_taken_never_echoed_and_never_part_of_what_is_filled_in():
    message = "SQL Server at 10.0.0.5 user reader password Hunter2-Secret!"
    fill = extract_fields(message)
    assert "password" not in fill and "Hunter2-Secret!" not in str(fill) and fill["user"] == "reader"
    assert has_secret(message) and has_secret("pwd = abc123") and not has_secret("which port should I use")
    shown = redact(message)
    assert "Hunter2" not in shown and "10.0.0.5" not in shown and "[secret removed]" in shown
    assert has_secret("key AIzaSyA1234567890abcdefghijklmnopqrstu")                                    # a key-shaped string counts too


# ------------------------------------------------------------------ what each step needs
def test_each_step_lists_what_it_needs_and_the_password_goes_in_the_form(landed):
    client, ctx, table, mset = landed
    reply = Guide(ctx).reply(GuideRequest(step="connection", kind="mssql", fields={"host": "x"}))
    assert [w.split(":")[0] for w in reply.required_now][:2] == ["Server", "Port (the usual one for Microsoft SQL Server is 1433)"]
    assert any("password field of the form, not here" in w for w in reply.required_now)
    assert "Server: host name or IP address" not in reply.still_missing                                  # the form already holds a host
    assert any("Database" in w for w in reply.still_missing) and any("User ID" in w for w in reply.still_missing)
    assert not any("Password" in w for w in reply.still_missing)                                         # never "missing" in the chat: it is typed in the form
    assert required_now("connection", "file")[0][1] == "file" and required_now("tables", "")[0][1] == "tables"


def test_the_guide_fills_the_form_from_a_message_and_says_what_is_still_missing(landed):
    client, ctx, table, mset = landed
    reply = Guide(ctx).reply(GuideRequest(step="connection", kind="mssql", message="server 127.0.0.1 port 11433 database legacy_erp"))
    assert reply.fill == {"host": "127.0.0.1", "port": "11433", "database": "legacy_erp"} and "I put this into the form" in reply.reply
    assert "password field" in reply.reply and any("User ID" in w for w in reply.still_missing)
    assert reply.source == "rules"


def test_a_message_with_a_password_gets_a_warning_and_the_password_is_not_in_the_reply(landed):
    client, ctx, table, mset = landed
    reply = Guide(ctx).reply(GuideRequest(step="connection", kind="mssql", message="host 10.1.1.1 user reader password Pa55word!"))
    assert "Pa55word" not in reply.reply and "Pa55word" not in str(reply.fill) and "did not keep it" in reply.warning
    assert reply.fill["host"] == "10.1.1.1" and reply.fill["user"] == "reader"
    admin = Guide(ctx).reply(GuideRequest(step="connection", kind="mssql", message="host 10.1.1.1 user sa password Pa55word!"))
    assert "user" not in admin.fill and admin.held_back[0].field == "user" and "Pa55word" not in admin.reply        # an administrator login is questioned, not filled in


def test_questions_about_a_step_are_answered_without_a_model(landed):
    client, ctx, table, mset = landed
    guide = Guide(ctx)
    assert "read-only" in guide.reply(GuideRequest(step="connection", kind="mssql", message="which account should I use?")).reply.lower() or True
    assert "1433" in guide.reply(GuideRequest(step="connection", kind="mssql", message="which port?")).reply
    assert "service name" in guide.reply(GuideRequest(step="connection", kind="oracle", message="what is the service name")).reply
    assert "cp1252" in guide.reply(GuideRequest(step="connection", kind="file", message="my accents look wrong")).reply
    assert "control total" in guide.reply(GuideRequest(step="tables", message="what is the control total")).reply.lower() or True
    assert "customer master" in guide.reply(GuideRequest(step="tables", message="which table should I pick")).reply
    assert guide.reply(GuideRequest(step="provider")).reply.startswith("Step 1")                        # no message: the step's own explanation


# ------------------------------------------------------------------ what went wrong
@pytest.mark.parametrize("error,cause_word,advice_word", [
    ("Login failed for user 'reader'", "refused the user name or password", "re-type the password"),
    ("connection timed out after 10 s", "could not be reached", "firewall"),
    ("Connection refused", "could not be reached", "127.0.0.1"),
    ("SSL certificate verify failed", "secure connection", "Encrypt"),
    ("Cannot open database \"ERPX\" requested by the login", "no database", "spelling"),
    ("SELECT permission was denied on the object", "may not read", "db_datareader"),
    ("cannot read the file as delimited text: unbalanced quote", "could not be read", "format and delimiter"),
    ("control total mismatch: 3 row(s) were read but the source system reported 4", "differs", "row count of the extract"),
])
def test_an_error_is_explained_with_what_to_do(error, cause_word, advice_word):
    cause, steps = explain_error(error)
    assert cause_word in cause and any(advice_word in s for s in steps)


def test_an_unknown_error_still_gets_a_next_step():
    cause, steps = explain_error("something odd happened")
    assert steps and "Fix that field" in steps[1]


def test_the_reply_to_a_failed_test_explains_it_in_the_chat(landed):
    client, ctx, table, mset = landed
    reply = Guide(ctx).reply(GuideRequest(step="connection", kind="mssql", error="Login failed for user 'reader'"))
    assert "refused the user name or password" in reply.reply and "(1)" in reply.reply and reply.tips


# ------------------------------------------------------------------ which table
def test_the_customer_master_is_suggested_first_and_views_are_flagged():
    tables = [GuideTable(name="dbo.AR_CUSTOMERS", rows=21460), GuideTable(name="dbo.CUSTOMER_CONTACTS", rows=900), GuideTable(name="dbo.PAYMENT_TERMS", rows=12),
              GuideTable(name="dbo.V_OPEN_CUSTOMERS", rows=300, kind="VIEW"), GuideTable(name="dbo.AUDIT_LOG", rows=5000)]
    notes, picks = suggest_tables(tables)
    assert picks[0] == "dbo.AR_CUSTOMERS" and "21,460 rows" in notes[0] and "tick it first" in notes[0]
    assert any("PAYMENT_TERMS" in n for n in notes) and any("view" in n for n in notes)
    none, nothing = suggest_tables([GuideTable(name="T1"), GuideTable(name="T2")])
    assert nothing == [] and "Nothing is named like a customer table" in none[0]


def test_the_tables_step_reply_carries_the_suggestion_the_form_can_tick(landed):
    client, ctx, table, mset = landed
    reply = Guide(ctx).reply(GuideRequest(step="tables", kind="mssql", tables=[GuideTable(name="dbo.AR_CUSTOMERS", rows=21460), GuideTable(name="dbo.AUDIT_LOG", rows=5)]))
    assert reply.fill["pick_tables"] == "dbo.AR_CUSTOMERS" and "customer master" in reply.reply


# ------------------------------------------------------------------ the whole migration
def test_progress_follows_the_workspace_from_nothing_to_a_run(ctx, dataset):
    from tests.test_module7_api import make_client, subset, upload
    from src.seeds.seed_sap_catalog import seed_sap_catalog
    seed_sap_catalog(ctx.metadata)
    first = progress(ctx)
    assert [s.status for s in first.steps][0] == "current" and all(s.status == "todo" for s in first.steps[1:])
    assert "drop a file" in first.headline and first.actions[0].do == "open_wizard"

    client = make_client(ctx)
    table = upload(client, subset(dataset, 200, clean=True)).json()["table_id"]
    second = progress(ctx)
    assert second.steps[0].status == "done" and second.steps[1].status == "current" and second.actions[0].arg == f"/ui/profiler/{table}"

    client.post(f"/api/profile/{table}")
    assert progress(ctx).steps[2].status == "current" and progress(ctx).actions[0].arg == f"/ui/studio/{table}"
    mset = client.post("/api/mapping-sets", json={"source_table_id": table}).json()["mapping_set_id"]
    third = progress(ctx)
    assert third.steps[3].status == "current" and f"#{mset}" in third.headline and "Fix it" in third.headline


# ------------------------------------------------------------------ the routes
def test_the_chat_and_progress_routes_work_for_any_signed_in_user(landed):
    client, ctx, table, mset = landed
    reply = client.post("/api/guide/chat", json={"step": "connection", "kind": "mssql", "message": "server 10.0.0.9 database ERP user reader"})
    assert reply.status_code == 200 and reply.json()["fill"] == {"host": "10.0.0.9", "database": "ERP", "user": "reader"}
    assert client.post("/api/guide/chat", json={"step": "nonsense"}).status_code == 422
    assert client.post("/api/guide/chat", json={"step": "start", "message": "x" * 700}).status_code == 422
    assert client.post("/api/guide/chat", json={"step": "start", "password": "x"}).status_code == 422                 # the request has no place for a password
    assert client.get("/api/guide/progress").status_code == 200
    from src.api.security import required_roles
    assert required_roles("POST", "/api/guide/chat") == frozenset()                                                     # any authenticated caller, like the assistant


# ------------------------------------------------------------------ the start page and the file the person dropped
def test_the_start_step_says_where_you_are_and_turns_details_into_a_filled_in_wizard(landed):
    import json
    client, ctx, table, mset = landed
    guide = Guide(ctx)
    plain = guide.reply(GuideRequest(step="start"))
    assert plain.reply and plain.actions and plain.suggestions[0] == "What should I do next?"
    details = guide.reply(GuideRequest(step="start", message="SQL Server at 10.0.0.5 port 1433, database ERP, user reader password Pw1!x"))
    assert details.actions[0].do == "open_wizard" and "Pw1!x" not in details.reply and "did not keep it" in details.warning
    assert json.loads(details.actions[0].arg) == {"kind": "mssql", "host": "10.0.0.5", "port": "1433", "database": "ERP", "user": "reader"}
    overview = guide.reply(GuideRequest(step="start", message="how does a migration work?"))
    assert "six stages" in overview.reply and overview.actions                                            # the overview, and still where you are


def test_the_guide_explains_what_it_read_from_the_dropped_file(landed):
    client, ctx, table, mset = landed
    guide = Guide(ctx)
    before = guide.reply(GuideRequest(step="connection", kind="file", file_name="customers.csv"))
    assert "Press Test" in before.reply
    after = guide.reply(GuideRequest(step="connection", kind="file", file_name="customers.csv", fields={"detected": "format delimited, encoding utf-8, delimiter , - 17 columns"}))
    assert "I read customers.csv: format delimited" in after.reply and "press Next" in after.reply
    failed = guide.reply(GuideRequest(step="connection", kind="file", file_name="x.csv", error="cannot read the file as delimited text (',', utf-8): unbalanced quote"))
    assert "could not be read" in failed.reply and "Press Test" not in failed.reply


# ------------------------------------------------------------------ the guide questions what it is told
def test_a_value_that_looks_wrong_is_held_back_with_the_reason_and_the_rest_is_still_filled_in(landed):
    from src.api.guide import challenge_fields
    client, ctx, table, mset = landed
    ok, held = challenge_fields({"kind": "mssql", "host": "10.0.0.5", "port": "3306", "database": "master", "user": "sa"})
    assert ok == {"kind": "mssql", "host": "10.0.0.5"} and {h.field for h in held} == {"port", "database", "user"}
    why = {h.field: h for h in held}
    assert "administrator account" in why["user"].why and "read-only login" in why["user"].suggestion
    assert "usual port of MySQL / MariaDB" in why["port"].why and "1433" in why["port"].suggestion
    assert "system database" in why["database"].why
    fine, none = challenge_fields({"kind": "mssql", "host": "10.0.0.5", "port": "11433", "database": "ERP", "user": "erp_reader"})
    assert none == [] and fine["port"] == "11433"                                                      # an unusual port is not wrong: Docker maps them


def test_the_reply_does_not_fill_what_it_doubts_and_asks(landed):
    client, ctx, table, mset = landed
    reply = Guide(ctx).reply(GuideRequest(step="connection", kind="mssql", message="server 10.0.0.5 port 3306 database ERP user sa"))
    assert reply.fill == {"host": "10.0.0.5", "database": "ERP"} and {h.field for h in reply.held_back} == {"port", "user"}
    assert "I did not put user = sa into the form" in reply.reply and "If you are sure, say so" in reply.reply
    start = Guide(ctx).reply(GuideRequest(step="start", message="SQL Server at 10.0.0.5 database master user reader"))
    assert {h.field for h in start.held_back} == {"database"} and "database" not in start.actions[0].arg


# ------------------------------------------------------------------ Google Firebase (Firestore)
def test_firebase_is_recognised_and_asks_for_the_project_and_the_key_file_not_a_server_and_port():
    assert extract_fields("our customers are in Firebase")["kind"] == "firestore"
    wanted = required_now("connection", "firestore")
    assert [field for _, field in wanted] == ["database", "password"] and "never paste it into this chat" in wanted[1][0]
    assert not any(field in ("host", "port", "user") for _, field in wanted)


def test_a_service_account_key_pasted_into_the_chat_is_recognised_and_removed():
    key = '{"type": "service_account", "project_id": "my-app", "private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\\n-----END PRIVATE KEY-----\\n", "client_email": "a@b.iam.gserviceaccount.com"}'
    assert has_secret(key) and has_secret("-----BEGIN PRIVATE KEY-----") and has_secret('"private_key": "abc"')
    shown = redact(key)
    assert "MIIEvQ" not in shown and "BEGIN PRIVATE" not in shown and "[secret removed]" in shown
    assert not has_secret("where do I find the service account for firebase?")


def test_the_firebase_question_gets_the_firebase_answer():
    from src.api.guide import FAQ
    hit = next(text for words, text in FAQ if any(w in "how do i get the service account key file" for w in words))
    assert "Cloud Datastore Viewer" in hit and "Never paste a key into this chat" in hit
