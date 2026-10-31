"""One-click fixes: what a person can have the application do about a problem, instead of only being told what to do.

Every fix is a small, named action the server performs, never free-form code. What it will do is written out before the button is pressed (`Remedy.what_happens`), what it
did is written out after (`FixResult.changes`), and anything it could not decide safely is handed back (`needs_decision`) instead of being guessed.

  apply_lookups      fill the lookup table of a value-mapping rule: values that already are SAP keys or are on the built-in synonym list are translated without a model, the AI
                     is asked about the rest and every key it proposes is checked against the SAP value list; only the certain entries are written
  set_static_value   give one SAP field one value for every record (an account group, a category) - the person supplies it, and it is checked against the field's allowed values
  add_placeholders   one placeholder rule per mandatory SAP field that nothing maps, so the records are reported as lacking it and Data Construction can supply it
  copy_to_draft      an approved or submitted mapping cannot change: make an editable DRAFT copy (rules, lookups, rows to leave out)

A fix on a mapping that cannot change (approved, submitted, archived) first makes the draft copy and applies the fix there. The copy belongs to the person who pressed the
button, so somebody else has to approve it: a fix never approves itself, and the approved mapping that produced an earlier run stays exactly as it was.
"""

from __future__ import annotations

import re
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select

from src.api.context import AppContext
from src.db.enums import MappingStatus, RuleType
from src.db.models import CatColumn, CatTable, MapFieldRule, MapTableRule, MapValueLookup
from src.schemas.exclusion import ExclusionRuleInput
from src.schemas.metadata import MappingSetCreate
from src.schemas.preflight import PreflightReport
from src.schemas.remedy import Challenge, FixRequest, FixResult, Remedy, RemedyInput
from src.services.ai_assist import AssistError
from src.services.metadata_service import NotFoundError

MAX_STATIC_SUGGESTIONS = 12
# The only fields where "one value for every record" is a sensible answer: organisational and configuration fields, not data about the customer. A button that set every
# city or every name to one value would corrupt the data, so it is never offered and never accepted.
CONSTANT_FIELDS = frozenset({
    "BUT000.TYPE", "BUT000.BU_GROUP", "BUT100.RLTYP", "KNB1.BUKRS", "KNB1.AKONT", "KNB1.ZTERM", "KNB1.ZWELS", "KNVV.VKORG", "KNVV.VTWEG", "KNVV.SPART", "KNVV.WAERS", "KNVV.INCO1",
    "LFB1.BUKRS", "LFB1.AKONT", "LFB1.ZTERM", "LFB1.ZWELS", "LFM1.EKORG", "LFM1.WAERS", "LFM1.ZTERM", "LFM1.INCO1"})
MAX_FIXES_SHOWN = 10

# What to do about each kind of defect in a RUN (the mapping-level advice is in mapping_preflight).
RUN_ADVICE: dict[str, str] = {
    "MANDATORY": "This SAP field is required but empty for these records. If one value fits every record, I can set it for you once you approve the proposal below. If it differs from record to record, "
                 "supply it in Data Construction (Governance page) or map a legacy column that holds it.",
    "LOOKUP_FAILED": "The lookup table has no entry for these legacy values. I can draft the missing entries if you approve the proposal below: the certain ones are written and you decide the rest.",
    "CHECKTABLE": "These values are not valid SAP keys for the field. I can translate them with a lookup table if you approve the proposal below, or you can correct the codes in the source.",
    "LENGTH": "These values are longer than the SAP field allows. Shorten them in the mapping rule (for example LEFT(TRIM(col), N)) in an editable copy of the mapping, or correct the source.",
    "CONVERSION": "These values have the wrong type or contain control characters. Clean them in the mapping rule (TRIM, REPLACE, a date format) in an editable copy of the mapping, or correct the source.",
    "FORMAT": "These values do not have the shape SAP expects (an e-mail address, an IBAN). Clean them in the mapping rule or correct the source.",
}


