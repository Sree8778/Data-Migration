"""Content packs: an approved mapping, its lookups and its exclusion rules as one portable, checksummed file that the next migration (another wave, another legacy
system with similar columns) can import as a DRAFT to review.

Reuse is where migration programmes save the most, and where they lose control: a copied mapping is only as good as its fit to the new source. So an import
never trusts a pack: the checksum must match, every rule goes through the same guardrails as a hand-written one, a rule whose source column the new table does not
have is skipped and reported, imported rules are flagged NEEDS_REVIEW, the result is a DRAFT that still needs the pre-submission check and an independent approver,
and a dry run shows all of it before anything is written. A pack holds rules, lookup tables and exclusion rules; it never holds record values from the source data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.api.routes.mapping import _checked_logic
from src.db.enums import RuleType, TargetType
from src.db.models import CatColumn, CatDataSource, CatTable, MapFieldRule, MapTableRule
from src.schemas.exclusion import ExclusionRuleInput
from src.schemas.metadata import MappingSetCreate
from src.services.metadata_service import ConflictError, NotFoundError
from src.services.row_exclusion import ExclusionError, validate_rule

router = APIRouter(prefix="/packs", tags=["packs"])
FORMAT, VERSION = "migration-content-pack", 1


class PackRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_table: str = Field(min_length=1, max_length=64)
    target_column: str = Field(min_length=1, max_length=64)
    rule_type: Literal["DIRECT_COPY", "VALUE_MAPPING", "SQL_EXPRESSION", "STATIC_VALUE"]
    source_column: Optional[str] = Field(default=None, max_length=128)
    transformation_logic: Optional[str] = Field(default=None, max_length=1000)
    lookups: dict[str, str] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class PackExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str
    operator: Literal["equals", "in", "is_blank", "starts_with", "contains", "regex"]
    values: list[str] = Field(default_factory=list)
    reason: str
    ignore_case: bool = True


class Pack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str
    version: int
    name: str = Field(min_length=1, max_length=128)
    exported_at: str
    exported_by: str
    target_object: str = "BUSINESS_PARTNER"
    source: dict[str, Any] = Field(default_factory=dict)  # what it was built for: table name and columns (names and types only)
    rules: list[PackRule] = Field(max_length=2000)
    exclusions: list[PackExclusion] = Field(default_factory=list, max_length=500)
    provenance: dict[str, Any] = Field(default_factory=dict)
    sha256: str


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pack: Pack
    dry_run: bool = False


class Skipped(BaseModel):
    what: str
    reason: str


class ImportResult(BaseModel):
    dry_run: bool
    pack_name: str
    pack_sha256: str
    mapping_set_id: Optional[int] = None
    rules_imported: int
    rules_skipped: list[Skipped]
    lookup_entries: int
    exclusions_imported: int
    exclusions_skipped: list[Skipped]
    next_steps: list[str]


def _digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ export
@router.get("/export/{mapping_set_id}", response_model=Pack)
def export_pack(mapping_set_id: int, name: Optional[str] = None, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> Pack:
    """The mapping set as a content pack: its rules (with their lookup tables) and its exclusion rules, with a checksum. Record values are not included."""
    with ctx.session_factory() as s:
        mapping = s.get(MapTableRule, mapping_set_id)
        if mapping is None:
            raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
        source_table = s.get(CatTable, mapping.source_table_id)
        object_name = s.get(CatTable, mapping.target_table_id).business_object
        columns = [{"name": c.column_name, "type": c.data_type} for c in s.scalars(select(CatColumn).where(CatColumn.table_id == mapping.source_table_id).order_by(CatColumn.ordinal_position))]
        rules = []
        for rule in s.scalars(select(MapFieldRule).where(MapFieldRule.mapping_set_id == mapping_set_id).order_by(MapFieldRule.id)):
            target = s.get(CatColumn, rule.target_column_id)
            source = s.get(CatColumn, rule.source_column_id) if rule.source_column_id else None
            rules.append({
                "target_table": s.get(CatTable, target.table_id).table_name, "target_column": target.column_name, "rule_type": rule.rule_type.value,
                "source_column": source.column_name if source else None, "transformation_logic": rule.transformation_logic,
                "lookups": ctx.lookups.get_lookups(rule.id) if rule.rule_type == RuleType.VALUE_MAPPING else {},
                "provenance": {"ai_suggested": bool(rule.ai_suggested), "model": rule.ai_model_name,
                               "confidence": float(rule.ai_confidence_score) if rule.ai_confidence_score is not None else None}})
        provenance = {"mapping_set_id": mapping.id, "version": mapping.version, "status": mapping.status.value, "created_by": mapping.created_by, "approved_by": mapping.approved_by}
        source_name = source_table.table_name
    exclusions = [{"column": e.column, "operator": e.operator, "values": e.values, "reason": e.reason, "ignore_case": e.ignore_case} for e in ctx.exclusions.rules(mapping_set_id)]
    body = {"format": FORMAT, "version": VERSION, "name": (name or f"{source_name} mapping v{provenance['version']}")[:128], "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "exported_by": user, "target_object": object_name, "source": {"table_name": source_name, "columns": columns}, "rules": rules, "exclusions": exclusions, "provenance": provenance}
    return Pack(**body, sha256=_digest(body))


# ------------------------------------------------------------------ import
def _lookup_clash(lookups: dict[str, str]) -> Optional[str]:
    """Lookups ignore case and blanks, so two entries that differ only there and point to different keys are ambiguous: refuse the rule, not half of it."""
    seen: dict[str, tuple[str, str]] = {}
    for source, target in lookups.items():
        key = source.strip().upper()
        if key in seen and seen[key][1] != target:
            return f"the lookup is ambiguous: '{seen[key][0]}' -> {seen[key][1]} and '{source}' -> {target} are the same value once case and blanks are ignored"
        seen[key] = (source, target)
    return None


def _plan(ctx: AppContext, pack: Pack, source_table_id: int) -> tuple[list[tuple[PackRule, int, int, Optional[int], Optional[str]]], list[Skipped], list[str]]:
    """(rules that can be imported as (rule, target column id, mandatory flag, source column id, checked logic), skipped, the new table's column names)."""
    ok: list = []
    skipped: list[Skipped] = []
    with ctx.session_factory() as s:
        table = s.get(CatTable, source_table_id)
        if table is None:
            raise NotFoundError(f"table {source_table_id} does not exist")
        source_columns = {c.column_name.lower(): c for c in s.scalars(select(CatColumn).where(CatColumn.table_id == source_table_id))}
        names = [c.column_name for c in source_columns.values()]
        stub = MapTableRule(source_table_id=source_table_id)
        for rule in pack.rules:
            label = f"{rule.target_table}.{rule.target_column}"
            target = s.scalar(select(CatColumn).join(CatTable, CatColumn.table_id == CatTable.id).join(CatDataSource, CatTable.source_id == CatDataSource.id).where(
                CatDataSource.target_type == TargetType.TARGET, CatTable.business_object == pack.target_object,
                func.upper(CatTable.table_name) == rule.target_table.strip().upper(), func.upper(CatColumn.column_name) == rule.target_column.strip().upper()))
            if target is None:
                skipped.append(Skipped(what=label, reason=f"the target {label} is not in this installation's catalog for {pack.target_object}"))
                continue
            source = None
            if rule.rule_type != "STATIC_VALUE" and rule.source_column:
                source = source_columns.get(rule.source_column.strip().lower())
                if source is None:
                    skipped.append(Skipped(what=label, reason=f"the source column {rule.source_column} is not in table {table.table_name}"))
                    continue
            try:
                logic = _checked_logic(s, stub, rule.rule_type, source, rule.transformation_logic)
            except HTTPException as exc:
                skipped.append(Skipped(what=label, reason=str(exc.detail)))
                continue
            clash = _lookup_clash(rule.lookups)
            if clash:
                skipped.append(Skipped(what=label, reason=clash))
                continue
            ok.append((rule, target.id, int(bool(target.is_mandatory)), source.id if source else None, logic))
    return ok, skipped, names


@router.post("/import/{source_table_id}", response_model=ImportResult, status_code=201)
def import_pack(source_table_id: int, body: ImportRequest, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> ImportResult:
    """Import a pack as a new DRAFT mapping set on a table. The checksum must match; every rule passes the guardrails; a rule that does not fit is skipped and listed;
    imported rules are flagged NEEDS_REVIEW; `dry_run` reports all of this without writing anything. The caller becomes the creator (an independent person approves)."""
    pack = body.pack
    if pack.format != FORMAT or pack.version != VERSION:
        raise HTTPException(422, f"this is not a version {VERSION} content pack (format '{pack.format}', version {pack.version})")
    claimed = pack.model_dump(exclude={"sha256"})
    if _digest(claimed) != pack.sha256:
        raise HTTPException(422, "the pack's checksum does not match its content: it was changed after it was exported; export it again from the source system")
    try:
        target_table = ctx.metadata.get_target_dictionary(pack.target_object).tables[0].table
    except NotFoundError as exc:
        raise HTTPException(422, f"this installation has no target catalog for {pack.target_object}") from exc

    ok, skipped, names = _plan(ctx, pack, source_table_id)
    lookup_entries = sum(len(r.lookups) for r, *_ in ok)
    mapping_set_id: Optional[int] = None
    valid_exclusions: list[PackExclusion] = []
    skipped_exclusions: list[Skipped] = []
    source = ctx.source_file(source_table_id)
    for ex in pack.exclusions:
        label = f"{ex.column} {ex.operator} {ex.values}"
        if source is None:
            skipped_exclusions.append(Skipped(what=label, reason="no source file is landed for this table, so the rule cannot be checked"))
            continue
        try:
            validate_rule(source, ExclusionRuleInput(column=ex.column, operator=ex.operator, values=ex.values, reason=ex.reason, ignore_case=ex.ignore_case))
            valid_exclusions.append(ex)
        except (ExclusionError, ValueError) as exc:
            skipped_exclusions.append(Skipped(what=label, reason=str(exc)))

    if not body.dry_run:
        if not ok:
            raise HTTPException(422, "none of the pack's rules fits this table, so no mapping set was created: " + "; ".join(f"{x.what}: {x.reason}" for x in skipped[:5]))
        created = ctx.metadata.create_mapping_set(MappingSetCreate(source_table_id=source_table_id, target_table_id=target_table.id, created_by=user))
        mapping_set_id = created.id
        note = f"[NEEDS_REVIEW] imported from a content pack; confirm it fits this source | Imported from pack '{pack.name}' ({pack.sha256[:12]}) by {user}."
        with ctx.session_factory.begin() as s:
            for rule, target_id, mandatory, source_id, logic in ok:
                s.add(MapFieldRule(mapping_set_id=mapping_set_id, source_column_id=source_id, target_column_id=target_id, rule_type=RuleType(rule.rule_type), transformation_logic=logic,
                                   ai_suggested=False, ai_reasoning=note, is_mandatory_target=bool(mandatory)))
            try:
                s.flush()
            except IntegrityError as exc:
                raise ConflictError("the pack maps one target field twice") from exc
            ids = {(s.get(CatTable, s.get(CatColumn, r.target_column_id).table_id).table_name.upper(), s.get(CatColumn, r.target_column_id).column_name.upper()): r.id
                   for r in s.scalars(select(MapFieldRule).where(MapFieldRule.mapping_set_id == mapping_set_id))}
        for rule, *_ in ok:
            if rule.lookups:
                ctx.lookups.register_lookups(ids[(rule.target_table.strip().upper(), rule.target_column.strip().upper())], rule.lookups)
        for ex in valid_exclusions:
            if True:
                ctx.exclusions.add(mapping_set_id, ExclusionRuleInput(column=ex.column, operator=ex.operator, values=ex.values, reason=ex.reason, ignore_case=ex.ignore_case), ex.column, user)

    steps = ["Review every imported rule in the Mapping Studio (they are flagged NEEDS_REVIEW).",
             "Run 'Check rules on sample' (the pre-submission check) before submitting.", "An independent approver approves the mapping set; you created it, so you cannot."]
    if skipped:
        steps.insert(0, f"{len(skipped)} rule(s) did not fit this table and were skipped: map those fields by hand or with the AI suggestions.")
    return ImportResult(dry_run=body.dry_run, pack_name=pack.name, pack_sha256=pack.sha256, mapping_set_id=mapping_set_id, rules_imported=len(ok), rules_skipped=skipped,
                        lookup_entries=lookup_entries, exclusions_imported=len(valid_exclusions), exclusions_skipped=skipped_exclusions, next_steps=steps)
