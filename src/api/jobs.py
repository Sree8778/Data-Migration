"""Background jobs for long-running calls (AI mapping on a CPU-bound local LLM).

Jobs live in memory in the API process: a single worker thread runs them one at a time (one LLM,
one CPU), the browser polls `GET /api/mapping/jobs/{id}`, and a restart of the server forgets them
(the finished DRAFT mapping sets themselves are in the database). A job for a table that is
already queued or running is reused instead of being duplicated.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from src.api.errors import error_info

ACTIVE = ("queued", "running", "cancelling")


class JobCancelled(Exception):
    """Raised inside a job (from a progress callback) when the user cancelled it."""


class Job:
    def __init__(self, kind: str, source_table_id: int, started_by: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.source_table_id = source_table_id
        self.started_by = started_by
        self.status = "queued"
        self.created_at = datetime.now(timezone.utc)
        self.finished_at: Optional[datetime] = None
        self.current = 0
        self.total = 0
        self.column: Optional[str] = None
        self.message = "Queued"
        self.result: Optional[dict[str, Any]] = None
        self.error: Optional[dict[str, Any]] = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- progress (called by the worker)
    def report(self, event: str, position: int, total: int, column: Optional[str]) -> None:
        """Progress callback for MappingOrchestrator; honours cancellation at every step."""
        if self._cancel.is_set():
            raise JobCancelled()
        with self._lock:
            self.total = total
            if event == "start":
                self.message = f"Preparing candidates for {total} columns"
            elif event == "column":
                self.current, self.column = position - 1, column
                self.message = f"Evaluating column {position} of {total}: {column}"
            elif event == "persist":
                self.current, self.column = total, None
                self.message = "Validating rules and saving the draft mapping"

    def request_cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            if self.status in ("queued", "running"):
                self.status = "cancelling"
                self.message = "Cancelling after the current column..."

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.id, "kind": self.kind, "source_table_id": self.source_table_id,
                "started_by": self.started_by, "status": self.status, "created_at": self.created_at,
                "finished_at": self.finished_at,
                "progress": {"current": self.current, "total": self.total, "column": self.column, "message": self.message},
                "result": self.result, "error": self.error,
            }

    def _finish(self, status: str, message: str, **fields: Any) -> None:
        with self._lock:
            self.status, self.message = status, message
            self.finished_at = datetime.now(timezone.utc)
            for key, value in fields.items():
                setattr(self, key, value)


class JobManager:
    def __init__(self, max_kept: int = 50) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="migration-job")
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_kept = max_kept

    def submit(self, kind: str, source_table_id: int, user: str, fn: Callable[[Job], dict[str, Any]]) -> tuple[Job, bool]:
        """Queue `fn(job)`; returns (job, created). An active job for the same table is reused."""
        with self._lock:
            for job in self._jobs.values():
                if job.kind == kind and job.source_table_id == source_table_id and job.status in ACTIVE:
                    return job, False
            job = Job(kind, source_table_id, user)
            self._jobs[job.id] = job
            while len(self._jobs) > self._max_kept:  # forget the oldest finished jobs only
                oldest = next((j for j in self._jobs.values() if j.status not in ACTIVE), None)
                if oldest is None:
                    break
                del self._jobs[oldest.id]
        self._executor.submit(self._run, job, fn)
        return job, True

    @staticmethod
    def _run(job: Job, fn: Callable[[Job], dict[str, Any]]) -> None:
        if job._cancel.is_set():
            job._finish("cancelled", "Cancelled before it started")
            return
        with job._lock:
            job.status, job.message = "running", "Starting"
        try:
            result = fn(job)
            job._finish("succeeded", "Done", result=result)
        except JobCancelled:
            job._finish("cancelled", "Cancelled; nothing was saved")
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as a structured error
            _, code, detail = error_info(exc)
            job._finish("failed", f"Failed: {detail}", error={"code": code, "detail": detail})

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, source_table_id: Optional[int] = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j for j in reversed(jobs) if source_table_id is None or j.source_table_id == source_table_id]

    def shutdown(self) -> None:
        for job in self.list():
            if job.status in ACTIVE:
                job.request_cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
