"""Migration run orchestrator: transformation + deduplication with an audit trail in
`mig_migration_runs` / `mig_cell_lineage` (existing Module 1 tables, schema untouched).

Column mapping (the locked schema predates this module's naming):
    source_records_count      -> records_extracted
    transformed_records_count -> records_transformed   (rows with no failed cell)
    failed_records_count      -> records_failed        (rows with >= 1 failed cell)
    status SUCCESS            -> RunStatus.SUCCEEDED
`records_loaded` stays 0: nothing is loaded into SAP at this stage.
The run stays in phase TRANSFORM (the enum has no dedup phase); later modules advance it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from src.db.enums import MappingStatus, MigrationPhase, RunStatus
from src.db.models import MapTableRule, MigMigrationRun
from src.schemas.execution import DedupConfig, DedupReport, RunReport, TransformationResult
from src.services.deduplication_engine import DeduplicationEngine
from src.services.metadata_service import MappingValidationError, NotFoundError
from src.services.transformation_engine import TransformationEngine


class ExecutionError(Exception):
    """The run failed; the `mig_migration_runs` row is marked FAILED. `run_id` identifies it."""

    def __init__(self, run_id: int, message: str) -> None:
        super().__init__(f"run {run_id} failed: {message}")
        self.run_id = run_id


class ExecutionOrchestrator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        transformation_engine: TransformationEngine,
        dedup_engine: Optional[DeduplicationEngine] = None,
        output_dir: str | Path = "staging_output",
        max_failed_ratio: Optional[float] = None,
    ) -> None:
        self._session_factory = session_factory
        self._transformer = transformation_engine
        self._deduper = dedup_engine or DeduplicationEngine()
        self._output_dir = Path(output_dir)
        self._max_failed_ratio = max_failed_ratio

    def execute_run(
        self,
        mapping_set_id: int,
        source_path: str | Path,
        wave_name: str = "WAVE_1",
        dedup_config: Optional[DedupConfig] = None,
        **transform_options: Any,
    ) -> RunReport:
        """Transform (and optionally deduplicate) a source file under a new migration run.

        `transform_options` are passed to `TransformationEngine.transform` (passthrough_columns,
        lookup_fallback, lineage_mode, ...). Raises ExecutionError if anything fails; a failed-record
        ratio above `max_failed_ratio` yields a report with status FAILED instead.
        """
        run_id, started_at = self._start_run(mapping_set_id, wave_name)
        run_dir = self._output_dir / f"run_{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        transformation: Optional[TransformationResult] = None
        dedup: Optional[DedupReport] = None
        try:
            transformation = self._transformer.transform(
                mapping_set_id, source_path, run_dir / "staging.parquet", run_id=run_id, **transform_options
            )
            self._update_run(
                run_id,
                records_extracted=transformation.source_records,
                records_transformed=transformation.ok_records,
                records_failed=transformation.failed_records,
            )
            if dedup_config is not None:
                dedup = self._deduper.deduplicate(transformation.output_path, run_dir / "deduplicated.parquet", dedup_config)

            message = None
            status = RunStatus.SUCCEEDED
            if (
                self._max_failed_ratio is not None
                and transformation.source_records
                and transformation.failed_records / transformation.source_records > self._max_failed_ratio
            ):
                status = RunStatus.FAILED
                message = (
                    f"failed-record ratio {transformation.failed_records / transformation.source_records:.2%} "
                    f"exceeds limit {self._max_failed_ratio:.2%}"
                )
        except Exception as exc:
            completed_at = self._finish_run(run_id, RunStatus.FAILED)
            (run_dir / "report.json").write_text(
                json.dumps({"run_id": run_id, "status": "FAILED", "error": str(exc),
                            "completed_at": completed_at.isoformat()}, indent=2),
                encoding="utf-8",
            )
            raise ExecutionError(run_id, str(exc)) from exc

        completed_at = self._finish_run(run_id, status)
        report = RunReport(
            run_id=run_id,
            mapping_set_id=mapping_set_id,
            wave_name=wave_name,
            status=status.value,
            source_records=transformation.source_records,
            transformed_records=transformation.ok_records,
            failed_records=transformation.failed_records,
            lineage_rows=transformation.lineage_rows,
            started_at=started_at,
            completed_at=completed_at,
            staging_path=transformation.output_path,
            deduplicated_path=dedup.output_path if dedup else None,
            transformation=transformation,
            deduplication=dedup,
            report_path=str(run_dir / "report.json"),
            message=message,
        )
        (run_dir / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return report

    # ------------------------------------------------------------------ run bookkeeping
    def _start_run(self, mapping_set_id: int, wave_name: str) -> tuple[int, datetime]:
        started = datetime.now(timezone.utc)
        with self._session_factory.begin() as session:
            mapping = session.get(MapTableRule, mapping_set_id)
            if mapping is None:
                raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
            if mapping.status == MappingStatus.REJECTED:
                raise MappingValidationError(f"mapping set {mapping_set_id} is REJECTED and cannot be executed")
            run = MigMigrationRun(
                mapping_set_id=mapping_set_id,
                wave_name=wave_name,
                current_phase=MigrationPhase.TRANSFORM,
                status=RunStatus.RUNNING,
                started_at=started,
            )
            session.add(run)
            session.flush()
            return run.id, started

    def _update_run(self, run_id: int, **values: Any) -> None:
        with self._session_factory.begin() as session:
            session.execute(update(MigMigrationRun).where(MigMigrationRun.id == run_id).values(**values))

    def _finish_run(self, run_id: int, status: RunStatus) -> datetime:
        completed = datetime.now(timezone.utc)
        self._update_run(run_id, status=status, completed_at=completed)
        return completed
