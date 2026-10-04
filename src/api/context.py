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

import threading
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import Session, sessionmaker

from src.api.settings import Settings
from src.schemas.validation import ValidationReport
from src.services.deduplication_engine import DeduplicationEngine
from src.services.defect_service import DefectService
from src.services.embedding_service import EmbeddingService
from src.services.execution_orchestrator import ExecutionOrchestrator
from src.services.governance_service import GovernanceService
from src.services.jira_service import JiraService
from src.services.llm_mapper_service import LLMMapperService
from src.services.lookup_service import LookupService
from src.services.mapping_orchestrator import MappingOrchestrator
from src.services.metadata_service import MetadataService
from src.services.profiler_service import ProfilerService
from src.services.reconciliation_service import ReconciliationService
from src.services.rule_validator import RuleValidator
from src.services.sap_client import HttpSapClient, SandboxSapClient, SapClient, SapConfig
from src.services.transformation_engine import TransformationEngine
from src.services.validation_engine import ValidationEngine

SOURCE_SUFFIXES = (".csv", ".parquet")


class NotConfiguredError(Exception):
    """A requested capability (e.g. the live SAP target) has no configuration."""


class AppContext:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        llm_mapper: Optional[LLMMapperService] = None,
        encoder=None,
        jira: Optional[JiraService] = None,
        live_sap_client: Optional[Callable[[], SapClient]] = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        sf = session_factory
        self.workspace = settings.workspace_dir
        self.metadata = MetadataService(sf)
        self.profiler = ProfilerService(sf)
        self.lookups = LookupService(sf)
        self.defects = DefectService(sf)
        self.governance = GovernanceService(sf, self.defects, authorized_approvers=settings.approvers)
        self.jira = jira or JiraService(sf, self.defects)
        self.validation = ValidationEngine(sf)
        self.executor = ExecutionOrchestrator(
            sf, TransformationEngine(sf, self.lookups), DeduplicationEngine(), output_dir=self.workspace / "runs")
        self.reconciliation = ReconciliationService(sf)
        self._llm = llm_mapper
        self._encoder = encoder
        self._mapping_orchestrator: Optional[MappingOrchestrator] = None
        self._live_sap_client = live_sap_client
        self._lock = threading.Lock()
        self._sandbox_next = 1000000001
        self.workspace.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ AI mapping stack (lazy)
    def mapping_orchestrator(self) -> MappingOrchestrator:
        with self._lock:
            if self._mapping_orchestrator is None:
                sf = self.session_factory
                embeddings = EmbeddingService(sf, encoder=self._encoder, cache_dir=self.workspace / "cache")
                llm = self._llm or LLMMapperService()
                self._mapping_orchestrator = MappingOrchestrator(sf, embeddings, llm, RuleValidator(sf))
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
            return HttpSapClient(config), False
        with self._lock:
            return SandboxSapClient(first_partner=self._sandbox_next), True

    def advance_sandbox(self, used: int) -> None:
        with self._lock:
            self._sandbox_next += used

    # ------------------------------------------------------------------ workspace paths
    def table_dir(self, table_id: int) -> Path:
        return self.workspace / "tables" / str(table_id)

    def source_file(self, table_id: int) -> Optional[Path]:
        for suffix in SOURCE_SUFFIXES:
            candidate = self.table_dir(table_id) / f"source{suffix}"
            if candidate.is_file():
                return candidate
        return None

    def run_dir(self, run_id: int) -> Path:
        return self.workspace / "runs" / f"run_{run_id}"

    def load_validation_report(self, run_id: int) -> Optional[ValidationReport]:
        path = self.run_dir(run_id) / "validation.json"
        if not path.is_file():
            return None
        return ValidationReport.model_validate_json(path.read_text(encoding="utf-8"))
