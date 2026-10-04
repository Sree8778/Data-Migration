"""Request dependencies: the app context and caller identity.

Identity matters because approvals are SOX controls: the approver is ALWAYS the authenticated
caller, never a name taken from a request body.

* API-key mode  (MIGRATION_API_KEYS set): `X-API-Key` is required and maps to the user.
* dev-header mode (no keys configured): the user is taken from `X-User`. Only for local use
  behind a trusted gateway - `/api/health` reports the mode so it is visible.
"""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from src.api.context import AppContext

ANONYMOUS = "anonymous"


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def identify(
    ctx: AppContext = Depends(get_ctx),
    x_api_key: Optional[str] = Header(default=None),
    x_user: Optional[str] = Header(default=None),
) -> str:
    keys = ctx.settings.api_keys
    if keys:
        if not x_api_key:
            raise HTTPException(401, "X-API-Key header required", headers={"WWW-Authenticate": "ApiKey"})
        for key, user in keys.items():
            if hmac.compare_digest(key.encode(), x_api_key.encode()):
                return user
        raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "ApiKey"})
    return (x_user or "").strip() or ANONYMOUS


def require_user(user: str = Depends(identify)) -> str:
    """For state-changing calls: the caller must be identifiable (it ends up in audit fields)."""
    if user == ANONYMOUS:
        raise HTTPException(401, "identify yourself with the X-User header")
    return user
