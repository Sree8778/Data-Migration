"""Application context: the wired-up services plus the on-disk workspace layout.

Workspace layout (all paths derived from ids, so no extra database tables are needed):
    tables/<table_id>/source.csv|parquet    uploaded legacy extract
    tables/<table_id>/profile.json          last profile response (PII-masked)
    runs/run_<id>/                          Module 4/5 outputs (staging, deduplicated, validation/...)
    runs/run_<id>/validation.json           Module 5 ValidationReport
    runs/run_<id>/signoff.json              steward load sign-off
    runs/run_<id>/load.json, cockpit.xlsx, reconciliation_run_<id>.md/.html
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, Optional

import httpx
from sqlalchemy.orm import Session, sessionmaker

from src.api.exceptions import NotConfiguredError
from src.services._duckdb_util import bounded_connection, lit
from src.api.archive import MappingArchive
from src.api.mapping_targets import MappingTargets
from src.connectors.store import ConnectionStore
from src.services.ai_assist import AiAssistService
from src.services.audit_log import AuditLog
from src.services.duplicate_review import DuplicateReview
from src.services.quality_rules import QualityService
from src.services.retention import RetentionService
from src.services.stewardship import Notifier, TaskStore
from src.services.db_loader_service import DbLoaderService
from src.services.delta_service import DeltaService
from src.api.jobs import JobManager
from src.api.settings import Settings
from src.schemas.validation import ValidationReport
from src.services.construction_service import ConstructionService
from src.services.deduplication_engine import DeduplicationEngine
from src.services.defect_service import DefectService
from src.services.embedding_service import EmbeddingService
from src.services.gemini_key_manager import GeminiKeyManager
from src.services.execution_orchestrator import ExecutionOrchestrator
from src.services.governance_service import GovernanceService
from src.services.jira_service import JiraService
from src.services.llm_mapper_service import LLMMapperService
from src.services.lookup_service import LookupService
from src.services.mapping_preflight import MappingPreflightService
from src.services.mapping_orchestrator import MappingOrchestrator
from src.services.metadata_service import MetadataService
from src.services.row_exclusion import ExclusionStore
from src.services.profiler_service import ProfilerService
from src.services.reconciliation_service import ReconciliationService
from src.services.source_landing import SourceLandingService
from src.services.rule_validator import RuleValidator
from src.services.sap_client import HttpSapClient, SandboxSapClient, SapClient, SapConfig
from src.services.transformation_engine import TransformationEngine
from src.services.validation_engine import ValidationEngine

SOURCE_SUFFIXES = (".parquet", ".csv")  # landed snapshots are Parquet; .csv is kept for tables created before landing existed


class AppContext:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        llm_mapper: Optional[LLMMapperService] = None,
        encoder=None,
        jira: Optional[JiraService] = None,
        live_sap_client: Optional[Callable[[], SapClient]] = None,
        gemini_keys: Optional[GeminiKeyManager] = None,
        sap_transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._sap_transport = sap_transport  # tests only: a stand-in for the SAP gateway
        self.settings = settings
        self.session_factory = session_factory
        sf = session_factory
        self.workspace = settings.workspace_dir
        self.audit = AuditLog(self.workspace / "audit", settings.audit_hmac_key)   # tamper-evident trail of who did what (see services/audit_log)
        self.metadata = MetadataService(sf)
        self.profiler = ProfilerService(sf)
        self.lookups = LookupService(sf)
        self.defects = DefectService(sf)
        self.governance = GovernanceService(sf, self.defects, authorized_approvers=settings.approvers)
        self.jira = jira or JiraService(sf, self.defects)
        self.validation = ValidationEngine(sf)
        self.transformer = TransformationEngine(sf, self.lookups)
        self.executor = ExecutionOrchestrator(sf, self.transformer, DeduplicationEngine(), output_dir=self.workspace / "runs")
        self.preflight = MappingPreflightService(sf, self.transformer, self.validation, self.lookups)
        self.ai_assist = AiAssistService(sf, self.mapping_orchestrator, self.lookups, self.workspace)
        self.reconciliation = ReconciliationService(sf)
        self.landing = SourceLandingService()
        self.archive = MappingArchive(self.workspace)
        self.exclusions = ExclusionStore(self.workspace)
        self.mapping_targets = MappingTargets(self.workspace)   # which destination each mapping set is pointed at
        self.connections = ConnectionStore(self.workspace, allowed_hosts=settings.db_allowed_hosts)
        self.db_loader = DbLoaderService(sf)
        self.tasks = TaskStore(self.workspace, Notifier(settings.notify_webhook))   # who fixes what, by when
        self.duplicate_review = DuplicateReview(self.workspace)   # what people decided about possible duplicates; applied by every run of the table
        self.quality = QualityService(self.workspace)   # data quality rule library and assessments (the history is the monitoring)
        self.retention = RetentionService(sf, self.workspace)   # retention, erasure and legal hold of personal-data artefacts
        self.delta = DeltaService(self.db_loader)
        self.construction = ConstructionService(sf, self.workspace / "runs", self.validation, self.defects, self.governance)
        self._llm = llm_mapper
        self.gemini_keys = gemini_keys or GeminiKeyManager.from_env(persist_path=settings.gemini_keys_file)
        self.llm_provider = getattr(llm_mapper, "provider", None) or os.environ.get("LLM_PROVIDER", "ollama").strip().lower()
        self._encoder = encoder
        self._mapping_orchestrator: Optional[MappingOrchestrator] = None
        self._live_sap_client = live_sap_client
        self.jobs = JobManager()
        self._lock = threading.Lock()
        self._sandbox_next = 1000000001
        self.workspace.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ maintenance
    def run_maintenance(self, user: str = "system:maintenance") -> dict:
        """The jobs nobody should have to remember: erase what is past the retention period (runs under legal hold are skipped) and remind the webhook about overdue tasks.
        Each action is on the audit trail under the user `system:maintenance`."""
        from src.services.audit_log import AuditUnavailable
        out: dict = {"retention": None, "overdue_tasks": 0}
        if self.settings.retention_days:
            result = self.retention.sweep(self.settings.retention_days, user, dry_run=False)
            out["retention"] = {"erased_runs": [e["run_id"] for e in result["erased"]], "skipped_under_hold": [s["run_id"] for s in result["skipped"]]}
        out["overdue_tasks"] = len(self.tasks.remind())
        try:
            self.audit.append({"phase": "done", "user": user, "roles": ["system"], "method": "JOB", "path": "/maintenance", "status": 200, "outcome": "ok", "ids": {"erased_runs": ",".join(str(i) for i in (out["retention"] or {}).get("erased_runs", []))} or None})
        except AuditUnavailable:
            pass
        return out

    def start_maintenance(self) -> None:
        """Run the maintenance job every MIGRATION_MAINTENANCE_HOURS hours in a daemon thread (off by default)."""
        hours = self.settings.maintenance_hours
        if hours <= 0 or getattr(self, "_maintenance_stop", None) is not None:
            return
        self._maintenance_stop = threading.Event()

        def loop() -> None:
            while not self._maintenance_stop.wait(hours * 3600):
                try:
                    self.run_maintenance()
                except Exception:  # noqa: BLE001 - a failed run is tried again next time, and must never take the server down
                    import logging
                    logging.getLogger("migration.api").exception("maintenance job failed")

        threading.Thread(target=loop, daemon=True, name="migration-maintenance").start()

    def stop_maintenance(self) -> None:
        stop = getattr(self, "_maintenance_stop", None)
        if stop is not None:
            stop.set()

    # ------------------------------------------------------------------ AI mapping stack (lazy)
    def mapping_orchestrator(self) -> MappingOrchestrator:
        with self._lock:
            if self._mapping_orchestrator is None:
                sf = self.session_factory
                embeddings = EmbeddingService(sf, encoder=self._encoder, cache_dir=self.workspace / "cache")
                llm = self._llm or LLMMapperService.from_env(gemini=self.gemini_keys)
                self._mapping_orchestrator = MappingOrchestrator(sf, embeddings, llm, RuleValidator(sf), consistency_runs=self.settings.ai_consistency_runs)
            return self._mapping_orchestrator

    # ------------------------------------------------------------------ SAP clients
    def sap_client(self, target: str) -> tuple[SapClient, bool]:
        """(client, simulated). The sandbox hands out numbers that stay unique across loads."""
        if target == "live":
            if self._live_sap_client is not None:
                return self._live_sap_client(), False
            config = SapConfig.from_env()
            if config is None:
                raise NotConfiguredError("live SAP target requested but SAP_BASE_URL / SAP_USER / SAP_PASSWORD are not set")
            return HttpSapClient(config, transport=self._sap_transport), False
        with self._lock:
            return SandboxSapClient(first_partner=self._sandbox_next), True

    def advance_sandbox(self, used: int) -> None:
        with self._lock:
            self._sandbox_next += used

    # ------------------------------------------------------------------ workspace paths
    def preflight_file(self, mapping_set_id: int) -> Path:
        """The last pre-submission check of a mapping set (what the approver sees)."""
        return self.workspace / "preflight" / f"mapping_{mapping_set_id}.json"

    def table_dir(self, table_id: int) -> Path:
        return self.workspace / "tables" / str(table_id)

    def source_file(self, table_id: int) -> Optional[Path]:
        for suffix in SOURCE_SUFFIXES:
            candidate = self.table_dir(table_id) / f"source{suffix}"
            if candidate.is_file():
                return candidate
        return None

    def file_columns(self, table_id: int) -> Optional[set[str]]:
        """Lower-cased column names of the CURRENT landed file, or None when nothing is landed. The catalog can still
        hold columns of an earlier layout; this is what the file really has."""
        path = self.source_file(table_id)
        if path is None:
            return None
        reader = "read_parquet" if path.suffix.lower() in (".parquet", ".pq") else "read_csv_auto"
        with bounded_connection(256) as con:
            return {r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {reader}({lit(path.as_posix())})").fetchall()}

    def run_dir(self, run_id: int) -> Path:
        return self.workspace / "runs" / f"run_{run_id}"

    def load_validation_report(self, run_id: int) -> Optional[ValidationReport]:
        path = self.run_dir(run_id) / "validation.json"
        if not path.is_file():
            return None
        return ValidationReport.model_validate_json(path.read_text(encoding="utf-8"))
