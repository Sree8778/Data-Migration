"""Combine landed tables (joins and filters) into one new source table, described as data, never as SQL."""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,29}$")
REF = re.compile(r"^([A-Za-z][A-Za-z0-9_]{0,29})\.(.{1,128})$")

FilterOp = Literal["equals", "not_equals", "in", "contains", "starts_with", "regex", "is_blank", "not_blank"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TableRef(_Strict):
    table_id: int = Field(ge=1)
    alias: str = Field(description="a short name used in column references, e.g. 'c' for customers")

    @field_validator("alias")
    @classmethod
    def _alias(cls, v: str) -> str:
        if not ALIAS.match(v):
            raise ValueError("an alias starts with a letter and has letters, digits or _ (up to 30 characters)")
        return v


class JoinCondition(_Strict):
    left: str = Field(description="alias.column of a table that is already part of the query")
    right: str = Field(description="alias.column of the table being joined")


class JoinStep(TableRef):
    type: Literal["inner", "left"] = "left"
    on: list[JoinCondition] = Field(min_length=1, max_length=4)


class FilterStep(_Strict):
    column: str = Field(description="alias.column")
    op: FilterOp
    values: list[str] = Field(default_factory=list, max_length=200)
    ignore_case: bool = True

    @model_validator(mode="after")
    def _shape(self) -> "FilterStep":
        if self.op in ("is_blank", "not_blank"):
            if self.values:
                raise ValueError(f"{self.op} takes no values")
        elif self.op in ("in",):
            if not self.values:
                raise ValueError("'in' needs at least one value")
        elif len(self.values) != 1 or not self.values[0].strip():
            raise ValueError(f"{self.op} needs exactly one non-empty value")
        return self


class ColumnPick(_Strict):
    ref: str = Field(description="alias.column")
    name: Optional[str] = Field(default=None, max_length=128, description="the column's name in the combined table (default: its own name)")


class CombineSpec(_Strict):
    base: TableRef
    joins: list[JoinStep] = Field(default_factory=list, max_length=5)
    filters: list[FilterStep] = Field(default_factory=list, max_length=12)
    columns: list[ColumnPick] = Field(default_factory=list, max_length=300, description="empty = every column of every table")
    match: Literal["exact", "trim", "trim_ci"] = Field(default="trim_ci", description="how join keys are compared: as they are, ignoring blanks around them, or also ignoring case")
    allow_fanout: bool = Field(default=False, description="allow a join that turns one base record into several rows (otherwise it is refused)")

    @model_validator(mode="after")
    def _aliases(self) -> "CombineSpec":
        aliases = [self.base.alias, *(j.alias for j in self.joins)]
        if len({a.lower() for a in aliases}) != len(aliases):
            raise ValueError("every table needs its own alias")
        return self


class CombineRequest(CombineSpec):
    system_name: str = Field(min_length=1, max_length=128)
    table_name: str = Field(min_length=1, max_length=128)
    business_name: Optional[str] = Field(default=None, max_length=256)
    primary_key_columns: list[str] = Field(default_factory=list, max_length=8, description="output column names that identify a record; a key that repeats or is blank is rejected")


class CombinePreviewRequest(CombineSpec):
    sample_rows: int = Field(default=10, ge=1, le=50)


class OutputColumn(BaseModel):
    name: str
    from_table: str  # the alias
    source_column: str


class JoinStats(BaseModel):
    alias: str
    table_name: str
    type: str
    left_alias: str  # the table whose records the join looks partners up for
    left_rows: int
    right_rows: int
    left_rows_without_a_match: int  # records that find no partner in this table
    left_rows_with_several: int  # records that find more than one partner (the cause of repeated records)
    most_partners: int


class CombinePreview(BaseModel):
    base_rows: int
    rows_after_joins: int
    rows_out: int  # after the filters
    removed_by_filters: int
    columns: list[OutputColumn]
    joins: list[JoinStats]
    warnings: list[str]
    blocked: Optional[str] = None  # why the combination cannot be created as described (e.g. a join repeats base records)
    sample: list[list[str]]
    sql: str  # what will run, for the record (all values are literals, all names are checked)
