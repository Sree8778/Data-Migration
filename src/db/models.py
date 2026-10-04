"""SQLAlchemy 2.0 ORM models for the Module 1 metadata catalog.

Table prefixes:  cat_ = catalog,  map_ = mapping specification,
                 mig_ = migration execution,  gov_ = governance.

Design rules:
* Every constraint is named (see NAMING_CONVENTION) so it can be referenced in audits
  and migrations.
* Foreign keys default to RESTRICT: governed data is never silently deleted.
  The only CASCADE edges are pure child detail rows (columns -> table, lookups -> rule).
* Business invariants that can be expressed in a single row are CHECK constraints so they
  hold regardless of which application writes to the database.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.db.enums import (
    MappingStatus,
    MigrationPhase,
    PiiClassification,
    RuleType,
    RunStatus,
    SystemType,
    TargetType,
)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


def _enum(enum_cls: type, name: str) -> sa.Enum:
    """Enum stored as VARCHAR + named CHECK (easier to extend than a native PG enum)."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [m.value for m in members],
    )


def _jsonb() -> JSONB:
    # none_as_null: Python None -> SQL NULL (not the JSON literal 'null').
    return JSONB(none_as_null=True)


def _in_list(column: str, members: list[str]) -> str:
    return f"{column} IN ({', '.join(repr(m) for m in members)})"


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class TimestampMixin(CreatedAtMixin):
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


