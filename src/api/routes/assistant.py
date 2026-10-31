"""The plain-English assistant (see src/api/assistant.py for how its answers are kept honest)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.assistant import Assistant
from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.schemas.assistant import AskRequest, AssistantAnswer

router = APIRouter(tags=["assistant"])


@router.post("/assistant/ask", response_model=AssistantAnswer)
def ask(body: AskRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> AssistantAnswer:
    """Ask a question about a run or a mapping in plain English. A model only chooses read-only tools; the server runs them against the real data; the answer's
    wording is checked: every figure in it must appear in what the tools returned, otherwise the facts are stated plainly instead. Customer records are shown to
    you in tables and are not sent to the model."""
    return Assistant(ctx).ask(body.question, body.run_id, body.mapping_set_id)
