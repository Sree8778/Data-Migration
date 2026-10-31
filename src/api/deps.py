"""Request dependencies: the app context and caller identity.

Identity matters because approvals are SOX controls: the approver is ALWAYS the authenticated
caller, never a name taken from a request body.

* API-key mode  (MIGRATION_API_KEYS set): `X-API-Key` is required and maps to the user.
* dev-header mode (no keys configured): the user is taken from `X-User`. Only for local use
  behind a trusted gateway - `/api/health` reports the mode so it is visible.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from src.api.context import AppContext
from src.api.settings import ROLES

ANONYMOUS = "anonymous"


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


@dataclass(frozen=True)
class Principal:
    """Who is calling, and what they are allowed to be: the roles of their API key (every role in dev-header mode)."""

    user: str
    roles: frozenset
    auth: str  # "api-key" | "dev-header"


def _key_matches(stored: str, presented: str) -> bool:
    if stored.startswith("sha256="):   # the environment holds only the hash of the key
        return hmac.compare_digest(stored[7:].strip().lower(), hashlib.sha256(presented.encode()).hexdigest())
    return hmac.compare_digest(stored.encode(), presented.encode())


def principal(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    x_api_key: Optional[str] = Header(default=None),
    x_user: Optional[str] = Header(default=None),
) -> Principal:
    keys = ctx.settings.api_keys
    if keys:
        if not x_api_key:
            raise HTTPException(401, "X-API-Key header required", headers={"WWW-Authenticate": "ApiKey"})
        found = None
        for key, user in keys.items():
            if _key_matches(key, x_api_key):
                found = Principal(user, frozenset(ctx.settings.key_roles.get(key) or ROLES), "api-key")
                break
        if found is None:
            raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "ApiKey"})
    else:
        found = Principal((x_user or "").strip() or ANONYMOUS, frozenset(ROLES), "dev-header")
    request.state.principal = found   # the audit trail reads it after the call
    return found


def identify(who: Principal = Depends(principal)) -> str:
    return who.user


def require_user(user: str = Depends(identify)) -> str:
    """For state-changing calls: the caller must be identifiable (it ends up in audit fields)."""
    if user == ANONYMOUS:
        raise HTTPException(401, "identify yourself with the X-User header")
    return user


def require_key_admin(user: str = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> str:
    """Adding / removing API keys is restricted to MIGRATION_KEY_ADMINS when that list is configured."""
    admins = ctx.settings.key_admins
    if admins is not None and user.casefold() not in {a.casefold() for a in admins}:
        raise HTTPException(403, f"'{user}' is not allowed to manage API keys")
    return user


def require_connection_admin(user: str = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> str:
    """Creating / deleting / testing database connections (the server connects outbound) can be restricted to MIGRATION_CONNECTION_ADMINS."""
    admins = ctx.settings.connection_admins
    if admins is not None and user.casefold() not in {a.casefold() for a in admins}:
        raise HTTPException(403, f"'{user}' is not allowed to manage database connections")
    return user
