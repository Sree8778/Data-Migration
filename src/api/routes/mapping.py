"""AI mapping generation and the human review of mapping rules (the Mapping Studio's back end)."""

from __future__ import annotations

from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.schemas import (
    ArchiveRequest,
    GenerateResponse,
    JobStatus,
    MappingDetail,
    MappingSetCreateRequest,
    MappingSetSummary,
    RuleCreate,
    RuleDetail,
    RuleUpdate,
)
from src.api.views import mapping_detail, mapping_summary, parse_reasoning, rule_detail, run_preflight
from src.schemas.exclusion import (
    ExclusionOverview, ExclusionRule, ExclusionRuleCount, ExclusionRuleInput, ExclusionSuggestion)
from src.schemas.ai_assist import (
    LookupApplyRequest, LookupApplyResult, LookupDraft, LookupDraftRequest, RuleSuggestion, SuggestRuleRequest)
from src.schemas.preflight import PreflightReport
from src.services.ai_assist import AssistError
from src.services.row_exclusion import ExclusionError, apply_exclusions, preview_rule, suggest, validate_rule
from src.db.enums import MappingStatus, RuleType, TargetType
from src.db.models import CatColumn, CatDataSource, CatTable, MapFieldRule, MapTableRule
from src.schemas.metadata import MappingSetCreate
from src.services.metadata_service import ConflictError, NotFoundError
from src.services.rule_validator import validate_sql_expression

router = APIRouter(tags=["mapping"])


@router.post("/mapping/generate/{source_table_id}", response_model=Union[GenerateResponse, JobStatus], status_code=201)
def generate_mapping(
    source_table_id: int, response: Response, background: bool = False,
    partner_side: str = Query("auto", pattern="^(auto|customer|supplier|both)$", description="which side of the business partner the source is: its customer data, its supplier data, or both. "
                              "auto decides from the table and column names; the other side's SAP tables are then not offered to the model"),
    consistency_runs: Optional[int] = Query(None, ge=1, le=5, description="ask the model this many times per column and flag columns whose answers differ (default: the server setting)"),
    ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user),
):
    """Run retrieval + local LLM + AST guardrails and store the result as a DRAFT mapping set.

    Default: synchronous, returns the mapping (201). With `?background=true` the work is queued as a
    job and returns at once with a job id (202; 200 when an identical job is already running) - poll
    `GET /api/mapping/jobs/{job_id}` for per-column progress and the final result. Use this for
    anything beyond a few columns: a CPU-bound 7B model needs 1-2 minutes per column.
    """
    if not background:
        report = ctx.mapping_orchestrator().generate_table_mapping_report(source_table_id, consistency_runs=consistency_runs, partner_side=partner_side)
        return GenerateResponse(
            mapping_set_id=report.mapping_set_id, report=report, mapping=mapping_detail(ctx, report.mapping_set_id))

    with ctx.session_factory() as s:  # fail fast on an unknown table before queueing
        if s.get(CatTable, source_table_id) is None:
            raise NotFoundError(f"table {source_table_id} does not exist")

    def work(job):
        warm = getattr(ctx.mapping_orchestrator()._llm, "keep_warm", None)
        if warm:
            job.message = "Loading the model"
            warm()
        report = ctx.mapping_orchestrator().generate_table_mapping_report(source_table_id, progress=job.report, consistency_runs=consistency_runs, partner_side=partner_side)
        return GenerateResponse(
            mapping_set_id=report.mapping_set_id, report=report,
            mapping=mapping_detail(ctx, report.mapping_set_id)).model_dump(mode="json")

    job, created = ctx.jobs.submit("ai-mapping", source_table_id, user, work)
    response.status_code = 202 if created else 200
    return JobStatus(**job.snapshot(), already_running=not created)


@router.get("/mapping/jobs", response_model=list[JobStatus])
def list_jobs(source_table_id: Optional[int] = None, ctx: AppContext = Depends(get_ctx)):
    return [JobStatus(**j.snapshot()) for j in ctx.jobs.list(source_table_id)]


