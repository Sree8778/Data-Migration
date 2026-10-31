"""Stewardship tasks: who fixes what, by when."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.api.context import AppContext
from src.api.deps import get_ctx, require_user
from src.services.stewardship import KINDS, PRIORITIES, STATUSES, TaskError

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=200)
    kind: Literal["DEFECT_GROUP", "QUALITY_FINDING", "DUPLICATE_REVIEW", "CUSTOM"] = "CUSTOM"
    detail: str = Field(default="", max_length=2000)
    owner: Optional[str] = Field(default=None, max_length=128)
    due: Optional[str] = Field(default=None, description="YYYY-MM-DD")
    priority: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"
    ref: dict[str, Any] = Field(default_factory=dict, description="what it is about: {run_id, signature}, {table_id, rule_id}, {run_id, pk_a, pk_b}...")


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["OPEN", "IN_PROGRESS", "DONE", "WONT_FIX"]] = None
    owner: Optional[str] = Field(default=None, max_length=128, description="empty string = unassign")
    due: Optional[str] = Field(default=None, description="YYYY-MM-DD, empty string = no due date")
    priority: Optional[Literal["HIGH", "MEDIUM", "LOW"]] = None
    note: Optional[str] = Field(default=None, max_length=1000)


class FromDefects(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: Optional[str] = Field(default=None, max_length=128, description="who gets the tasks unless owners_by_table says otherwise")
    due: Optional[str] = None
    owners_by_table: dict[str, str] = Field(default_factory=dict, description="route by SAP table, e.g. {\"ADRC\": \"address.owner\", \"KNB1\": \"finance.owner\"}")


@router.get("/meta")
def meta() -> dict[str, Any]:
    return {"statuses": list(STATUSES), "priorities": list(PRIORITIES), "kinds": list(KINDS)}


@router.get("/summary")
def summary(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Open, overdue and unassigned counts, per owner and per status."""
    return ctx.tasks.summary()


@router.get("")
def list_tasks(owner: Optional[str] = None, status: Optional[str] = None, kind: Optional[str] = None, overdue: Optional[bool] = None, run_id: Optional[int] = None,
               open_only: bool = False, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Tasks, the ones that are late or due soonest first. `owner=` shows one person's work."""
    tasks = ctx.tasks.list(owner, status, kind, overdue, run_id, open_only)
    return {"total": len(tasks), "tasks": tasks}


@router.get("/{task_id}")
def get_task(task_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    try:
        return ctx.tasks.get(task_id)
    except TaskError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("", status_code=201)
def create_task(body: TaskCreate, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict[str, Any]:
    try:
        return ctx.tasks.create(user, body.title, body.kind, body.detail, body.owner, body.due, body.priority, body.ref)
    except TaskError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{task_id}/update")
def update_task(task_id: int, body: TaskUpdate, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict[str, Any]:
    """Change status, owner, due date or priority and/or add a note; what changed is recorded with who and when."""
    try:
        return ctx.tasks.update(task_id, user, body.status, body.owner, body.due, body.priority, body.note)
    except TaskError as exc:
        raise HTTPException(404 if "does not exist" in str(exc) else 422, str(exc)) from exc


@router.post("/from-defects/{run_id}", status_code=201)
def tasks_from_defects(run_id: int, body: FromDefects = FromDefects(), ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict[str, Any]:
    """One task per open defect group of the run that has none yet (critical ones HIGH priority), routed to owners by SAP table."""
    from src.api.routes.audit import get_defects   # the same view the Governance hub shows
    try:
        made = ctx.tasks.from_defects(user, run_id, get_defects(run_id, ctx).defects, body.owner, body.due, body.owners_by_table)
    except TaskError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"created": len(made), "tasks": made}


@router.post("/sync/{run_id}")
def sync_tasks(run_id: int, ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict[str, Any]:
    """Close the defect tasks of this run whose defect is no longer open (after a fix and a re-validation)."""
    from src.api.routes.audit import get_defects
    still_open = {d.rule_signature for d in get_defects(run_id, ctx).defects if d.is_open}
    closed = ctx.tasks.sync_defects(run_id, still_open, user)
    return {"closed": len(closed), "tasks": closed}


@router.post("/remind")
def remind(ctx: AppContext = Depends(get_ctx), user: str = Depends(require_user)) -> dict[str, Any]:
    """Tell the configured webhook about every overdue task (call it from a scheduler)."""
    late = ctx.tasks.remind()
    return {"overdue": len(late), "webhook": "configured" if ctx.settings.notify_webhook else "not configured"}
