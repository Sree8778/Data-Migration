"""The plain-English assistant: a question, the read-only tools it may use, and an answer whose figures are checked against what the tools returned."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.schemas.remedy import Remedy

TOOLS = ("run_summary", "explain_defects", "find_records", "column_facts", "mapping_rules", "delta_summary")


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=3, max_length=500)
    run_id: Optional[int] = Field(default=None, ge=1, description="the run the question is about")
    mapping_set_id: Optional[int] = Field(default=None, ge=1, description="the mapping set the question is about (its latest run is used when no run is given)")


class ToolCall(BaseModel):
    """One read-only question put to the data. Flat on purpose: a model fills a flat form far more reliably than a nested one."""

    tool: Literal["run_summary", "explain_defects", "find_records", "column_facts", "mapping_rules", "delta_summary", "next_steps", "fix_plan"]
    run_id: Optional[int] = None
    mapping_set_id: Optional[int] = None
    table_id: Optional[int] = None
    signature: Optional[str] = Field(default=None, description="explain_defects: part of a rule signature or target field, e.g. CITY1 or LOOKUP")
    group: Optional[Literal["rejected", "duplicates", "excluded", "valid"]] = Field(default=None, description="find_records: which records")
    column: Optional[str] = Field(default=None, description="find_records / column_facts: a column name")
    op: Optional[Literal["equals", "contains", "is_blank", "not_blank"]] = None
    value: Optional[str] = None
    limit: Optional[int] = Field(default=None, ge=1, le=20)
    target: Optional[str] = Field(default=None, description="mapping_rules: part of a target field name")


class ToolPlan(BaseModel):
    calls: list[ToolCall] = Field(default_factory=list, max_length=3)
    cannot_answer: Optional[str] = Field(default=None, description="why none of the tools can answer this question")


class AnswerOut(BaseModel):
    answer: str = Field(min_length=1, max_length=900)


class TableOut(BaseModel):
    title: str
    columns: list[str]
    rows: list[list[str]]
    total: int  # how many records match, of which `rows` shows the first few


class ToolRun(BaseModel):
    tool: str
    args: dict[str, Any]
    ok: bool
    summary: str  # what was looked at, or why it could not be


class AssistantAnswer(BaseModel):
    question: str
    answer: str
    verified: bool  # every figure in `answer` appears in the tool results
    source: Literal["model", "facts"]  # the model's wording, or the plain statement of the facts (when the model was unavailable or a figure could not be verified)
    tool_runs: list[ToolRun]
    tables: list[TableOut]
    facts: dict[str, Any]  # what the answer is based on: exactly what the tools returned
    notes: list[str] = Field(default_factory=list)
    scope: dict[str, Optional[int]] = Field(default_factory=dict)
    fixes: list[Remedy] = Field(default_factory=list)  # the 'Fix it' buttons: each says what it will do, and does it only when pressed
