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
from typing import Optional

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
    ValidationResult,
)
from src.services.embedding_service import EmbeddingService
from src.services.llm_mapper_service import (
    MAX_PROMPT_SAMPLES,
    LLMMapperService,
    LLMOutputError,
)
from src.services.metadata_service import MappingValidationError, NotFoundError
from src.services.profiler_service import _mask_shape  # same masking rule as Module 2
from src.services.rule_validator import RuleValidator

_MASKED_CLASSES = (PiiClassification.CONFIDENTIAL, PiiClassification.RESTRICTED_PII)
TOP_K = 5


class MappingOrchestrator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        embedding_service: EmbeddingService,
        llm_mapper: LLMMapperService,
        validator: RuleValidator,
        created_by: str = "ai-mapper",
    ) -> None:
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

    def generate_table_mapping_report(
        self,
        source_table_id: int,
        target_object_name: str = "BUSINESS_PARTNER",
        mapping_set_name: str = "AI_Draft_Mapping",
    ) -> MappingGenerationReport:
        """Same as `generate_table_mapping` but returns the full per-column report.

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

        proposals: list[ColumnProposal] = []
        validations: dict[str, ValidationResult] = {}
        confidences: dict[str, float] = {}
        for ctx in columns:
            candidates = self._embeddings.get_top_k_candidates(ctx.column_name, ctx.data_type, TOP_K)
            labels = [f"{c.table_name}.{c.column_name}" for c in candidates]
            try:
                rec = self._llm.recommend(ctx, candidates)  # LLMUnavailableError aborts the run
            except LLMOutputError as exc:
                proposals.append(ColumnProposal(
                    source_column=ctx.column_name, outcome=ProposalOutcome.ERROR,
                    status=ReviewStatus.NEEDS_REVIEW, reasons=[str(exc)], candidates=labels,
                ))
                continue
            if rec is None:
                proposals.append(ColumnProposal(
                    source_column=ctx.column_name, outcome=ProposalOutcome.NO_MATCH, candidates=labels,
                ))
                continue

            result = self._validator.validate(
                rec, allowed_source_columns=all_names, candidates=candidates
            )
            validations[ctx.column_name] = result
            confidences[ctx.column_name] = rec.confidence_score
            proposals.append(ColumnProposal(
                source_column=ctx.column_name,
                outcome=ProposalOutcome.SAVED if result.persistable else ProposalOutcome.REJECTED,
                status=result.status,
                recommendation=rec,
                reasons=result.reasons,
                candidates=labels,
            ))

        self._resolve_target_conflicts(proposals, validations)
        return self._persist(
            source_table_id, target_object_name, mapping_set_name, proposals, validations, skipped
        )

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
                samples = [str(v) for v in (col.sample_values or [])[:MAX_PROMPT_SAMPLES]]
                if col.pii_classification in _MASKED_CLASSES:
                    samples = [_mask_shape(s) for s in samples]
                contexts.append(SourceColumnContext(
                    column_id=col.id,
                    column_name=col.column_name,
                    data_type=col.data_type,
                    char_max_length=col.char_max_length,
                    null_ratio=float(col.null_ratio),
                    distinct_ratio=float(col.distinct_ratio),
                    sample_values=samples,
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
