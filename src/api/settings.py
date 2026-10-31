"""API settings, read from the environment (all optional, safe defaults for local use).

MIGRATION_WORKSPACE        directory for uploaded sources and run artefacts (default ./workspace)
MIGRATION_CORS_ORIGINS     comma separated allowed browser origins (default: the local dev origins)
MIGRATION_API_KEYS         "key1:alice:mapper+loader,key2:bob:approver" - when set, every request needs X-API-Key and the user identity comes from the
                           key (X-User is then ignored). The third part lists the key's roles (viewer, mapper, approver, loader, auditor, admin); a key
                           without roles keeps every role (the old behaviour: give keys roles on any shared server). A key can be given as sha256=<hex of
                           the key> so that the secret itself is not in the environment.
MIGRATION_RETENTION_DAYS   personal-data artefacts of a run older than this many days are due for erasure (POST /api/compliance/retention/sweep)
MIGRATION_AUDIT_HMAC_KEY   secret that keys the audit-trail hash chain: nobody who can edit the file but lacks the key can forge a valid chain
MIGRATION_NOTIFY_WEBHOOK    URL that is told (JSON POST) when a stewardship task is created, assigned, closed or overdue; set by the administrator only
MIGRATION_MAINTENANCE_HOURS  run the maintenance job (retention sweep that really erases, overdue-task reminders) every this many hours; 0 = never (default)
MIGRATION_RATE_LIMIT       requests per minute per caller (default 1200; 0 = off); heavy calls (runs, uploads, loads) get a tenth of it
MIGRATION_MAX_JSON_MB      largest JSON request body (default 10)
MIGRATION_APPROVERS        comma separated users allowed to approve / sign off (default: any user
                           other than the mapping's creator)
MIGRATION_MAX_UPLOAD_MB    upload size limit (default 500)
MIGRATION_REQUIRE_AUTH      "true" = refuse to start unless MIGRATION_API_KEYS is set. Without keys anybody can name themselves in the X-User header, approvers
                           included, so segregation of duties means nothing: set this on any server that is not a developer's own machine.
MIGRATION_AUTO_SEED        seed the SAP BP target catalog at startup (default true)
MIGRATION_KEY_ADMINS       comma separated users allowed to add / remove / test Gemini API keys (default: any
                           identified user)
MIGRATION_DB_ALLOWED_HOSTS comma separated host names / IPs that database connections may point at (default: any). Set this in
                           production: the server opens outbound connections to whatever host a user enters.
MIGRATION_CONNECTION_ADMINS comma separated users allowed to create / delete / test database connections (default: any identified user)
MIGRATION_MAX_EXTRACT_MB   size limit of one database table extract, compressed (default 2048)
MIGRATION_AI_CONSISTENCY_RUNS  how many times the AI is asked about each column (1-5, default 2): a column whose answers differ
                           is flagged "unstable" for a human. A local model at temperature 0 is asked once whatever this says.
MIGRATION_PREFLIGHT        what the pre-submission check does (default "enforce"): "enforce" refuses to submit a mapping set whose
                           rules fail the check (an empty lookup table, a rule that fails most of a sample of the real rows),
                           "warn" reports the same problems but lets the submission through, "off" skips the check.
MIGRATION_GEMINI_KEYS_FILE optional path; keys added through the UI are saved there (owner-only permissions).
                           Unset = keys live in memory only. Keys from GEMINI_API_KEYS are never written to disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

# Least privilege (SOX segregation of duties, NIST AC-5/AC-6, OWASP API5): what a caller may do depends on the roles of its API key.
#   viewer    reads (runs, defects, reports); anything not listed below
#   mapper    registers sources, profiles, builds/edits mapping sets and exclusions, submits for approval, runs the pipeline
#   approver  approves / rejects mapping sets and signs off a run for load (never the creator of the mapping: enforced separately)
#   loader    loads into SAP / a database, verifies, rolls back, recovers a delivery, and runs the pipeline
#   auditor   reads the audit trail and the compliance report
#   admin     everything, including connections, keys, personal-data erasure and legal hold
ROLES = ("viewer", "mapper", "approver", "loader", "auditor", "admin")

DEFAULT_CORS = ("http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:5173")


def _preflight_mode(value: str) -> str:
    mode = value.strip().lower() or "enforce"
    if mode not in ("enforce", "warn", "off"):
        raise ValueError("MIGRATION_PREFLIGHT must be enforce, warn or off")
    return mode


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    workspace_dir: Path = Path("workspace")
    cors_origins: tuple[str, ...] = DEFAULT_CORS
    api_keys: Mapping[str, str] = field(default_factory=dict, repr=False)
    approvers: Optional[tuple[str, ...]] = None
    max_upload_mb: int = 500
    auto_seed: bool = True
    key_admins: Optional[tuple[str, ...]] = None
    gemini_keys_file: Optional[Path] = None
    db_allowed_hosts: Optional[tuple[str, ...]] = None
    connection_admins: Optional[tuple[str, ...]] = None
    max_extract_mb: int = 2048
    ai_consistency_runs: int = 2
    preflight: str = "enforce"
    require_auth: bool = False
    key_roles: Mapping[str, frozenset] = field(default_factory=dict, repr=False)   # key -> roles; a key not in here has every role
    retention_days: Optional[int] = None
    audit_hmac_key: Optional[str] = field(default=None, repr=False)
    rate_limit_per_minute: int = 1200
    notify_webhook: Optional[str] = None
    maintenance_hours: int = 0
    max_json_mb: int = 10

    @property
    def auth_mode(self) -> str:
        return "api-key" if self.api_keys else "dev-header"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        keys: dict[str, str] = {}
        key_roles: dict[str, frozenset] = {}
        for pair in _split(env.get("MIGRATION_API_KEYS", "")):
            key, _, rest = pair.partition(":")
            user, _, role_text = rest.partition(":")
            if key and user:
                keys[key] = user.strip()
                roles = frozenset(r.strip().lower() for r in role_text.split("+") if r.strip())
                unknown = roles - set(ROLES)
                if unknown:
                    raise ValueError(f"MIGRATION_API_KEYS: unknown role(s) {sorted(unknown)} for user '{user}' (roles: {', '.join(ROLES)})")
                if roles:
                    key_roles[key] = roles
        approvers = _split(env.get("MIGRATION_APPROVERS", ""))
        admins = _split(env.get("MIGRATION_KEY_ADMINS", ""))
        keys_file = env.get("MIGRATION_GEMINI_KEYS_FILE", "").strip()
        origins = _split(env.get("MIGRATION_CORS_ORIGINS", "")) or list(DEFAULT_CORS)
        return cls(
            workspace_dir=Path(env.get("MIGRATION_WORKSPACE", "workspace")),
            cors_origins=tuple(origins),
            api_keys=keys,
            key_roles=key_roles,
            approvers=tuple(approvers) if approvers else None,
            max_upload_mb=int(env.get("MIGRATION_MAX_UPLOAD_MB", "500")),
            auto_seed=env.get("MIGRATION_AUTO_SEED", "true").strip().lower() != "false",
            key_admins=tuple(admins) if admins else None,
            gemini_keys_file=Path(keys_file) if keys_file else None,
            db_allowed_hosts=tuple(_split(env.get("MIGRATION_DB_ALLOWED_HOSTS", ""))) or None,
            connection_admins=tuple(_split(env.get("MIGRATION_CONNECTION_ADMINS", ""))) or None,
            max_extract_mb=int(env.get("MIGRATION_MAX_EXTRACT_MB", "2048")),
            ai_consistency_runs=max(1, min(5, int(env.get("MIGRATION_AI_CONSISTENCY_RUNS", "2")))),
            preflight=_preflight_mode(env.get("MIGRATION_PREFLIGHT", "enforce")),
            retention_days=int(env["MIGRATION_RETENTION_DAYS"]) if env.get("MIGRATION_RETENTION_DAYS", "").strip() else None,
            audit_hmac_key=env.get("MIGRATION_AUDIT_HMAC_KEY", "").strip() or None,
            maintenance_hours=max(0, int(env.get("MIGRATION_MAINTENANCE_HOURS", "0") or 0)),
            notify_webhook=env.get("MIGRATION_NOTIFY_WEBHOOK", "").strip() or None,
            rate_limit_per_minute=max(0, int(env.get("MIGRATION_RATE_LIMIT", "1200"))),
            max_json_mb=max(1, int(env.get("MIGRATION_MAX_JSON_MB", "10"))),
            require_auth=env.get("MIGRATION_REQUIRE_AUTH", "false").strip().lower() in ("1", "true", "yes", "on"),
        )
