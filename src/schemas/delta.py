"""What changed between two runs of the same migration: the basis of delta loads."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class DeltaCounts(BaseModel):
    new: int  # valid now, not valid in the baseline
    changed: int  # valid in both, but at least one target value differs
    unchanged: int  # valid in both and identical: nothing to deliver
    removed: int  # valid in the baseline, not valid now (gone from the source, rejected, a duplicate, or excluded)
    current_valid: int
    baseline_valid: int


class DeltaReport(BaseModel):
    run_id: int
    baseline_run_id: int
    mapping_set_id: int
    baseline_mapping_set_id: int
    checked_at: datetime
    counts: DeltaCounts
    removed_reasons: dict[str, int] = Field(default_factory=dict)  # NOW_REJECTED / NOW_DUPLICATE / NOT_IN_CURRENT_VALID -> records
    examples: dict[str, list[str]] = Field(default_factory=dict)  # a few source keys per kind
    delta_path: Optional[str] = None  # the valid records to deliver (new + changed), with _DELTA_KIND
    removed_path: Optional[str] = None  # the keys that disappeared, with _WHY
    columns_compared: int = 0
