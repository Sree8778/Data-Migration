"""Jira bridge: one Jira issue per defect signature, linked back into `gov_defect_aggregates`.

Configuration (environment variables):
    JIRA_URL            base URL, e.g. https://yourcompany.atlassian.net
    JIRA_API_TOKEN      API token (Jira Cloud) or personal access token (Server / Data Center)
    JIRA_PROJECT_KEY    project that receives the tickets, e.g. MIG
    JIRA_USER_EMAIL     optional; when set, Basic auth (email + token) is used, otherwise Bearer
    JIRA_ISSUE_TYPE     optional, default "Bug"

If URL, token or project key is missing the service runs in DRY-RUN mode: it builds the exact
payloads (kept in `last_payloads`), makes no HTTP call and writes nothing to the database. Dry-run
keys are returned as `DRY-RUN:<signature>` so they cannot be mistaken for real issue keys.

The HTTP layer is a thin `JiraClient` over httpx; tests inject an `httpx.MockTransport`, so the
real auth headers and request building are exercised.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import CatColumn, CatTable, GovDefectAggregate, MapFieldRule, MigMigrationRun
from src.schemas.validation import DefectRead, ROOT_CAUSE_BY_KIND, kind_for_signature
from src.services.defect_service import DefectService

CLOSED_STATUSES = {"DONE", "CLOSED", "RESOLVED"}
OPEN_STATUS = "OPEN"
_API = "/rest/api/2"  # v2 accepts plain wiki-markup descriptions
DRY_RUN_PREFIX = "DRY-RUN:"


class JiraError(Exception):
    pass


class JiraAuthError(JiraError):
    """401/403: credentials are wrong or lack permission; the whole sync aborts."""


@dataclass(frozen=True)
class JiraConfig:
    url: str
    project_key: str
    api_token: str = field(repr=False)
    user_email: Optional[str] = None
    issue_type: str = "Bug"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> Optional["JiraConfig"]:
        env = os.environ if env is None else env
        url, token, project = (env.get(k, "").strip() for k in ("JIRA_URL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"))
        if not (url and token and project):
            return None
        return cls(
            url=url.rstrip("/"), project_key=project, api_token=token,
            user_email=(env.get("JIRA_USER_EMAIL") or "").strip() or None,
            issue_type=(env.get("JIRA_ISSUE_TYPE") or "Bug").strip(),
        )


class JiraClient:
    """Minimal Jira REST v2 wrapper with one retry on 429 / 5xx."""

    def __init__(
        self, config: JiraConfig, transport: Optional[httpx.BaseTransport] = None, retry_delay: float = 1.0
    ) -> None:
        self._config = config
        self._retry_delay = retry_delay
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        auth = None
        if config.user_email:
            auth = httpx.BasicAuth(config.user_email, config.api_token)
        else:
            headers["Authorization"] = f"Bearer {config.api_token}"
        self._client = httpx.Client(
            base_url=config.url, headers=headers, auth=auth, timeout=30.0, transport=transport
        )

    def create_issue(self, summary: str, description: str, labels: list[str]) -> str:
        body = {"fields": {
            "project": {"key": self._config.project_key},
            "issuetype": {"name": self._config.issue_type},
            "summary": summary, "description": description, "labels": labels,
        }}
        data = self._request("POST", f"{_API}/issue", json=body).json()
        key = data.get("key")
        if not key:
            raise JiraError("Jira did not return an issue key")
        return str(key)

    def update_issue(self, key: str, summary: str, description: str) -> None:
        self._request("PUT", f"{_API}/issue/{key}", json={"fields": {"summary": summary, "description": description}})

    def get_status(self, key: str) -> str:
        data = self._request("GET", f"{_API}/issue/{key}", params={"fields": "status"}).json()
        return str(data["fields"]["status"]["name"])

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in (1, 2):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise JiraError(f"cannot reach Jira: {exc!r}") from exc
                time.sleep(self._retry_delay)
                continue
            if response.status_code in (401, 403):
                raise JiraAuthError(f"Jira rejected the credentials (HTTP {response.status_code})")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 1:
                    time.sleep(self._retry_delay)
                    continue
            if response.status_code >= 400:
                raise JiraError(f"Jira returned HTTP {response.status_code}: {response.text[:300]}")
            return response
        raise JiraError("unreachable")  # pragma: no cover


@dataclass
class DefectContext:
    target: str  # TABLE.COLUMN
    mapping_set_id: int


class JiraService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        defect_service: Optional[DefectService] = None,
        config: Optional[JiraConfig] = None,
        transport: Optional[httpx.BaseTransport] = None,
        retry_delay: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._defects = defect_service or DefectService(session_factory)
        self._config = config if config is not None else JiraConfig.from_env()
        self._client = JiraClient(self._config, transport, retry_delay) if self._config else None
        self.last_payloads: list[dict[str, Any]] = []
        self.last_errors: list[str] = []

    @property
    def dry_run(self) -> bool:
        return self._client is None

    # ------------------------------------------------------------------ sync
    def sync_defects_to_jira(self, run_id: int) -> list[str]:
        """Create/update one issue per open defect of the run; returns the issue keys.

        Open = violation_count > 0 and the linked ticket (if any) not closed in Jira. An existing
        ticket for the same (field rule, signature) from an earlier run is reused rather than
        duplicated. Per-defect Jira failures are collected in `last_errors` and do not stop the
        sync; authentication failures abort it.
        """
        self.last_payloads, self.last_errors = [], []
        keys: list[str] = []
        for defect in self._defects.get_defects(run_id, open_only=True):
            if defect.jira_issue_key and (defect.jira_status or "").upper() in CLOSED_STATUSES:
                continue
            context = self._context(defect)
            summary, description, labels = build_issue_content(defect, context, run_id)
            self.last_payloads.append({"signature": defect.rule_signature, "summary": summary,
                                       "description": description, "labels": labels})
            if self._client is None:
                keys.append(f"{DRY_RUN_PREFIX}{defect.rule_signature}")
                continue
            try:
                key = defect.jira_issue_key or self._reusable_key(defect)
                if key:
                    self._client.update_issue(key, summary, description)
                    status = self._client.get_status(key).upper()
                else:
                    key, status = self._client.create_issue(summary, description, labels), OPEN_STATUS
            except JiraAuthError:
                raise
            except JiraError as exc:
                self.last_errors.append(f"{defect.rule_signature}: {exc}")
                continue
            self._defects.set_jira_link(defect.id, key, status)
            keys.append(key)
        return keys

    # ------------------------------------------------------------------ helpers
    def _reusable_key(self, defect: DefectRead) -> Optional[str]:
        with self._session_factory() as session:
            rows = session.execute(
                select(GovDefectAggregate.jira_issue_key, GovDefectAggregate.jira_status)
                .where(
                    GovDefectAggregate.field_rule_id == defect.field_rule_id,
                    GovDefectAggregate.rule_signature == defect.rule_signature,
                    GovDefectAggregate.jira_issue_key.is_not(None),
                    GovDefectAggregate.id != defect.id,
                ).order_by(GovDefectAggregate.id.desc())
            ).all()
        for key, status in rows:
            if (status or "").upper() not in CLOSED_STATUSES:
                return key
        return None

    def _context(self, defect: DefectRead) -> DefectContext:
        with self._session_factory() as session:
            rule = session.get(MapFieldRule, defect.field_rule_id)
            col = session.get(CatColumn, rule.target_column_id)
            table = session.get(CatTable, col.table_id)
            run = session.get(MigMigrationRun, defect.run_id)
            return DefectContext(target=f"{table.table_name}.{col.column_name}", mapping_set_id=run.mapping_set_id)


def build_issue_content(defect: DefectRead, context: DefectContext, run_id: int) -> tuple[str, str, list[str]]:
    """Summary, wiki-markup description and labels for a defect."""
    kind = kind_for_signature(defect.rule_signature)
    cause = ROOT_CAUSE_BY_KIND[kind] if kind else "See the validation report for details."
    summary = (
        f"[{defect.severity.value}] {defect.rule_signature}: {defect.violation_count} record(s) failing "
        f"on {context.target}"
    )[:250]
    samples = "\n".join(f"* {pk}" for pk in defect.sample_failing_record_ids) or "* (none recorded)"
    description = "\n".join([
        "h3. Data migration pre-load validation defect",
        "",
        "||Attribute||Value||",
        f"|Rule signature|{{{{{defect.rule_signature}}}}}|",
        f"|Severity|{defect.severity.value}|",
        f"|Target field|{context.target}|",
        f"|Failing records|{defect.violation_count}|",
        f"|Migration run|{run_id}|",
        f"|Mapping set|{context.mapping_set_id}|",
        "",
        "h4. Root cause",
        cause,
        "",
        f"h4. Sample failing source IDs (up to {len(defect.sample_failing_record_ids) or 5})",
        samples,
        "",
        "_Created automatically by the ERP migration accelerator. The count is refreshed on every "
        "validation run; fix the cause and re-validate to clear the defect._",
    ])
    labels = ["data-migration", f"severity-{defect.severity.value.lower()}", defect.rule_signature.lower()]
    return summary, description, labels
