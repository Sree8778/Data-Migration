"""Read-model builders shared by the routes: column health, rule details, mapping detail."""

from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.api.context import AppContext
from src.api.schemas import (
    Band,
    ColumnHealth,
    MappingDetail,
    MappingSetSummary,
    RuleDetail,
    SourceColumnView,
)
from src.db.enums import TargetType
from src.db.models import CatColumn, CatDataSource, CatTable, MapFieldRule, MapTableRule, MapValueLookup
from src.schemas.profiler import ColumnProfile
from src.services.metadata_service import NotFoundError
from src.services.profiler_service import _mask_shape  # same shape mask as the catalog samples

HIGH, MEDIUM = 0.85, 0.60  # confidence / health thresholds shared by badges everywhere
# Identifiers that SAP or the loader generates; every other mandatory field, key fields included, must be supplied.
GENERATED_KEYS = {"PARTNER", "KUNNR", "ADDRNUMBER", "RLTYP"}


def band_for(score: float) -> Band:
    """>85% green, 60-85% amber, <60% red (inputs on a 0..1 scale)."""
    if score > HIGH:
        return "high"
    return "medium" if score >= MEDIUM else "low"


# ------------------------------------------------------------------ column health
_PATTERN_FLAGS = {"email": "EMAIL_PATTERN", "iso_country_2": "COUNTRY_ISO_PATTERN", "postal_code": "POSTAL_PATTERN"}
_PATTERN_HINTS = {
    "email": re.compile(r"MAIL", re.IGNORECASE),
    "iso_country_2": re.compile(r"COUNTRY|CTRY|NATION|LAND", re.IGNORECASE),
    "postal_code": re.compile(r"POST|ZIP|PLZ|PIN", re.IGNORECASE),
}


def column_health(p: ColumnProfile, pii_masked: bool) -> ColumnHealth:
    """Deterministic 0-100 health score: completeness, minus penalties for risks the profiler found."""
    score = 100.0 * (1.0 - p.null_ratio)
    flags: list[str] = []
    if p.null_ratio > 0.2:
        flags.append("HIGH_NULLS")
    if p.leading_zero_risk:
        flags.append("LEADING_ZERO_RISK")
        score -= 10
    if p.distinct_count <= 1 and p.total_rows > 1 and p.null_count < p.total_rows:
        flags.append("CONSTANT")
        score -= 5
    if p.null_count == 0 and p.distinct_ratio >= 0.95:
        flags.append("NEAR_UNIQUE_KEY")
    for key, flag in _PATTERN_FLAGS.items():
        c = p.conformity.get(key)
        # only for columns whose NAME says what they hold (a payment-terms column is not a postal code),
        # and only when a meaningful share conforms: the rest are then likely errors
        if c and c.checked and _PATTERN_HINTS[key].search(p.column_name) and 0.2 <= c.ratio < 1.0:
            flags.append(f"{flag}_VIOLATIONS")
            score -= 20.0 * (1.0 - c.ratio)
    score = round(max(0.0, min(100.0, score)), 1)

    def mask(v: str) -> str:
        return _mask_shape(v) if pii_masked else v

    return ColumnHealth(
        column_name=p.column_name, position=p.position, detected_type=p.detected_type, null_count=p.null_count,
        null_ratio=p.null_ratio, distinct_count=p.distinct_count, distinct_ratio=p.distinct_ratio,
        min_length=p.min_length, max_length=p.max_length,
        top_values=[t.model_copy(update={"value": mask(t.value)}) for t in p.top_values],
        sample_values=[mask(v) for v in p.sample_values], pii_masked=pii_masked,
        health_score=score, health_band=band_for(score / 100.0), flags=flags,
    )


# ------------------------------------------------------------------ rule review markers
_NEEDS_REVIEW = re.compile(r"^\[NEEDS_REVIEW\]\s*(?P<reasons>.*?)\s*\|\s*(?P<rest>.*)$", re.S)
_REVIEWED = re.compile(r"\s*\[REVIEWED by (?P<by>[^\]]+)\]\s*$")


def parse_reasoning(text: Optional[str]) -> tuple[bool, list[str], Optional[str], Optional[str]]:
    """-> (needs_review, review_reasons, clean_reasoning, reviewed_by)."""
    if not text:
        return False, [], None, None
    reviewed_by = None
    m = _REVIEWED.search(text)
    if m:
        reviewed_by, text = m.group("by"), text[: m.start()]
    flagged = _NEEDS_REVIEW.match(text)
    if flagged:
        reasons = [r for r in flagged.group("reasons").split("; ") if r]
        return reviewed_by is None, reasons, flagged.group("rest"), reviewed_by
    return False, [], text, reviewed_by