# ====================================================================== state
def _mapping(ctx: AppContext, mapping_set_id: int) -> tuple[str, bool]:
    """(status, editable now)"""
    with ctx.session_factory() as s:
        mapping = s.get(MapTableRule, mapping_set_id)
        if mapping is None:
            raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
        status = mapping.status
    archived = bool(ctx.archive.get(mapping_set_id))
    return ("archived" if archived else status.value), status == MappingStatus.DRAFT and not archived


def _copy_note(status: str, mapping_set_id: int) -> list[str]:
    return [f"Mapping set #{mapping_set_id} is {status} and cannot be changed, so an editable DRAFT copy is made first and the fix goes into the copy. "
            "Somebody else has to approve the copy before it can run; the approved mapping is left exactly as it is."]


def _allowed_values(ctx: AppContext, table: str, column: str) -> list[str]:
    with ctx.session_factory() as s:
        col = s.scalar(select(CatColumn).join(CatTable, CatColumn.table_id == CatTable.id).where(CatTable.table_name == table, CatColumn.column_name == column))
        return [str(v) for v in (col.sample_values or [])][:MAX_STATIC_SUGGESTIONS] if col is not None else []


# ====================================================================== building the proposals
def _impact(ctx: AppContext, mset: int, editable: bool, status: str) -> tuple[list[str], str]:
    """(what certainly does not change, how to take it back) for a fix that goes into mapping set `mset`."""
    if editable:
        return ([f"Your legacy data and the file that was read from it.", f"Every other rule of mapping set #{mset}."],
                "While the mapping is a draft you can change or delete what was written, or archive the whole draft: nothing is final until it is approved by somebody else.")
    return ([f"Your legacy data and the file that was read from it.", f"Mapping set #{mset} (it is {status}) and the run that was made with it.", "Nothing is approved or loaded by this."],
            "Archive the new draft and nothing of it remains; the approved mapping never changed.")


def _lookup_remedy(ctx: AppContext, mset: int, rule_id: int, target: str, situation: str = "") -> Remedy:
    status, editable = _mapping(ctx, mset)
    unchanged, undo = _impact(ctx, mset, editable, status)
    what = ["Reads the most frequent legacy values in your file.",
            "Values that already are SAP keys, or that are on the built-in synonym list, are translated without the AI. The AI is asked only about the rest, and a key it proposes "
            "that is not on the SAP value list is discarded.",
            "Writes only the entries that are certain. The ones it is not sure about are listed for you to decide."]
    return Remedy(
        kind="apply_lookups", title=f"Fill the lookup table of {target}",
        situation=situation or f"The lookup table of {target} does not cover the values in your file, so records carrying the missing values would be refused.",
        proposal=f"I read the values in your file and fill the lookup table of {target}: the ones that are certain I write, the ones I am not sure about I list for you.",
        what_happens=what + ([] if editable else _copy_note(status, mset)), unchanged=unchanged, undo=undo, question=f"Do you approve that I fill the lookup table of {target}?",
        mapping_set_id=mset, rule_id=rule_id, target=target, editable_now=editable)


def _static_remedy(ctx: AppContext, mset: int, target: str, situation: str = "") -> Remedy:
    status, editable = _mapping(ctx, mset)
    unchanged, undo = _impact(ctx, mset, editable, status)
    table, _, column = target.partition(".")
    allowed = _allowed_values(ctx, table, column)
    what = [f"Sets {target} to the one value you enter, for every record, in place of the empty placeholder or the current rule.",
            "The value is checked against what the field allows" + (": " + ", ".join(allowed[:6]) + ("..." if len(allowed) > 6 else "") if allowed else " (its length and the SAP value list, where there is one).")]
    return Remedy(
        kind="set_static_value", title=f"Set {target} to one value for every record",
        situation=situation or f"{target} is required by SAP but nothing fills it, so every record would be refused.",
        proposal=f"If one value is right for every record, I set {target} to it. I will not guess it: you tell me which.",
        what_happens=what + ([] if editable else _copy_note(status, mset)), unchanged=unchanged, undo=undo, question=f"Which value should {target} have for every record, and do you approve setting it?",
        mapping_set_id=mset, target=target, editable_now=editable,
        inputs=[RemedyInput(name="value", label=f"Value for {target}", suggestions=allowed, hint="the same value is used for every record")])