@router.get("/mapping/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str, ctx: AppContext = Depends(get_ctx)) -> JobStatus:
    job = ctx.jobs.get(job_id)
    if job is None:
        raise NotFoundError(f"job {job_id} does not exist (jobs are forgotten when the server restarts)")
    return JobStatus(**job.snapshot())


@router.post("/mapping/jobs/{job_id}/cancel", response_model=JobStatus)
def cancel_job(job_id: str, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> JobStatus:
    """Stop after the column currently being evaluated; nothing is saved."""
    job = ctx.jobs.get(job_id)
    if job is None:
        raise NotFoundError(f"job {job_id} does not exist")
    job.request_cancel()
    return JobStatus(**job.snapshot())


@router.post("/mapping-sets", response_model=MappingSetSummary, status_code=201)
def create_mapping_set(
    body: MappingSetCreateRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)
) -> MappingSetSummary:
    """Start an EMPTY draft mapping set (no AI needed); add rules by hand or via placeholders.

    The caller becomes the creator, so a different person has to approve it (segregation of duties)."""
    target = ctx.metadata.get_target_dictionary(body.target_object).tables[0].table
    created = ctx.metadata.create_mapping_set(MappingSetCreate(
        source_table_id=body.source_table_id, target_table_id=target.id, created_by=user))
    with ctx.session_factory() as s:
        return mapping_summary(s, s.get(MapTableRule, created.id))


@router.get("/mapping-sets", response_model=list[MappingSetSummary])
def list_mapping_sets(
    source_table_id: Optional[int] = None, include_archived: bool = False, ctx: AppContext = Depends(get_ctx),
) -> list[MappingSetSummary]:
    """Newest first. Archived sets are hidden unless `include_archived=true`."""
    archived = ctx.archive.entries()
    with ctx.session_factory() as s:
        query = select(MapTableRule).order_by(MapTableRule.id.desc())
        if source_table_id is not None:
            query = query.where(MapTableRule.source_table_id == source_table_id)
        return [mapping_summary(s, m, archived) for m in s.scalars(query) if include_archived or m.id not in archived]


@router.post("/mapping/{mapping_set_id}/archive", response_model=MappingSetSummary)
def archive_mapping_set(
    mapping_set_id: int, body: ArchiveRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user),
) -> MappingSetSummary:
    """Hide an obsolete mapping set (any status except SUBMITTED, which is waiting for a reviewer).

    Nothing is deleted: the set keeps its runs, lineage and approval record and can be restored. The workflow status
    is untouched, because APPROVED is terminal and REJECTED means a named reviewer refused it."""
    with ctx.session_factory() as s:
        mapping = s.get(MapTableRule, mapping_set_id)
        if mapping is None:
            raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
        if mapping.status == MappingStatus.SUBMITTED:
            raise HTTPException(409, f"mapping set {mapping_set_id} is waiting for approval; approve or reject it first")
    ctx.archive.archive(mapping_set_id, user, body.reason.strip())
    with ctx.session_factory() as s:
        return mapping_summary(s, s.get(MapTableRule, mapping_set_id), ctx.archive.entries())


@router.post("/mapping/{mapping_set_id}/unarchive", response_model=MappingSetSummary)
def unarchive_mapping_set(mapping_set_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> MappingSetSummary:
    with ctx.session_factory() as s:
        mapping = s.get(MapTableRule, mapping_set_id)
        if mapping is None:
            raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
        if not ctx.archive.restore(mapping_set_id):
            raise HTTPException(409, f"mapping set {mapping_set_id} is not archived")
        return mapping_summary(s, mapping, ctx.archive.entries())


@router.get("/mapping/{mapping_set_id}/preflight", response_model=Optional[PreflightReport])
def get_preflight(mapping_set_id: int, ctx: AppContext = Depends(get_ctx)) -> Optional[PreflightReport]:
    """The last pre-submission check of this mapping set (run at submission, or by the Studio's 'Check rules on sample'); null when
    none has been run yet - POST to this URL to run one."""
    path = ctx.preflight_file(mapping_set_id)
    return PreflightReport.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None


@router.post("/mapping/{mapping_set_id}/preflight", response_model=PreflightReport)
def post_preflight(
    mapping_set_id: int, sample_rows: int = Query(2000, ge=100, le=20000), block_rate: float = Query(0.5, gt=0, le=1),
    warn_rate: float = Query(0.05, ge=0, le=1), ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user),
) -> PreflightReport:
    """Dry-run the mapping on a reproducible sample of the landed rows through the real transformation and validation (nothing is
    written to the run tables) and check the rules' structure. Every rule gets a measured pass rate; a lookup rule lists the legacy values
    it has no entry for. Works on any status, so an approver can re-check a submitted mapping."""
    return run_preflight(ctx, mapping_set_id, sample_rows, block_rate, warn_rate)


# ------------------------------------------------------------------ the AI assistant: a rule for a missed column, drafted lookups
def _assist(call):
    try:
        return call()
    except AssistError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def _table_of_set(ctx: AppContext, mapping_set_id: int) -> int:
    with ctx.session_factory() as s:
        mapping = s.get(MapTableRule, mapping_set_id)
        if mapping is None:
            raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
        return mapping.source_table_id


