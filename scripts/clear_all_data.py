"""Clear ALL application data: every registered source, table, mapping set, run, defect, lookup and lineage row, plus the landed files.

    1. Stop the server.
    2. python scripts/clear_all_data.py --yes
    3. Start the server again (it re-seeds the SAP target catalog at startup).

What it removes
  * every catalog / mapping / run / defect / lineage table in the database (ids restart at 1); the Alembic version is kept
  * workspace/tables, workspace/runs, workspace/tmp, workspace/mapping_archive.json, workspace/rule_deletions.jsonl
What it keeps
  * the database schema and migrations, the SAP target catalog (re-seeded on start), workspace/connections.json (saved connections,
    no passwords), workspace/cache (embedding cache), mock_data/, the demo source database `legacy_demo`, the test database
A safety backup (catalog without lineage + zipped workspace files) is written to backups/ first unless --no-backup is given.
Refuses to run without --yes, and refuses a database whose name ends in _test.
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import make_url

from src.db.session import get_database_url

ROOT = Path(__file__).resolve().parent.parent


def backup(url) -> Path:
    out = ROOT / "backups" / f"before_clear_{dt.datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    ws = ROOT / "workspace"
    for part in ("tables", "runs"):
        if (ws / part).is_dir():
            shutil.make_archive(str(out / f"workspace_{part}"), "zip", str(ws / part))
    for name in ("mapping_archive.json", "rule_deletions.jsonl", "connections.json"):
        if (ws / name).is_file():
            shutil.copy2(ws / name, out / name)
    try:  # a SQL dump of the catalog, via the dev container when it is the one running
        with open(out / "catalog.sql", "wb") as fh:
            subprocess.run(["docker", "exec", "-e", f"PGPASSWORD={url.password}", "erp_migration_pg", "pg_dump", "-U", url.username, "-d", url.database,
                            "--exclude-table-data=mig_cell_lineage"], stdout=fh, check=True, timeout=300)
    except Exception as exc:  # noqa: BLE001
        print(f"note: SQL dump skipped ({exc}); the file backups were still written")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="really delete (required)")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()
    url = make_url(get_database_url())
    if url.database.endswith("_test"):
        print("refusing: this is the test database")
        return 2
    engine = sa.create_engine(url)
    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(sa.text("select tablename from pg_tables where schemaname='public' and tablename <> 'alembic_version' order by 1"))]
        counts = {n: conn.execute(sa.text(f'select count(*) from "{n}"')).scalar() for n in names}
    print(f"database {url.database}: " + ", ".join(f"{n}={c:,}" for n, c in counts.items() if c))
    if not args.yes:
        print("nothing deleted. Run again with --yes to clear all of this (stop the server first).")
        return 1
    if not args.no_backup:
        print("backup written to", backup(url))
    with engine.begin() as conn:
        conn.execute(sa.text("TRUNCATE " + ", ".join(f'"{n}"' for n in names) + " RESTART IDENTITY CASCADE"))
    ws = ROOT / "workspace"
    for part in ("tables", "runs", "tmp"):
        shutil.rmtree(ws / part, ignore_errors=True)
        (ws / part).mkdir(parents=True, exist_ok=True)
    for name in ("mapping_archive.json", "rule_deletions.jsonl"):
        (ws / name).unlink(missing_ok=True)
    print("cleared. Start the server; it re-seeds the SAP target catalog.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