def _placeholder_remedy(ctx: AppContext, mset: int, targets: list[str]) -> Remedy:
    status, editable = _mapping(ctx, mset)
    unchanged, undo = _impact(ctx, mset, editable, status)
    what = [f"Adds an empty placeholder rule for each of the {len(targets)} mandatory SAP field(s) nothing maps: " + ", ".join(targets[:6]) + ("..." if len(targets) > 6 else "") + ".",
            "The records are then reported as lacking those values, so they can be supplied in Data Construction. Nothing is invented."]
    return Remedy(
        kind="add_placeholders", title=f"Add placeholders for {len(targets)} missing mandatory field(s)",
        situation=f"{len(targets)} mandatory SAP field(s) have no rule: " + ", ".join(targets[:6]) + ("..." if len(targets) > 6 else "") + ". They would be empty and SAP requires them.",
        proposal="I add an empty placeholder rule for each, so the gaps are reported record by record and can be filled in Data Construction. I do not invent any value.",
        what_happens=what + ([] if editable else _copy_note(status, mset)), unchanged=unchanged, undo=undo, question="Do you approve that I add these placeholders?",
        mapping_set_id=mset, editable_now=editable)


def remedies_for_mapping(ctx: AppContext, mset: int, report: PreflightReport) -> list[Remedy]:
    """The proposals for the problems the pre-submission check found."""
    out: list[Remedy] = []
    seen: set[tuple] = set()
    for f in report.findings:
        if f.action == "draft_lookups" and f.rule_id and ("lookup", f.rule_id) not in seen:
            seen.add(("lookup", f.rule_id))
            out.append(_lookup_remedy(ctx, mset, f.rule_id, f.target or "this rule", f.message))
    if any(f.code in ("MANDATORY_UNMAPPED", "AWAITING_CONSTRUCTION") for f in report.findings):
        from src.api.views import mapping_detail
        missing = mapping_detail(ctx, mset).unmapped_mandatory_targets
        for target in [t for t in missing if t in CONSTANT_FIELDS][:MAX_FIXES_SHOWN - 1]:
            out.append(_static_remedy(ctx, mset, target))
        if missing:
            out.append(_placeholder_remedy(ctx, mset, missing))
    return out[:MAX_FIXES_SHOWN]


def defects_of_run(ctx: AppContext, run_id: int) -> tuple[Optional[int], list[dict], list[Remedy]]:
    """(mapping set, the defect groups with what to do about each, the proposals) from the run's validation result."""
    report = ctx.load_validation_report(run_id)
    if report is None:
        return None, [], []
    rows: list[dict] = []
    remedies: list[Remedy] = []
    seen: set[tuple] = set()
    with ctx.session_factory() as s:
        rule_types = {r.id: r.rule_type for r in s.scalars(select(MapFieldRule).where(MapFieldRule.mapping_set_id == report.mapping_set_id))}
    for f in sorted(report.failures, key=lambda x: (x.severity.value != "CRITICAL", -x.violation_count)):
        kind = f.kind.value
        fixable = False
        if kind == "MANDATORY" and f.target in CONSTANT_FIELDS and ("static", f.target) not in seen:
            seen.add(("static", f.target)); fixable = True
            remedies.append(_static_remedy(ctx, report.mapping_set_id, f.target,
                                           f"{f.violation_count:,} records of run {run_id} were refused because {f.target} is empty, and SAP requires it."))
        elif kind in ("LOOKUP_FAILED", "CHECKTABLE") and rule_types.get(f.field_rule_id) == RuleType.VALUE_MAPPING and ("lookup", f.field_rule_id) not in seen:
            seen.add(("lookup", f.field_rule_id)); fixable = True
            remedies.append(_lookup_remedy(ctx, report.mapping_set_id, f.field_rule_id, f.target,
                                           f"{f.violation_count:,} records of run {run_id} carry a legacy value for {f.target} that the lookup table has no entry for."))
        rows.append({"field": f.target, "severity": f.severity.value, "records": f.violation_count, "problem": kind,
                     "what_to_do": RUN_ADVICE.get(kind, "Open the rule and compare its result with the legacy values."), "has_fix_button": fixable})
    return report.mapping_set_id, rows, remedies[:MAX_FIXES_SHOWN]