# --------------------------------------------------------------------------- catalog
class CatDataSource(TimestampMixin, Base):
    """Registered source / staging / target system."""

    __tablename__ = "cat_data_sources"
    __table_args__ = (sa.UniqueConstraint("system_name", name="uq_cat_data_sources_system_name"),)

    id: Mapped[int] = mapped_column(sa.Integer, sa.Identity(), primary_key=True)
    system_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    system_type: Mapped[SystemType] = mapped_column(_enum(SystemType, "system_type"), nullable=False)
    target_type: Mapped[TargetType] = mapped_column(_enum(TargetType, "target_type"), nullable=False)
    # Must hold connection parameters and *references* to secrets, never secret values.
    connection_config: Mapped[dict[str, Any]] = mapped_column(
        _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    tables: Mapped[list[CatTable]] = relationship(back_populates="source", passive_deletes=True)


class CatTable(TimestampMixin, Base):
    """Discovered or registered table."""

    __tablename__ = "cat_tables"
    __table_args__ = (
        sa.UniqueConstraint("source_id", "table_name", name="uq_cat_tables_source_id_table_name"),
        sa.CheckConstraint(
            "row_count_estimate IS NULL OR row_count_estimate >= 0", name="row_count_non_negative"
        ),
        sa.Index("ix_cat_tables_business_object", "business_object"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, sa.Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(
        sa.ForeignKey("cat_data_sources.id", ondelete="RESTRICT"), nullable=False
    )
    table_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    business_name: Mapped[str | None] = mapped_column(sa.String(256))
    description: Mapped[str | None] = mapped_column(sa.Text)
    row_count_estimate: Mapped[int | None] = mapped_column(sa.BigInteger)
    last_profiled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # Extension: groups target tables into a migration object (e.g. BUSINESS_PARTNER).
    business_object: Mapped[str | None] = mapped_column(sa.String(64))

    source: Mapped[CatDataSource] = relationship(back_populates="tables")
    columns: Mapped[list[CatColumn]] = relationship(
        back_populates="table", order_by="CatColumn.ordinal_position", passive_deletes=True
    )


class CatColumn(TimestampMixin, Base):
    """Column-level metadata, including profiling statistics and PII class."""

    __tablename__ = "cat_columns"
    __table_args__ = (
        sa.UniqueConstraint("table_id", "column_name", name="uq_cat_columns_table_id_column_name"),
        sa.CheckConstraint("char_max_length IS NULL OR char_max_length > 0", name="char_length_positive"),
        sa.CheckConstraint("numeric_precision IS NULL OR numeric_precision > 0", name="precision_positive"),
        sa.CheckConstraint(
            "numeric_scale IS NULL OR (numeric_precision IS NOT NULL AND numeric_scale >= 0"
            " AND numeric_scale <= numeric_precision)",
            name="scale_within_precision",
        ),
        sa.CheckConstraint("null_ratio IS NULL OR null_ratio BETWEEN 0 AND 1", name="null_ratio_range"),
        sa.CheckConstraint(
            "distinct_ratio IS NULL OR distinct_ratio BETWEEN 0 AND 1", name="distinct_ratio_range"
        ),
        sa.CheckConstraint("NOT (is_primary_key AND is_nullable)", name="pk_not_nullable"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, sa.Identity(), primary_key=True)
    table_id: Mapped[int] = mapped_column(
        sa.ForeignKey("cat_tables.id", ondelete="CASCADE"), nullable=False
    )
    column_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    data_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    char_max_length: Mapped[int | None] = mapped_column(sa.Integer)
    numeric_precision: Mapped[int | None] = mapped_column(sa.Integer)
    numeric_scale: Mapped[int | None] = mapped_column(sa.Integer)
    is_nullable: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    is_primary_key: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    pii_classification: Mapped[PiiClassification] = mapped_column(
        _enum(PiiClassification, "pii_classification"),
        nullable=False,
        server_default=PiiClassification.INTERNAL.value,
    )
    sample_values: Mapped[list[Any] | None] = mapped_column(_jsonb())
    null_ratio: Mapped[Decimal | None] = mapped_column(sa.Numeric(5, 4))
    distinct_ratio: Mapped[Decimal | None] = mapped_column(sa.Numeric(5, 4))
    # Extensions required by the SAP target dictionary.
    ordinal_position: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    is_mandatory: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    check_table: Mapped[str | None] = mapped_column(sa.String(64))
    description: Mapped[str | None] = mapped_column(sa.Text)

    table: Mapped[CatTable] = relationship(back_populates="columns")


# --------------------------------------------------------------------------- mapping
class MapTableRule(TimestampMixin, Base):
    """Mapping set: one versioned source-table -> target-table mapping with approval state."""

    __tablename__ = "map_table_rules"
    __table_args__ = (
        sa.UniqueConstraint(
            "source_table_id", "target_table_id", "version", name="uq_map_table_rules_src_tgt_version"
        ),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint("source_table_id <> target_table_id", name="source_ne_target"),
        sa.CheckConstraint(
            "status <> 'APPROVED' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="approved_requires_approver",
        ),
        # SOX segregation of duties: the author of a mapping cannot approve it.
        sa.CheckConstraint(
            "approved_by IS NULL OR approved_by <> created_by", name="maker_checker_separation"
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, sa.Identity(), primary_key=True)
    source_table_id: Mapped[int] = mapped_column(
        sa.ForeignKey("cat_tables.id", ondelete="RESTRICT"), nullable=False
    )
    target_table_id: Mapped[int] = mapped_column(
        sa.ForeignKey("cat_tables.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    status: Mapped[MappingStatus] = mapped_column(
        _enum(MappingStatus, "mapping_status"),
        nullable=False,
        server_default=MappingStatus.DRAFT.value,
    )
    created_by: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(sa.String(128))
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    source_table: Mapped[CatTable] = relationship(foreign_keys=[source_table_id])
    target_table: Mapped[CatTable] = relationship(foreign_keys=[target_table_id])


class MapFieldRule(TimestampMixin, Base):
    """Column-to-column mapping specification inside a mapping set."""

    __tablename__ = "map_field_rules"
    __table_args__ = (
        sa.UniqueConstraint(
            "mapping_set_id", "target_column_id", name="uq_map_field_rules_set_target_column"
        ),
        sa.CheckConstraint(
            "rule_type NOT IN ('DIRECT_COPY', 'VALUE_MAPPING') OR source_column_id IS NOT NULL",
            name="copy_rules_need_source",
        ),
        sa.CheckConstraint(
            "rule_type NOT IN ('SQL_EXPRESSION', 'PYTHON_SNIPPET', 'STATIC_VALUE')"
            " OR transformation_logic IS NOT NULL",
            name="logic_rules_need_logic",
        ),
        sa.CheckConstraint("NOT ai_suggested OR ai_model_name IS NOT NULL", name="ai_requires_model"),
        sa.CheckConstraint(
            "ai_confidence_score IS NULL OR ai_confidence_score BETWEEN 0 AND 1",
            name="ai_confidence_range",
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, sa.Identity(), primary_key=True)
    mapping_set_id: Mapped[int] = mapped_column(
        sa.ForeignKey("map_table_rules.id", ondelete="RESTRICT"), nullable=False
    )
    source_column_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("cat_columns.id", ondelete="RESTRICT")
    )
    target_column_id: Mapped[int] = mapped_column(
        sa.ForeignKey("cat_columns.id", ondelete="RESTRICT"), nullable=False
    )
    rule_type: Mapped[RuleType] = mapped_column(_enum(RuleType, "rule_type"), nullable=False)
    transformation_logic: Mapped[str | None] = mapped_column(sa.Text)
    ai_suggested: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    ai_model_name: Mapped[str | None] = mapped_column(sa.String(128))
    ai_confidence_score: Mapped[Decimal | None] = mapped_column(sa.Numeric(5, 4))
    ai_reasoning: Mapped[str | None] = mapped_column(sa.Text)
    is_mandatory_target: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    value_lookups: Mapped[list[MapValueLookup]] = relationship(
        back_populates="field_rule", passive_deletes=True
    )


class MapValueLookup(Base):
    """Discrete categorical translation belonging to a VALUE_MAPPING field rule."""

    __tablename__ = "map_value_lookups"
    __table_args__ = (
        sa.UniqueConstraint(
            "field_rule_id", "source_value", name="uq_map_value_lookups_rule_source_value"
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, sa.Identity(), primary_key=True)
    field_rule_id: Mapped[int] = mapped_column(
        sa.ForeignKey("map_field_rules.id", ondelete="CASCADE"), nullable=False
    )
    source_value: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    target_value: Mapped[str] = mapped_column(sa.String(512), nullable=False)

    field_rule: Mapped[MapFieldRule] = relationship(back_populates="value_lookups")


# --------------------------------------------------------------------------- migration
class MigMigrationRun(CreatedAtMixin, Base):
    """One execution of an (approved) mapping set within a migration wave."""

    __tablename__ = "mig_migration_runs"
    __table_args__ = (
        sa.CheckConstraint(
            "records_extracted >= 0 AND records_transformed >= 0"
            " AND records_loaded >= 0 AND records_failed >= 0",
            name="counters_non_negative",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="completed_after_started",
        ),
        sa.Index("ix_mig_migration_runs_mapping_set_id", "mapping_set_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, sa.Identity(), primary_key=True)
    mapping_set_id: Mapped[int] = mapped_column(
        sa.ForeignKey("map_table_rules.id", ondelete="RESTRICT"), nullable=False
    )
    wave_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    current_phase: Mapped[MigrationPhase] = mapped_column(
        _enum(MigrationPhase, "migration_phase"),
        nullable=False,
        server_default=MigrationPhase.EXTRACT.value,
    )
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), nullable=False, server_default=RunStatus.PENDING.value
    )
    records_extracted: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    records_transformed: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    records_loaded: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    records_failed: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class MigCellLineage(CreatedAtMixin, Base):
    """Cell-level lineage: raw source value -> transformed value, per rule, per run."""

    __tablename__ = "mig_cell_lineage"
    __table_args__ = (
        sa.Index("ix_mig_cell_lineage_run_id_source_pk_value", "run_id", "source_pk_value"),
        sa.Index("ix_mig_cell_lineage_run_id_target_pk_value", "run_id", "target_pk_value"),
        sa.Index("ix_mig_cell_lineage_rule_applied_id", "rule_applied_id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)  # BIGSERIAL-equivalent
    run_id: Mapped[int] = mapped_column(
        sa.ForeignKey("mig_migration_runs.id", ondelete="RESTRICT"), nullable=False
    )
    source_pk_value: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    target_pk_value: Mapped[str | None] = mapped_column(sa.String(512))
    target_column_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    source_raw_value: Mapped[str | None] = mapped_column(sa.Text)
    transformed_value: Mapped[str | None] = mapped_column(sa.Text)
    rule_applied_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("map_field_rules.id", ondelete="RESTRICT")
    )
    loaded_successfully: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    error_message: Mapped[str | None] = mapped_column(sa.Text)


# --------------------------------------------------------------------------- governance
class GovDefectAggregate(TimestampMixin, Base):
    """Defects aggregated per rule signature, linkable to a Jira issue."""

    __tablename__ = "gov_defect_aggregates"
    __table_args__ = (
        sa.UniqueConstraint(
            "run_id", "field_rule_id", "rule_signature", name="uq_gov_defect_aggregates_run_rule_sig"
        ),
        sa.CheckConstraint("violation_count >= 0", name="violation_count_non_negative"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, sa.Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        sa.ForeignKey("mig_migration_runs.id", ondelete="RESTRICT"), nullable=False
    )
    field_rule_id: Mapped[int] = mapped_column(
        sa.ForeignKey("map_field_rules.id", ondelete="RESTRICT"), nullable=False
    )
    rule_signature: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    violation_count: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    sample_failing_record_ids: Mapped[list[Any] | None] = mapped_column(_jsonb())
    jira_issue_key: Mapped[str | None] = mapped_column(sa.String(32))
    jira_status: Mapped[str | None] = mapped_column(sa.String(64))
