"""Data quality assessment of a landed legacy table: which rules ran, what they found, and how that is changing from one assessment to the next."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Dimension = Literal["Completeness", "Validity", "Uniqueness", "Consistency", "Conformity", "Credibility", "Integrity"]
Severity = Literal["HIGH", "MEDIUM", "LOW"]


class RuleInfo(BaseModel):
    rule_id: str
    title: str
    dimension: Dimension
    severity: Severity
    applies_to: list[str]  # column roles: any, email, phone, tax, postal, country, name, date, amount, key
    hint: str  # what to do about a finding


class RuleResult(BaseModel):
    rule_id: str
    column: str
    role: str
    title: str
    dimension: Dimension
    severity: Severity
    checked: int  # rows the rule looked at (non-empty values, or all rows for uniqueness)
    violations: int
    rate: float  # violations / checked
    samples: list[str] = Field(default_factory=list)  # a few offending values; shape-masked for columns that hold personal data
    hint: str = ""


class ColumnQuality(BaseModel):
    column: str
    role: str
    pii: str
    filled: int
    score: float  # 0-100, severity-weighted share of checks passed
    findings: int


class QualityReport(BaseModel):
    table_id: int
    table_name: str
    assessment_no: int
    assessed_at: str
    assessed_by: str
    rows: int
    rules_run: int
    overall_score: float
    dimension_scores: dict[str, float]
    columns: list[ColumnQuality]
    findings: list[RuleResult]  # only rules that found something, worst first
    previous_score: Optional[float] = None
    change: Optional[float] = None  # overall_score - previous_score
    notes: list[str] = Field(default_factory=list)


class QualityHistoryItem(BaseModel):
    assessment_no: int
    assessed_at: str
    assessed_by: str
    overall_score: float
    findings: int
    high_findings: int
    rows: int
