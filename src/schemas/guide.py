"""The setup guide: a chat beside the step the person is doing. What it is told (never a password), and what it answers."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

GuideStep = Literal["start", "provider", "connection", "tables", "options", "summary", "done"]


class GuideTable(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(max_length=200)
    rows: Optional[int] = None
    kind: str = ""  # TABLE | VIEW


class GuideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    step: GuideStep = "start"
    kind: str = Field(default="", max_length=40)  # file | mssql | oracle | ... what the person chose as the provider
    message: str = Field(default="", max_length=600)
    error: str = Field(default="", max_length=600)  # what the screen last said went wrong (test result, file problem)
    fields: dict[str, str] = Field(default_factory=dict, max_length=12)  # what the form holds so far; the password is never among them
    tables: list[GuideTable] = Field(default_factory=list, max_length=300)
    file_name: str = Field(default="", max_length=200)


class HeldBack(BaseModel):
    """A value the person gave that was NOT put into the form, because it looks wrong. The person decides: use it anyway, or change it."""

    field: str
    value: str
    why: str
    suggestion: str = ""


class GuideAction(BaseModel):
    label: str
    do: Literal["open_wizard", "open_tab", "ask"]
    arg: str = ""


class GuideReply(BaseModel):
    reply: str
    required_now: list[str] = Field(default_factory=list)  # what this step needs from the person
    still_missing: list[str] = Field(default_factory=list)  # of those, what the form does not hold yet
    tips: list[str] = Field(default_factory=list)
    fill: dict[str, str] = Field(default_factory=dict)  # values the screen may put into the form: never a password
    suggestions: list[str] = Field(default_factory=list)  # questions worth asking here
    actions: list[GuideAction] = Field(default_factory=list)
    warning: str = ""
    held_back: list[HeldBack] = Field(default_factory=list)  # what was not filled in, and why
    source: Literal["rules", "model"] = "rules"


class ProgressStep(BaseModel):
    key: str
    label: str
    status: Literal["done", "current", "todo"]
    detail: str = ""


class GuideProgress(BaseModel):
    steps: list[ProgressStep]
    headline: str  # what to do now, in one sentence
    actions: list[GuideAction] = Field(default_factory=list)
