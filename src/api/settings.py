"""API settings, read from the environment (all optional, safe defaults for local use).

MIGRATION_WORKSPACE        directory for uploaded sources and run artefacts (default ./workspace)
MIGRATION_CORS_ORIGINS     comma separated allowed browser origins (default: the local dev origins)
MIGRATION_API_KEYS         "key1:alice,key2:bob" - when set, every request needs X-API-Key and the
                           user identity comes from the key (X-User is then ignored)
MIGRATION_APPROVERS        comma separated users allowed to approve / sign off (default: any user
                           other than the mapping's creator)
MIGRATION_MAX_UPLOAD_MB    upload size limit (default 500)
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

    @property
    def auth_mode(self) -> str:
        return "api-key" if self.api_keys else "dev-header"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        keys: dict[str, str] = {}
        for pair in _split(env.get("MIGRATION_API_KEYS", "")):
            key, _, user = pair.partition(":")
            if key and user:
                keys[key] = user
        approvers = _split(env.get("MIGRATION_APPROVERS", ""))
        admins = _split(env.get("MIGRATION_KEY_ADMINS", ""))
        keys_file = env.get("MIGRATION_GEMINI_KEYS_FILE", "").strip()
        origins = _split(env.get("MIGRATION_CORS_ORIGINS", "")) or list(DEFAULT_CORS)
        return cls(
            workspace_dir=Path(env.get("MIGRATION_WORKSPACE", "workspace")),
            cors_origins=tuple(origins),
            api_keys=keys,
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
        )
