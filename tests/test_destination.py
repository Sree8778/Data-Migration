"""The destination of a mapping set: chosen with the set, its own rules applied in the pre-submission check, and said again in the delivery preview.

Firebase turns a record's key into a document ID (never empty, at most 1,500 bytes, no '/'), and has a free-plan quota. These used to surface only at the last step.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from src.api.mapping_targets import MappingTargets
from src.connectors.base import ConnectionSpec
from src.connectors.firestore import FirestoreConnector
from src.services.destination_checks import key_sql_for, key_statistics
from tests.test_db_targets import ready_run  # noqa: F401  (a signed-off run on a landed table)
from tests.test_module7_api import client, ctx, dataset, encoder, env  # noqa: F401


def connector() -> FirestoreConnector:
    return FirestoreConnector(ConnectionSpec("firestore", "firestore.googleapis.com", 443, "my-project", "sa", "", {}))


def codes(checks) -> dict[str, str]:
    return {c["code"]: c["level"] for c in checks}


# ------------------------------------------------------------------ the facts about the keys
def test_key_statistics_count_the_keys_a_document_id_cannot_hold(tmp_path):
    path = tmp_path / "k.parquet"
    frame = pd.DataFrame({"A": ["1", "2", "2", "  ", "a/b", "x" * 1600, None], "B": ["p", "q", "q", "r", "s", "t", "u"]})
    duckdb.connect().execute(f"COPY (SELECT * FROM frame) TO '{path.as_posix()}' (FORMAT parquet)") if False else None
    con = duckdb.connect()
    con.register("frame", frame)
    con.execute(f"COPY (SELECT * FROM frame) TO '{path.as_posix()}' (FORMAT parquet)")
    one = key_statistics(path, key_sql_for(["A"]))
    assert one == {"rows": 7, "empty": 1, "too_long": 1, "with_slash": 1, "duplicates": 1}          # NULL is not an empty key: that row gets a row-number key in the pipeline
    two = key_statistics(path, key_sql_for(["A", "B"]))
    assert two["rows"] == 7 and two["duplicates"] == 1 and two["empty"] == 0                          # "  " + "r" is not blank as a pair


# ------------------------------------------------------------------ Firebase's rules
def test_firebase_says_what_a_delivery_costs_and_what_it_cannot_take():
    plain = connector().destination_checks(rows=1500, key_stats={"rows": 1500, "empty": 0, "too_long": 0, "with_slash": 0, "duplicates": 0}, exact=True)
    assert codes(plain) == {"DEST_COST": "info"} and "1,500 document(s)" in plain[0]["message"] and "up to" not in plain[0]["message"]
    estimate = connector().destination_checks(rows=1500, exact=False)
    assert "up to 1,500" in estimate[0]["message"]
    big = connector().destination_checks(rows=20_001, exact=True)
    assert codes(big)["DEST_QUOTA"] == "warn" and "Blaze" in next(c for c in big if c["code"] == "DEST_QUOTA")["next_step"]
    assert "DEST_QUOTA" not in codes(connector().destination_checks(rows=20_000))
    bad = connector().destination_checks(rows=10, key_stats={"empty": 2, "too_long": 1, "duplicates": 3, "with_slash": 4})
    assert codes(bad) == {"DEST_COST": "info", "DEST_KEY_EMPTY": "block", "DEST_KEY_TOO_LONG": "block", "DEST_KEY_DUPLICATE": "block", "DEST_KEY_SLASH": "info"}
    assert all(c["next_step"] for c in bad if c["level"] == "block")                                  # a problem always comes with what to do about it
    assert codes(connector().destination_checks(rows=1, table="bad/name"))["DEST_NAME"] == "block"


def test_a_destination_that_cannot_be_reached_is_a_warning_not_a_crash():
    c = FirestoreConnector(ConnectionSpec("firestore", "localhost", 1, "demo", "u", "", {"connect_timeout": 1}))
    out = c.destination_checks(rows=5, table="mig_new", exact=True, check_target=True)
    assert codes(out)["DEST_UNREACHABLE"] == "warn"


# ------------------------------------------------------------------ the store
def test_the_chosen_destination_is_kept_and_forgotten(tmp_path):
    store = MappingTargets(tmp_path)
    assert store.get(7) is None
    store.set(7, "FB", "alice")
    assert store.get(7) == "FB" and store.record(7)["set_by"] == "alice" and MappingTargets(tmp_path).get(7) == "FB"        # survives a restart
    store.set(7, None, "alice")
    assert store.get(7) is None


# ------------------------------------------------------------------ through the API
def _draft_set(client):
    table_id = client.get("/api/tables").json()[0]["table_id"]
    made = client.post("/api/mapping-sets", json={"source_table_id": table_id})
    assert made.status_code == 201, made.text
    return made.json()["mapping_set_id"]


def _connection(client, name, role):
    body = {"name": name, "dialect": "firestore", "host": "localhost", "port": 18080, "database": "demo-erp", "user": "u", "role": role}
    if role == "target":
        body["default_schema"] = "(default)"
    r = client.post("/api/connections", json=body)
    assert r.status_code == 201, r.text


def test_a_draft_mapping_set_can_be_pointed_at_a_target_and_back(client, ctx, ready_run):
    mapping = _draft_set(client)
    assert client.get(f"/api/mapping/{mapping}/target").json()["connection"] is None
    _connection(client, "fb_t", "target")
    _connection(client, "fb_s", "source")
    r = client.put(f"/api/mapping/{mapping}/target", json={"connection": "fb_t"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["connection"] == "fb_t" and "Google Firebase (Firestore)" in got["label"] and got["default_schema"] == "(default)" and got["set_by"] and not got["problem"]
    assert client.get(f"/api/mapping/{mapping}/target").json()["connection"] == "fb_t"
    assert client.put(f"/api/mapping/{mapping}/target", json={"connection": "fb_s"}).status_code == 409           # a source connection is read-only
    assert client.put(f"/api/mapping/{mapping}/target", json={"connection": "nope"}).status_code == 404
    assert client.put("/api/mapping/99999/target", json={"connection": "fb_t"}).status_code == 404
    assert client.put(f"/api/mapping/{ready_run['mapping']}/target", json={"connection": "fb_t"}).status_code == 409   # approved: reviewed with the destination it had
    back = client.put(f"/api/mapping/{mapping}/target", json={"connection": None}).json()
    assert back["connection"] is None and "SAP" in back["label"]


def test_the_pre_submission_check_applies_the_destinations_rules(client, ctx, ready_run, monkeypatch):
    mapping = _draft_set(client)
    assert client.post(f"/api/mapping/{mapping}/preflight").json()["destination"] is None                  # the default: no destination rules
    _connection(client, "fb_t", "target")
    client.put(f"/api/mapping/{mapping}/target", json={"connection": "fb_t"})
    report = client.post(f"/api/mapping/{mapping}/preflight").json()
    dest = report["destination"]
    assert dest["connection"] == "fb_t" and "Google Firebase" in dest["label"] and dest["dialect"] == "firestore"
    assert "DEST_COST" in {c["code"] for c in dest["checks"]} and "up to" in dest["checks"][0]["message"]       # before the run the count is an upper bound

    def refuses(self, **facts):
        return [{"level": "block", "code": "DEST_KEY_EMPTY", "message": "3 record(s) have an empty key.", "next_step": "Choose a key that is always filled."}]
    monkeypatch.setattr(FirestoreConnector, "destination_checks", refuses)
    blocked = client.post(f"/api/mapping/{mapping}/preflight").json()
    finding = next(f for f in blocked["findings"] if f["code"] == "DEST_KEY_EMPTY")
    assert finding["severity"] == "BLOCKER" and finding["message"].startswith("Destination Google Firebase") and finding["next_step"] == "Choose a key that is always filled."
    assert blocked["can_submit"] is False and any("always filled" in step for step in blocked["next_steps"])
    client.delete("/api/connections/fb_t")                                                                  # a destination that was removed afterwards is said, not crashed on
    gone = client.post(f"/api/mapping/{mapping}/preflight").json()["destination"]
    assert gone["checks"][0]["code"] == "DEST_MISSING" and client.get(f"/api/mapping/{mapping}/target").json()["problem"]
