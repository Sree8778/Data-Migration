"""The review of possible duplicates: what the engine could not decide, and what the people decided.

The engine works in three outcomes (merge / possible duplicate / different). The middle one is a question for a person, and a question needs a place to be answered
and an answer that sticks. Decisions are kept per SOURCE TABLE, keyed by the records' primary-key values, so they survive re-runs and a re-landed file: the second
mock load does not ask again what the first one settled. Every run feeds them back into the engine (`must_link` / `cannot_link`), and the audit trail records
who decided (the call itself is on the trail, and each decision keeps who, when and why).

Two lists need people:
* **possible duplicates**: pairs below the merge threshold but above the review threshold. Nothing was merged; both records load unless a person says merge;
* **name-only merges**: records merged on the name alone (no address to confirm it). Merged and dropped from the load, so a person should be able to say "no, these are two".
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb

from src.services._duckdb_util import bounded_connection, lit, q

DECISIONS_FILE = "duplicate_decisions.json"
MERGE, DISTINCT = "MERGE", "DISTINCT"


class ReviewError(Exception):
    """The decision cannot be recorded (an unknown value, a pair of one record...); the message is safe to show."""


def pair_key(a: str, b: str) -> str:
    x, y = sorted((str(a), str(b)))
    return f"{x}|{y}"


class DuplicateReview:
    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)
        self._lock = threading.RLock()

    def _path(self, table_id: int) -> Path:
        return self._workspace / "tables" / str(table_id) / DECISIONS_FILE

    # ------------------------------------------------------------------ the decisions
    def decisions(self, table_id: int) -> dict[str, dict[str, Any]]:
        with self._lock:
            try:
                return json.loads(self._path(table_id).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}

    def decide(self, table_id: int, run_id: int, items: list[dict[str, Any]], user: str) -> dict[str, int]:
        """Record decisions; a later decision about the same pair replaces the earlier one (the earlier one is kept in `history`). `CLEAR` withdraws one."""
        counts = {MERGE: 0, DISTINCT: 0, "CLEAR": 0}
        with self._lock:
            data = self.decisions(table_id)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for item in items:
                a, b, what = str(item["pk_a"]).strip(), str(item["pk_b"]).strip(), item["decision"]
                if not a or not b or a == b:
                    raise ReviewError("a decision is about two different records")
                if what not in (MERGE, DISTINCT, "CLEAR"):
                    raise ReviewError(f"decision must be MERGE, DISTINCT or CLEAR, not '{what}'")
                key = pair_key(a, b)
                old = data.get(key)
                if what == "CLEAR":
                    data.pop(key, None)
                else:
                    entry = {"pk_a": min(a, b), "pk_b": max(a, b), "decision": what, "by": user, "at": now, "run_id": run_id, "reason": (item.get("reason") or "").strip()[:300]}
                    if old:
                        entry["history"] = [*old.get("history", []), {k: old.get(k) for k in ("decision", "by", "at", "reason")}]
                    data[key] = entry
                counts[what] += 1
            self._save(table_id, data)
        return counts

    def _save(self, table_id: int, data: dict) -> None:
        path = self._path(table_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)

    def pairs(self, table_id: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """(must_link, cannot_link) for the engine."""
        must, cannot = [], []
        for d in self.decisions(table_id).values():
            (must if d["decision"] == MERGE else cannot).append((d["pk_a"], d["pk_b"]))
        return must, cannot

    # ------------------------------------------------------------------ what still needs a person
    def possible(self, run_dir: Path, table_id: int, limit: int = 100, offset: int = 0, undecided_only: bool = False) -> dict[str, Any]:
        """The possible-duplicate pairs of a run (best match first), each with the decision already taken, if any."""
        path = Path(run_dir) / "possible_duplicates.parquet"
        decided = self.decisions(table_id)
        if not path.is_file():
            return {"total": 0, "undecided": 0, "items": []}
        with bounded_connection(256) as con:
            rows = con.execute(f"SELECT pk_a, pk_b, name_a, name_b, address_a, address_b, similarity FROM read_parquet({lit(path.as_posix())}) ORDER BY similarity DESC, pk_a").fetchall()
        items = []
        for pk_a, pk_b, name_a, name_b, address_a, address_b, similarity in rows:
            d = decided.get(pair_key(pk_a, pk_b))
            items.append({"pk_a": pk_a, "pk_b": pk_b, "name_a": name_a, "name_b": name_b, "address_a": address_a or "", "address_b": address_b or "", "similarity": similarity,
                          "decision": d["decision"] if d else None, "decided_by": d["by"] if d else None})
        undecided = sum(1 for i in items if i["decision"] is None)
        if undecided_only:
            items = [i for i in items if i["decision"] is None]
        return {"total": len(rows), "undecided": undecided, "items": items[offset: offset + limit]}

    def merges(self, run_dir: Path, table_id: int, strength: Optional[str] = "NAME_ONLY", limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Records the run merged away, with the record each was merged into, filtered by how strong the evidence was (default: the name alone)."""
        base = Path(run_dir)
        deduplicated = base / "deduplicated.parquet"
        if not deduplicated.is_file():
            return {"total": 0, "items": []}
        try:
            config = json.loads((base / "dedup_config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        pk, name = config.get("pk_column", "_SRC_PK"), config.get("name_column")
        decided = self.decisions(table_id)
        src = lit(deduplicated.as_posix())
        where = "c.is_duplicate_child" + (f" AND c.dedup_match_strength = {lit(strength)}" if strength else "")
        name_sql = (f"CAST(c.{q(name)} AS VARCHAR), CAST(m.{q(name)} AS VARCHAR)" if name else "NULL, NULL")
        with bounded_connection(256) as con:
            rows = con.execute(
                f"SELECT CAST(c.{q(pk)} AS VARCHAR), CAST(m.{q(pk)} AS VARCHAR), {name_sql}, c.dedup_match_type, c.dedup_match_strength FROM read_parquet({src}) c "
                f"JOIN read_parquet({src}) m ON CAST(m.{q(pk)} AS VARCHAR) = c.surviving_master_pk WHERE {where} ORDER BY 1").fetchall()
        items = []
        for child, master, child_name, master_name, match_type, level in rows:
            d = decided.get(pair_key(child, master))
            items.append({"child_pk": child, "master_pk": master, "child_name": child_name, "master_name": master_name, "match_type": match_type, "strength": level,
                          "decision": d["decision"] if d else None, "decided_by": d["by"] if d else None})
        return {"total": len(items), "items": items[offset: offset + limit]}
