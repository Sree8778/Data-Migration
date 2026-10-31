"""Personal-data retention, erasure and legal hold.

A migration copies customer master data many times: the landed snapshot, the staging and deduplicated files of every run, the rejected and duplicate sets, the
Cockpit workbook, the before-images of merges, the cell lineage in PostgreSQL and the sample values in the catalog. GDPR art. 5(1)(e) (storage limitation)
and art. 17 (erasure) mean those copies must not outlive their purpose, while SOX and ISO 27001 mean the EVIDENCE of what was done must stay. So erasure is
split along that line:

* removed: every file of a run that holds record values, the lineage rows of the run, and the examples inside the exclusion evidence;
* kept: counts, rules, hashes, sign-offs, delivery records, the certificate and the reconciliation reports (identifiers and numbers, no record values),
  and a `purged.json` that says who erased what, when and why. The audit trail records the call as well.

A legal hold (`legal_hold.json`) stops erasure of a run, by hand or by the retention sweep, until it is released: for a dispute, an audit or an investigation.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from src.db.enums import PiiClassification, RunStatus, TargetType
from src.db.models import CatColumn, CatDataSource, CatTable, MigCellLineage, MigMigrationRun
from src.services.pii_detector import classify
from src.services.profiler_service import _mask_shape

PURGED_FILE, HOLD_FILE = "purged.json", "legal_hold.json"
# What stays in a run folder: evidence, not data. Everything else in the folder is removed by an erasure.
EVIDENCE = {"report.json", "validation.json", "signoff.json", "load.json", "db_loads.json", "db_loads_pending.json", "source_snapshot.json", "dedup_config.json",
            "exclusions_applied.json", PURGED_FILE, HOLD_FILE}
EVIDENCE_PREFIXES = ("reconciliation_run_",)


class RetentionError(Exception):
    """The request cannot be carried out (a hold, a running run, no reason given...); the message is safe to show."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RetentionService:
    def __init__(self, session_factory: sessionmaker[Session], workspace: Path) -> None:
        self._sf = session_factory
        self._workspace = Path(workspace)

    def run_dir(self, run_id: int) -> Path:
        return self._workspace / "runs" / f"run_{run_id}"

    def table_dir(self, table_id: int) -> Path:
        return self._workspace / "tables" / str(table_id)

    # ------------------------------------------------------------------ legal hold
    def hold(self, run_id: int, user: str, reason: str) -> dict[str, Any]:
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise RetentionError("give a reason for the legal hold (it is kept in the record)")
        self._need_run(run_id)
        record = {"run_id": run_id, "held_by": user, "held_at": _now().isoformat(timespec="seconds"), "reason": reason}
        self.run_dir(run_id).mkdir(parents=True, exist_ok=True)
        (self.run_dir(run_id) / HOLD_FILE).write_text(json.dumps(record, indent=1), encoding="utf-8")
        return record

    def release(self, run_id: int) -> bool:
        path = self.run_dir(run_id) / HOLD_FILE
        existed = path.is_file()
        path.unlink(missing_ok=True)
        return existed

    def held(self, run_id: int) -> Optional[dict[str, Any]]:
        try:
            return json.loads((self.run_dir(run_id) / HOLD_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def purged(self, run_id: int) -> Optional[dict[str, Any]]:
        try:
            return json.loads((self.run_dir(run_id) / PURGED_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _need_run(self, run_id: int) -> MigMigrationRun:
        with self._sf() as s:
            run = s.get(MigMigrationRun, run_id)
            if run is None:
                raise RetentionError(f"run {run_id} does not exist")
            s.expunge(run)
            return run

    # ------------------------------------------------------------------ erasure of a run
    def data_files(self, run_id: int) -> list[Path]:
        """Every file of the run folder that holds record values (everything that is not evidence)."""
        base = self.run_dir(run_id)
        if not base.is_dir():
            return []
        out = []
        for path in sorted(base.rglob("*")):
            if path.is_file() and not (path.parent == base and (path.name in EVIDENCE or path.name.startswith(EVIDENCE_PREFIXES))):
                out.append(path)
        return out

    def erase_run(self, run_id: int, user: str, reason: str) -> dict[str, Any]:
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise RetentionError("give a reason for the erasure (it is kept in the record)")
        run = self._need_run(run_id)
        if run.status == RunStatus.RUNNING:
            raise RetentionError(f"run {run_id} is still running")
        if (hold := self.held(run_id)):
            raise RetentionError(f"run {run_id} is under legal hold since {hold['held_at']} ({hold['reason']}); release the hold first")
        files = self.data_files(run_id)
        removed_bytes, names = 0, []
        for path in files:
            try:
                removed_bytes += path.stat().st_size
                path.unlink()
                names.append(str(path.relative_to(self.run_dir(run_id))).replace("\\", "/"))
            except OSError as exc:
                raise RetentionError(f"cannot remove {path.name}: {exc}") from exc
        base = self.run_dir(run_id)
        for sub in sorted((p for p in base.rglob("*") if p.is_dir()), reverse=True):
            try:
                sub.rmdir()                                   # folders that are empty now
            except OSError:
                pass
        self._scrub_exclusion_evidence(run_id)
        with self._sf.begin() as s:
            lineage = s.execute(delete(MigCellLineage).where(MigCellLineage.run_id == run_id)).rowcount or 0
        record = {"run_id": run_id, "erased_by": user, "erased_at": _now().isoformat(timespec="seconds"), "reason": reason,
                  "files_removed": names, "bytes_removed": removed_bytes, "lineage_rows_removed": int(lineage),
                  "kept": "counts, rules, hashes, sign-offs, delivery records and the reconciliation reports (no record values)"}
        previous = self.purged(run_id)
        if previous:
            record["earlier_erasures"] = [*(previous.get("earlier_erasures") or []), {k: previous[k] for k in ("erased_by", "erased_at", "reason")}]
        (base).mkdir(parents=True, exist_ok=True)
        (base / PURGED_FILE).write_text(json.dumps(record, indent=1), encoding="utf-8")
        return record

    def _scrub_exclusion_evidence(self, run_id: int) -> None:
        """The exclusion evidence keeps its rules and counts; the example values it shows (names of test customers...) and the paths of deleted files go."""
        path = self.run_dir(run_id) / "exclusions_applied.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for rule in data.get("rules", []):
            rule["examples"] = []
        data["included_path"] = data["excluded_path"] = None
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------ erasure of a landed source table
    def erase_table(self, table_id: int, user: str, reason: str) -> dict[str, Any]:
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise RetentionError("give a reason for the erasure (it is kept in the record)")
        base = self.table_dir(table_id)
        removed, removed_bytes = [], 0
        for path in [*base.glob("source.*"), base / "profile.json"]:
            if path.is_file():
                removed_bytes += path.stat().st_size
                path.unlink()
                removed.append(path.name)
        original = base / "original"
        if original.is_dir():
            removed_bytes += sum(f.stat().st_size for f in original.rglob("*") if f.is_file())
            shutil.rmtree(original, ignore_errors=True)
            removed.append("original/")
        with self._sf.begin() as s:
            cleared = s.execute(update(CatColumn).where(CatColumn.table_id == table_id).values(sample_values=None)).rowcount or 0
        record = {"table_id": table_id, "erased_by": user, "erased_at": _now().isoformat(timespec="seconds"), "reason": reason, "files_removed": removed,
                  "bytes_removed": removed_bytes, "catalog_columns_cleared": int(cleared), "kept": "snapshot.json (checksum, row counts, options, detection)"}
        base.mkdir(parents=True, exist_ok=True)
        (base / "purged.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
        return record

    # ------------------------------------------------------------------ the retention sweep
    def due(self, retention_days: int) -> list[dict[str, Any]]:
        """Runs older than the retention period whose data is still there: -> [{run_id, completed_at, age_days, held}]."""
        cutoff = _now() - timedelta(days=retention_days)
        with self._sf() as s:
            rows = s.execute(select(MigMigrationRun.id, MigMigrationRun.completed_at, MigMigrationRun.created_at).where(MigMigrationRun.status != RunStatus.RUNNING)).all()
        out = []
        for run_id, completed, created in rows:
            moment = completed or created
            if moment is None or moment > cutoff or not self.data_files(run_id):
                continue
            out.append({"run_id": run_id, "finished_at": moment.isoformat(timespec="seconds"), "age_days": (_now() - moment).days,
                        "held": bool(self.held(run_id)), "files": len(self.data_files(run_id))})
        return sorted(out, key=lambda r: r["run_id"])

    def sweep(self, retention_days: int, user: str, dry_run: bool = True) -> dict[str, Any]:
        due = self.due(retention_days)
        erased, skipped = [], []
        for item in due:
            if item["held"]:
                skipped.append({**item, "why": "legal hold"})
            elif not dry_run:
                erased.append(self.erase_run(item["run_id"], user, f"retention policy: older than {retention_days} days"))
        return {"retention_days": retention_days, "dry_run": dry_run, "due": due, "erased": erased, "skipped": skipped}

    # ------------------------------------------------------------------ personal data that predates the detector
    def classify_existing(self) -> dict[str, Any]:
        """Apply the personal-data detector to every source column already in the catalog. A table registered before automatic classification has its personal
        columns marked INTERNAL and their sample values stored raw (names, e-mail addresses, tax numbers). This raises each such classification (never lowers
        one), turns the stored samples of RESTRICTED_PII columns into shape masks, and masks the stored profile screens of the tables concerned. Safe to repeat."""
        raised: list[str] = []
        masked_columns = 0
        masked_names: dict[int, set[str]] = {}
        with self._sf.begin() as s:
            rows = s.execute(select(CatColumn, CatTable.id, CatTable.table_name).join(CatTable, CatColumn.table_id == CatTable.id)
                             .join(CatDataSource, CatTable.source_id == CatDataSource.id).where(CatDataSource.target_type != TargetType.TARGET)).all()
            for col, table_id, table_name in rows:
                new = classify(col.column_name, col.sample_values or [], col.pii_classification)
                if new != col.pii_classification:
                    raised.append(f"{table_name}.{col.column_name}: {col.pii_classification.value} -> {new.value}")
                    col.pii_classification = new
                if new == PiiClassification.RESTRICTED_PII and col.sample_values:
                    shaped = [_mask_shape(str(v)) for v in col.sample_values]
                    if shaped != list(col.sample_values):
                        col.sample_values = shaped
                        masked_columns += 1
                if new in (PiiClassification.CONFIDENTIAL, PiiClassification.RESTRICTED_PII):
                    masked_names.setdefault(table_id, set()).add(col.column_name.lower())
        profiles = 0
        for table_id, names in masked_names.items():
            path = self.table_dir(table_id) / "profile.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            touched = False
            for column in data.get("columns", []):
                if column.get("column_name", "").lower() in names and not column.get("pii_masked"):
                    column["sample_values"] = [_mask_shape(str(v)) for v in column.get("sample_values", [])]
                    for top in column.get("top_values", []):
                        top["value"] = _mask_shape(str(top.get("value", "")))
                    column["pii_masked"] = touched = True
            if touched:
                path.write_text(json.dumps(data), encoding="utf-8")
                profiles += 1
        return {"classifications_raised": len(raised), "columns": raised[:200], "sample_columns_masked": masked_columns, "profile_screens_masked": profiles}

