"""Archive register for mapping sets, and an audit log of deleted rules.

The catalog schema is locked and the mapping lifecycle (DRAFT -> SUBMITTED -> APPROVED / REJECTED) is a governance
state machine, so "this set is obsolete" is not a status. Archiving only HIDES a set (from the default listing and
from the Studio); nothing is deleted, the set keeps its runs, lineage and approval, and it can be restored.
Both files live in the workspace, are written atomically, and record who did it, when and why.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MappingArchive:
    def __init__(self, directory: Path) -> None:
        self._file = directory / "mapping_archive.json"
        self._log = directory / "rule_deletions.jsonl"
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict]:
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def entries(self) -> dict[int, dict]:
        with self._lock:
            return {int(k): v for k, v in self._read().items()}

    def get(self, mapping_set_id: int) -> Optional[dict]:
        return self.entries().get(mapping_set_id)

    def archive(self, mapping_set_id: int, user: str, reason: str) -> dict:
        with self._lock:
            data = self._read()
            data[str(mapping_set_id)] = entry = {"archived_by": user, "archived_at": _now(), "reason": reason}
            self._write(data)
            return entry

    def restore(self, mapping_set_id: int) -> bool:
        with self._lock:
            data = self._read()
            removed = data.pop(str(mapping_set_id), None) is not None
            if removed:
                self._write(data)
            return removed

    def _write(self, data: dict) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self._file)

    def log_rule_deletion(self, user: str, record: dict) -> None:
        with self._lock:
            self._log.parent.mkdir(parents=True, exist_ok=True)
            with self._log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"deleted_by": user, "deleted_at": _now(), **record}) + "\n")
