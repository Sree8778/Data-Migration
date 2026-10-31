"""Orchestrates Module 3: retrieval -> LLM -> guardrails -> draft mapping set in Module 1 tables.

Design notes
* The slow LLM loop runs with no database transaction open; everything is persisted afterwards
  in ONE transaction, so a failure never leaves a half-written mapping set.
* `map_table_rules` links one source table to one target table, but a Business Partner spans
  several SAP tables. The set's `target_table_id` is the target table receiving most rules;
  each `map_field_rules` row carries its own exact `target_column_id`.
* `map_field_rules` is unique per (mapping set, target column): if several source columns
  claim the same target, the highest confidence wins and the rest are reported SUPERSEDED.
* There is no status column on `map_field_rules`, so NEEDS_REVIEW is recorded as a
  "[NEEDS_REVIEW] reasons | reasoning" prefix in `ai_reasoning`. The mapping set itself is
  always DRAFT; humans approve it (maker/checker is enforced by the Module 1 constraints).
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
import re
from typing import Callable, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from src.db.enums import MappingStatus, PiiClassification, RuleType, TargetType
from src.db.models import CatColumn, CatDataSource, CatTable, MapFieldRule, MapTableRule
from src.schemas.mapping_ai import (
    ColumnProposal,
    MappingGenerationReport,
    ProposalOutcome,
    ReviewStatus,
    SourceColumnContext,
    TargetCandidate,
    ValidationResult,
)
from src.services.embedding_service import EmbeddingService
from src.services.llm_mapper_service import (
    MAX_PROMPT_SAMPLES,
    LLMMapperService,
    LLMOutputError,
    is_pinned_model,
)
from src.services.metadata_service import MappingValidationError, NotFoundError
from src.services.profiler_service import _mask_shape  # same masking rule as Module 2
from src.services.rule_validator import RuleValidator

_MASKED_CLASSES = (PiiClassification.CONFIDENTIAL, PiiClassification.RESTRICTED_PII)
TOP_K = 8  # wider than the 5 that suited tidy names: cryptic legacy names need the extra candidates
MAX_CONSISTENCY_RUNS = 5
_NO_MATCH, _ERROR = ("NO_MATCH",), ("ERROR",)


def _answer_key(rec) -> tuple:
    """What two answers must share to count as the same decision: the target field and the kind of rule."""
    return (rec.target_table.strip().upper(), rec.target_column.strip().upper(), rec.rule_type)


def _describe(key: tuple) -> str:
    return key[0] if len(key) == 1 else f"{key[0]}.{key[1]} ({key[2]})"


def tally_answers(answers: list, runs: int) -> tuple[tuple, float, list[str], Optional[str]]:
    """The vote over repeated answers (each a recommendation, None for NO_MATCH, or the error text): (winning decision, share of runs that gave it,
    the losing decisions as "n x TABLE.COLUMN (RULE_TYPE)", a sentence saying the column is unstable or None). A tie goes to the answer the model was
    most confident about, preferring a real mapping to NO_MATCH or an error."""
    votes = Counter(_ERROR if isinstance(a, str) else _NO_MATCH if a is None else _answer_key(a) for a in answers)
    best = max(votes.values())
    leaders = [k for k, n in votes.items() if n == best]
    if len(leaders) > 1:
        leaders.sort(key=lambda k: (k in (_NO_MATCH, _ERROR), -max((a.confidence_score for a in answers if not isinstance(a, str) and a is not None
                                                                      and _answer_key(a) == k), default=0.0)))
    winner = leaders[0]
    losers = [f"{n} x {_describe(k)}" for k, n in votes.most_common() if k != winner]
    unstable = (f"unstable: {votes[winner]} of {runs} runs gave {_describe(winner)}, the others gave " + ", ".join(losers)) if losers else None
    return winner, votes[winner] / runs, losers, unstable



# A business partner is a customer, a supplier or both, and the target has separate tables for each side. A customer extract that is offered the supplier tables gets a supplier
# currency or payment term now and then (their descriptions are alike), and every such choice is a wrong field in SAP. The side is decided from what the source calls itself.
CUSTOMER_WORDS = ("CUST", "KUNNR", "KUNDE", "CLIENT", "DEBITOR", "DEBTOR", "RECEIVABLE", "BUYER", "AR_")
SUPPLIER_WORDS = ("VEND", "LIFNR", "SUPPLIER", "LIEFERANT", "KREDITOR", "CREDITOR", "PAYABLE", "SELLER", "AP_")
OTHER_SIDE_TABLES = {"customer": frozenset({"LFB1", "LFM1"}), "supplier": frozenset({"KNB1", "KNVV"}), "both": frozenset()}


_KEY_OWNERS = {"cust", "customer", "kunde", "client", "partner", "vendor", "supplier", "debitor", "creditor"}
_KEY_WORDS = {"id", "no", "nr", "num", "number", "key", "code"}
_OTHER_DATA = {"tax", "vat", "house", "street", "bank", "iban", "phone", "tel", "fax", "order", "invoice", "zip", "postal", "region", "country", "city", "email", "mail", "currency", "terms",
               "address", "addr", "name", "payment", "account", "group", "type", "language", "sales", "company"}


def pinned_targets(column_name: str) -> list[tuple[str, str]]:
    """The targets that must be offered for a column whose name says it is the customer's or supplier's own key (CUST_ID, CUSTOMER_NO, KUNNR, LIFNR, a bare ID): the external partner
    number. Every migration maps the legacy key there, and similarity alone can rank bank numbers above it."""
    tokens = set(re.sub(r"[_\-\.\s]+", " ", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", column_name)).lower().split())
    if tokens & _OTHER_DATA:
        return []
    if tokens & {"kunnr", "lifnr"} or (tokens & _KEY_OWNERS and tokens & _KEY_WORDS) or tokens in ({"id"}, {"key"}):
        return [("BUT000", "BP_EXT")]
    return []


def infer_partner_side(table_name: str, column_names: Iterable[str]) -> str:
    """'customer', 'supplier' or 'both', from the table name (counted double) and the column names. Nothing to go on: customer, which is what this application migrates first."""
    def score(words: tuple[str, ...]) -> int:
        return sum(2 for w in words if w in table_name.upper()) + sum(1 for n in column_names for w in words if w in n.upper())
    customer, supplier = score(CUSTOMER_WORDS), score(SUPPLIER_WORDS)
    if customer and supplier and min(customer, supplier) * 2 >= max(customer, supplier):
        return "both"
    return "supplier" if supplier > customer else "customer"

class MappingOrchestrator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        embedding_service: EmbeddingService,
        llm_mapper: LLMMapperService,
        validator: RuleValidator,
        created_by: str = "ai-mapper",
        consistency_runs: int = 1,
    ) -> None:
        self._consistency_runs = max(1, min(MAX_CONSISTENCY_RUNS, int(consistency_runs)))
        self._session_factory = session_factory
        self._embeddings = embedding_service
        self._llm = llm_mapper
        self._validator = validator
        self._created_by = created_by

    # ------------------------------------------------------------------ public API
    def generate_table_mapping(
        self,
        source_table_id: int,
        target_object_name: str = "BUSINESS_PARTNER",
        mapping_set_name: str = "AI_Draft_Mapping",
    ) -> int:
        """Create a DRAFT mapping set and return its `map_table_rules.id`."""
        return self.generate_table_mapping_report(
            source_table_id, target_object_name, mapping_set_name
        ).mapping_set_id

    def side_exclusions(self, source_table_id: int, partner_side: str = "auto") -> tuple[str, frozenset[str]]:
        """(the side the source is treated as, the target tables that are then not offered): a customer extract is never offered the supplier tables, and the other way round."""
        table_name, _, _ = self._load_source(source_table_id)
        names = [c.column_name for c in self._all_source_columns(source_table_id)]
        side = partner_side if partner_side in ("customer", "supplier", "both") else infer_partner_side(table_name, names)
        return side, OTHER_SIDE_TABLES.get(side, frozenset())

    def generate_table_mapping_report(
        self,
        source_table_id: int,
        target_object_name: str = "BUSINESS_PARTNER",
        mapping_set_name: str = "AI_Draft_Mapping",
        progress: Optional[Callable[[str, int, int, Optional[str]], None]] = None,
        consistency_runs: Optional[int] = None,
        partner_side: str = "auto",
    ) -> MappingGenerationReport:
        """Same as `generate_table_mapping` but returns the full per-column report.

        `consistency_runs` (1-5; default from the constructor): ask the model that many times about every column. The most common
        answer is kept; a column whose answers differ is flagged NEEDS_REVIEW as unstable, with the losing answers listed. A model that
        is deterministic (a local Ollama model at temperature 0 and a fixed seed) is asked once however many runs are requested.

        `progress(event, position, total, column)` is called with event "start" (total known),
        "column" (before each LLM call; `position` is 1-based) and "persist" (before saving). An
        exception raised by the callback aborts the run before anything is written.

        `mapping_set_name` is echoed in the report only: `map_table_rules` has no name column
        (Module 1 schema is locked).
        """
        for svc_object in (self._embeddings.object_name, self._validator.object_name):
            if svc_object != target_object_name:
                raise ValueError(
                    f"services are configured for '{svc_object}', not '{target_object_name}'"
                )

        table_name, columns, skipped = self._load_source(source_table_id)
        if not columns:
            raise MappingValidationError(
                f"table {table_name} has no profiled columns; run the profiler (Module 2) first"
            )
        self._embeddings.build_index()
        all_names = [c.column_name for c in self._all_source_columns(source_table_id)]
        side, excluded = self.side_exclusions(source_table_id, partner_side)

        runs = self._consistency_runs if consistency_runs is None else max(1, min(MAX_CONSISTENCY_RUNS, int(consistency_runs)))
        if getattr(self._llm, "deterministic", False):
            runs = 1
        proposals: list[ColumnProposal] = []
        validations: dict[str, ValidationResult] = {}
        confidences: dict[str, float] = {}
        notify = progress or (lambda *_: None)
        notify("start", 0, len(columns), None)
        for position, ctx in enumerate(columns, start=1):
            notify("column", position, len(columns), ctx.column_name)
            candidates = self._embeddings.get_top_k_candidates(ctx.column_name, ctx.data_type, TOP_K, ctx.sample_values, exclude_tables=excluded, must_include=pinned_targets(ctx.column_name))
            labels = [f"{c.table_name}.{c.column_name}" for c in candidates]
            answers: list = []  # a FieldMappingRecommendation, None (NO_MATCH) or the error text, one per run
            for _ in range(runs):
                try:
                    answers.append(self._llm.recommend(ctx, candidates))  # LLMUnavailableError aborts the run
                except LLMOutputError as exc:
                    answers.append(str(exc))
            winner, agreement, losers, unstable = tally_answers(answers, runs)

            if winner == _ERROR:
                reasons = [next(a for a in answers if isinstance(a, str))] + ([unstable] if unstable else [])
                proposals.append(ColumnProposal(
                    source_column=ctx.column_name, outcome=ProposalOutcome.ERROR, status=ReviewStatus.NEEDS_REVIEW,
                    reasons=reasons, candidates=labels, runs=runs, agreement=agreement, alternatives=losers,
                ))
                continue
            if winner == _NO_MATCH:
                proposals.append(ColumnProposal(
                    source_column=ctx.column_name, outcome=ProposalOutcome.NO_MATCH, candidates=labels,
                    status=ReviewStatus.NEEDS_REVIEW if unstable else None, reasons=[unstable] if unstable else [],
                    runs=runs, agreement=agreement, alternatives=losers,
                ))
                continue

            rec = max((a for a in answers if not isinstance(a, str) and a is not None and _answer_key(a) == winner), key=lambda a: a.confidence_score)
            result = self._validator.validate(rec, allowed_source_columns=all_names, candidates=candidates, source=ctx)
            reasons = result.reasons + ([unstable] if unstable else [])
            validations[ctx.column_name] = result
            confidences[ctx.column_name] = rec.confidence_score
            proposals.append(ColumnProposal(
                source_column=ctx.column_name,
                outcome=ProposalOutcome.SAVED if result.persistable else ProposalOutcome.REJECTED,
                status=ReviewStatus.NEEDS_REVIEW if reasons else ReviewStatus.OK,
                recommendation=rec,
                reasons=reasons,
                candidates=labels,
                runs=runs, agreement=agreement, alternatives=losers,
            ))

        notify("persist", len(columns), len(columns), None)
        self._resolve_target_conflicts(proposals, validations)
        report = self._persist(
            source_table_id, target_object_name, mapping_set_name, proposals, validations, skipped, runs
        )
        report.partner_side = side
        return report

    # ------------------------------------------------------------------ single-column helpers (the AI assistant's "suggest a rule")
    def context_for_column(self, source_table_id: int, column_name: str) -> SourceColumnContext:
        """What the model may see about one source column (profile stats and masked samples); the column must have been profiled."""
        _, contexts, skipped = self._load_source(source_table_id)
        for ctx in contexts:
            if ctx.column_name.lower() == column_name.strip().lower():
                return ctx
        if any(c.lower() == column_name.strip().lower() for c in skipped):
            raise MappingValidationError(f"column {column_name} has not been profiled; run the profiler first")
        raise NotFoundError(f"source column '{column_name}' does not exist")

    def candidates_for(self, ctx: SourceColumnContext, exclude_column_ids: Iterable[int] = (), top_k: int = TOP_K + 4, hint: Optional[str] = None,
                       exclude_tables: Iterable[str] = ()) -> list[TargetCandidate]:
        """The best target columns for one source column, leaving out targets that already have a rule. A person's hint steers the retrieval too."""
        taken = set(exclude_column_ids)
        self._embeddings.build_index()
        found = self._embeddings.get_top_k_candidates(ctx.column_name, ctx.data_type, top_k + len(taken), ctx.sample_values, exclude_tables=exclude_tables, must_include=pinned_targets(ctx.column_name),
                                                      **({"hint": hint} if hint else {}))
        return [c for c in found if c.column_id not in taken][:top_k]

    def all_source_column_names(self, source_table_id: int) -> list[str]:
        return [c.column_name for c in self._all_source_columns(source_table_id)]

    @property
    def llm(self) -> LLMMapperService:
        return self._llm

    @property
    def validator(self) -> RuleValidator:
        return self._validator

    # ------------------------------------------------------------------ loading
    def _load_source(self, source_table_id: int) -> tuple[str, list[SourceColumnContext], list[str]]:
        with self._session_factory() as session:
            table = session.scalar(
                select(CatTable)
                .where(CatTable.id == source_table_id)
                .options(selectinload(CatTable.columns), selectinload(CatTable.source))
            )
            if table is None:
                raise NotFoundError(f"table {source_table_id} does not exist")
            if table.source.target_type == TargetType.TARGET:
                raise MappingValidationError(
                    f"table {table.table_name} belongs to a TARGET system and cannot be a mapping source"
                )
            contexts, skipped = [], []
            for col in table.columns:
                if col.null_ratio is None or col.distinct_ratio is None:
                    skipped.append(col.column_name)
                    continue
                every = [str(v) for v in (col.sample_values or [])]
                if col.pii_classification in _MASKED_CLASSES:
                    every = [_mask_shape(s) for s in every]
                samples = every[:MAX_PROMPT_SAMPLES]
                contexts.append(SourceColumnContext(
                    column_id=col.id,
                    column_name=col.column_name,
                    data_type=col.data_type,
                    char_max_length=col.char_max_length,
                    null_ratio=float(col.null_ratio),
                    distinct_ratio=float(col.distinct_ratio),
                    sample_values=samples,
                    evidence_values=every,
                    table_name=table.table_name,
                ))
            return table.table_name, contexts, skipped

    def _all_source_columns(self, source_table_id: int) -> list[CatColumn]:
        with self._session_factory() as session:
            return list(session.scalars(select(CatColumn).where(CatColumn.table_id == source_table_id)))

    # ------------------------------------------------------------------ conflicts
    @staticmethod
    def _resolve_target_conflicts(
        proposals: list[ColumnProposal], validations: dict[str, ValidationResult]
    ) -> None:
        claims: dict[int, list[ColumnProposal]] = {}
        for p in proposals:
            if p.outcome == ProposalOutcome.SAVED:
                claims.setdefault(validations[p.source_column].target_column_id, []).append(p)
        for group in claims.values():
            if len(group) < 2:
                continue
            # highest confidence wins; ties go to the earlier column (stable sort)
            ranked = sorted(group, key=lambda p: -p.recommendation.confidence_score)
            winner = ranked[0]
            for loser in ranked[1:]:
                loser.outcome = ProposalOutcome.SUPERSEDED
                loser.status = ReviewStatus.NEEDS_REVIEW
                loser.reasons = loser.reasons + [
                    f"target {loser.recommendation.target_table}.{loser.recommendation.target_column} "
                    f"already claimed by {winner.source_column}"
                ]

    # ------------------------------------------------------------------ persistence
    def _persist(
        self,
        source_table_id: int,
        object_name: str,
        mapping_set_name: str,
        proposals: list[ColumnProposal],
        validations: dict[str, ValidationResult],
        skipped: list[str],
        runs: int = 1,
    ) -> MappingGenerationReport:
        to_save = [p for p in proposals if p.outcome == ProposalOutcome.SAVED]
        with self._session_factory.begin() as session:
            target_table_id = self._pick_target_table(session, object_name, to_save, validations)
            current_max = session.scalar(
                select(func.max(MapTableRule.version)).where(
                    MapTableRule.source_table_id == source_table_id,
                    MapTableRule.target_table_id == target_table_id,
                )
            )
            version = (current_max or 0) + 1
            mapping = MapTableRule(
                source_table_id=source_table_id,
                target_table_id=target_table_id,
                version=version,
                status=MappingStatus.DRAFT,
                created_by=self._created_by,
            )
            session.add(mapping)
            session.flush()

            source_ids = {
                c.column_name: c.id
                for c in session.scalars(select(CatColumn).where(CatColumn.table_id == source_table_id))
            }
            rules: list[tuple[ColumnProposal, MapFieldRule]] = []
            for p in to_save:
                rec, val = p.recommendation, validations[p.source_column]
                reasoning = rec.reasoning
                if p.status == ReviewStatus.NEEDS_REVIEW:
                    reasoning = f"[NEEDS_REVIEW] {'; '.join(p.reasons)} | {reasoning}"
                rule = MapFieldRule(
                    mapping_set_id=mapping.id,
                    source_column_id=source_ids[p.source_column],
                    target_column_id=val.target_column_id,
                    rule_type=RuleType(rec.rule_type),
                    transformation_logic=val.transformation_sql,
                    ai_suggested=True,
                    ai_model_name=self._llm.model_name,
                    ai_confidence_score=Decimal(str(rec.confidence_score)).quantize(Decimal("0.0001")),
                    ai_reasoning=reasoning,
                    is_mandatory_target=val.target_is_mandatory,
                )
                session.add(rule)
                rules.append((p, rule))
            session.flush()
            for p, rule in rules:
                p.field_rule_id = rule.id

            mapped_target_ids = {r.target_column_id for _, r in rules}
            unmapped = [
                f"{table_name}.{column_name}"
                for table_name, column_name, column_id in session.execute(
                    select(CatTable.table_name, CatColumn.column_name, CatColumn.id)
                    .join(CatColumn, CatColumn.table_id == CatTable.id)
                    .join(CatDataSource, CatTable.source_id == CatDataSource.id)
                    .where(
                        CatDataSource.target_type == TargetType.TARGET,
                        CatTable.business_object == object_name,
                        CatColumn.is_mandatory.is_(True),
                        CatColumn.is_primary_key.is_(False),
                    )
                    .order_by(CatTable.id, CatColumn.ordinal_position)
                )
                if column_id not in mapped_target_ids
            ]
            return MappingGenerationReport(
                mapping_set_id=mapping.id,
                mapping_set_name=mapping_set_name,
                source_table_id=source_table_id,
                target_table_id=target_table_id,
                version=version,
                model_name=self._llm.model_name,
                model_pinned=is_pinned_model(self._llm),
                consistency_runs=runs,
                proposals=proposals,
                skipped_unprofiled_columns=skipped,
                unmapped_mandatory_targets=unmapped,
            )

    @staticmethod
    def _pick_target_table(
        session: Session,
        object_name: str,
        to_save: list[ColumnProposal],
        validations: dict[str, ValidationResult],
    ) -> int:
        counts = Counter(validations[p.source_column].target_table_id for p in to_save)
        if counts:
            # most rules wins; ties resolved by lowest table id (deterministic)
            return min(counts, key=lambda tid: (-counts[tid], tid))
        first: Optional[int] = session.scalar(
            select(CatTable.id)
            .join(CatDataSource, CatTable.source_id == CatDataSource.id)
            .where(CatDataSource.target_type == TargetType.TARGET, CatTable.business_object == object_name)
            .order_by(CatTable.id)
            .limit(1)
        )
        if first is None:
            raise NotFoundError(f"no target tables registered for object '{object_name}'")
        return first
