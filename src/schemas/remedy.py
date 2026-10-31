"""A fix the person can apply with one click: what it is called, what exactly will happen, what it needs from the person, and what came of it."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

RemedyKind = Literal["apply_lookups", "add_placeholders", "set_static_value", "copy_to_draft"]


class RemedyInput(BaseModel):
    name: str  # the key in FixRequest.values
    label: str
    suggestions: list[str] = Field(default_factory=list)  # values the catalog or the SAP key list allows, offered as a pick list
    hint: str = ""


class Remedy(BaseModel):
    """A proposal that waits for the person's approval. Nothing happens until it is given, and the text says what will: the situation, what is proposed, what changes, what does
    not, how to undo it, and the question to answer."""

    kind: RemedyKind
    title: str
    situation: str = ""  # what is wrong, in plain words, with the real figures
    proposal: str = ""  # what the application suggests doing about it
    what_happens: list[str]  # what will change, as plain sentences
    unchanged: list[str] = Field(default_factory=list)  # what will certainly not change
    undo: str = ""  # how to take it back
    question: str = ""  # what the person is asked to approve
    mapping_set_id: int
    rule_id: Optional[int] = None
    target: Optional[str] = None  # TABLE.COLUMN
    inputs: list[RemedyInput] = Field(default_factory=list)  # what the person has to supply first
    editable_now: bool = True  # False: the mapping set is approved / submitted / archived, so a DRAFT copy is made first and the fix goes into the copy


class Challenge(BaseModel):
    """The application disagrees with what the person asked for. `block`: it is wrong and will not be done. `warn`: it is unusual or risky: it is done only if the person, having read
    the reason, says so explicitly (FixRequest.override)."""

    level: Literal["block", "warn"]
    reason: str  # what is wrong, in one sentence
    why: str  # what it would cause
    suggestion: str = ""  # what to do instead


class FixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: RemedyKind
    mapping_set_id: int = Field(ge=1)
    rule_id: Optional[int] = Field(default=None, ge=1)
    target: Optional[str] = Field(default=None, max_length=128)
    values: dict[str, str] = Field(default_factory=dict, max_length=8)
    # The person's answer to the proposal. Without an explicit True nothing is changed: the request is recorded as a refusal. `shown` is the title of the proposal that was put
    # in front of the person, kept in the audit trail beside the decision.
    approved: bool = False
    shown: str = Field(default="", max_length=300)
    override: bool = False  # "I have read the objection and want it anyway": needed only after a `warn` challenge


class FixResult(BaseModel):
    ok: bool
    declined: bool = False  # the person said no (or did not say yes): nothing was changed
    challenge: Optional[Challenge] = None  # the application did not do it, and says why; with level `warn` the person may insist
    message: str  # one plain sentence: what was done, or why nothing was
    changes: list[str] = Field(default_factory=list)  # each change that was written
    needs_decision: list[str] = Field(default_factory=list)  # what was left for the person, and why
    mapping_set_id: int  # where the change landed: the copy, when one had to be made
    copied_from: Optional[int] = None
    next_step: str = ""
