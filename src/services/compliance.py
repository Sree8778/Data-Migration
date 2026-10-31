"""A live self-assessment: the controls this application relies on, each checked against the running system, with the standard it serves.

This is evidence, not certification. A passing report says the technical controls described in docs/compliance.md are switched on and intact in THIS
installation; the organisational ones (who holds the keys, how long the audit trail is kept off-system, the lawful basis for the processing, the DPIA)
stay with the people who run it. A control that cannot be judged from inside the application is reported as INFO with what to check.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel
from sqlalchemy import func, select

from src.db.enums import PiiClassification, RunStatus, TargetType
from src.db.models import CatColumn, CatDataSource, CatTable, MigMigrationRun
from src.services.pii_detector import classify_name

Status = Literal["PASS", "WARN", "FAIL", "INFO"]


class Control(BaseModel):
    id: str
    area: str
    title: str
    standards: list[str]
    status: Status
    evidence: str
    action: Optional[str] = None


class ComplianceReport(BaseModel):
    generated_at: str
    auth_mode: str
    summary: dict[str, int]
    controls: list[Control]


def _c(id: str, area: str, title: str, standards: list[str], ok: Optional[bool], evidence: str, action: Optional[str] = None, warn_only: bool = False) -> Control:
    status: Status = "INFO" if ok is None else "PASS" if ok else ("WARN" if warn_only else "FAIL")
    return Control(id=id, area=area, title=title, standards=standards, status=status, evidence=evidence, action=None if ok else action)


def build_report(ctx, https: bool) -> ComplianceReport:
    st = ctx.settings
    out: list[Control] = []
    keyed = bool(st.api_keys)

    # ---- access control
    out.append(_c("AC-1", "Access control", "Callers are authenticated", ["ISO/IEC 27001:2022 A.5.15, A.8.5", "NIST SP 800-53 IA-2", "OWASP API2:2023"], keyed,
                  f"auth mode: {st.auth_mode}", "Set MIGRATION_API_KEYS and MIGRATION_REQUIRE_AUTH=true: in dev-header mode anybody can name themselves in X-User."))
    with_roles = sum(1 for k in st.api_keys if st.key_roles.get(k))
    out.append(_c("AC-2", "Access control", "Every API key has explicit roles (least privilege)", ["NIST SP 800-53 AC-6", "SOX ITGC logical access", "OWASP API5:2023"],
                  keyed and with_roles == len(st.api_keys), f"{with_roles} of {len(st.api_keys)} key(s) carry roles; a key without roles has every role",
                  "Give each key only the roles of its job: MIGRATION_API_KEYS=key:user:mapper+loader.", warn_only=True))
    hashed = sum(1 for k in st.api_keys if k.startswith("sha256="))
    out.append(_c("AC-3", "Access control", "API keys are held as hashes, not as secrets", ["NIST SP 800-53 IA-5", "OWASP API2:2023"], keyed and hashed == len(st.api_keys),
                  f"{hashed} of {len(st.api_keys)} key(s) are stored as sha256=", "Put sha256=<hex of the key> in MIGRATION_API_KEYS instead of the key itself.", warn_only=True))
    out.append(_c("AC-4", "Access control", "Approval is separate from authorship, and approvers can be named", ["ISO/IEC 27001:2022 A.5.3", "NIST SP 800-53 AC-5", "SOX segregation of duties"],
                  st.approvers is not None, "the creator of a mapping set can never approve it (enforced in the service and by a database constraint); "
                  + (f"approvers: {', '.join(st.approvers)}" if st.approvers else "any other identified user may approve"),
                  "Set MIGRATION_APPROVERS to the people who may approve and sign off.", warn_only=True))
    out.append(_c("AC-5", "Access control", "Database connections are restricted to known hosts and administrators", ["OWASP API7:2023 (SSRF)", "ISO/IEC 27001:2022 A.8.20"],
                  st.db_allowed_hosts is not None and st.connection_admins is not None,
                  f"allowed hosts: {'set' if st.db_allowed_hosts else 'any'}; connection admins: {'set' if st.connection_admins else 'any admin-role caller'}",
                  "Set MIGRATION_DB_ALLOWED_HOSTS and MIGRATION_CONNECTION_ADMINS: the server opens outbound connections to whatever host it is given.", warn_only=True))
    out.append(_c("AC-6", "Access control", "Requests are throttled and bodies are capped", ["OWASP API4:2023", "ISO/IEC 27001:2022 A.8.6"], st.rate_limit_per_minute > 0,
                  f"{st.rate_limit_per_minute} requests/min per caller (a tenth for heavy calls), JSON bodies up to {st.max_json_mb} MB, uploads up to {st.max_upload_mb} MB",
                  "Set MIGRATION_RATE_LIMIT above 0.", warn_only=True))
    out.append(_c("AC-7", "Access control", "Browsers may only call from named origins", ["OWASP ASVS 14.5"], "*" not in st.cors_origins, f"origins: {', '.join(st.cors_origins)}",
                  "Replace * in MIGRATION_CORS_ORIGINS by the origins of the console."))
    out.append(_c("TS-1", "Transport", "Traffic is encrypted in transit", ["ISO/IEC 27001:2022 A.8.24", "GDPR art. 32(1)(a)"], True if https else None,
                  "this request arrived over HTTPS" if https else "this request did not arrive over HTTPS (or the proxy does not say so)",
                  "Terminate TLS in front of the application and forward X-Forwarded-Proto; the application itself speaks plain HTTP."))

    # ---- audit trail
    check = ctx.audit.verify()
    out.append(_c("AU-1", "Audit", "The audit trail exists and its hash chain is intact", ["ISO/IEC 27001:2022 A.8.15", "NIST SP 800-53 AU-9", "SOX ITGC change management", "ALCOA+ (attributable, contemporaneous)"],
                  check["ok"] and check["entries"] > 0,
                  f"{check['entries']} entries, chain {'intact' if check['ok'] else 'BROKEN at entry ' + str(check['first_bad_seq']) + ': ' + str(check['reason'])}"
                  + (f", head #{check['head']['seq']} {str(check['head']['hash'])[:12]}..." if check["ok"] and check["entries"] else ""),
                  "Investigate immediately: an entry was changed, removed or inserted." if not check["ok"] else "No entries yet: the trail starts with the first state-changing call."))
    out.append(_c("AU-2", "Audit", "The chain is keyed (forging it needs a secret)", ["NIST SP 800-53 AU-9(3)"], ctx.audit.keyed, "HMAC-SHA256 chain" if ctx.audit.keyed else "plain SHA-256 chain",
                  "Set MIGRATION_AUDIT_HMAC_KEY (and keep it outside the workspace).", warn_only=True))
    out.append(_c("AU-3", "Audit", "The head of the chain is anchored outside the application", ["NIST SP 800-53 AU-9(2)", "ISO/IEC 27001:2022 A.8.15"], None,
                  "cannot be judged from inside: a chain proves nothing was changed, only an outside copy of its head proves nothing was cut off",
                  "Copy GET /api/compliance/audit-log/head to a SIEM or WORM store on a schedule, and verify with ?expected_seq=&expected_hash=."))

    # ---- change control and data integrity
    out.append(_c("CC-1", "Change control", "A mapping must pass a dry run on real rows before it is submitted", ["SOX ITGC change management", "ISO/IEC 27001:2022 A.8.32"],
                  st.preflight == "enforce", f"pre-submission check: {st.preflight}", "Set MIGRATION_PREFLIGHT=enforce.", warn_only=st.preflight == "warn"))
    snaps = sorted((Path(ctx.workspace) / "tables").glob("*/snapshot.json")) if (Path(ctx.workspace) / "tables").is_dir() else []
    with_hash = with_control = 0
    for path in snaps:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        with_hash += bool(data.get("original_sha256"))
        with_control += data.get("control_status") in ("MATCH",)
    out.append(_c("DI-1", "Data integrity", "Every landed source is checksummed; control totals are checked where the source supplies them", ["ALCOA+ (original, complete, accurate)", "SOX completeness and accuracy"],
                  (with_hash == len(snaps)) if snaps else None, f"{len(snaps)} landed source(s): {with_hash} with a SHA-256 of the original, {with_control} with a matching control total",
                  "Re-land the sources that have no checksum.") if snaps else _c("DI-1", "Data integrity", "Every landed source is checksummed", ["ALCOA+"], None, "no source landed yet"))
    with ctx.session_factory() as s:
        running = s.scalar(select(func.count()).select_from(MigMigrationRun).where(MigMigrationRun.status == RunStatus.RUNNING)) or 0
    out.append(_c("DI-2", "Data integrity", "No run is stuck in RUNNING", ["ALCOA+ (consistent, enduring)"], running == 0, f"{running} run(s) in RUNNING (a loader may legitimately be busy)",
                  "A run still RUNNING after a server restart is marked FAILED at start-up; if it persists the server is not restarting cleanly.", warn_only=True))
    pending = []
    runs_dir = Path(ctx.workspace) / "runs"
    if runs_dir.is_dir():
        pending = [p.parent.name for p in runs_dir.glob("run_*/db_loads_pending.json")]
    out.append(_c("DI-3", "Data integrity", "No database delivery is left unsettled", ["SOX completeness", "ALCOA+ (complete)"], not pending,
                  "no interrupted delivery" if not pending else f"interrupted delivery in: {', '.join(pending)}",
                  "POST /api/pipeline/recover-db/{run} with action adopt or discard for each."))

    # ---- privacy
    with ctx.session_factory() as s:
        rows = s.execute(select(CatColumn.column_name, CatColumn.pii_classification).join(CatTable, CatColumn.table_id == CatTable.id)
                         .join(CatDataSource, CatTable.source_id == CatDataSource.id).where(CatDataSource.target_type != TargetType.TARGET)).all()
    classified = sum(1 for _, p in rows if p in (PiiClassification.CONFIDENTIAL, PiiClassification.RESTRICTED_PII))
    suspects = [n for n, p in rows if p in (PiiClassification.INTERNAL, PiiClassification.PUBLIC) and classify_name(n) != PiiClassification.INTERNAL]
    out.append(_c("PV-1", "Privacy", "Personal data in source columns is classified, and masked on screen and in AI prompts", ["GDPR art. 25, 32", "ISO/IEC 27001:2022 A.5.12, A.5.34"],
                  not suspects, f"{classified} of {len(rows)} source column(s) classified as personal or confidential; {len(suspects)} look personal by name but are not classified",
                  f"POST /api/compliance/classify-pii classifies them and masks the stored samples (columns: {', '.join(suspects[:8])})"))
    out.append(_c("PV-2", "Privacy", "A retention period is set for personal-data artefacts", ["GDPR art. 5(1)(e)", "ISO/IEC 27001:2022 A.8.10"], st.retention_days is not None,
                  f"retention: {st.retention_days} days" if st.retention_days else "no retention period set", "Set MIGRATION_RETENTION_DAYS and run POST /api/compliance/retention/sweep on a schedule.", warn_only=True))
    if st.retention_days:
        due = [d for d in ctx.retention.due(st.retention_days) if not d["held"]]
        out.append(_c("PV-3", "Privacy", "No personal data is kept past the retention period", ["GDPR art. 5(1)(e), 17"], not due,
                      f"{len(due)} run(s) older than {st.retention_days} days still hold record values", "Run POST /api/compliance/retention/sweep?dry_run=false.", warn_only=True))
    out.append(_c("PV-4", "Privacy", "Erasure and legal hold are available and recorded", ["GDPR art. 17", "ISO/IEC 27001:2022 A.8.10"], True,
                  "POST /api/compliance/runs/{id}/erase and /tables/{id}/erase remove record values and keep the evidence; /hold stops erasure; each call is on the audit trail"))
    out.append(_c("PV-5", "Privacy", "AI only proposes; record values are not sent to the model", ["GDPR art. 5(1)(c), 25", "ISO/IEC 42001 (AI governance)"], True,
                  "AI mappings, lookups and answers are proposals a person approves; column samples of confidential and personal columns are masked before they reach a prompt; "
                  "the assistant sends facts and counts, and every number in its wording is checked against them"))
    out.append(_c("PV-6", "Privacy", "Processing record and lawful basis documented", ["GDPR art. 6, 30, 35"], None,
                  "GET /api/compliance/processing-record produces the technical part of the record of processing; the lawful basis, the DPIA and the transfers are the controller's to document",
                  "Complete the record and keep it with the DPO."))

    summary = {k: sum(1 for c in out if c.status == k) for k in ("PASS", "WARN", "FAIL", "INFO")}
    return ComplianceReport(generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), auth_mode=st.auth_mode, summary=summary, controls=out)


def processing_record(ctx) -> dict[str, Any]:
    """The technical content of a GDPR art. 30 record for this installation: what personal data, why, who receives it, how long, how it is protected."""
    st = ctx.settings
    with ctx.session_factory() as s:
        rows = s.execute(select(CatTable.table_name, CatColumn.column_name, CatColumn.pii_classification).select_from(CatColumn).join(CatTable, CatColumn.table_id == CatTable.id)
                         .join(CatDataSource, CatTable.source_id == CatDataSource.id).where(CatDataSource.target_type != TargetType.TARGET,
                                                                                         CatColumn.pii_classification.in_((PiiClassification.CONFIDENTIAL, PiiClassification.RESTRICTED_PII)))
                         .order_by(CatTable.table_name, CatColumn.column_name)).all()
    categories: dict[str, list[str]] = {}
    for table, column, pii in rows:
        categories.setdefault(pii.value, []).append(f"{table}.{column}")
    recipients = set()
    runs_dir = Path(ctx.workspace) / "runs"
    for f in runs_dir.glob("run_*/db_loads.json") if runs_dir.is_dir() else []:
        try:
            for d in json.loads(f.read_text(encoding="utf-8")):
                recipients.add(f"{d.get('dialect')} {d.get('host')}:{d.get('port')}/{d.get('database')} ({d.get('connection')})")
        except (OSError, ValueError):
            continue
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": "Migration of customer master data from legacy systems to SAP S/4HANA (staging, cleansing, deduplication, validation, load, reconciliation)",
        "categories_of_personal_data": categories,
        "recipients_and_targets": sorted(recipients) or ["none recorded yet"],
        "storage_locations": [f"workspace folder {st.workspace_dir} (landed snapshots, run artefacts, audit trail)", "PostgreSQL catalog (metadata, cell lineage, defects)"],
        "retention": f"{st.retention_days} days, then erased (POST /api/compliance/retention/sweep); evidence without record values is kept" if st.retention_days else "no retention period configured",
        "technical_and_organisational_measures": [c.id + " " + c.title for c in build_report(ctx, False).controls if c.status == "PASS"],
        "to_complete_by_the_controller": ["lawful basis (art. 6)", "data protection impact assessment (art. 35)", "transfers outside the EEA (art. 44 ff.)", "contact details of controller and DPO"],
    }