@router.post("/mapping/{mapping_set_id}/suggest-rule", response_model=RuleSuggestion)
def suggest_rule(mapping_set_id: int, body: SuggestRuleRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> RuleSuggestion:
    """Ask the AI for a rule for ONE source column (one the first pass missed, say). The model sees a wider candidate list that leaves out targets
    that already have a rule, plus your optional hint; it is asked several times and the most common answer is kept; the answer goes through the same
    guardrails as every AI rule and is measured against the landed file (how many rows already hold valid SAP keys, how many fit). Nothing is saved:
    accept the suggestion to create the rule, which still needs a human review."""
    source = ctx.source_file(_table_of_set(ctx, mapping_set_id))
    return _assist(lambda: ctx.ai_assist.suggest_rule(mapping_set_id, body.column, source, user, hint=body.hint, runs=body.runs))


@router.post("/mapping/{mapping_set_id}/suggestions/{suggestion_id}/accept", response_model=RuleDetail, status_code=201)
def accept_suggestion(mapping_set_id: int, suggestion_id: str, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> RuleDetail:
    rule_id = _assist(lambda: ctx.ai_assist.accept_suggestion(mapping_set_id, suggestion_id, user))
    with ctx.session_factory() as s:
        return rule_detail(s, s.get(MapFieldRule, rule_id))


def _source_for_rule(ctx: AppContext, rule_id: int):
    with ctx.session_factory() as s:
        rule = s.get(MapFieldRule, rule_id)
        if rule is None:
            raise NotFoundError(f"field rule {rule_id} does not exist")
        return ctx.source_file(s.get(MapTableRule, rule.mapping_set_id).source_table_id)


@router.get("/mapping/rules/{rule_id}/lookup-draft", response_model=Optional[LookupDraft])
def get_lookup_draft(rule_id: int, ctx: AppContext = Depends(get_ctx)) -> Optional[LookupDraft]:
    """The last drafted lookup table of this value-mapping rule (null until one has been drafted)."""
    return ctx.ai_assist.get_draft(rule_id)


@router.post("/mapping/rules/{rule_id}/lookup-draft", response_model=LookupDraft)
def draft_lookups(rule_id: int, body: LookupDraftRequest = LookupDraftRequest(), ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> LookupDraft:
    """Draft the lookup table of a value-mapping rule from the most frequent legacy values in the landed file. Values that already are valid SAP keys, or
    that a built-in synonym list knows, are translated without the model; the others are put to the model with the list of allowed keys, and a key that is
    not on that list is discarded. Only possible where the system knows the SAP key list of the target's check table. Nothing is applied: approve entries
    with .../apply."""
    source = _source_for_rule(ctx, rule_id)
    return _assist(lambda: ctx.ai_assist.draft_lookups(rule_id, source, user, runs=body.runs, limit=body.limit))


@router.post("/mapping/rules/{rule_id}/lookup-draft/apply", response_model=LookupApplyResult)
def apply_lookup_draft(rule_id: int, body: LookupApplyRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> LookupApplyResult:
    """Write the entries the steward approved (optionally with the steward's own key instead of the proposal; any key must exist in the SAP key list).
    The approver and the model that drafted them are recorded on the rule."""
    return _assist(lambda: ctx.ai_assist.apply_draft(rule_id, body.accept, user, body.overrides))


# ------------------------------------------------------------------ rows to leave out
def _exclusion_target(ctx: AppContext, mapping_set_id: int, must_edit: bool):
    """(landed source file or None, editable?) for a mapping set; 409 when the rules may not change any more."""
    with ctx.session_factory() as s:
        mapping = s.get(MapTableRule, mapping_set_id)
        if mapping is None:
            raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
        status, table_id = mapping.status, mapping.source_table_id
    editable = status == MappingStatus.DRAFT and not ctx.archive.get(mapping_set_id)
    if must_edit and not editable:
        raise HTTPException(409, f"mapping set {mapping_set_id} is {status.value}"
                                 f"{' and archived' if ctx.archive.get(mapping_set_id) else ''}; only a DRAFT mapping set can change its exclusion rules")
    return ctx.source_file(table_id), editable


def _need_file(source):
    if source is None:
        raise HTTPException(409, "no source file is landed for this table, so a rule cannot be checked against its columns")
    return source


@router.get("/mapping/{mapping_set_id}/exclusions", response_model=ExclusionOverview)
def list_exclusions(mapping_set_id: int, ctx: AppContext = Depends(get_ctx)) -> ExclusionOverview:
    """The rows this mapping set leaves out of the migration, and how many each rule removes from the landed file."""
    source, editable = _exclusion_target(ctx, mapping_set_id, False)
    rules = ctx.exclusions.rules(mapping_set_id)
    summary = None
    if source is not None and rules:
        try:
            summary = apply_exclusions(source, rules)
        except ExclusionError:  # a rule that no longer fits the file (column gone, or it would remove everything): the rules are still listed
            summary = None
    return ExclusionOverview(mapping_set_id=mapping_set_id, editable=editable, rules=rules, summary=summary)


@router.get("/mapping/{mapping_set_id}/exclusions/suggestions", response_model=list[ExclusionSuggestion])
def exclusion_suggestions(mapping_set_id: int, ctx: AppContext = Depends(get_ctx)) -> list[ExclusionSuggestion]:
    """Rules the data points to (a set deletion flag, names that say TEST or DUMMY) that are not already in place. Nothing is applied."""
    source, _ = _exclusion_target(ctx, mapping_set_id, False)
    if source is None:
        return []
    have = {(r.column.lower(), r.operator, tuple(r.values)) for r in ctx.exclusions.rules(mapping_set_id)}
    return [s for s in suggest(source) if (s.column.lower(), s.operator, tuple(s.values)) not in have]


@router.post("/mapping/{mapping_set_id}/exclusions/preview", response_model=ExclusionRuleCount)
def preview_exclusion(mapping_set_id: int, body: ExclusionRuleInput, ctx: AppContext = Depends(get_ctx)) -> ExclusionRuleCount:
    """How many rows a rule would remove from the landed file, and a few examples, without saving it."""
    source, _ = _exclusion_target(ctx, mapping_set_id, False)
    return preview_rule(_need_file(source), body)


@router.post("/mapping/{mapping_set_id}/exclusions", response_model=ExclusionRuleCount, status_code=201)
def add_exclusion(
    mapping_set_id: int, body: ExclusionRuleInput, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user),
) -> ExclusionRuleCount:
    """Leave rows out of the migration. Needs a reason, a column that exists in the landed file, and a DRAFT mapping set; the rule,
    its author and its reason travel with the run and appear on the reconciliation certificate. A rule set that would remove every
    row is refused."""
    source, _ = _exclusion_target(ctx, mapping_set_id, True)
    source = _need_file(source)
    column = validate_rule(source, body)
    tentative = ExclusionRule(rule_id=10**6, column=column, operator=body.operator, values=body.values, reason=body.reason,
                              ignore_case=body.ignore_case, created_by=user, created_at="")
    apply_exclusions(source, [*ctx.exclusions.rules(mapping_set_id), tentative])  # raises when everything would go
    created = ctx.exclusions.add(mapping_set_id, body, column, user)
    counted = preview_rule(source, body)
    return counted.model_copy(update={"rule_id": created.rule_id})


@router.delete("/mapping/{mapping_set_id}/exclusions/{rule_id}")
def delete_exclusion(mapping_set_id: int, rule_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict:
    _exclusion_target(ctx, mapping_set_id, True)
    gone = ctx.exclusions.delete(mapping_set_id, rule_id)
    if gone is None:
        raise NotFoundError(f"mapping set {mapping_set_id} has no exclusion rule {rule_id}")
    ctx.archive.log_rule_deletion(user, {"kind": "exclusion_rule", "mapping_set_id": mapping_set_id, "rule": gone.model_dump()})
    return {"deleted": rule_id, "mapping_set_id": mapping_set_id}


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


@router.delete("/mapping/rules/{rule_id}")
def delete_rule(rule_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict:
    """Remove a rule from a DRAFT mapping set (its lookups go with it). APPROVED / SUBMITTED sets are immutable:
    change an approved mapping by creating a new version. A rule that already produced lineage or defects in a run
    cannot be deleted, because those records must stay traceable; edit it, or archive the whole set instead.
    Every deletion is written to an audit log (who, when, what the rule was)."""
    try:
        with ctx.session_factory.begin() as s:
            rule = s.scalar(select(MapFieldRule).where(MapFieldRule.id == rule_id).with_for_update())
            if rule is None:
                raise NotFoundError(f"field rule {rule_id} does not exist")
            _draft_mapping(s, rule.mapping_set_id)
            detail = rule_detail(s, rule)
            s.delete(rule)
            s.flush()
    except IntegrityError as exc:
        raise ConflictError(f"rule {rule_id} already has run history (lineage or defects) and cannot be deleted; "
                            "archive the mapping set or create a new version instead") from exc
    ctx.archive.log_rule_deletion(user, {
        "rule_id": rule_id, "mapping_set_id": detail.mapping_set_id, "source_column": detail.source_column,
        "target": f"{detail.target_table}.{detail.target_column}", "rule_type": detail.rule_type,
        "transformation_logic": detail.transformation_logic, "lookups": detail.lookup_count})
    return {"deleted": rule_id, "mapping_set_id": detail.mapping_set_id}


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