# ------------------------------------------------------------------ mapping detail
def rule_detail(session: Session, rule: MapFieldRule) -> RuleDetail:
    target = session.get(CatColumn, rule.target_column_id)
    table = session.get(CatTable, target.table_id)
    source = session.get(CatColumn, rule.source_column_id) if rule.source_column_id else None
    needs_review, reasons, clean, reviewed_by = parse_reasoning(rule.ai_reasoning)
    confidence = float(rule.ai_confidence_score) if rule.ai_confidence_score is not None else None
    lookup_count = session.scalar(
        select(func.count()).select_from(MapValueLookup).where(MapValueLookup.field_rule_id == rule.id))
    return RuleDetail(
        rule_id=rule.id, mapping_set_id=rule.mapping_set_id,
        source_column=source.column_name if source else None,
        target_table=table.table_name, target_column=target.column_name, target_data_type=target.data_type,
        target_length=target.char_max_length, target_mandatory=bool(target.is_mandatory or rule.is_mandatory_target),
        rule_type=rule.rule_type.value, transformation_logic=rule.transformation_logic,
        ai_suggested=rule.ai_suggested, ai_model_name=rule.ai_model_name, confidence=confidence,
        confidence_band=band_for(confidence) if confidence is not None else None, reasoning=clean,
        needs_review=needs_review, review_reasons=reasons, reviewed_by=reviewed_by, lookup_count=int(lookup_count or 0),
    )


def mapping_summary(session: Session, mapping: MapTableRule, archive: Optional[dict] = None) -> MappingSetSummary:
    entry = (archive or {}).get(mapping.id) or {}
    target_table = session.get(CatTable, mapping.target_table_id)
    count = session.scalar(select(func.count()).select_from(MapFieldRule).where(MapFieldRule.mapping_set_id == mapping.id))
    return MappingSetSummary(
        mapping_set_id=mapping.id, source_table_id=mapping.source_table_id, target_table=target_table.table_name,
        version=mapping.version, status=mapping.status.value, created_by=mapping.created_by,
        approved_by=mapping.approved_by, approved_at=mapping.approved_at, rule_count=int(count or 0),
        archived=bool(entry), archive_reason=entry.get("reason"), archived_by=entry.get("archived_by"),
    )


def mapping_detail(ctx: AppContext, mapping_set_id: int) -> MappingDetail:
    with ctx.session_factory() as session:
        mapping = session.get(MapTableRule, mapping_set_id)
        if mapping is None:
            raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
        rules = session.scalars(
            select(MapFieldRule).where(MapFieldRule.mapping_set_id == mapping_set_id).order_by(MapFieldRule.id)).all()
        details = [rule_detail(session, r) for r in rules]
        mapped_sources = {d.source_column for d in details if d.source_column}
        mapped_targets = {r.target_column_id for r in rules}
        source_cols = session.scalars(
            select(CatColumn).where(CatColumn.table_id == mapping.source_table_id)
            .order_by(CatColumn.ordinal_position, CatColumn.id)).all()
        in_file = ctx.file_columns(mapping.source_table_id)  # None when no file is landed (offer the catalog as is)
        shown = [c for c in source_cols if in_file is None or c.column_name.lower() in in_file or c.column_name in mapped_sources]
        views = [SourceColumnView(
            missing_from_file=in_file is not None and c.column_name.lower() not in in_file,
            column_name=c.column_name, data_type=c.data_type,
            null_ratio=float(c.null_ratio) if c.null_ratio is not None else None,
            distinct_ratio=float(c.distinct_ratio) if c.distinct_ratio is not None else None,
            sample_values=[str(v) for v in (c.sample_values or [])][:3], mapped=c.column_name in mapped_sources,
        ) for c in shown]

        object_name = session.get(CatTable, mapping.target_table_id).business_object
        unmapped = [
            f"{t}.{c}" for t, c, cid in session.execute(
                select(CatTable.table_name, CatColumn.column_name, CatColumn.id)
                .join(CatColumn, CatColumn.table_id == CatTable.id)
                .join(CatDataSource, CatTable.source_id == CatDataSource.id)
                .where(CatDataSource.target_type == TargetType.TARGET, CatTable.business_object == object_name,
                       CatColumn.is_mandatory.is_(True), CatColumn.column_name.not_in(GENERATED_KEYS))
                .order_by(CatTable.id, CatColumn.ordinal_position))
            if cid not in mapped_targets
        ]
        return MappingDetail(mapping=mapping_summary(session, mapping, ctx.archive.entries()), rules=details, source_columns=views,
                             unmapped_mandatory_targets=unmapped)


# ------------------------------------------------------------------ pre-submission check
def run_preflight(ctx: AppContext, mapping_set_id: int, sample_rows: int = 2000, block_rate: float = 0.5, warn_rate: float = 0.05):
    """Run the pre-submission check on the landed file, keep the result where the approver finds it, and return it."""
    with ctx.session_factory() as session:
        mapping = session.get(MapTableRule, mapping_set_id)
        if mapping is None:
            raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
        source_table_id = mapping.source_table_id
    unmapped = mapping_detail(ctx, mapping_set_id).unmapped_mandatory_targets
    report = ctx.preflight.check(
        mapping_set_id, ctx.source_file(source_table_id), sample_rows=sample_rows, block_rate=block_rate, warn_rate=warn_rate,
        file_columns=ctx.file_columns(source_table_id), unmapped_mandatory=unmapped)
    path = ctx.preflight_file(mapping_set_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=1), encoding="utf-8")
    return report
