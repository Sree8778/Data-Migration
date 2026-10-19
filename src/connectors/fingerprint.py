"""A content fingerprint of a table: proof that the VALUES in a database are the values that were sent, not just the same number of rows.

A row count cannot tell a delivery that arrived intact from one where an umlaut became '?', a long name was cut, or a value was re-typed on the
way in. The fingerprint hashes every cell together with its row key and adds the hashes up per column, so it does not depend on the order the
database returns the rows in, it costs one pass over the data on each side, and a mismatch names the COLUMNS that differ.

Values are compared as the exact text of what they are (`to_text`), with NULL and the empty string treated as the same: Oracle stores an empty
string as NULL, so it could never be told apart there, and for every other database the two mean the same for a migration.
"""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

_MASK = (1 << 64) - 1


def _norm(value: Any) -> str:
    from src.connectors.base import to_text  # imported here: base.py imports this module for the delivery check

    return to_text(value)


class Fingerprint:
    def __init__(self, columns: Sequence[str], key_positions: Sequence[int]) -> None:
        if not columns or not key_positions:
            raise ValueError("a fingerprint needs columns and at least one key column")
        self.columns = list(columns)
        self._keys = list(key_positions)
        self.rows = 0
        self.sums = [0] * len(self.columns)

    def add(self, row: Sequence[Any]) -> None:
        values = [_norm(v) for v in row]
        if len(values) != len(self.columns):
            raise ValueError(f"a row has {len(values)} value(s), the fingerprint expects {len(self.columns)}")
        key = "\x1e".join(values[i] for i in self._keys)
        for i, v in enumerate(values):
            h = hashlib.blake2b(f"{key}\x1f{i}\x1f{v}".encode("utf-8", "surrogatepass"), digest_size=8).digest()
            self.sums[i] = (self.sums[i] + int.from_bytes(h, "big")) & _MASK
        self.rows += 1

    @property
    def cells(self) -> int:
        return self.rows * len(self.columns)

    @property
    def digest(self) -> str:
        """One short value for the whole table, for the record and the certificate."""
        h = hashlib.blake2b(digest_size=8)
        h.update(str(self.rows).encode())
        for s in self.sums:
            h.update(s.to_bytes(8, "big"))
        return h.hexdigest()

    def differing_columns(self, other: "Fingerprint") -> list[str]:
        """Columns whose values differ (all of them when the row counts differ: the sums are then not comparable)."""
        if self.columns != other.columns:
            raise ValueError("the two fingerprints cover different columns")
        if self.rows != other.rows:
            return list(self.columns)
        return [c for c, a, b in zip(self.columns, self.sums, other.sums) if a != b]
