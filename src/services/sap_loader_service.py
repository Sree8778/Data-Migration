"""SAP ingestion loop, strictly behind the governance gate.

`load(run_id, valid_parquet)`:
  1. Gate: the run must pass `GovernanceService.evaluate_readiness` (phase VALIDATE or later,
     run SUCCEEDED, mapping APPROVED, zero open CRITICAL defects) - checked BEFORE any row is read -
     and must still be in phase VALIDATE, so a run can never be loaded twice.
  2. The run is claimed atomically (VALIDATE/SUCCEEDED -> LOAD/RUNNING); a concurrent loader loses.
  3. Records stream out of `valid.parquet` in batches (default 100). Each batch is sent through the
     `SapClient`, and the batch's lineage + counters are committed in one transaction:
       success -> `target_pk_value` = SAP partner number, `loaded_successfully` = True
       failure -> the exact SAP message(s) go to `error_message`, `records_failed` += 1
     In addition one outcome row (target column `BUT000.PARTNER`) is written per record, so every
     record has an audit row even when lineage was recorded in EXCEPTIONS/NONE mode.
  4. Final state: phase LOAD (the schema's enum value for "load to SAP"); status SUCCEEDED when every
     record loaded, FAILED when any did not (or the load aborted). Retrying a partly loaded run
     is deliberately not supported here: it needs an explicit resume design, not a blind re-send.

The pre-existing `records_failed` (transform-stage failures) is preserved; load failures are added.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import insert, text, update
from sqlalchemy.orm import Session, sessionmaker

from src.db.enums import MigrationPhase, RunStatus
from src.db.models import MigCellLineage, MigMigrationRun
from src.schemas.sap import BapiRet2, BusinessPartnerRecord, LoadResult, SapRecordResult
from src.services._duckdb_util import bounded_connection, lit
from src.services.bp_model import BpConfig, iter_bp_records
from src.services.governance_service import GateBlockedError, GovernanceService
from src.services.sap_client import SapClient

PARTNER_COLUMN = "BUT000.PARTNER"
_ERROR_SAMPLES = 20

_UPDATE_LINEAGE = text(
    """
    UPDATE mig_cell_lineage l
       SET target_pk_value = v.bp,
           loaded_successfully = v.ok,
           error_message = COALESCE(v.err, l.error_message)
      FROM unnest(CAST(:pks AS text[]), CAST(:bps AS text[]), CAST(:oks AS boolean[]), CAST(:errs AS text[]))
           AS v(pk, bp, ok, err)
     WHERE l.run_id = :run_id AND l.source_pk_value = v.pk
    """
)


class LoadStateError(Exception):
    """The run is not in a loadable state (already loaded, claimed elsewhere...)."""


class LoadInputError(Exception):
    """The supplied file is not a clean Module 5 `valid.parquet`."""


class SapLoaderService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        governance: GovernanceService,
        sap_client: SapClient,
        batch_size: int = 100,
        bp_config: BpConfig = BpConfig(),
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._session_factory = session_factory
        self._governance = governance
        self._client = sap_client
        self._batch_size = batch_size
        self._bp_config = bp_config

    # ------------------------------------------------------------------ gate
    def is_ready_for_load(self, run_id: int) -> bool:
        return self._governance.evaluate_readiness(run_id).ready

    # ------------------------------------------------------------------ load
    def load(self, run_id: int, valid_parquet: str | Path) -> LoadResult:
        readiness = self._governance.evaluate_readiness(run_id)  # raises NotFoundError for unknown runs
        if not readiness.ready:
            raise GateBlockedError(readiness)  # nothing has been read or sent
        path = Path(valid_parquet)
        self._check_input(path)
        self._claim(run_id)

        started = datetime.now(timezone.utc)
        attempted = loaded = failed = batches = 0
        samples: list[str] = []
        try:
            batch: list[BusinessPartnerRecord] = []
            for record in iter_bp_records(path, self._bp_config):
                batch.append(record)
                if len(batch) == self._batch_size:
                    ok, bad = self._process(run_id, batch, samples)
                    attempted, loaded, failed, batches = attempted + len(batch), loaded + ok, failed + bad, batches + 1
                    batch = []
            if batch:
                ok, bad = self._process(run_id, batch, samples)
                attempted, loaded, failed, batches = attempted + len(batch), loaded + ok, failed + bad, batches + 1
        except BaseException:
            self._finish(run_id, RunStatus.FAILED)  # partial progress stays recorded, batch by batch
            raise

        status = RunStatus.SUCCEEDED if failed == 0 else RunStatus.FAILED
        completed = self._finish(run_id, status)
        return LoadResult(
            run_id=run_id, status=status.value, attempted=attempted, loaded=loaded, failed=failed,
            batches=batches, batch_size=self._batch_size, error_samples=samples,
            started_at=started, completed_at=completed,
        )

    # ------------------------------------------------------------------ steps
    @staticmethod
    def _check_input(path: Path) -> None:
        """Refuse anything that is not a clean valid payload (no rejects, duplicates or failed rows)."""
        if not path.is_file():
            raise FileNotFoundError(path)
        source = f"read_parquet({lit(path.as_posix())})"
        with bounded_connection(256) as con:
            columns = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}
            problems: list[str] = []
            if "_VALIDATION_ERRORS" in columns:
                problems.append("it has a _VALIDATION_ERRORS column (rejected rows)")
            counted = {
                "duplicate children": ("is_duplicate_child", "coalesce(is_duplicate_child, false)"),
                "rows with failed transformation": ("_TRANSFORM_STATUS", "coalesce(_TRANSFORM_STATUS, 'OK') <> 'OK'"),
            }
            for label, (column, predicate) in counted.items():
                if column in columns:
                    n = con.execute(f"SELECT count(*) FROM {source} WHERE {predicate}").fetchone()[0]
                    if n:
                        problems.append(f"{n} {label}")
            if problems:
                raise LoadInputError(f"{path.name} is not a clean valid payload: " + "; ".join(problems))

    def _claim(self, run_id: int) -> None:
        with self._session_factory.begin() as session:
            claimed = session.execute(
                update(MigMigrationRun)
                .where(MigMigrationRun.id == run_id, MigMigrationRun.current_phase == MigrationPhase.VALIDATE,
                       MigMigrationRun.status == RunStatus.SUCCEEDED)
                .values(current_phase=MigrationPhase.LOAD, status=RunStatus.RUNNING)
            ).rowcount
            if claimed != 1:
                run = session.get(MigMigrationRun, run_id)
                raise LoadStateError(
                    f"run {run_id} is in phase {run.current_phase.value}/{run.status.value}; "
                    "only a run in phase VALIDATE can be loaded, and only once"
                )

    def _finish(self, run_id: int, status: RunStatus) -> datetime:
        completed = datetime.now(timezone.utc)
        with self._session_factory.begin() as session:
            session.execute(update(MigMigrationRun).where(MigMigrationRun.id == run_id)
                            .values(status=status, completed_at=completed))
        return completed

    def _process(self, run_id: int, batch: list[BusinessPartnerRecord], samples: list[str]) -> tuple[int, int]:
        sendable = [r for r in batch if not r.blocking_issues]
        returned = {res.source_pk: res for res in self._client.create_business_partners(sendable)} if sendable else {}
        results: list[SapRecordResult] = []
        for rec in batch:
            if rec.blocking_issues:
                results.append(SapRecordResult(source_pk=rec.source_pk, messages=[BapiRet2(
                    TYPE="E", ID="MIG", NUMBER="001",
                    MESSAGE="not sent to SAP: " + "; ".join(rec.blocking_issues))]))
            elif rec.source_pk in returned:
                results.append(returned[rec.source_pk])
            else:
                results.append(SapRecordResult(source_pk=rec.source_pk, messages=[BapiRet2(
                    TYPE="E", ID="MIG", NUMBER="002", MESSAGE="SAP client returned no result for this record")]))

        ok = sum(1 for r in results if r.success)
        bad = len(results) - ok
        for r in results:
            if not r.success and len(samples) < _ERROR_SAMPLES:
                samples.append(f"{r.source_pk}: {r.error_text()}")

        pks = [r.source_pk for r in results]
        bps = [r.partner if r.success else None for r in results]
        oks = [r.success for r in results]
        errs = [None if r.success else r.error_text() for r in results]
        with self._session_factory.begin() as session:
            session.execute(_UPDATE_LINEAGE, {"pks": pks, "bps": bps, "oks": oks, "errs": errs, "run_id": run_id})
            session.execute(insert(MigCellLineage), [
                {
                    "run_id": run_id, "source_pk_value": pk, "target_pk_value": bp,
                    "target_column_name": PARTNER_COLUMN, "source_raw_value": None, "transformed_value": bp,
                    "rule_applied_id": None, "loaded_successfully": success, "error_message": err,
                }
                for pk, bp, success, err in zip(pks, bps, oks, errs)
            ])
            session.execute(
                update(MigMigrationRun).where(MigMigrationRun.id == run_id).values(
                    records_loaded=MigMigrationRun.records_loaded + ok,
                    records_failed=MigMigrationRun.records_failed + bad,
                )
            )
        return ok, bad
