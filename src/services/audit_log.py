"""Tamper-evident audit trail: an append-only, hash-chained log of who did what, when, and with what outcome.

Why this shape. SOX ITGC, ISO/IEC 27001:2022 control 8.15 (logging: records protected against tampering) and NIST SP 800-53 AU-9 (protection of audit
information) all ask that the trail of changes cannot be altered unnoticed, and ALCOA+ asks that every record is attributable and contemporaneous. Each entry
carries the hash of the entry before it, so changing, removing or inserting any entry breaks every hash after it and `verify()` names the first broken
entry. With `MIGRATION_AUDIT_HMAC_KEY` set the hash is an HMAC: somebody who can edit the file but does not hold the key cannot recompute the chain either.

What it does NOT do: a chain cannot show that the last entries were cut off. For that, copy `head()` (sequence number and hash) somewhere the application
cannot write (a SIEM, a ticket, a WORM bucket) from time to time; `verify(expected_head=...)` then also proves nothing after it was removed.

An entry holds identifiers and outcomes, never request bodies or record values: the trail must not become a second copy of the personal data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger("migration.audit")
GENESIS = "0" * 64
FILE_NAME = "audit.jsonl"


class AuditUnavailable(RuntimeError):
    """The trail cannot be written; an action that must be on record is not allowed to proceed."""


def _canonical(entry: dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class AuditLog:
    def __init__(self, directory: Path, hmac_key: Optional[str] = None) -> None:
        self._dir = Path(directory)
        self._file = self._dir / FILE_NAME
        self._key = hmac_key.encode("utf-8") if hmac_key else None
        self._lock = threading.Lock()
        self._seq = 0
        self._last = GENESIS
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._file

    @property
    def keyed(self) -> bool:
        return self._key is not None

    # ------------------------------------------------------------------ hashing
    def _digest(self, prev: str, entry: dict[str, Any]) -> str:
        message = (prev + _canonical(entry)).encode("utf-8")
        if self._key:
            return hmac.new(self._key, message, hashlib.sha256).hexdigest()
        return hashlib.sha256(message).hexdigest()

    # ------------------------------------------------------------------ writing
    def _load_tail(self) -> None:
        """Continue the chain of an existing file (the last line carries the sequence number and the hash to chain to)."""
        self._loaded = True
        try:
            with open(self._file, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 65536))
                tail = fh.read().splitlines()
        except OSError:
            return
        for raw in reversed(tail):
            try:
                last = json.loads(raw)
                self._seq, self._last = int(last["seq"]), str(last["hash"])
                return
            except (ValueError, KeyError):
                continue

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """Add one entry; returns it with its sequence number and hash. Raises AuditUnavailable when it cannot be written durably."""
        with self._lock:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                if not self._loaded:
                    self._load_tail()
                entry = {"seq": self._seq + 1, "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **event, "prev": self._last}
                entry["hash"] = self._digest(self._last, {k: v for k, v in entry.items() if k != "hash"})
                line = _canonical(entry) + "\n"
                with open(self._file, "ab") as fh:
                    fh.write(line.encode("utf-8"))
                    fh.flush()
                    os.fsync(fh.fileno())              # on disk before the action it records goes ahead
            except OSError as exc:
                raise AuditUnavailable(f"the audit trail cannot be written: {exc}") from exc
            self._seq, self._last = entry["seq"], entry["hash"]
        log.info("%s", _canonical({k: v for k, v in entry.items() if k not in ("prev",)}))   # the same record for a SIEM that reads stdout
        return entry

    # ------------------------------------------------------------------ reading
    def entries(self) -> Iterator[dict[str, Any]]:
        try:
            with open(self._file, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        yield json.loads(line)
        except FileNotFoundError:
            return

    def head(self) -> dict[str, Any]:
        """Where the chain ends now: copy this somewhere the application cannot write, to be able to prove later that nothing was cut off."""
        with self._lock:
            if not self._loaded:
                self._load_tail()
            return {"seq": self._seq, "hash": self._last}

    def verify(self, expected_head: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Re-compute the whole chain. `ok` is false at the first entry that was altered, removed or inserted (`first_bad_seq`)."""
        prev, count, seq, bad, reason = GENESIS, 0, 0, None, None
        try:
            for entry in self.entries():
                count += 1
                seq += 1
                shown = entry.get("seq")
                body = {k: v for k, v in entry.items() if k != "hash"}
                if shown != seq:
                    bad, reason = shown, f"sequence jumps: expected {seq}"
                elif entry.get("prev") != prev:
                    bad, reason = shown, "does not follow the entry before it"
                elif not hmac.compare_digest(str(entry.get("hash", "")), self._digest(prev, body)):
                    bad, reason = shown, "its content does not match its hash"
                if bad is not None:
                    break
                prev = entry["hash"]
        except (ValueError, TypeError) as exc:
            bad, reason = count, f"an entry cannot be read ({exc})"
        result: dict[str, Any] = {"ok": bad is None, "entries": count, "head": {"seq": seq if bad is None else None, "hash": prev if bad is None else None},
                                  "keyed": self.keyed, "first_bad_seq": bad, "reason": reason}
        if bad is None and expected_head:
            if (expected_head.get("seq"), expected_head.get("hash")) == (seq, prev):
                result["matches_expected_head"] = True
            else:
                # the chain is intact, but it is not the chain that was anchored: either entries were cut off, or the anchor is from later than this file
                result.update(ok=False, matches_expected_head=False, reason=f"the chain ends at entry {seq}, not at the anchored entry {expected_head.get('seq')}")
        return result

    def read(self, limit: int = 100, offset: int = 0, user: Optional[str] = None, path_contains: Optional[str] = None,
             outcome: Optional[str] = None) -> tuple[list[dict[str, Any]], int]:
        """The newest entries first, optionally filtered. -> (entries, matching total)."""
        rows = [e for e in self.entries()
                if (not user or e.get("user") == user) and (not path_contains or path_contains in str(e.get("path", ""))) and (not outcome or e.get("outcome") == outcome)]
        rows.reverse()
        return rows[offset: offset + limit], len(rows)