# ====================================================================== doing it
def copy_to_draft(ctx: AppContext, mset: int, user: str) -> int:
    """An editable DRAFT copy of a mapping set: its rules, their lookups and its rows-to-leave-out rules. The copy is the caller's, so somebody else approves it."""
    with ctx.session_factory() as s:
        old = s.get(MapTableRule, mset)
        if old is None:
            raise NotFoundError(f"mapping set {mset} does not exist")
        source_id, target_id = old.source_table_id, old.target_table_id
    new_id = ctx.metadata.create_mapping_set(MappingSetCreate(source_table_id=source_id, target_table_id=target_id, created_by=user)).id
    with ctx.session_factory.begin() as s:
        for r in s.scalars(select(MapFieldRule).where(MapFieldRule.mapping_set_id == mset).order_by(MapFieldRule.id)):
            copy = MapFieldRule(
                mapping_set_id=new_id, source_column_id=r.source_column_id, target_column_id=r.target_column_id, rule_type=r.rule_type, transformation_logic=r.transformation_logic,
                ai_suggested=r.ai_suggested, ai_model_name=r.ai_model_name, ai_confidence_score=r.ai_confidence_score,
                ai_reasoning=(r.ai_reasoning or "") + f" [Copied from mapping set #{mset} by {user}.]", is_mandatory_target=r.is_mandatory_target)
            s.add(copy)
            s.flush()
            for lk in r.value_lookups:
                s.add(MapValueLookup(field_rule_id=copy.id, source_value=lk.source_value, target_value=lk.target_value))
    for ex in ctx.exclusions.rules(mset):
        ctx.exclusions.add(new_id, ExclusionRuleInput(column=ex.column, operator=ex.operator, values=ex.values, reason=ex.reason, ignore_case=ex.ignore_case), ex.column, ex.created_by)
    return new_id


DUMMY_VALUE = re.compile(r"^(x+|0+|9+|test|dummy|tbd|todo|n/?a|none|null|unknown|\?+|-+|\.+|abc|xyz|temp|default|asdf)$", re.I)
ACCOUNT_FIELDS = {"KNB1.AKONT", "LFB1.AKONT"}
FREE_TEXT_DISTINCT_RATIO = 0.2


