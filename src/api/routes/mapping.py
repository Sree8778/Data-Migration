"""AI mapping generation and the human review of mapping rules (the Mapping Studio's back end)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.schemas import (
    GenerateResponse,
    MappingDetail,
    MappingSetSummary,
    RuleCreate,
    RuleDetail,
    RuleUpdate,
)
from src.api.views import mapping_detail, mapping_summary, parse_reasoning, rule_detail
from src.db.enums import MappingStatus, RuleType, TargetType
from src.db.models import CatColumn, CatDataSource, CatTable, MapFieldRule, MapTableRule
from src.services.metadata_service import ConflictError, NotFoundError
from src.services.rule_validator import validate_sql_expression

router = APIRouter(tags=["mapping"])


@router.post("/mapping/generate/{source_table_id}", response_model=GenerateResponse, status_code=201)
def generate_mapping(
    source_table_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)
) -> GenerateResponse:
    """Run retrieval + local LLM + AST guardrails and store the result as a DRAFT mapping set.

    Synchronous: duration is dominated by one LLM call per profiled column.
    """
    report = ctx.mapping_orchestrator().generate_table_mapping_report(source_table_id)
    return GenerateResponse(
        mapping_set_id=report.mapping_set_id, report=report, mapping=mapping_detail(ctx, report.mapping_set_id))


@router.get("/mapping-sets", response_model=list[MappingSetSummary])
def list_mapping_sets(source_table_id: Optional[int] = None, ctx: AppContext = Depends(get_ctx)) -> list[MappingSetSummary]:
    with ctx.session_factory() as s:
        query = select(MapTableRule).order_by(MapTableRule.id.desc())
        if source_table_id is not None:
            query = query.where(MapTableRule.source_table_id == source_table_id)
        return [mapping_summary(s, m) for m in s.scalars(query)]


@router.get("/mapping/{mapping_set_id}", response_model=MappingDetail)
def get_mapping(mapping_set_id: int, ctx: AppContext = Depends(get_ctx)) -> MappingDetail:
    return mapping_detail(ctx, mapping_set_id)


# ------------------------------------------------------------------ rule edits
def _draft_mapping(session: Session, mapping_set_id: int) -> MapTableRule:
    mapping = session.scalar(select(MapTableRule).where(MapTableRule.id == mapping_set_id).with_for_update())
    if mapping is None:
        raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
    if mapping.status != MappingStatus.DRAFT:
        raise HTTPException(409, f"mapping set {mapping_set_id} is {mapping.status.value}; only DRAFT mapping sets can be edited")
    return mapping


def _find_target(session: Session, mapping: MapTableRule, table_name: str, column_name: str) -> CatColumn:
    object_name = session.get(CatTable, mapping.target_table_id).business_object
    column = session.scalar(
        select(CatColumn).join(CatTable, CatColumn.table_id == CatTable.id)
        .join(CatDataSource, CatTable.source_id == CatDataSource.id)
        .where(CatDataSource.target_type == TargetType.TARGET, CatTable.business_object == object_name,
               func.upper(CatTable.table_name) == table_name.strip().upper(),
               func.upper(CatColumn.column_name) == column_name.strip().upper()))
    if column is None:
        raise NotFoundError(f"target {table_name}.{column_name} is not in the target catalog")
    return column


def _source_column(session: Session, mapping: MapTableRule, name: str) -> CatColumn:
    column = session.scalar(select(CatColumn).where(
        CatColumn.table_id == mapping.source_table_id, func.upper(CatColumn.column_name) == name.strip().upper()))
    if column is None:
        raise NotFoundError(f"source column '{name}' is not in the source table")
    return column


def _checked_logic(session: Session, mapping: MapTableRule, rule_type: str, source: Optional[CatColumn],
                   logic: Optional[str]) -> Optional[str]:
    """Enforce the rule-type contract and run the AST guardrails on any SQL."""
    if rule_type in ("DIRECT_COPY", "VALUE_MAPPING"):
        if source is None:
            raise HTTPException(422, f"{rule_type} needs a source_column")
        if logic:
            raise HTTPException(422, f"{rule_type} must not carry transformation_logic")
        return None
    if not logic:
        raise HTTPException(422, f"{rule_type} needs transformation_logic")
    names = [c.column_name for c in session.scalars(select(CatColumn).where(CatColumn.table_id == mapping.source_table_id))]
    normalized, errors = validate_sql_expression(logic, names, allow_columns=rule_type != "STATIC_VALUE")
    if errors:
        raise HTTPException(422, "transformation_logic rejected by the SQL guardrails: " + "; ".join(errors))
    return normalized


def _compose_reasoning(old: Optional[str], user: str, changed: bool, confirm: bool, note: Optional[str]) -> str:
    needs_review, reasons, clean, _ = parse_reasoning(old)
    text = clean or ""
    if changed:
        text = f"{text} [ADJUSTED by {user}]".strip()
    if note:
        text = f"{text} Note ({user}): {note}".strip()
    if confirm:
        return f"{text} [REVIEWED by {user}]".strip()
    if needs_review:
        return f"[NEEDS_REVIEW] {'; '.join(reasons)} | {text}"
    return text


@router.post("/mapping/{mapping_set_id}/rules", response_model=RuleDetail, status_code=201)
def create_rule(
    mapping_set_id: int, body: RuleCreate, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)
) -> RuleDetail:
    """Add a manual rule (for example a STATIC_VALUE default for a mandatory SAP field)."""
    try:
        with ctx.session_factory.begin() as s:
            mapping = _draft_mapping(s, mapping_set_id)
            target = _find_target(s, mapping, body.target_table, body.target_column)
            source = _source_column(s, mapping, body.source_column) if body.source_column else None
            if body.rule_type == "STATIC_VALUE":
                source = None
            logic = _checked_logic(s, mapping, body.rule_type, source, body.transformation_logic)
            if body.lookups and body.rule_type != "VALUE_MAPPING":
                raise HTTPException(422, "lookups are only valid for VALUE_MAPPING rules")
            note = f" {body.note}" if body.note else ""
            rule = MapFieldRule(
                mapping_set_id=mapping_set_id, source_column_id=source.id if source else None,
                target_column_id=target.id, rule_type=RuleType(body.rule_type), transformation_logic=logic,
                ai_suggested=False, ai_reasoning=f"Manual rule created by {user}.{note}",
                is_mandatory_target=bool(target.is_mandatory))
            s.add(rule)
            s.flush()
            rule_id = rule.id
    except IntegrityError as exc:
        raise ConflictError(f"the mapping set already has a rule for {body.target_table}.{body.target_column}") from exc
    if body.lookups:
        ctx.lookups.register_lookups(rule_id, body.lookups)
    with ctx.session_factory() as s:
        return rule_detail(s, s.get(MapFieldRule, rule_id))


@router.put("/mapping/rules/{rule_id}", response_model=RuleDetail)
def update_rule(
    rule_id: int, body: RuleUpdate, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)
) -> RuleDetail:
    """Adjust a rule (type, SQL, source, target, lookups) and/or confirm it as human-reviewed.

    Only DRAFT mapping sets are editable. AI provenance (model, confidence) is never overwritten;
    edits and reviews are recorded in the reasoning trail with the acting user.
    """
    if (body.target_table is None) != (body.target_column is None):
        raise HTTPException(422, "target_table and target_column must be given together")
    try:
        with ctx.session_factory.begin() as s:
            rule = s.scalar(select(MapFieldRule).where(MapFieldRule.id == rule_id).with_for_update())
            if rule is None:
                raise NotFoundError(f"field rule {rule_id} does not exist")
            mapping = _draft_mapping(s, rule.mapping_set_id)

            rule_type = body.rule_type or rule.rule_type.value
            source = s.get(CatColumn, rule.source_column_id) if rule.source_column_id else None
            if body.source_column:
                source = _source_column(s, mapping, body.source_column)
            if rule_type == "STATIC_VALUE":
                source = None
            logic = rule.transformation_logic if body.transformation_logic is None else (body.transformation_logic or None)
            if body.rule_type and body.transformation_logic is None and rule_type in ("DIRECT_COPY", "VALUE_MAPPING"):
                logic = None  # switching to a copy-style rule drops the old expression
            normalized = _checked_logic(s, mapping, rule_type, source, logic)
            if body.lookups and rule_type != "VALUE_MAPPING":
                raise HTTPException(422, "lookups are only valid for VALUE_MAPPING rules")

            changed = (
                rule_type != rule.rule_type.value or normalized != rule.transformation_logic
                or (source.id if source else None) != rule.source_column_id
            )
            if body.target_table:
                target = _find_target(s, mapping, body.target_table, body.target_column)
                if target.id != rule.target_column_id:
                    rule.target_column_id = target.id
                    rule.is_mandatory_target = bool(target.is_mandatory)
                    changed = True
            rule.rule_type = RuleType(rule_type)
            rule.transformation_logic = normalized
            rule.source_column_id = source.id if source else None
            if changed or body.confirm or body.note:
                rule.ai_reasoning = _compose_reasoning(rule.ai_reasoning, user, changed, body.confirm, body.note)
            s.flush()
    except IntegrityError as exc:
        raise ConflictError("another rule in this mapping set already maps that target column") from exc
    if body.lookups:
        ctx.lookups.register_lookups(rule_id, body.lookups)
    with ctx.session_factory() as s:
        return rule_detail(s, s.get(MapFieldRule, rule_id))
