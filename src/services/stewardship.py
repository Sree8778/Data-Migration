"""Data stewardship: who is responsible for fixing what, by when.

A migration is finished by the business, not by the tool: the legacy owner of the customer master has to correct the 7,000 payment terms, somebody has to decide
the 400 possible duplicates, the credit team has to fill the missing account groups. Findings that sit in a report get fixed late or never. A task turns a finding
(a defect group of a run, a data quality finding, a pair to review, or anything else) into a piece of work with an owner, a due date, a priority and a status, and
keeps what was said about it. Tasks are listed per owner, so each person sees their own work; overdue ones come to the dashboard; a defect task closes itself when a
later run no longer has the defect.

Tasks hold references and counts (a defect signature, a column, a rule), never record values. An optional webhook (MIGRATION_NOTIFY_WEBHOOK, set by the administrator,
never by a caller) is told when a task is created, assigned, closed or overdue, so an e-mail or chat bridge can notify the owner; a failing webhook never blocks the work.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import httpx

log = logging.getLogger("migration.tasks")
STATUSES = ("OPEN", "IN_PROGRESS", "DONE", "WONT_FIX")
PRIORITIES = ("HIGH", "MEDIUM", "LOW")
KINDS = ("DEFECT_GROUP", "QUALITY_FINDING", "DUPLICATE_REVIEW", "CUSTOM")
CLOSED = ("DONE", "WONT_FIX")


class TaskError(ValueError):
    """The request cannot be carried out (an unknown status, a missing task...); the message is safe to show."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_due(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise TaskError(f"the due date must look like 2026-12-31, not '{value}'") from exc


class Notifier:
    """Tell an outside system (an e-mail or chat bridge) that something happened to a task. Never raises; runs off the request thread."""

    def __init__(self, url: Optional[str], transport: Optional[httpx.BaseTransport] = None, synchronous: bool = False) -> None:
        self._url, self._transport, self._sync = url, transport, synchronous

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def send(self, event: str, task: dict[str, Any]) -> None:
        if not self._url:
            return
        payload = {"event": event, "at": _now(), "task": {k: task.get(k) for k in ("id", "title", "kind", "owner", "due", "priority", "status", "ref")}}

        def post() -> None:
            try:
                with httpx.Client(timeout=5.0, transport=self._transport) as client:
                    client.post(self._url, json=payload)
            except Exception as exc:  # noqa: BLE001 - a bridge that is down must not block anybody's work
                log.warning("notification '%s' for task %s was not delivered: %s", event, task.get("id"), exc)

        if self._sync:
            post()
        else:
            threading.Thread(target=post, daemon=True, name="task-notify").start()