def challenge_for(ctx: AppContext, req: FixRequest) -> Optional[Challenge]:
    """Where the application disagrees with what it was asked to do. Looked at before anything is written, and on every approval: being asked is not being right."""
    mset = req.mapping_set_id
    if req.kind == "set_static_value" and req.target and "." in req.target:
        table, _, column = req.target.partition(".")
        value = (req.values.get("value") or "").strip()
        with ctx.session_factory() as s:
            target_col = s.scalar(select(CatColumn).join(CatTable, CatColumn.table_id == CatTable.id).where(CatTable.table_name == table.upper(), CatColumn.column_name == column.upper()))
            rules = {f"{s.get(CatTable, s.get(CatColumn, r.target_column_id).table_id).table_name}.{s.get(CatColumn, r.target_column_id).column_name}": r
                     for r in s.scalars(select(MapFieldRule).where(MapFieldRule.mapping_set_id == mset))}
            existing = rules.get(req.target.upper())
            source = s.get(CatColumn, existing.source_column_id).column_name if existing is not None and existing.source_column_id else None
        if req.target.upper() == "BUT000.TYPE" and value in ("1", "2"):
            persons, orgs = "BUT000.NAME_LAST" in rules or "BUT000.NAME_FIRST" in rules, "BUT000.NAME_ORG1" in rules
            if value == "1" and orgs and not persons:
                return Challenge(level="block", reason="You asked for category 1 (a person), but this mapping fills organisation names (NAME_ORG1) and no last name.",
                                 why="SAP names a person by last name. Every one of these records would be refused for a missing last name, or would be created as a person with no name.",
                                 suggestion="Use 2 (organisation), which is what your mapping fills. If these really are people, map NAME_LAST first.")
            if value == "2" and persons and not orgs:
                return Challenge(level="block", reason="You asked for category 2 (an organisation), but this mapping fills first and last names and no organisation name.",
                                 why="An organisation needs NAME_ORG1. Every record would be refused for a missing organisation name.",
                                 suggestion="Use 1 (person), which is what your mapping fills, or map NAME_ORG1.")
        if source and not req.override:
            return Challenge(level="warn", reason=f"{req.target} is not empty: it currently takes its values from the legacy column {source}.",
                             why="A fixed value replaces that for every record. If the legacy values differ from customer to customer, they are thrown away and everyone gets the same one.",
                             suggestion=f"If {source} holds the right values, keep the rule and fix the values it has. Use a fixed value only if every customer really has the same one.")
        if DUMMY_VALUE.match(value) and not req.override:
            return Challenge(level="warn", reason=f"'{value}' looks like a placeholder, not a real value.",
                             why=f"SAP may accept it, but every customer would carry '{value}' from then on, and nothing would show later that it was never real.",
                             suggestion="Use the real value, or leave the field to be supplied in Data Construction.")
        if req.target.upper() in ACCOUNT_FIELDS and not req.override:
            return Challenge(level="warn", reason=f"I cannot check that G/L account '{value}' exists in your SAP system.",
                             why="If it does not exist, SAP refuses every customer when the data is loaded, after the whole preparation is done.",
                             suggestion="Ask whoever maintains your chart of accounts which reconciliation account customers use, and confirm it is the one you entered.")
    if req.kind == "apply_lookups" and req.rule_id and not req.override:
        with ctx.session_factory() as s:
            rule = s.get(MapFieldRule, req.rule_id)
            col = s.get(CatColumn, rule.source_column_id) if rule is not None and rule.source_column_id else None
            ratio = float(col.distinct_ratio) if col is not None and col.distinct_ratio is not None else None
            name = col.column_name if col is not None else None
        if ratio is not None and ratio > FREE_TEXT_DISTINCT_RATIO:
            return Challenge(level="warn", reason=f"The legacy column {name} has a different value in about {ratio:.0%} of the rows: it looks like free text, not a short list of codes.",
                             why="A lookup table translates a short list of codes. For free text it would hold thousands of entries and still miss the next new spelling.",
                             suggestion="Check that this is the right column. If it is a name or an address, copy it as it is and clean it, instead of translating it.")
    return None


def _record_decision(ctx: AppContext, req: FixRequest, user: str, decision: Optional[str] = None, why: Optional[str] = None) -> None:
    """The decision on the audit trail, before anything is done: a fix whose approval cannot be recorded is not made."""
    from src.services.audit_log import AuditUnavailable
    try:
        ctx.audit.append({"phase": "decision", "user": user, "method": "DECISION", "path": "/api/mapping/fix", "decision": decision or ("approved" if req.approved else "declined"),
                          "kind": req.kind, "mapping_set_id": req.mapping_set_id, "target": req.target, "shown": req.shown or None, "overridden": bool(req.override) or None, "why": why,
                          "values": {k: v for k, v in (req.values or {}).items()} or None})
    except AuditUnavailable as exc:
        raise HTTPException(503, f"refused: your decision must be on the audit trail first, and {exc}") from exc


