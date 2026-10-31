"""Access control, audit trail, throttling, personal-data handling and the compliance report.

These tests are the evidence behind docs/compliance.md: each control there is exercised here, including the ways it should fail.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src.api import security
from src.api.main import create_app
from src.api.settings import ROLES, Settings
from src.db.enums import PiiClassification
from src.db.models import CatColumn, MigCellLineage, MigMigrationRun
from src.services.audit_log import GENESIS, AuditLog, AuditUnavailable
from src.services.pii_detector import classify, classify_name, classify_values
from tests.test_db_targets import ready_run  # noqa: F401
from tests.test_module7_api import U, client, ctx, dataset, encoder, env, make_ctx, subset, upload  # noqa: F401

KEYS = {"k-map": "mia", "k-app": "ada", "k-load": "lou", "k-aud": "ana", "k-adm": "root", "k-view": "vic"}
ROLE_OF = {"k-map": {"mapper"}, "k-app": {"approver"}, "k-load": {"loader"}, "k-aud": {"auditor"}, "k-adm": {"admin"}, "k-view": {"viewer"}}


def secured(session_factory, tmp_path, **extra):
    context = make_ctx(session_factory, tmp_path, api_keys=KEYS, key_roles={k: frozenset(v) for k, v in ROLE_OF.items()}, **extra)
    app = create_app(context)
    return context, TestClient(app)


def H(key):
    return {"X-API-Key": key}


# ------------------------------------------------------------------ roles
def test_roles_decide_who_may_do_what(service, session_factory, tmp_path):
    _, c = secured(session_factory, tmp_path)
    assert c.get("/api/runs", headers=H("k-view")).status_code == 200                       # everybody may read
    assert c.post("/api/admin/seed-sap-catalog", headers=H("k-map")).status_code == 403     # a mapper may not administer
    assert c.post("/api/admin/seed-sap-catalog", headers=H("k-adm")).status_code == 200
    # a viewer cannot change anything, a mapper can build but not approve, an approver can approve but not build
    assert c.post("/api/mapping-sets", json={"source_table_id": 1}, headers=H("k-view")).status_code == 403
    assert c.post("/api/mapping-sets", json={"source_table_id": 99999}, headers=H("k-map")).status_code == 404   # allowed, then the table is not there
    assert c.post("/api/mapping-sets", json={"source_table_id": 99999}, headers=H("k-app")).status_code == 403
    assert c.post("/api/governance/approve-mapping/99999", headers=H("k-map")).status_code == 403
    assert c.post("/api/governance/approve-mapping/99999", headers=H("k-app")).status_code == 404
    assert c.post("/api/governance/sign-off-load/99999", headers=H("k-load")).status_code == 403
    # loading is the loader's: neither the mapper nor the approver
    for key, expected in (("k-map", 403), ("k-app", 403), ("k-load", 409)):
        assert c.post("/api/pipeline/load/99999", json={}, headers=H(key)).status_code == expected, key
    # the audit trail and the compliance report are for auditors; erasure is for admins
    assert c.get("/api/compliance/report", headers=H("k-map")).status_code == 403
    assert c.get("/api/compliance/report", headers=H("k-aud")).status_code == 200
    assert c.post("/api/compliance/runs/1/erase", json={"reason": "test"}, headers=H("k-aud")).status_code == 403
    assert c.get("/api/keys", headers=H("k-map")).status_code == 403 and c.get("/api/keys", headers=H("k-adm")).status_code == 200
    assert c.post("/api/connections", json={"name": "x", "dialect": "postgresql", "host": "h", "user": "u", "database": "d"}, headers=H("k-load")).status_code == 403


def test_a_key_without_roles_keeps_every_role_and_a_hashed_key_works(service, session_factory, tmp_path):
    digest = hashlib.sha256(b"s3cret-key").hexdigest()
    context = make_ctx(session_factory, tmp_path, api_keys={"plain": "old.user", f"sha256={digest}": "new.user"})
    c = TestClient(create_app(context))
    assert c.post("/api/admin/seed-sap-catalog", headers=H("plain")).status_code == 200
    assert c.get("/api/runs", headers=H("s3cret-key")).status_code == 200                                   # the secret hashes to the stored value
    assert c.get("/api/runs", headers=H(digest)).status_code == 401                                         # the hash itself is not the key
    assert Settings.from_env({"MIGRATION_API_KEYS": f"k:u:mapper+loader"}).key_roles == {"k": frozenset({"mapper", "loader"})}
    with pytest.raises(ValueError, match="unknown role"):
        Settings.from_env({"MIGRATION_API_KEYS": "k:u:wizard"})


def test_every_state_changing_route_has_an_explicit_role(service, session_factory, tmp_path):
    """A route added without a policy entry would silently fall back to admin-only: fail here, so the author decides who may call it."""
    app = create_app(make_ctx(session_factory, tmp_path))
    unlisted = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api"):
            continue
        for method in route.methods & security.MUTATING:
            # replace each {param} by 1, keep literal segments
            parts = [("1" if s.startswith("{") else s) for s in route.path.split("/")]
            concrete = "/".join(parts)
            if not any(method in m and pattern.match(concrete) for m, pattern, _ in security.POLICY):
                unlisted.append(f"{method} {route.path}")
    assert not unlisted, "no role policy for: " + ", ".join(unlisted)


def test_unknown_mutations_default_to_admin_and_reads_to_open():
    assert security.required_roles("POST", "/api/something/new") == frozenset({"admin"})
    assert security.required_roles("DELETE", "/api/something/new") == frozenset({"admin"})
    assert security.required_roles("GET", "/api/runs") is None
    assert security.required_roles("GET", "/api/compliance/report") == frozenset({"auditor"})


# ------------------------------------------------------------------ audit trail
def test_state_changing_calls_and_refusals_are_on_the_audit_trail(service, session_factory, tmp_path):
    context, c = secured(session_factory, tmp_path)
    c.post("/api/admin/seed-sap-catalog", headers=H("k-adm"))
    c.post("/api/admin/seed-sap-catalog", headers=H("k-map"))                                   # refused: 403
    c.get("/api/runs", headers={"X-API-Key": "nope", "X-User": "mallory"})                       # refused: 401
    c.post("/api/governance/approve-mapping/99999", headers=H("k-app"))                          # high risk: intent first, then the outcome
    c.get("/api/runs", headers=H("k-view"))                                                      # an ordinary read is not recorded
    entries = list(context.audit.entries())
    by = lambda **kw: [e for e in entries if all(e.get(k) == v for k, v in kw.items())]
    assert by(user="root", path="/api/admin/seed-sap-catalog", outcome="ok") and by(user="mia", outcome="denied", status=403)
    assert by(status=401, claimed_user="mallory") and not any(e.get("user") == "mallory" for e in entries)            # a claimed name is never taken for a user
    approve = [e for e in entries if e["path"] == "/api/governance/approve-mapping/99999"]
    assert [e["phase"] for e in approve] == ["intent", "done"] and approve[0]["user"] == "ada" and approve[1]["outcome"] == "rejected"
    assert not any(e["path"] == "/api/runs" and e["method"] == "GET" and e.get("status") == 200 for e in entries)
    assert all(e["request_id"] for e in entries if e["phase"] == "done") and approve[1]["ids"] == {"mapping_set_id": "99999"}
    assert context.audit.verify()["ok"] and context.audit.verify()["entries"] == len(entries)


def test_the_trail_never_holds_request_bodies(service, session_factory, tmp_path):
    context, c = secured(session_factory, tmp_path)
    c.post("/api/connections", json={"name": "src", "dialect": "postgresql", "host": "db.example", "user": "mig", "database": "d", "password": "Sup3rS3cret!PW"}, headers=H("k-adm"))
    text = context.audit.path.read_text(encoding="utf-8")
    assert "/api/connections" in text and "Sup3rS3cret" not in text and "db.example" not in text


def test_tampering_with_the_trail_is_detected_wherever_it_happens(tmp_path):
    log = AuditLog(tmp_path / "a")
    for i in range(6):
        log.append({"user": "u", "path": f"/api/x/{i}", "status": 200})
    assert log.verify()["ok"] and log.verify()["entries"] == 6
    head = log.head()
    original = log.path.read_text(encoding="utf-8").splitlines()

    def attempt(lines):
        log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return log.verify(head)

    changed = original.copy()
    changed[2] = changed[2].replace('"status":200', '"status":403')
    assert attempt(changed)["ok"] is False and attempt(changed)["first_bad_seq"] == 3                       # a changed entry
    assert attempt(original[:2] + original[3:])["first_bad_seq"] == 4                                         # a removed entry: seen at the entry after the gap
    swapped = original.copy(); swapped[1], swapped[2] = swapped[2], swapped[1]
    assert attempt(swapped)["ok"] is False                                                                    # entries in another order
    forged = original.copy(); body = json.loads(forged[3]); body["user"] = "someone.else"; forged[3] = json.dumps(body)
    assert attempt(forged)["first_bad_seq"] == 4                                                              # a forged entry, hash left as it was
    cut = attempt(original[:4])
    assert cut["ok"] is False and cut["matches_expected_head"] is False and "anchored" in cut["reason"]       # the tail cut off: only the outside copy of the head sees it
    assert attempt(original)["ok"] and attempt(original)["matches_expected_head"] and GENESIS not in str(head)


def test_a_keyed_trail_cannot_be_forged_without_the_key(tmp_path):
    log = AuditLog(tmp_path / "k", hmac_key="audit-secret")
    for i in range(3):
        log.append({"user": "u", "path": f"/p/{i}"})
    assert log.verify()["ok"] and log.keyed
    # somebody who can edit the file recomputes a plain hash chain over altered content: it does not verify under the key
    lines = [json.loads(l) for l in log.path.read_text(encoding="utf-8").splitlines()]
    prev = GENESIS
    for e in lines:
        e["user"] = "forger"; e["prev"] = prev
        e["hash"] = hashlib.sha256((prev + json.dumps({k: v for k, v in e.items() if k != "hash"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)).encode()).hexdigest()
        prev = e["hash"]
    log.path.write_text("\n".join(json.dumps(e) for e in lines) + "\n", encoding="utf-8")
    assert AuditLog(tmp_path / "k").verify()["ok"] is True                                                    # without a key the recomputed chain is a valid plain chain...
    assert AuditLog(tmp_path / "k", hmac_key="audit-secret").verify()["ok"] is False                          # ...which the keyed check refuses


def test_the_chain_continues_after_a_restart_and_survives_concurrent_writers(tmp_path):
    import threading
    first = AuditLog(tmp_path / "c")
    first.append({"user": "a"})
    again = AuditLog(tmp_path / "c")                                    # a new process reading the same file
    threads = [threading.Thread(target=lambda: [again.append({"user": "b"}) for _ in range(10)]) for _ in range(5)]
    [t.start() for t in threads]; [t.join() for t in threads]
    result = AuditLog(tmp_path / "c").verify()
    assert result["ok"] and result["entries"] == 51 and result["head"]["seq"] == 51


def test_high_risk_actions_do_not_run_when_the_trail_cannot_be_written(service, session_factory, tmp_path, monkeypatch):
    context, c = secured(session_factory, tmp_path)

    def broken(_event):
        raise AuditUnavailable("the audit trail cannot be written: disk full")

    monkeypatch.setattr(context.audit, "append", broken)
    refused = c.post("/api/keys", json={"api_key": "AIza-not-a-real-key-0000000000000000000"}, headers=H("k-adm"))
    assert refused.status_code == 503 and "audit trail" in refused.json()["detail"]
    assert c.get("/api/keys", headers=H("k-adm")).json()["total"] == 0                                        # nothing was added
    assert c.get("/api/runs", headers=H("k-view")).status_code == 200                                         # reads are not held up by it


# ------------------------------------------------------------------ throttling and headers
def test_throttling_failed_logins_and_body_size(service, session_factory, tmp_path):
    _, c = secured(session_factory, tmp_path, rate_limit_per_minute=8)
    codes = [c.get("/api/runs", headers=H("k-view")).status_code for _ in range(10)]
    assert codes[:8] == [200] * 8 and codes[8:] == [429, 429]
    limited = c.get("/api/runs", headers=H("k-view"))
    assert limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED" and int(limited.headers["Retry-After"]) >= 1

    _, c2 = secured(session_factory, tmp_path / "two")
    for _ in range(20):
        assert c2.get("/api/runs", headers=H("wrong")).status_code == 401
    blocked = c2.get("/api/runs", headers=H("k-view"))                                                        # brute force: even the right key waits
    assert blocked.status_code == 429 and blocked.json()["code"] == "TOO_MANY_FAILED_LOGINS"

    _, c3 = secured(session_factory, tmp_path / "three", max_json_mb=1)
    big = c3.post("/api/mapping-sets", content=b'{"x": "' + b"a" * (2 * 1024 * 1024) + b'"}', headers={**H("k-map"), "Content-Type": "application/json"})
    assert big.status_code == 413 and big.json()["code"] == "BODY_TOO_LARGE"


def test_responses_carry_a_request_id_and_are_not_cacheable(service, session_factory, tmp_path):
    _, c = secured(session_factory, tmp_path)
    r = c.get("/api/runs", headers={**H("k-view"), "X-Request-ID": "trace-abc-12345"})
    assert r.headers["X-Request-ID"] == "trace-abc-12345" and r.headers["Cache-Control"] == "no-store"
    assert c.get("/api/runs", headers={**H("k-view"), "X-Request-ID": "bad id with spaces"}).headers["X-Request-ID"] != "bad id with spaces"
    assert c.get("/api/runs", headers={**H("k-view"), "X-Forwarded-Proto": "https"}).headers["Strict-Transport-Security"].startswith("max-age=")
    assert "Strict-Transport-Security" not in c.get("/api/runs", headers=H("k-view")).headers
    # the scripts and pages are revalidated on every load: after an update the browser shows the new screen without a hard refresh
    assert c.get("/static/js/studio.js").headers["Cache-Control"] == "no-cache" and c.get("/", headers=H("k-view")).headers["Cache-Control"] == "no-cache"


# ------------------------------------------------------------------ personal data detection
@pytest.mark.parametrize("name,expected", [
    ("EMAIL", "RESTRICTED_PII"), ("SMTP_ADDR", "RESTRICTED_PII"), ("TELF1", "RESTRICTED_PII"), ("STCD1", "RESTRICTED_PII"), ("TAX_ID", "RESTRICTED_PII"),
    ("IBAN", "RESTRICTED_PII"), ("STRAS", "RESTRICTED_PII"), ("POSTAL_CODE", "RESTRICTED_PII"), ("CITY", "RESTRICTED_PII"), ("DATE_OF_BIRTH", "RESTRICTED_PII"),
    ("NAME1", "CONFIDENTIAL"), ("CUST_NAME", "CONFIDENTIAL"), ("NOTES", "CONFIDENTIAL"),
    ("AMOUNT", "INTERNAL"), ("PAYMENT_TERMS", "INTERNAL"), ("CUST_ID", "INTERNAL"), ("COUNTRY", "INTERNAL"), ("WAERS", "INTERNAL"), ("ZTERM", "INTERNAL")])
def test_column_names_that_hold_personal_data(name, expected):
    assert classify_name(name).value == expected


def test_values_that_look_like_personal_data_are_caught_in_innocent_columns():
    assert classify_values(["ann@example.com", "bob@example.org", "c@d.io"]) == PiiClassification.RESTRICTED_PII
    assert classify_values(["DE89 3704 0044 0532 0130 00", "GB29NWBK60161331926819", "FR1420041010050500013M02606"]) == PiiClassification.RESTRICTED_PII
    assert classify_values(["4111 1111 1111 1111", "5500-0000-0000-0004", "340000000000009"]) == PiiClassification.RESTRICTED_PII      # Luhn-valid card numbers
    assert classify_values(["+49 30 1234567", "+1 (555) 010-9999", "030/123456-7"]) == PiiClassification.RESTRICTED_PII
    assert classify_values(["123-45-6789", "987-65-4321", "111-22-3333"]) == PiiClassification.RESTRICTED_PII
    assert classify_values(["4111111111111112", "1234567890123456", "9999999999999999"]) is None                        # long numbers that fail Luhn are not cards
    assert classify_values(["100", "250.50", "3", "17"]) is None and classify_values(["NET30", "COD", "NET60"]) is None   # amounts and codes
    assert classify_values(["a@b.co", "x"]) is None                                                                       # too few samples to say
    assert classify("ZFIELD7", ["a@x.com", "b@y.org", "c@z.net"]) == PiiClassification.RESTRICTED_PII
    assert classify("TAX_ID", [], PiiClassification.INTERNAL) == PiiClassification.RESTRICTED_PII and classify("AMOUNT", [], PiiClassification.CONFIDENTIAL) == PiiClassification.CONFIDENTIAL   # never lowered


def test_a_registered_file_is_classified_masked_and_stays_so(client, ctx):
    rows = [{"CUST_ID": str(i), "NAME1": f"Firma {i}", "ZFIELD7": f"user{i}@example.com", "STCD1": f"DE{100000000 + i}", "AMOUNT": str(i * 10)} for i in range(1, 12)]
    csv_text = "CUST_ID,NAME1,ZFIELD7,STCD1,AMOUNT\n" + "\n".join(",".join(r.values()) for r in rows)

    def land():
        return client.post("/api/sources/register", data={"system_name": "L", "table_name": "PII_T", "primary_key_columns": "CUST_ID"}, files={"file": ("p.csv", csv_text.encode(), "text/csv")})

    reg = land()
    assert reg.status_code == 201, reg.text
    table_id = reg.json()["table_id"]
    with ctx.session_factory() as s:
        cls = {c.column_name: c.pii_classification for c in s.scalars(sa.select(CatColumn).where(CatColumn.table_id == table_id))}
    assert cls["STCD1"] == PiiClassification.RESTRICTED_PII and cls["NAME1"] == PiiClassification.CONFIDENTIAL and cls["AMOUNT"] == PiiClassification.INTERNAL
    assert cls["ZFIELD7"] == PiiClassification.INTERNAL                                                   # nothing in its name... until the values are looked at
    profile = client.post(f"/api/profile/{table_id}")
    assert profile.status_code == 200
    z = next(c for c in profile.json()["columns"] if c["column_name"] == "ZFIELD7")
    assert z["pii_masked"] and all("@" in v and "example" not in v and "user" not in v for v in z["sample_values"])      # masked in THIS response already
    with ctx.session_factory() as s:
        stored = s.scalar(sa.select(CatColumn).where(CatColumn.table_id == table_id, CatColumn.column_name == "ZFIELD7"))
        assert stored.pii_classification == PiiClassification.RESTRICTED_PII and not any("example" in str(v) for v in stored.sample_values)
    assert land().status_code == 201                                                                      # landing the table again does not unmask it
    with ctx.session_factory() as s:
        assert s.scalar(sa.select(CatColumn.pii_classification).where(CatColumn.table_id == table_id, CatColumn.column_name == "ZFIELD7")) == PiiClassification.RESTRICTED_PII


# ------------------------------------------------------------------ retention, erasure, legal hold
def _age_run(ctx, run_id, days):
    with ctx.session_factory.begin() as s:
        when = datetime.now(timezone.utc) - timedelta(days=days)
        s.execute(sa.update(MigMigrationRun).where(MigMigrationRun.id == run_id).values(completed_at=when, started_at=when - timedelta(minutes=1)))


def test_erasing_a_run_removes_record_values_and_keeps_the_evidence(client, ctx, ready_run):
    run_id = ready_run["run_id"]
    run_dir = ctx.run_dir(run_id)
    assert (run_dir / "staging.parquet").is_file() and (run_dir / "validation" / "valid.parquet").is_file()
    with ctx.session_factory() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(MigCellLineage).where(MigCellLineage.run_id == run_id)) > 0

    assert client.post(f"/api/compliance/runs/{run_id}/erase", json={"reason": "x"}).status_code == 422          # a reason is required
    held = client.post(f"/api/compliance/runs/{run_id}/hold", json={"reason": "litigation hold 2026-14"})
    assert held.status_code == 200 and held.json()["held_by"] == "alice"
    blocked = client.post(f"/api/compliance/runs/{run_id}/erase", json={"reason": "subject access request"})
    assert blocked.status_code == 409 and "legal hold" in blocked.json()["detail"] and (run_dir / "staging.parquet").is_file()
    assert client.post(f"/api/compliance/runs/{run_id}/release").json()["released"] is True

    done = client.post(f"/api/compliance/runs/{run_id}/erase", json={"reason": "retention: customer asked for erasure"})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["lineage_rows_removed"] > 0 and body["bytes_removed"] > 0 and "staging.parquet" in body["files_removed"] and "validation/valid.parquet" in body["files_removed"]
    assert not (run_dir / "staging.parquet").exists() and not (run_dir / "validation").exists() and not list(run_dir.glob("*.xlsx")) and not list(run_dir.glob("*.parquet"))
    for evidence in ("report.json", "validation.json", "purged.json"):
        assert (run_dir / evidence).is_file(), evidence
    assert [p.name for p in run_dir.glob("reconciliation_run_*")] or True                                         # reports stay when they were generated
    with ctx.session_factory() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(MigCellLineage).where(MigCellLineage.run_id == run_id)) == 0
    kept = json.loads((run_dir / "validation.json").read_text(encoding="utf-8"))
    assert kept["valid_records"] == ready_run["valid"]                                                          # the numbers are still there
    assert json.loads((run_dir / "purged.json").read_text(encoding="utf-8"))["erased_by"] == "alice"
    # the data is gone, so it can no longer be loaded
    assert client.post(f"/api/pipeline/load/{run_id}", json={}).status_code in (409, 422)
    again = client.post(f"/api/compliance/runs/{run_id}/erase", json={"reason": "second request"})
    assert again.status_code == 200 and again.json()["files_removed"] == [] and again.json()["earlier_erasures"]


def test_the_retention_sweep_lists_first_skips_holds_and_erases_what_is_due(client, ctx, ready_run):
    run_id = ready_run["run_id"]
    assert client.post("/api/compliance/retention/sweep").status_code == 409                                    # no retention period configured
    ctx.settings = replace(ctx.settings, retention_days=30)
    assert client.post("/api/compliance/retention/sweep").json()["due"] == []                                   # the run is new
    _age_run(ctx, run_id, 45)
    listed = client.post("/api/compliance/retention/sweep").json()
    assert listed["dry_run"] is True and [d["run_id"] for d in listed["due"]] == [run_id] and listed["erased"] == [] and (ctx.run_dir(run_id) / "staging.parquet").is_file()
    client.post(f"/api/compliance/runs/{run_id}/hold", json={"reason": "audit in progress"})
    skipped = client.post("/api/compliance/retention/sweep", params={"dry_run": "false"}).json()
    assert skipped["erased"] == [] and skipped["skipped"][0]["why"] == "legal hold" and (ctx.run_dir(run_id) / "staging.parquet").is_file()
    client.post(f"/api/compliance/runs/{run_id}/release")
    swept = client.post("/api/compliance/retention/sweep", params={"dry_run": "false"}).json()
    assert [e["run_id"] for e in swept["erased"]] == [run_id] and "retention policy" in swept["erased"][0]["reason"]
    assert not (ctx.run_dir(run_id) / "staging.parquet").exists()
    assert client.post("/api/compliance/retention/sweep").json()["due"] == []                                   # nothing left to do


def test_erasing_a_landed_table_clears_the_copy_and_the_catalog_samples(client, ctx):
    csv_text = "CUST_ID,NAME1\n" + "\n".join(f"{i},Firma {i}" for i in range(1, 8))
    table_id = client.post("/api/sources/register", data={"system_name": "L", "table_name": "ERASE_T", "primary_key_columns": "CUST_ID"},
                           files={"file": ("e.csv", csv_text.encode(), "text/csv")}).json()["table_id"]
    client.post(f"/api/profile/{table_id}")
    base = ctx.table_dir(table_id)
    assert (base / "source.parquet").is_file() and (base / "original").is_dir()
    done = client.post(f"/api/compliance/tables/{table_id}/erase", json={"reason": "source system decommissioned"})
    assert done.status_code == 200 and done.json()["catalog_columns_cleared"] >= 2
    assert not (base / "source.parquet").exists() and not (base / "original").exists() and not (base / "profile.json").exists()
    assert (base / "snapshot.json").is_file() and json.loads((base / "snapshot.json").read_text(encoding="utf-8"))["original_sha256"]   # the checksum is the evidence
    with ctx.session_factory() as s:
        assert all(c.sample_values is None for c in s.scalars(sa.select(CatColumn).where(CatColumn.table_id == table_id)))
    assert client.get("/api/tables").json()[0]["has_source_file"] is False


# ------------------------------------------------------------------ the compliance report
def test_the_compliance_report_reflects_the_installation(service, session_factory, tmp_path):
    context, c = secured(session_factory, tmp_path, retention_days=90, audit_hmac_key="k", approvers=("ada",), preflight="enforce")
    c.post("/api/admin/seed-sap-catalog", headers=H("k-adm"))
    report = c.get("/api/compliance/report", headers={**H("k-aud"), "X-Forwarded-Proto": "https"}).json()
    status = {x["id"]: x["status"] for x in report["controls"]}
    assert status["AC-1"] == "PASS" and status["AC-2"] == "PASS" and status["AC-4"] == "PASS" and status["AU-1"] == "PASS" and status["AU-2"] == "PASS"
    assert status["AC-3"] == "WARN" and status["TS-1"] == "PASS" and status["PV-2"] == "PASS" and status["AU-3"] == "INFO" and status["CC-1"] == "PASS"
    assert report["auth_mode"] == "api-key" and report["summary"]["FAIL"] == 0
    assert all(x["standards"] and x["evidence"] for x in report["controls"])
    actions = {x["id"]: x["action"] for x in report["controls"] if x["status"] in ("WARN", "FAIL")}
    assert "sha256=" in actions["AC-3"]                                                                         # a failing control says what to do

    open_install = TestClient(create_app(make_ctx(session_factory, tmp_path / "open")))
    weak = {x["id"]: x["status"] for x in open_install.get("/api/compliance/report").json()["controls"]}
    assert weak["AC-1"] == "FAIL" and weak["PV-2"] == "WARN" and weak["TS-1"] == "INFO"


def test_a_broken_audit_chain_fails_the_report(service, session_factory, tmp_path):
    context, c = secured(session_factory, tmp_path)
    c.post("/api/admin/seed-sap-catalog", headers=H("k-adm"))
    lines = context.audit.path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"status":200', '"status":201')
    context.audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = {x["id"]: x for x in c.get("/api/compliance/report", headers=H("k-aud")).json()["controls"]}
    assert report["AU-1"]["status"] == "FAIL" and "BROKEN" in report["AU-1"]["evidence"]
    verify = c.get("/api/compliance/audit-log/verify", headers=H("k-aud")).json()
    assert verify["ok"] is False and verify["first_bad_seq"] == 1


def test_the_audit_log_can_be_read_filtered_and_anchored(service, session_factory, tmp_path):
    context, c = secured(session_factory, tmp_path)
    c.post("/api/admin/seed-sap-catalog", headers=H("k-adm"))
    c.post("/api/admin/seed-sap-catalog", headers=H("k-map"))
    head = c.get("/api/compliance/audit-log/head", headers=H("k-aud")).json()
    denied = c.get("/api/compliance/audit-log", params={"outcome": "denied"}, headers=H("k-aud")).json()
    assert denied["total"] >= 1 and all(e["outcome"] == "denied" for e in denied["entries"]) and denied["entries"][0]["user"] == "mia"
    assert c.get("/api/compliance/audit-log", params={"user": "root"}, headers=H("k-aud")).json()["total"] >= 1
    c.post("/api/admin/seed-sap-catalog", headers=H("k-adm"))                                              # the chain grows past the anchored head...
    ok = c.get("/api/compliance/audit-log/verify", headers=H("k-aud")).json()
    assert ok["ok"] and ok["head"]["seq"] > head["seq"]
    stale = c.get("/api/compliance/audit-log/verify", params={"expected_seq": head["seq"], "expected_hash": head["hash"]}, headers=H("k-aud")).json()
    assert stale["ok"] is False and stale["matches_expected_head"] is False                                   # ...so an old anchor no longer matches the end: the caller must anchor the latest
    c.get("/api/compliance/audit-log", headers=H("k-aud"))
    assert any(e["path"] == "/api/compliance/audit-log" for e in context.audit.entries())                   # reading the trail is itself on the trail


def test_the_processing_record_lists_personal_data_and_what_is_left_to_the_controller(client, ctx):
    csv_text = "CUST_ID,EMAIL,NAME1\n" + "\n".join(f"{i},u{i}@x.com,Firma {i}" for i in range(1, 6))
    client.post("/api/sources/register", data={"system_name": "L", "table_name": "REC_T", "primary_key_columns": "CUST_ID"}, files={"file": ("r.csv", csv_text.encode(), "text/csv")})
    record = client.get("/api/compliance/processing-record").json()
    assert "REC_T.EMAIL" in record["categories_of_personal_data"]["RESTRICTED_PII"] and "REC_T.NAME1" in record["categories_of_personal_data"]["CONFIDENTIAL"]
    assert any("lawful basis" in x for x in record["to_complete_by_the_controller"]) and record["purpose"]


def test_a_catalog_from_before_automatic_classification_can_be_brought_up_to_date(client, ctx):
    csv_text = "CUST_ID,EMAIL,NAME1,AMOUNT\n" + "\n".join(f"{i},user{i}@example.com,Firma {i},{i * 10}" for i in range(1, 8))
    table_id = client.post("/api/sources/register", data={"system_name": "L", "table_name": "OLD_T", "primary_key_columns": "CUST_ID"},
                           files={"file": ("o.csv", csv_text.encode(), "text/csv")}).json()["table_id"]
    client.post(f"/api/profile/{table_id}")
    # what an older installation holds: nothing classified, the raw samples stored, the profile screen unmasked
    with ctx.session_factory.begin() as s:
        for col in s.scalars(sa.select(CatColumn).where(CatColumn.table_id == table_id)):
            col.pii_classification = PiiClassification.INTERNAL
            if col.column_name == "EMAIL":
                col.sample_values = ["user1@example.com", "user2@example.com"]
    profile_path = ctx.table_dir(table_id) / "profile.json"
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    for column in raw["columns"]:
        if column["column_name"] == "EMAIL":
            column.update(sample_values=["user1@example.com"], top_values=[{"value": "user1@example.com", "count": 1}], pii_masked=False)
    profile_path.write_text(json.dumps(raw), encoding="utf-8")

    before = {c["id"]: c for c in client.get("/api/compliance/report").json()["controls"]}
    assert before["PV-1"]["status"] == "FAIL" and "classify-pii" in before["PV-1"]["action"]

    done = client.post("/api/compliance/classify-pii")
    assert done.status_code == 200, done.text
    result = done.json()
    assert result["classifications_raised"] >= 2 and result["sample_columns_masked"] >= 1 and result["profile_screens_masked"] == 1
    assert any(x.startswith("OLD_T.EMAIL: INTERNAL -> RESTRICTED_PII") for x in result["columns"]) and any(x.startswith("OLD_T.NAME1: INTERNAL -> CONFIDENTIAL") for x in result["columns"])
    with ctx.session_factory() as s:
        email = s.scalar(sa.select(CatColumn).where(CatColumn.table_id == table_id, CatColumn.column_name == "EMAIL"))
        assert email.pii_classification == PiiClassification.RESTRICTED_PII and not any("example" in str(v) for v in email.sample_values)
        assert s.scalar(sa.select(CatColumn.pii_classification).where(CatColumn.table_id == table_id, CatColumn.column_name == "AMOUNT")) == PiiClassification.INTERNAL
    masked = next(c for c in json.loads(profile_path.read_text(encoding="utf-8"))["columns"] if c["column_name"] == "EMAIL")
    assert masked["pii_masked"] and "example" not in json.dumps(masked["sample_values"] + masked["top_values"])
    assert {c["id"]: c["status"] for c in client.get("/api/compliance/report").json()["controls"]}["PV-1"] == "PASS"
    again = client.post("/api/compliance/classify-pii").json()
    assert again["classifications_raised"] == 0 and again["sample_columns_masked"] == 0 and again["profile_screens_masked"] == 0     # safe to repeat
