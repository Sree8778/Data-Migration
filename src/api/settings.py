"""API settings, read from the environment (all optional, safe defaults for local use).

MIGRATION_WORKSPACE        directory for uploaded sources and run artefacts (default ./workspace)
MIGRATION_CORS_ORIGINS     comma separated allowed browser origins (default: the local dev origins)
MIGRATION_API_KEYS         "key1:alice,key2:bob" - when set, every request needs X-API-Key and the
                           user identity comes from the key (X-User is then ignored)
MIGRATION_APPROVERS        comma separated users allowed to approve / sign off (default: any user
                           other than the mapping's creator)
MIGRATION_MAX_UPLOAD_MB    upload size limit (default 500)
MIGRATION_AUTO_SEED        seed the SAP BP target catalog at startup (default true)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

DEFAULT_CORS = ("http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:5173")


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
        origins = _split(env.get("MIGRATION_CORS_ORIGINS", "")) or list(DEFAULT_CORS)
        return cls(
            workspace_dir=Path(env.get("MIGRATION_WORKSPACE", "workspace")),
            cors_origins=tuple(origins),
            api_keys=keys,
            approvers=tuple(approvers) if approvers else None,
            max_upload_mb=int(env.get("MIGRATION_MAX_UPLOAD_MB", "500")),
            auto_seed=env.get("MIGRATION_AUTO_SEED", "true").strip().lower() != "false",
        )
