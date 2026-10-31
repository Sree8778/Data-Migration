"""The AI assistant's proposals: a rule for a column the first pass missed, and drafted lookup tables. A proposal is never applied by itself."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ------------------------------------------------------------------ evidence measured on the landed file (not asked of the model)
class ColumnEvidence(BaseModel):
    rows: int
    non_blank: int
    distinct: int
    max_length: Optional[int] = None
    target_length: Optional[int] = None
    length_fit_rate: Optional[float] = None  # share of the non-blank rows that fit the target field
    domain: Optional[str] = None  # the SAP check table whose keys the values were compared with
    domain_note: Optional[str] = None  # where that key list comes from (and its limits)
    domain_valid_rate: Optional[float] = None  # share of the non-blank rows whose value is already a valid key
    top_invalid: list[tuple[str, int]] = Field(default_factory=list)  # the most frequent values that are not valid keys


# ------------------------------------------------------------------ suggest a rule for one column
class SuggestRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    column: str = Field(min_length=1, max_length=128)
    hint: Optional[str] = Field(default=None, max_length=300, description="what you know about the column, e.g. 'this is the customer account group'")
    runs: int = Field(default=3, ge=1, le=5, description="how many times the model is asked (the most common answer is kept)")


class RuleSuggestion(BaseModel):
    suggestion_id: str
    mapping_set_id: int
    source_column: str
    outcome: Literal["SUGGESTED", "NO_MATCH", "REJECTED", "ERROR"]
    target_table: Optional[str] = None
    target_column: Optional[str] = None
    rule_type: Optional[str] = None
    transformation_sql: Optional[str] = None
    confidence: Optional[float] = None
    reasoning: Optional[str] = None
    review_reasons: list[str] = Field(default_factory=list)
    runs: int = 1
    agreement: float = 1.0
    alternatives: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    evidence: Optional[ColumnEvidence] = None
    model_name: str = ""
    needs_lookups: bool = False  # the suggested rule is a value mapping: draft its lookups next
    created_by: str = ""
    acceptable: bool = False  # false when it could not be stored (no match, an unsafe expression, an unknown target)


# ------------------------------------------------------------------ drafted lookups
class LookupEntry(BaseModel):
    legacy_value: str
    rows: int  # how many rows carry this legacy value
    proposed: Optional[str] = None  # a key that exists in the SAP key list, or None when the value could not be translated
    method: Literal["valid_key", "synonym_list", "ai", "none"] = "none"
    confidence: float = 0.0
    agreement: bool = True  # every AI run proposed the same key
    reason: str = ""
    suggested_accept: bool = False  # pre-ticked in the screen: certain, or the AI was sure and consistent
    status: Literal["proposed", "approved"] = "proposed"
    approved_by: Optional[str] = None
    approved_key: Optional[str] = None


class LookupCoverage(BaseModel):
    non_blank_rows: int
    covered_now: int  # rows whose value already has a lookup entry
    covered_if_suggested: int  # ... plus the entries pre-ticked as suggested
    covered_if_all: int  # ... plus every proposal


class LookupDraft(BaseModel):
    rule_id: int
    target: str
    source_column: str
    domain: str
    domain_note: str
    domain_size: int
    model_name: str
    created_by: str
    created_at: str
    distinct_values: int
    drafted_values: int  # the most frequent values that were looked at
    not_drafted: int  # rarer values left for the steward (they stay uncovered)
    entries: list[LookupEntry]
    coverage: LookupCoverage


class LookupDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: int = Field(default=2, ge=1, le=4)
    limit: int = Field(default=150, ge=10, le=400, description="how many of the most frequent legacy values to draft")


class LookupApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    accept: list[str] = Field(min_length=1, max_length=400, description="the legacy values whose proposed key the steward approves")
    overrides: dict[str, str] = Field(default_factory=dict, description="legacy value -> the steward's own key instead of the proposal (must exist in the SAP key list)")


class LookupApplyResult(BaseModel):
    rule_id: int
    applied: int
    lookups_now: int
    coverage: LookupCoverage
    approved_by: str


# ------------------------------------------------------------------ what the model is asked to return for lookups
class LookupAnswerItem(BaseModel):
    legacy_value: str
    sap_key: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class LookupAnswer(BaseModel):
    items: list[LookupAnswerItem]
