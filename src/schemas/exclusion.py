"""Rows that must not be migrated at all: deleted-flag records, test and dummy customers, anything the business says to leave behind."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

Operator = Literal["equals", "in", "is_blank", "starts_with", "contains", "regex"]


class ExclusionRule(BaseModel):
    """One 'leave this row out' rule. A row is excluded when ANY rule of the mapping set matches it; the first matching rule gets the credit."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rule_id: int
    column: str = Field(min_length=1, max_length=128)
    operator: Operator
    values: list[str] = Field(default_factory=list, max_length=200)  # one value for equals / starts_with / contains / regex, several for in, none for is_blank
    reason: str = Field(min_length=3, max_length=300)  # why these rows are left behind: kept with the rule and shown on the certificate
    ignore_case: bool = True
    created_by: str
    created_at: str


class ExclusionRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    column: str = Field(min_length=1, max_length=128)
    operator: Operator
    values: list[str] = Field(default_factory=list, max_length=200)
    reason: str = Field(min_length=3, max_length=300)
    ignore_case: bool = True

    @model_validator(mode="after")
    def _shape(self) -> "ExclusionRuleInput":
        values = [v for v in self.values if v is not None]
        if self.operator == "is_blank":
            if values:
                raise ValueError("is_blank takes no values")
        elif self.operator == "in":
            if not values:
                raise ValueError("'in' needs at least one value")
        elif len(values) != 1 or not values[0].strip():
            raise ValueError(f"{self.operator} needs exactly one non-empty value")
        self.values = values
        return self


class ExclusionRuleCount(BaseModel):
    rule_id: int
    column: str
    operator: str
    values: list[str]
    reason: str
    excluded: int  # rows this rule removed (a row matching several rules counts for the first one only)
    examples: list[str] = Field(default_factory=list)  # a few matching values, so the person adding the rule can see what it catches


class ExclusionResult(BaseModel):
    total_rows: int
    included_rows: int
    excluded_rows: int
    rules: list[ExclusionRuleCount]
    included_path: Optional[str] = None
    excluded_path: Optional[str] = None


class ExclusionSuggestion(BaseModel):
    """A rule the data itself suggests, for a person to accept or ignore."""

    column: str
    operator: Operator
    values: list[str]
    reason: str
    matches: int
    examples: list[str]


class ExclusionOverview(BaseModel):
    """What the Studio shows: the rules, whether they can still be changed, and what they remove from the landed file today."""

    mapping_set_id: int
    editable: bool  # only a DRAFT mapping set (not archived) can change its exclusion rules
    rules: list[ExclusionRule]
    summary: Optional[ExclusionResult] = None  # None when no file is landed or there are no rules
