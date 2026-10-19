"""Check a REAL SAP system before the first load: run this against your sandbox / quality system, read the report, fix what it names.

    set SAP_BASE_URL=https://my-s4.example.com:44300
    set SAP_USER=MIGUSER
    set SAP_PASSWORD=...            (only in your shell: it is never written anywhere)
    set SAP_CLIENT=100              (optional)
    set SAP_BP_EXTERNAL_ID_FIELD=...   (optional: the field that receives the legacy key; checked against $metadata when set)
    python scripts/verify_live_sap.py [--report sap_check.md]
    python scripts/verify_live_sap.py --write-test --yes-create-one-test-partner [--grouping BP01]

Read-only by default: connection and credentials, the CSRF token and the session cookie that must travel with it, the service's own $metadata
against every field this application sends (names and lengths), and read access. The write test creates ONE business partner named
'MIGRATION TEST - SAFE TO ARCHIVE ...', reads it back, compares it, and sends an invalid payload to prove SAP's rejection is understood; it only runs
with the explicit --yes-create-one-test-partner. Exit code 0 = nothing failed (warnings are listed), 1 = at least one check failed, 2 = not configured.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from src.services.sap_client import SapConfig  # noqa: E402
from src.services.sap_diagnostics import Diagnostic, SapDiagnostics, expected_odata_lengths  # noqa: E402

MARK = {"pass": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}


def catalog_lengths() -> dict[str, int]:
    """The field lengths our own validation allows (the SAP catalog in the migration database), or nothing when that database is not reachable."""
    try:
        from sqlalchemy import select
        from src.db.enums import TargetType
        from src.db.models import CatColumn, CatDataSource, CatTable
        from src.db.session import create_db_engine, create_session_factory
        with create_session_factory(create_db_engine())() as s:
            return {f"{t}.{c}": n for t, c, n in s.execute(
                select(CatTable.table_name, CatColumn.column_name, CatColumn.char_max_length).join(CatColumn, CatColumn.table_id == CatTable.id)
                .join(CatDataSource, CatTable.source_id == CatDataSource.id)
                .where(CatDataSource.target_type == TargetType.TARGET, CatTable.business_object == "BUSINESS_PARTNER"))}
    except Exception:  # noqa: BLE001 - the length comparison is optional
        return {}


def render(checks: list[Diagnostic], config: SapConfig, wrote: bool) -> str:
    lines = [f"# SAP system check - {datetime.now():%Y-%m-%d %H:%M}", "",
             f"System: {config.base_url}  user: {config.user}  client: {config.client or '(default)'}  external id field: {config.external_id_field or '(none: SAP numbers the partners)'}",
             f"Write test: {'ran (one test partner was created)' if wrote else 'not run'}", ""]
    for c in checks:
        lines.append(f"[{MARK[c.status]}] {c.name}: {c.detail}")
        if c.hint and c.status in ("fail", "warn", "pass"):
            lines.append(f"       -> {c.hint}")
    failed = [c for c in checks if c.status == "fail"]
    warned = [c for c in checks if c.status == "warn"]
    lines += ["", f"Result: {len(failed)} failed, {len(warned)} warning(s), {sum(c.status == 'pass' for c in checks)} passed, {sum(c.status == 'skip' for c in checks)} skipped."]
    if not failed:
        lines.append("Nothing blocks a trial load. Load a small wave first and read the reconciliation certificate.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None, env: Optional[Mapping[str, str]] = None, transport: Optional[httpx.BaseTransport] = None) -> int:
    ap = argparse.ArgumentParser(description="Check a live SAP system before loading business partners into it.")
    ap.add_argument("--write-test", action="store_true", help="also create one marked test partner and read it back")
    ap.add_argument("--yes-create-one-test-partner", action="store_true", help="the explicit yes the write test requires")
    ap.add_argument("--grouping", default="BP01", help="business partner grouping for the test partner (default BP01)")
    ap.add_argument("--report", help="also write the report to this file")
    args = ap.parse_args(argv)
    config = SapConfig.from_env(env)
    if config is None:
        print("Not configured: set SAP_BASE_URL, SAP_USER and SAP_PASSWORD (and optionally SAP_CLIENT) in this shell.", file=sys.stderr)
        return 2
    if args.write_test and not args.yes_create_one_test_partner:
        print("The write test creates a business partner in the system. Add --yes-create-one-test-partner to confirm it.", file=sys.stderr)
        return 2
    checks = SapDiagnostics(config, transport=transport).run(write_test=args.write_test, expected_lengths=expected_odata_lengths(catalog_lengths()), test_grouping=args.grouping)
    wrote = any(c.name == "write_test" and c.status == "pass" for c in checks)
    text = render(checks, config, wrote)
    print(text)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    return 1 if any(c.status == "fail" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
