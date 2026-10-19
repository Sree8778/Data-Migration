"""API-level exceptions (kept dependency-free so any module can import them)."""


class NotConfiguredError(Exception):
    """A requested capability (e.g. the live SAP target) has no configuration."""


class PreflightBlockedError(Exception):
    """The pre-submission check found problems that stop the mapping set from being submitted; `report` says which."""

    def __init__(self, report) -> None:
        blockers = [f.message for f in report.findings if f.severity.value == "BLOCKER"]
        super().__init__(f"mapping set {report.mapping_set_id} cannot be submitted: " + " | ".join(blockers[:4]) + (" | ..." if len(blockers) > 4 else ""))
        self.report = report
