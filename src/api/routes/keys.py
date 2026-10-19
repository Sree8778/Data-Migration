"""Gemini API key stack management. Responses only ever contain masked keys and short hashes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.context import AppContext
from src.api.deps import get_ctx, require_key_admin
from src.api.schemas import KeyAdd, KeyInfo, KeyPoolResponse, KeyTest, KeyTestResult
from src.services.metadata_service import NotFoundError

router = APIRouter(prefix="/keys", tags=["keys"])


def _pool(ctx: AppContext) -> KeyPoolResponse:
    keys = ctx.gemini_keys.get_pool_status()

    def count(status: str) -> int:
        return sum(1 for k in keys if k["status"] == status)

    return KeyPoolResponse(
        provider=ctx.llm_provider, model=ctx.gemini_keys.model, total=len(keys), active=count("ACTIVE"),
        cooling_down=count("RATE_LIMITED"), quota_exhausted=count("QUOTA_EXHAUSTED"), invalid=count("INVALID"),
        persisted=ctx.settings.gemini_keys_file is not None, keys=[KeyInfo(**k) for k in keys])


@router.get("", response_model=KeyPoolResponse)
def list_keys(ctx: AppContext = Depends(get_ctx)) -> KeyPoolResponse:
    """The key stack: masked keys, status (ACTIVE / RATE_LIMITED / QUOTA_EXHAUSTED / INVALID), counters."""
    return _pool(ctx)


@router.post("", response_model=KeyInfo, status_code=201)
def add_key(body: KeyAdd, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_key_admin)) -> KeyInfo:
    """Add a Gemini API key (ACTIVE). Kept in memory unless MIGRATION_GEMINI_KEYS_FILE is configured."""
    return KeyInfo(**ctx.gemini_keys.add_key(body.api_key, body.label))


@router.post("/test", response_model=KeyTestResult)
def test_key(body: KeyTest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_key_admin)) -> KeyTestResult:
    """Check a key with a lightweight models.get call (no generation quota used, pool state unchanged).

    Send `api_key` to test a key before adding it, or `key_hash` to test one already in the stack."""
    if (body.api_key is None) == (body.key_hash is None):
        raise HTTPException(422, "send exactly one of api_key or key_hash")
    if body.key_hash is not None:
        try:
            return KeyTestResult(**ctx.gemini_keys.test_pooled_key(body.key_hash))
        except KeyError:
            raise NotFoundError(f"no key with hash {body.key_hash} in the stack") from None
    return KeyTestResult(**ctx.gemini_keys.test_key(body.api_key))


@router.delete("/{key_hash}")
def delete_key(key_hash: str, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_key_admin)) -> dict:
    if not ctx.gemini_keys.remove_key(key_hash):
        raise NotFoundError(f"no key with hash {key_hash} in the stack")
    return {"removed": key_hash}