class TaskStore:
    def __init__(self, workspace: Path, notifier: Optional[Notifier] = None, today: Callable[[], date] = date.today) -> None:
        self._file = Path(workspace) / "tasks" / "tasks.json"
        self._lock = threading.RLock()
        self._notifier = notifier or Notifier(None)
        self._today = today

    # ------------------------------------------------------------------ storage
    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"next_id": 1, "tasks": []}

    def _write(self, data: dict[str, Any]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self._file)

    # ------------------------------------------------------------------ create
    def create(self, user: str, title: str, kind: str = "CUSTOM", detail: str = "", owner: Optional[str] = None, due: Optional[str] = None,
               priority: str = "MEDIUM", ref: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        title = (title or "").strip()
        if len(title) < 3:
            raise TaskError("a task needs a title of at least 3 characters")
        if kind not in KINDS:
            raise TaskError(f"kind must be one of {', '.join(KINDS)}")
        if priority not in PRIORITIES:
            raise TaskError(f"priority must be one of {', '.join(PRIORITIES)}")
        due = _parse_due(due)
        with self._lock:
            data = self._read()
            task = {"id": data["next_id"], "title": title[:200], "kind": kind, "detail": (detail or "")[:2000], "owner": (owner or "").strip() or None, "created_by": user,
                    "created_at": _now(), "updated_at": _now(), "due": due, "priority": priority, "status": "OPEN", "ref": ref or {}, "notes": []}
            data["next_id"] += 1
            data["tasks"].append(task)
            self._write(data)
        self._notifier.send("task_created", task)
        return task

    def from_defects(self, user: str, run_id: int, defects: Iterable[Any], owner: Optional[str], due: Optional[str], owners_by_table: Optional[dict[str, str]] = None) -> list[dict[str, Any]]:
        """One task per open defect group of a run that has none yet. `defects` are DefectDetail-like objects (rule_signature, target, severity, violation_count, root_cause).
        `owners_by_table` can route by SAP table ('ADRC' -> the address owner); everything else goes to `owner`."""
        existing = {(t["ref"].get("run_id"), t["ref"].get("signature")) for t in self.list(kind="DEFECT_GROUP") if t["status"] not in CLOSED}
        made = []
        for d in defects:
            if not d.is_open or (run_id, d.rule_signature) in existing:
                continue
            table = str(d.target).split(".")[0]
            made.append(self.create(user, f"Fix {d.rule_signature} ({d.violation_count:,} record(s))", "DEFECT_GROUP", f"{d.target}: {d.root_cause}",
                                    (owners_by_table or {}).get(table) or owner, due, "HIGH" if str(d.severity.value if hasattr(d.severity, 'value') else d.severity) == "CRITICAL" else "MEDIUM",
                                    {"run_id": run_id, "signature": d.rule_signature, "target": d.target, "violations": d.violation_count}))
        return made

    # ------------------------------------------------------------------ read
    def get(self, task_id: int) -> dict[str, Any]:
        for t in self._read()["tasks"]:
            if t["id"] == task_id:
                return self._decorate(t)
        raise TaskError(f"task {task_id} does not exist")

    def _decorate(self, task: dict[str, Any]) -> dict[str, Any]:
        overdue = bool(task.get("due") and task["status"] not in CLOSED and date.fromisoformat(task["due"]) < self._today())
        return {**task, "overdue": overdue}

    def list(self, owner: Optional[str] = None, status: Optional[str] = None, kind: Optional[str] = None, overdue: Optional[bool] = None, run_id: Optional[int] = None,
             open_only: bool = False) -> list[dict[str, Any]]:
        out = []
        for t in self._read()["tasks"]:
            d = self._decorate(t)
            if owner is not None and (d["owner"] or "").lower() != owner.lower():
                continue
            if status and d["status"] != status:
                continue
            if open_only and d["status"] in CLOSED:
                continue
            if kind and d["kind"] != kind:
                continue
            if overdue is not None and d["overdue"] != overdue:
                continue
            if run_id is not None and d["ref"].get("run_id") != run_id:
                continue
            out.append(d)
        return sorted(out, key=lambda d: (d["status"] in CLOSED, d["due"] or "9999-12-31", PRIORITIES.index(d["priority"]), d["id"]))

    def summary(self) -> dict[str, Any]:
        tasks = self.list()
        open_tasks = [t for t in tasks if t["status"] not in CLOSED]
        by_owner: dict[str, dict[str, int]] = {}
        for t in open_tasks:
            row = by_owner.setdefault(t["owner"] or "(unassigned)", {"open": 0, "overdue": 0})
            row["open"] += 1
            row["overdue"] += int(t["overdue"])
        return {"total": len(tasks), "open": len(open_tasks), "overdue": sum(1 for t in open_tasks if t["overdue"]), "unassigned": sum(1 for t in open_tasks if not t["owner"]),
                "by_status": {s: sum(1 for t in tasks if t["status"] == s) for s in STATUSES}, "by_owner": by_owner}

    # ------------------------------------------------------------------ change
    def update(self, task_id: int, user: str, status: Optional[str] = None, owner: Optional[str] = None, due: Optional[str] = None, priority: Optional[str] = None,
               note: Optional[str] = None) -> dict[str, Any]:
        if status is not None and status not in STATUSES:
            raise TaskError(f"status must be one of {', '.join(STATUSES)}")
        if priority is not None and priority not in PRIORITIES:
            raise TaskError(f"priority must be one of {', '.join(PRIORITIES)}")
        events: list[str] = []
        with self._lock:
            data = self._read()
            task = next((t for t in data["tasks"] if t["id"] == task_id), None)
            if task is None:
                raise TaskError(f"task {task_id} does not exist")
            changes = []
            if status is not None and status != task["status"]:
                changes.append(f"status {task['status']} -> {status}")
                task["status"] = status
                events.append("task_closed" if status in CLOSED else "task_status_changed")
            if owner is not None and (owner.strip() or None) != task["owner"]:
                changes.append(f"owner {task['owner'] or '(none)'} -> {owner.strip() or '(none)'}")
                task["owner"] = owner.strip() or None
                events.append("task_assigned")
            if due is not None:
                new_due = _parse_due(due) if due != "" else None
                if new_due != task["due"]:
                    changes.append(f"due {task['due'] or '(none)'} -> {new_due or '(none)'}")
                    task["due"] = new_due
            if priority is not None and priority != task["priority"]:
                changes.append(f"priority {task['priority']} -> {priority}")
                task["priority"] = priority
            if note or changes:
                task["notes"].append({"by": user, "at": _now(), "text": (note or "").strip()[:1000], "changes": changes})
            task["updated_at"] = _now()
            self._write(data)
        for ev in dict.fromkeys(events):
            self._notifier.send(ev, task)
        return self._decorate(task)

    def sync_defects(self, run_id: int, open_signatures: set[str], user: str = "system") -> list[dict[str, Any]]:
        """Close the defect tasks of a run whose defect is no longer open (fixed, then validated again)."""
        closed = []
        for t in self.list(kind="DEFECT_GROUP", run_id=run_id, open_only=True):
            if t["ref"].get("signature") not in open_signatures:
                closed.append(self.update(t["id"], user, status="DONE", note="closed automatically: the defect is no longer open in this run"))
        return closed

    def remind(self) -> list[dict[str, Any]]:
        """Tell the webhook about every overdue task (call it from a scheduler); returns them."""
        late = [t for t in self.list(overdue=True)]
        for t in late:
            self._notifier.send("task_overdue", t)
        return late
