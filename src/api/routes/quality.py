"""Data quality: the rule library, an assessment of a landed table against it, and the history of assessments (the trend is the monitoring)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.db.enums import PiiClassification
from src.db.models import CatColumn, CatTable
from src.schemas.quality import QualityHistoryItem, QualityReport, RuleInfo
from src.services.metadata_service import NotFoundError
from src.services.quality_rules import library

router = APIRouter(prefix="/quality", tags=["quality"])


@router.get("/rules", response_model=list[RuleInfo])
def rule_library() -> list[RuleInfo]:
    """Every rule the assessment can run, with the dimension it measures, its severity, the kind of column it applies to and what to do about a finding."""
    return library()


@router.post("/assess/{table_id}", response_model=QualityReport)
def assess_table(table_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> QualityReport:
    """Run the rule library over the landed copy of a table. Every column gets a role from its name (e-mail, phone, tax number, postal code, country, name, date, amount,
    key), the rules of that role are run in a few passes, and the findings come back worst first with example values (shape-masked for personal data). Each
    assessment is stored: the next one reports the change against it."""
    source = ctx.source_file(table_id)
    with ctx.session_factory() as s:
        table = s.get(CatTable, table_id)
        if table is None:
            raise NotFoundError(f"table {table_id} does not exist")
        columns = list(s.scalars(select(CatColumn).where(CatColumn.table_id == table_id)))
        name = table.table_name
        keys = [c.column_name for c in columns if c.is_primary_key]
        pii = {c.column_name: c.pii_classification for c in columns if c.pii_classification in (PiiClassification.CONFIDENTIAL, PiiClassification.RESTRICTED_PII)}
    if source is None:
        raise HTTPException(409, f"no source file is landed for table {table_id}: register it with a file first")
    return ctx.quality.assess(table_id, name, source, user, keys, pii)


@router.get("/{table_id}", response_model=QualityReport)
def latest_assessment(table_id: int, ctx: AppContext = Depends(get_ctx)) -> QualityReport:
    report = ctx.quality.latest(table_id)
    if report is None:
        raise HTTPException(404, f"table {table_id} has not been assessed yet")
    return report


@router.get("/{table_id}/history", response_model=list[QualityHistoryItem])
def assessment_history(table_id: int, ctx: AppContext = Depends(get_ctx)) -> list[QualityHistoryItem]:
    return ctx.quality.history(table_id)


@router.get("/{table_id}/assessment/{assessment_no}", response_model=QualityReport)
def one_assessment(table_id: int, assessment_no: int, ctx: AppContext = Depends(get_ctx)) -> QualityReport:
    report = ctx.quality.get(table_id, assessment_no)
    if report is None:
        raise HTTPException(404, f"table {table_id} has no assessment {assessment_no}")
    return report