def execute(ctx: AppContext, req: FixRequest, user: str) -> FixResult:
    mset = req.mapping_set_id
    status, editable = _mapping(ctx, mset)
    if not req.approved:
        _record_decision(ctx, req, user)
        return FixResult(ok=False, declined=True, message="Nothing was changed: it was not approved. I will not do this unless you ask again.", mapping_set_id=mset)
    objection = challenge_for(ctx, req)
    if objection is not None and (objection.level == "block" or not req.override):
        _record_decision(ctx, req, user, "challenged", objection.reason)
        return FixResult(ok=False, challenge=objection, mapping_set_id=mset, message=("I did not do it, and I disagree: " if objection.level == "warn" else "I will not do this: ") + objection.reason)
    _record_decision(ctx, req, user)
    if req.kind == "copy_to_draft":
        new = copy_to_draft(ctx, mset, user)
        return FixResult(ok=True, message=f"Made editable draft #{new}, a copy of mapping set #{mset}.", changes=[f"mapping set #{new} (DRAFT) with every rule, lookup and row-to-leave-out rule of #{mset}"],
                         mapping_set_id=new, copied_from=mset, next_step=f"Change what you need in #{new}, press 'Check rules on sample', submit it, and let somebody else approve it.")
    copied_from = None
    rule_id = req.rule_id
    if rule_id and editable:                                          # the buttons were made for another mapping set (the one this fix now goes into a copy of): the same field, in this set
        with ctx.session_factory() as s:
            named = s.get(MapFieldRule, rule_id)
            if named is not None and named.mapping_set_id != mset:
                rule_id = s.scalar(select(MapFieldRule.id).where(MapFieldRule.mapping_set_id == mset, MapFieldRule.target_column_id == named.target_column_id))
    if not editable:
        target_column = None
        if rule_id:
            with ctx.session_factory() as s:
                rule = s.get(MapFieldRule, rule_id)
                target_column = rule.target_column_id if rule is not None and rule.mapping_set_id == mset else None
        copied_from, mset = mset, copy_to_draft(ctx, mset, user)
        if rule_id:
            with ctx.session_factory() as s:
                rule_id = s.scalar(select(MapFieldRule.id).where(MapFieldRule.mapping_set_id == mset, MapFieldRule.target_column_id == target_column))
    try:
        if req.kind == "apply_lookups":
            message, changes, undecided = _apply_lookups(ctx, mset, rule_id, user)
        elif req.kind == "set_static_value":
            message, changes, undecided = _set_static(ctx, mset, req.target, req.values.get("value", ""), user)
        else:
            message, changes, undecided = _placeholders(ctx, mset, user)
    except AssistError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    if copied_from:
        changes = [f"made editable draft #{mset}, a copy of mapping set #{copied_from} ({status})"] + changes
        message = f"Made draft #{mset} (a copy of #{copied_from}) and applied the fix there. " + message
    done = bool(changes)
    next_step = ""
    if done:
        next_step = f"Press 'Check rules on sample' on mapping set #{mset} to confirm the fix" + (", then submit it for approval, and let somebody else approve it before running it again." if copied_from else ".")
    return FixResult(ok=done, message=message, changes=changes, needs_decision=undecided, mapping_set_id=mset, copied_from=copied_from, next_step=next_step)


def _apply_lookups(ctx: AppContext, mset: int, rule_id: Optional[int], user: str) -> tuple[str, list[str], list[str]]:
    with ctx.session_factory() as s:
        rule = s.get(MapFieldRule, rule_id) if rule_id else None
        if rule is None or rule.mapping_set_id != mset:
            raise HTTPException(404, "that rule is not in this mapping set")
        if rule.rule_type != RuleType.VALUE_MAPPING:
            raise HTTPException(422, "only a value-mapping rule has a lookup table to fill: change the rule type to VALUE_MAPPING first")
        source = ctx.source_file(s.get(MapTableRule, mset).source_table_id)
        target = f"{s.get(CatTable, s.get(CatColumn, rule.target_column_id).table_id).table_name}.{s.get(CatColumn, rule.target_column_id).column_name}"
    draft = ctx.ai_assist.draft_lookups(rule_id, source, user)
    pending = [e for e in draft.entries if e.status != "approved"]
    accept = [e.legacy_value for e in pending if e.proposed and e.suggested_accept]
    undecided = [f"'{e.legacy_value}' ({e.rows:,} rows): " + (f"the AI suggests {e.proposed} but is not sure enough" if e.proposed else "no safe SAP key found") for e in pending
                 if not (e.proposed and e.suggested_accept)][:8]
    if not accept:
        return (f"Nothing could be written to {target} without guessing." if pending else f"{target} already has a lookup for every value I looked at."), [], undecided
    result = ctx.ai_assist.apply_draft(rule_id, accept, user)
    covered = result.coverage.covered_now / result.coverage.non_blank_rows if result.coverage.non_blank_rows else 1.0
    how = ", ".join(sorted({e.method.replace("_", " ") for e in pending if e.legacy_value in set(accept)}))
    return (f"Wrote {result.applied} lookup(s) for {target} ({how}); {covered:.1%} of the rows are now covered." + (f" {len(undecided)} value(s) are left for you." if undecided else "")), \
        [f"{target}: {e.legacy_value!r} -> {e.proposed}" for e in pending if e.legacy_value in set(accept)][:20], undecided


