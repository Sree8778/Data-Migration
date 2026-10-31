"""The setup guide (see src/api/guide.py): a chat beside the step the person is doing, and where they are in the whole migration."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api import guide as guide_logic
from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.schemas.guide import GuideProgress, GuideReply, GuideRequest

router = APIRouter(tags=["guide"])


@router.post("/guide/chat", response_model=GuideReply)
def chat(body: GuideRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> GuideReply:
    """What the current step needs, an answer to a question about it, an explanation of what went wrong, and any connection details found in the message, to be put into the
    form. A password in the message is neither used nor kept."""
    return guide_logic.Guide(ctx).reply(body)


@router.get("/guide/progress", response_model=GuideProgress)
def progress(ctx: AppContext = Depends(get_ctx)) -> GuideProgress:
    """The stages of a migration, which are done, and what to do now."""
    return guide_logic.progress(ctx)
