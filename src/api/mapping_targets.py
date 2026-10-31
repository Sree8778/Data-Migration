"""Which destination a mapping set is pointed at.

A mapping set maps one source table to the SAP business-partner fields. WHERE the result is delivered (SAP, a database, Firebase) used to be chosen at the very last step. Choosing
it with the mapping set lets the pre-submission check apply that destination's own rules before anybody is asked to approve. Nothing about the rules or the data changes: this
only records the name of a saved TARGET connection (the default, no entry, is SAP). The file lives in the workspace, is written atomically, and records who chose it and when.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class MappingTargets:
    def __init__(self, directory: Path) -> None:
        self._file = directory / "mapping_targets.json"
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict]:
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def record(self, mapping_set_id: int) -> Optional[dict]:
        with self._lock:
            return self._read().get(str(mapping_set_id))

    def get(self, mapping_set_id: int) -> Optional[str]:
        entry = self.record(mapping_set_id)
        return entry["connection"] if entry else None

    def set(self, mapping_set_id: int, connection: Optional[str], user: str) -> Optional[dict]:
        """Point the set at a target connection; None goes back to the default (SAP)."""
        with self._lock:
            data = self._read()
            if connection:
                data[str(mapping_set_id)] = entry = {"connection": connection, "set_by": user, "set_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            else:
                data.pop(str(mapping_set_id), None)
                entry = None
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self._file)
        return entry