def _set_static(ctx: AppContext, mset: int, target: Optional[str], value: str, user: str) -> tuple[str, list[str], list[str]]:
    from src.api.routes.mapping import _checked_logic, _draft_mapping, _find_target
    if not target or "." not in target:
        raise HTTPException(422, "name the SAP field as TABLE.COLUMN")
    value = (value or "").strip()
    if not value:
        raise HTTPException(422, "enter the value first")
    if target.upper() not in CONSTANT_FIELDS:
        raise HTTPException(422, f"one value for every record does not make sense for {target}: it holds data about each customer. Supply it per record in Data Construction, or map a legacy column that has it")
    table, _, column = target.partition(".")
    with ctx.session_factory.begin() as s:
        mapping = _draft_mapping(s, mset)
        col = _find_target(s, mapping, table, column)
        allowed = [str(v) for v in (col.sample_values or [])]
        if allowed and value not in allowed:
            raise HTTPException(422, f"{target} allows: {', '.join(allowed[:12])}")
        if col.char_max_length and len(value) > col.char_max_length:
            raise HTTPException(422, f"{target} holds at most {col.char_max_length} characters")
        if col.check_table:
            domain = ctx.validation._domains.get_domain(col.check_table)
            if domain is not None and value not in domain:
                raise HTTPException(422, f"'{value}' is not a valid {col.check_table} key for {target}; for example: {', '.join(sorted(domain)[:8])}")
        logic = _checked_logic(s, mapping, "STATIC_VALUE", None, "'" + value.replace("'", "''") + "'")
        existing = s.scalar(select(MapFieldRule).where(MapFieldRule.mapping_set_id == mset, MapFieldRule.target_column_id == col.id))
        if existing is not None:
            was = f"{existing.rule_type.value}" + (f" of column {s.get(CatColumn, existing.source_column_id).column_name}" if existing.source_column_id else " placeholder")
            for lk in list(existing.value_lookups):
                s.delete(lk)
            existing.rule_type, existing.source_column_id, existing.transformation_logic = RuleType.STATIC_VALUE, None, logic
            existing.ai_suggested, existing.ai_model_name, existing.ai_confidence_score = False, None, None
            existing.ai_reasoning = f"Set to the fixed value {logic} by {user} with Fix it (was: {was})."
        else:
            was = "no rule"
            s.add(MapFieldRule(mapping_set_id=mset, source_column_id=None, target_column_id=col.id, rule_type=RuleType.STATIC_VALUE, transformation_logic=logic, ai_suggested=False,
                               ai_reasoning=f"Fixed value {logic} created by {user} with Fix it.", is_mandatory_target=bool(col.is_mandatory)))
    return f"{target} is now set to '{value}' for every record.", [f"{target}: {was} -> the fixed value '{value}'"], []


def _placeholders(ctx: AppContext, mset: int, user: str) -> tuple[str, list[str], list[str]]:
    added = ctx.construction.add_placeholder_rules(mset, user)
    if not added:
        return "Every mandatory SAP field already has a rule: no placeholder was needed.", [], []
    return (f"Added {len(added)} placeholder rule(s); the records will be reported as lacking those values until they are supplied in Data Construction.",
            [f"placeholder for {p.target}" for p in added], [])
