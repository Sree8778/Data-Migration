"""Data Construction: fill mandatory SAP fields the legacy system cannot supply, through Excel.

Flow
  1. `generate_construction_template(run_id)`  -> .xlsx with one row per record that has a mandatory /
     unresolved-lookup cell (classified by the SAME SQL the Module 5 validation uses, so the sheet
     matches the gate exactly). Context columns are locked, the cells to fill are unlocked, highlighted,
     and carry drop-downs (check-table domains) or length rules.
  2. `ingest_constructed_data(run_id, file, user)` -> validates structure (template id, ROW_REF +
     SOURCE_PK must match the staging rows) and every submitted cell (type, length, check table),
     merges the accepted cells into NEW staging files (originals are never modified) with one DuckDB
     update, and writes one `mig_cell_lineage` row per merged cell.
  3. `revalidate(run_id, user)` -> re-runs the Module 5 validation on the merged data, updates the
     defect aggregates (fixed signatures drop to zero), revokes any earlier load sign-off, and returns
     the new gate state.

Notes
* SOURCE_PK is not unique in dirty legacy data, so every row also carries ROW_REF (its position in
  the staging file); both must match on upload.
* `mig_cell_lineage` has no user column, so identity and time are written into `error_message` as
  "[CONSTRUCTED] by <user> at <iso> via <file>; previous: <old state>" (schema is locked).
* Only runs that are validated and not yet loaded can be constructed.
* The workbook protection is a usability aid, not security: every upload is re-validated server-side.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from src.db.enums import MappingStatus, MigrationPhase, RuleType, RunStatus, TargetType
from src.db.models import CatColumn, CatDataSource, CatTable, MapFieldRule, MapTableRule, MigMigrationRun
from src.schemas.construction import (
    ActivityEntry,
    ConstructionField,
    ConstructionResult,
    ConstructionSummary,
    PlaceholderRule,
    RejectedCell,
    RequiredField,
    RevalidationResult,
    TemplateResult,
)
from src.schemas.execution import DedupConfig
from src.schemas.validation import ValidationReport
from src.services._duckdb_util import DEFAULT_MEMORY_LIMIT_MB, bounded_connection, lit, q
from src.services.deduplication_engine import DeduplicationEngine
from src.services.defect_service import DefectService
from src.services.domain_provider import DomainProvider
from src.services.governance_service import GovernanceService
from src.services.metadata_service import NotFoundError
from src.services.transformation_engine import ERRORS_COLUMN, PK_COLUMN, STATUS_COLUMN
from src.services.validation_engine import DUPLICATE_COLUMN, ValidationEngine

CONSTRUCTIBLE_KINDS = ("MANDATORY", "LOOKUP_FAILED")
# Identifiers SAP or the loader generates (never constructed by hand); other key fields such as BUKRS / VKORG are not exempt.
GENERATED_KEYS = ("PARTNER", "KUNNR", "LIFNR", "ADDRNUMBER", "RLTYP")
DATA_SHEET, README_SHEET, LISTS_SHEET, META_SHEET = "Construction", "Instructions", "_Lists", "_META"
H_PK, H_ROW, H_ISSUES = "SOURCE_PK", "ROW_REF", "OTHER_ISSUES"
CONTEXT_PREFERENCE = ["BUT000_NAME_ORG1", "BUT000_NAME_ORG2", "ADRC_STREET", "ADRC_CITY1", "ADRC_COUNTRY", "ADRC_POST_CODE1"]
TEMPLATE_VERSION = "1"
DEFAULT_MAX_ROWS = 20_000
_MAX_UNZIPPED_MB = 300
_MAX_REJECTED_SAMPLES = 25
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MAX_DROPDOWN = 2000


class ConstructionError(Exception):
    pass


class ConstructionStateError(ConstructionError):
    """The run cannot be constructed right now (not validated, already loaded, nothing to merge...)."""


class ConstructionInputError(ConstructionError):
    """The uploaded workbook is not an acceptable construction template."""


def _cell_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).upper()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _safe(value: Any) -> Optional[str]:
    """Text for an Excel cell: control characters removed (formula injection is handled by data_type)."""
    return None if value is None else _XML_ILLEGAL.sub("", str(value))


class ConstructionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        runs_dir: str | Path,
        validation_engine: Optional[ValidationEngine] = None,
        defect_service: Optional[DefectService] = None,
        governance: Optional[GovernanceService] = None,
        domain_provider: Optional[DomainProvider] = None,
        memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB,
    ) -> None:
        self._sf = session_factory
        self._runs_dir = Path(runs_dir)
        self._defects = defect_service or DefectService(session_factory)
        self._governance = governance or GovernanceService(session_factory, self._defects)
        self._engine = validation_engine or ValidationEngine(session_factory, domain_provider)
        self._memory = memory_limit_mb
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------ paths / state
    def run_dir(self, run_id: int) -> Path:
        return self._runs_dir / f"run_{run_id}"

    def _cdir(self, run_id: int) -> Path:
        path = self.run_dir(run_id) / "construction"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _lock(self, run_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(run_id, threading.Lock())

    def _run(self, run_id: int) -> tuple[int, MigrationPhase, RunStatus]:
        with self._sf() as s:
            run = s.get(MigMigrationRun, run_id)
            if run is None:
                raise NotFoundError(f"migration run {run_id} does not exist")
            return run.mapping_set_id, run.current_phase, run.status

    def _guard(self, phase: MigrationPhase, status: RunStatus, run_id: int) -> None:
        if phase != MigrationPhase.VALIDATE or status != RunStatus.SUCCEEDED:
            raise ConstructionStateError(
                f"run {run_id} is {phase.value}/{status.value}; data can only be constructed for a validated run "
                "that has not been loaded")

    def _load_report(self, run_id: int) -> ValidationReport:
        path = self.run_dir(run_id) / "validation.json"
        if not path.is_file():
            raise ConstructionStateError(f"run {run_id} has no validation report; run the pipeline first")
        return ValidationReport.model_validate_json(path.read_text(encoding="utf-8"))

    def _base_staging(self, run_id: int) -> Path:
        constructed = self._cdir(run_id) / "staging.parquet"
        return constructed if constructed.is_file() else self.run_dir(run_id) / "staging.parquet"

    def _current_input(self, run_id: int, report: ValidationReport) -> Path:
        cdir = self._cdir(run_id)
        for candidate in (cdir / "deduplicated.parquet", cdir / "staging.parquet"):
            if candidate.is_file():
                return candidate
        return Path(report.staging_path)

    # ------------------------------------------------------------------ activity log
    def _log(self, run_id: int, event: str, user: str, **detail: Any) -> None:
        entry = {"event": event, "at": datetime.now(timezone.utc).isoformat(), "user": user, "detail": detail}
        with open(self._cdir(run_id) / "activity.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    def activity(self, run_id: int) -> list[ActivityEntry]:
        path = self.run_dir(run_id) / "construction" / "activity.jsonl"
        if not path.is_file():
            return []
        return [ActivityEntry.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # ------------------------------------------------------------------ analysis (shared by template + summary)
    def _analyze(self, con, run_id: int, mapping_set_id: int, input_path: Path):
        """Create temp table `inp` (+ need flags `need_<i>`) and return (specs, needing counts, total rows)."""
        specs = self._engine._load_specs(mapping_set_id)
        columns = [r[0] for r in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet({lit(input_path.as_posix())})").fetchall()]
        self._engine._check_columns(specs, columns)
        con.execute(f'CREATE TEMP TABLE inp AS SELECT CAST(row_number() OVER () AS BIGINT) AS "__row", * '
                    f"FROM read_parquet({lit(input_path.as_posix())})")
        self._engine._load_domains(con, specs)
        has_errors = ERRORS_COLUMN in columns
        dup = f"coalesce({q(DUPLICATE_COLUMN)}, false)" if DUPLICATE_COLUMN in columns else "false"
        kinds = ", ".join(f'{self._engine._kind_sql(s, has_errors)} AS "__k{s.idx}"' for s in specs)
        con.execute(f'CREATE TEMP TABLE kinds AS SELECT "__row", {dup} AS "__dup", {q(PK_COLUMN)} AS "__pk", {kinds} FROM inp')
        needs = ", ".join(
            f'(NOT "__dup" AND "__k{s.idx}" IN {tuple(CONSTRUCTIBLE_KINDS)}) AS "need_{s.idx}"' for s in specs)
        con.execute(f'CREATE TEMP TABLE needs AS SELECT *, {needs} FROM kinds')
        counts = con.execute("SELECT " + ", ".join(f'count(*) FILTER (WHERE "need_{s.idx}")' for s in specs) + " FROM needs").fetchone()
        any_need = " OR ".join(f'"need_{s.idx}"' for s in specs)
        total_rows = int(con.execute(f"SELECT count(*) FROM needs WHERE {any_need}").fetchone()[0])
        return specs, {s.idx: int(n) for s, n in zip(specs, counts)}, total_rows

    def _field_meta(self, specs) -> dict[int, dict[str, Any]]:
        with self._sf() as s:
            meta = {}
            for spec in specs:
                rule = s.get(MapFieldRule, spec.rule_id)
                col = s.get(CatColumn, rule.target_column_id)
                source = s.get(CatColumn, rule.source_column_id) if rule.source_column_id else None
                meta[spec.idx] = {"data_type": col.data_type, "max_length": col.char_max_length,
                                  "check_table": col.check_table, "column": col.column_name, "table": spec.table,
                                  "source_column": source.column_name if source else None}
            return meta

    # ------------------------------------------------------------------ summary
    def get_summary(self, run_id: int) -> ConstructionSummary:
        mapping_set_id, phase, status = self._run(run_id)
        log = self.activity(run_id)
        try:
            self._guard(phase, status, run_id)
            report = self._load_report(run_id)
        except ConstructionStateError as exc:
            return ConstructionSummary(run_id=run_id, status="LOCKED", can_construct=False, reason=str(exc),
                                       incomplete_records=0, required_fields=[], pending_cells=0, activity=log)

        with bounded_connection(self._memory) as con:
            specs, counts, total = self._analyze(con, run_id, mapping_set_id, self._current_input(run_id, report))
        required = [RequiredField(target=s.target, header=s.column, records=counts[s.idx],
                                  kind="MANDATORY / LOOKUP") for s in specs if counts[s.idx] > 0]
        last_upload = max((i for i, e in enumerate(log) if e.event == "upload" and e.detail.get("cells_modified")), default=-1)
        last_reval = max((i for i, e in enumerate(log) if e.event == "revalidate"), default=-1)
        if last_upload > last_reval:
            state = "REVALIDATION_REQUIRED"
        else:
            state = "PENDING" if required else "NOT_REQUIRED"
        return ConstructionSummary(
            run_id=run_id, status=state, can_construct=True, incomplete_records=total,
            required_fields=required, pending_cells=sum(counts.values()), activity=log)

    # ------------------------------------------------------------------ 1. template
    def generate_construction_template(
        self, run_id: int, output_path: Optional[str | Path] = None, max_rows: int = DEFAULT_MAX_ROWS, user: str = "system"
    ) -> TemplateResult:
        mapping_set_id, phase, status = self._run(run_id)
        self._guard(phase, status, run_id)
        report = self._load_report(run_id)
        input_path = self._current_input(run_id, report)
        if not input_path.is_file():
            raise ConstructionStateError(f"validation input {input_path.name} no longer exists")
        cdir = self._cdir(run_id)
        out = Path(output_path) if output_path else cdir / "construction_template.xlsx"
        out.parent.mkdir(parents=True, exist_ok=True)

        with bounded_connection(self._memory) as con:
            specs, counts, total_needing = self._analyze(con, run_id, mapping_set_id, input_path)
            n_base = con.execute(f"SELECT count(*) FROM read_parquet({lit(self._base_staging(run_id).as_posix())})").fetchone()[0]
            if n_base != con.execute('SELECT count(*) FROM inp').fetchone()[0]:
                raise ConstructionStateError("staging and validation input have different row counts; re-run the pipeline")
            fields_spec = [s for s in specs if counts[s.idx] > 0]
            if not fields_spec:
                raise ConstructionStateError(f"run {run_id} has no records that need data construction")
            meta = self._field_meta(specs)
            inp_cols = [r[0] for r in con.execute("DESCRIBE inp").fetchall()]
            field_cols = {s.staging for s in fields_spec}
            context_cols = [c for c in CONTEXT_PREFERENCE if c in inp_cols and c not in field_cols][:5]
            # what the legacy system actually said for each field to construct (kept by the pipeline as SRC_<column>)
            legacy = [f"SRC_{meta[s.idx]['source_column']}" for s in fields_spec if meta[s.idx]["source_column"]]
            context_cols += [c for c in dict.fromkeys(legacy) if c in inp_cols and c not in context_cols][:4]

            # unique, friendly headers
            used: set[str] = {H_PK, H_ROW, H_ISSUES}
            def header(table: str, column: str) -> str:
                name = f"LEGACY_{column}" if table == "LEGACY" else (column if column not in used else f"{table}.{column}")
                used.add(name)
                return name
            context_headers = [header("LEGACY", c[4:]) if c.startswith("SRC_") else header(c.split("_", 1)[0], c.split("_", 1)[1])
                               for c in context_cols]
            fields: list[ConstructionField] = []
            for s in fields_spec:
                m = meta[s.idx]
                fields.append(ConstructionField(
                    header=header(m["table"], m["column"]), staging_column=s.staging, target=s.target,
                    field_rule_id=s.rule_id, data_type=m["data_type"], max_length=m["max_length"],
                    check_table=m["check_table"], mandatory=s.mandatory, records_needing=counts[s.idx]))

            other = ", ".join(
                f"CASE WHEN \"__k{s.idx}\" IS NOT NULL AND \"__k{s.idx}\" NOT IN {tuple(CONSTRUCTIBLE_KINDS)} "
                f"THEN \"__k{s.idx}\" || ' ' || {lit(s.column)} END" for s in specs)
            any_need = " OR ".join(f'n."need_{s.idx}"' for s in fields_spec)
            select_cols = ", ".join(
                ['n."__row"', 'n."__pk"'] + [f"i.{q(c)}" for c in context_cols]
                + [f'n."need_{s.idx}"' for s in fields_spec] + [f"concat_ws('; ', {other}) AS issues"])
            rows = con.execute(
                f'SELECT {select_cols} FROM needs n JOIN inp i ON i."__row" = n."__row" WHERE {any_need} '
                f'ORDER BY n."__row" LIMIT {int(max_rows)}').fetchall()

            tpl_cols = ", ".join(f'"need_{s.idx}" AS {q("need_" + str(i))}' for i, s in enumerate(fields_spec))
            con.execute(
                f"COPY (SELECT \"__row\" AS row_ref, \"__pk\" AS pk, {tpl_cols} FROM needs WHERE {any_need.replace('n.', '')} "
                f"ORDER BY \"__row\" LIMIT {int(max_rows)}) "
                f"TO {lit((cdir / 'template_rows.parquet').as_posix())} (FORMAT parquet)")

        template_id = uuid.uuid4().hex
        generated = datetime.now(timezone.utc)
        domains = {s.idx: self._engine._domains.get_domain(meta[s.idx]["check_table"]) if meta[s.idx]["check_table"] else None
                   for s in fields_spec}
        self._write_workbook(out, run_id, mapping_set_id, template_id, generated, fields, fields_spec, domains,
                             context_cols, context_headers, rows)
        (cdir / "template_meta.json").write_text(json.dumps({
            "template_id": template_id, "run_id": run_id, "mapping_set_id": mapping_set_id,
            "generated_at": generated.isoformat(), "version": TEMPLATE_VERSION,
            "fields": [f.model_dump() for f in fields],
        }, indent=2), encoding="utf-8")
        self._log(run_id, "template", user, records=len(rows), fields=[f.target for f in fields])
        return TemplateResult(
            run_id=run_id, template_id=template_id, path=str(out), records=len(rows), fields=fields,
            pending_cells=sum(counts[s.idx] for s in fields_spec), truncated=total_needing > len(rows),
            total_records_needing=total_needing, generated_at=generated)

    def _write_workbook(self, path, run_id, mapping_set_id, template_id, generated, fields, fields_spec, domains,
                        context_cols, context_headers, rows) -> None:
        wb = Workbook()
        ws = wb.active
        ws.title = DATA_SHEET
        thin = Side(style="thin", color="CBD5E1")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        head_fill, edit_head_fill = PatternFill("solid", fgColor="1E293B"), PatternFill("solid", fgColor="B45309")
        ro_fill, edit_fill, na_fill = (PatternFill("solid", fgColor=c) for c in ("F1F5F9", "FEF3C7", "E2E8F0"))

        headers = [H_PK] + context_headers + [f.header for f in fields] + [H_ISSUES, H_ROW]
        n_ctx, n_fields = len(context_headers), len(fields)
        for col, name in enumerate(headers, start=1):
            c = ws.cell(row=1, column=col, value=name)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = edit_head_fill if n_ctx + 2 <= col < n_ctx + 2 + n_fields else head_fill
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = border
        ws.row_dimensions[1].height = 22

        n_noedit = 1 + n_ctx
        for r, row in enumerate(rows, start=2):
            row_ref, pk = row[0], row[1]
            ctx_vals = row[2:2 + n_ctx]
            need_flags = row[2 + n_ctx: 2 + n_ctx + n_fields]
            issues = row[2 + n_ctx + n_fields]
            values: list[Optional[str]] = [pk] + list(ctx_vals) + [None] * n_fields + [issues, row_ref]
            for col, val in enumerate(values, start=1):
                c = ws.cell(row=r, column=col)
                text = _safe(val) if not isinstance(val, (int, float)) or isinstance(val, bool) else val
                c.value = text
                if isinstance(text, str):
                    c.data_type = "s"  # never let legacy text such as "=cmd|..." become a formula
                c.border = border
                c.fill, c.protection = ro_fill, Protection(locked=True)
                if n_noedit < col <= n_noedit + n_fields:
                    needed = bool(need_flags[col - n_noedit - 1])
                    c.number_format = "@"
                    if needed:
                        c.fill, c.protection = edit_fill, Protection(locked=False)
                    else:
                        c.fill = na_fill
        last = len(rows) + 1

        # data validation: drop-down where a check-table domain is known, else a length limit
        lists = wb.create_sheet(LISTS_SHEET)
        list_col = 0
        for i, (f, s) in enumerate(zip(fields, fields_spec)):
            col_letter = get_column_letter(n_noedit + 1 + i)
            domain = domains.get(s.idx)
            if domain and len(domain) <= _MAX_DROPDOWN:
                list_col += 1
                letter = get_column_letter(list_col)
                for j, value in enumerate(sorted(domain), start=1):
                    lists[f"{letter}{j}"] = value
                dv = DataValidation(type="list", formula1=f"={LISTS_SHEET}!${letter}$1:${letter}${len(domain)}",
                                    allow_blank=True, showErrorMessage=True, errorTitle="Invalid value",
                                    error=f"Pick a valid {f.check_table} key from the list.")
            elif f.max_length:
                dv = DataValidation(type="textLength", operator="lessThanOrEqual", formula1=str(f.max_length),
                                    allow_blank=True, showErrorMessage=True, errorTitle="Too long",
                                    error=f"At most {f.max_length} characters.")
            else:
                continue
            ws.add_data_validation(dv)
            dv.add(f"{col_letter}2:{col_letter}{max(last, 2)}")
        lists.sheet_state = "hidden"

        widths = {1: 16}
        for col in range(1, len(headers) + 1):
            ws.column_dimensions[get_column_letter(col)].width = widths.get(col, 24 if col <= n_noedit + n_fields else 34)
        ws.column_dimensions[get_column_letter(len(headers))].width = 10
        ws.freeze_panes = "B2"
        ws.protection.sheet = True  # only the highlighted cells are editable
        ws.protection.formatColumns = False
        ws.protection.formatRows = False
        ws.protection.autoFilter = False
        ws.protection.sort = False
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(last, 2)}"

        info = wb.create_sheet(README_SHEET, 0)
        for i, line in enumerate([
            "Data Construction template", "",
            f"Run {run_id}, generated {generated.strftime('%Y-%m-%d %H:%M UTC')}", "",
            "1. Open the 'Construction' sheet. Grey cells are read-only context for the record.",
            "2. Fill the highlighted (amber) cells. Leave a cell blank if you cannot supply a value yet.",
            "3. Where a drop-down is offered, choose a value from it; otherwise respect the length limit.",
            "4. Do not add, delete, reorder or rename columns, and do not change ROW_REF / SOURCE_PK.",
            "5. Save as .xlsx and upload it in the Governance Hub. Rejected cells are reported with the reason.",
            "", "Fields to complete:"] + [f"  - {f.header}: {f.target} ({f.data_type}"
                                        + (f"({f.max_length})" if f.max_length else "") + f") - {f.records_needing} record(s)"
                                        + (f", check table {f.check_table}" if f.check_table else "") for f in fields], start=1):
            info.cell(row=i, column=1, value=line)
        info["A1"].font = Font(bold=True, size=14)
        info.column_dimensions["A"].width = 110

        meta = wb.create_sheet(META_SHEET)
        meta.append(["template_id", template_id])
        meta.append(["run_id", run_id])
        meta.append(["mapping_set_id", mapping_set_id])
        meta.append(["version", TEMPLATE_VERSION])
        meta.append(["generated_at", generated.isoformat()])
        meta.append([])
        meta.append(["header", "staging_column", "field_rule_id"])
        for f in fields:
            meta.append([f.header, f.staging_column, f.field_rule_id])
        meta.sheet_state = "hidden"
        wb.active = 1
        tmp = Path(str(path) + ".tmp")
        wb.save(tmp)
        os.replace(tmp, path)

    # ------------------------------------------------------------------ 2. ingest
    def ingest_constructed_data(self, run_id: int, workbook_path: str | Path, user: str, filename: Optional[str] = None) -> ConstructionResult:
        if not user or not user.strip():
            raise ValueError("user must not be empty")
        path = Path(workbook_path)
        filename = filename or path.name
        with self._lock(run_id):
            mapping_set_id, phase, status = self._run(run_id)
            self._guard(phase, status, run_id)
            self._load_report(run_id)
            cdir = self._cdir(run_id)
            template_meta = self._read_template_meta(cdir)
            submitted, editable = self._read_workbook(path, run_id, template_meta)
            fields = {f["header"]: f for f in template_meta["fields"]}
            uploaded_at = datetime.now(timezone.utc)

            with bounded_connection(self._memory) as con:
                specs = self._engine._load_specs(mapping_set_id)
                self._engine._load_domains(con, specs)
                spec_by_col = {s.staging: s for s in specs}
                for h, meta_field in fields.items():
                    if meta_field["staging_column"] not in spec_by_col:
                        raise ConstructionInputError(f"template field {meta_field['target']} is no longer part of the mapping")

                base = self._base_staging(run_id)
                tpl = cdir / "template_rows.parquet"
                con.execute(f'CREATE TEMP TABLE stg AS SELECT CAST(row_number() OVER () AS BIGINT) AS "__rn", * FROM read_parquet({lit(base.as_posix())})')
                con.execute(f"CREATE TEMP TABLE tpl AS SELECT * FROM read_parquet({lit(tpl.as_posix())})")
                stg_cols = {r[0]: r[1] for r in con.execute("DESCRIBE stg").fetchall()}

                frame = pd.DataFrame(submitted)
                # structural check: ROW_REF must exist in the template and SOURCE_PK must match the staging record
                con.register("sub_raw", frame)
                bad = con.execute(
                    f'SELECT s.row_ref, s.pk, g.{q(PK_COLUMN)}, (t.row_ref IS NULL) FROM sub_raw s '
                    f'LEFT JOIN tpl t ON t.row_ref = s.row_ref LEFT JOIN stg g ON g."__rn" = s.row_ref '
                    f"WHERE t.row_ref IS NULL OR g.{q(PK_COLUMN)} IS DISTINCT FROM s.pk LIMIT 10").fetchall()
                if bad:
                    detail = "; ".join(f"ROW_REF {r[0]} / SOURCE_PK '{r[1]}' " + ("is not in this template" if r[3] else f"does not match staging record '{r[2]}'") for r in bad)
                    raise ConstructionInputError("records do not match the expected staging data: " + detail)

                applied_parts, rejected_parts, ignored = [], [], 0
                for header_name, meta_field in fields.items():
                    s = spec_by_col[meta_field["staging_column"]]
                    i = s.idx
                    tpl_pos = [f["header"] for f in template_meta["fields"]].index(header_name)
                    col = f"c{tpl_pos}"
                    duck = stg_cols[meta_field["staging_column"]]
                    ci = s.domain is not None and all(d == d.upper() for d in s.domain)
                    v = f"nullif(trim(CAST({q(col)} AS VARCHAR)), '')"
                    vn = f"upper({v})" if ci else v
                    label = meta_field["data_type"]
                    checks = [f"WHEN {v} IS NULL THEN NULL", f"WHEN starts_with({v}, '=') THEN 'formulas are not allowed'"]
                    if s.kind_of_type in ("string", "numc") and s.max_len:
                        checks.append(f"WHEN length({vn}) > {s.max_len} THEN 'longer than {s.max_len} characters'")
                    if s.kind_of_type == "numc":
                        checks.append(f"WHEN NOT regexp_full_match({vn}, '[0-9]+') THEN 'digits only'")
                    if s.kind_of_type not in ("string", "numc"):
                        checks.append(f"WHEN TRY_CAST({vn} AS {duck}) IS NULL THEN 'not a valid {label}'")
                    if s.domain is not None:
                        checks.append(f"WHEN {vn} NOT IN (SELECT v FROM \"dom_{i}\") THEN 'not a valid {meta_field['check_table']} key'")
                    err_sql = "CASE " + " ".join(checks) + " END"
                    old_err_sql = f"g.{q(ERRORS_COLUMN)}" if ERRORS_COLUMN in stg_cols else "CAST(NULL AS VARCHAR)"
                    con.execute(f'''
                        CREATE TEMP TABLE cand_{i} AS
                        SELECT s.row_ref, s.pk, {vn} AS val, {err_sql} AS err, g.{q(meta_field["staging_column"])} AS old_val,
                               {old_err_sql} AS old_err,
                               (g.{q(meta_field["staging_column"])} IS NULL OR trim(CAST(g.{q(meta_field["staging_column"])} AS VARCHAR)) = '') AS needs_now,
                               coalesce(t.{q("need_" + str(tpl_pos))}, false) AS requested
                          FROM sub_raw s JOIN stg g ON g."__rn" = s.row_ref LEFT JOIN tpl t ON t.row_ref = s.row_ref
                         WHERE {v} IS NOT NULL''')
                    applied_parts.append(
                        f"SELECT row_ref, pk, {i} AS fidx, {lit(meta_field['staging_column'])} AS staging_col, {lit(meta_field['target'])} AS target, "
                        f"{int(meta_field['field_rule_id'])} AS rule_id, val, CAST(old_val AS VARCHAR) AS old_val, old_err "
                        f"FROM cand_{i} WHERE requested AND needs_now AND err IS NULL")
                    rejected_parts.append(
                        f"SELECT row_ref, pk, {lit(meta_field['target'])} AS field, "
                        f"coalesce(val, '') AS value, CASE WHEN NOT requested THEN 'not requested for this record' ELSE err END AS reason "
                        f"FROM cand_{i} WHERE (requested AND needs_now AND err IS NOT NULL) OR NOT requested")
                    ignored += int(con.execute(f"SELECT count(*) FROM cand_{i} WHERE requested AND NOT needs_now").fetchone()[0])

                con.execute("CREATE TEMP TABLE applied AS " + " UNION ALL ".join(applied_parts))
                con.execute("CREATE TEMP TABLE rejected AS " + " UNION ALL ".join(rejected_parts))
                cells_modified = int(con.execute("SELECT count(*) FROM applied").fetchone()[0])
                records_updated = int(con.execute("SELECT count(DISTINCT row_ref) FROM applied").fetchone()[0])
                cells_rejected = int(con.execute("SELECT count(*) FROM rejected").fetchone()[0])
                rejected_samples = [RejectedCell(row_ref=r[0], source_pk=r[1], field=r[2], value=r[3][:60], reason=r[4])
                                    for r in con.execute(f"SELECT * FROM rejected ORDER BY row_ref LIMIT {_MAX_REJECTED_SAMPLES}").fetchall()]

                lineage_rows = 0
                warnings: list[str] = []
                if cells_modified:
                    tmp_files = self._merge_into_files(con, run_id, cdir, fields, spec_by_col, stg_cols)
                    lineage_rows = self._write_lineage(con, run_id, user.strip(), uploaded_at, filename, tmp_files)
                    remaining = self._count_pending(con, cdir, tpl, template_meta, spec_by_col)
                else:
                    remaining = self._count_pending(con, cdir, tpl, template_meta, spec_by_col)

            result_status = "OK" if cells_rejected == 0 else ("PARTIAL" if cells_modified else "REJECTED")
            if cells_modified:
                upload_dir = cdir / "uploads"
                upload_dir.mkdir(exist_ok=True)
                safe_user = re.sub(r"[^A-Za-z0-9_.-]", "_", user)[:40]
                shutil.copy(path, upload_dir / f"upload_{len(list(upload_dir.iterdir())) + 1}_{safe_user}.xlsx")
            self._log(run_id, "upload", user.strip(), filename=filename, records_updated=records_updated,
                      cells_modified=cells_modified, cells_rejected=cells_rejected, status=result_status)
            return ConstructionResult(
                run_id=run_id, records_updated=records_updated, cells_modified=cells_modified, cells_rejected=cells_rejected,
                cells_ignored=ignored, validation_status=result_status, rejected_samples=rejected_samples,
                lineage_rows=lineage_rows, remaining_cells_pending=remaining, revalidation_required=cells_modified > 0,
                warnings=warnings, uploaded_by=user.strip(), uploaded_at=uploaded_at)

    # ---- reading the workbook
    @staticmethod
    def _read_template_meta(cdir: Path) -> dict[str, Any]:
        path = cdir / "template_meta.json"
        if not path.is_file():
            raise ConstructionStateError("no construction template was generated for this run; download one first")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _check_zip(path: Path) -> None:
        if not zipfile.is_zipfile(path):
            raise ConstructionInputError("the file is not a valid .xlsx workbook")
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > 500 or sum(i.file_size for i in infos) > _MAX_UNZIPPED_MB * 1024 * 1024:
                raise ConstructionInputError("the workbook is unreasonably large when unpacked")

    def _read_workbook(self, path: Path, run_id: int, template_meta: dict[str, Any]):
        self._check_zip(path)
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 - any parser failure means "not a usable workbook"
            raise ConstructionInputError(f"cannot read the workbook: {exc}") from exc
        try:
            if DATA_SHEET not in wb.sheetnames or META_SHEET not in wb.sheetnames:
                raise ConstructionInputError(f"the workbook must contain the '{DATA_SHEET}' and '{META_SHEET}' sheets of the template")
            meta = {str(r[0]): r[1] for r in wb[META_SHEET].iter_rows(min_row=1, max_row=5, values_only=True) if r and r[0]}
            if str(meta.get("run_id")) != str(run_id):
                raise ConstructionInputError(f"this template belongs to run {meta.get('run_id')}, not run {run_id}")
            if meta.get("template_id") != template_meta["template_id"]:
                raise ConstructionInputError("this template has been superseded; download a fresh one")

            rows = wb[DATA_SHEET].iter_rows(values_only=True)
            header = [(_cell_text(h) or "") for h in next(rows, ())]
            expected = [f["header"] for f in template_meta["fields"]]
            missing = [h for h in [H_PK, H_ROW] + expected if h not in header]
            if missing:
                raise ConstructionInputError(f"columns missing or renamed: {missing}")
            pos = {h: header.index(h) for h in [H_PK, H_ROW] + expected}
            data: dict[str, list] = {"row_ref": [], "pk": [], **{f"c{i}": [] for i in range(len(expected))}}
            seen: set[int] = set()
            for n, row in enumerate(rows, start=2):
                if row is None or all(c is None or str(c).strip() == "" for c in row):
                    continue
                raw_ref = row[pos[H_ROW]] if pos[H_ROW] < len(row) else None
                try:
                    ref = int(float(raw_ref))
                except (TypeError, ValueError):
                    raise ConstructionInputError(f"row {n}: ROW_REF '{raw_ref}' is not a number") from None
                if ref in seen:
                    raise ConstructionInputError(f"row {n}: ROW_REF {ref} appears more than once")
                seen.add(ref)
                pk = _cell_text(row[pos[H_PK]] if pos[H_PK] < len(row) else None)
                if pk is None:
                    raise ConstructionInputError(f"row {n}: SOURCE_PK is empty")
                data["row_ref"].append(ref)
                data["pk"].append(pk)
                for i, h in enumerate(expected):
                    data[f"c{i}"].append(_cell_text(row[pos[h]]) if pos[h] < len(row) else None)
            if not data["row_ref"]:
                raise ConstructionInputError("the workbook contains no data rows")
            return data, expected
        finally:
            wb.close()

    # ---- merge into staging files
    def _merge_into_files(self, con, run_id: int, cdir: Path, fields, spec_by_col, stg_cols) -> list[tuple[Path, Path]]:
        """Write merged copies of the staging file(s) to temp paths; returns [(tmp, final)] to be swapped in."""
        sources = [(self._base_staging(run_id), cdir / "staging.parquet")]
        original_dedup = self.run_dir(run_id) / "deduplicated.parquet"
        current_dedup = cdir / "deduplicated.parquet"
        if current_dedup.is_file():
            sources.append((current_dedup, current_dedup))
        elif original_dedup.is_file():
            sources.append((original_dedup, current_dedup))
        targets = sorted({m["target"] for m in fields.values()})
        staging_cols = [m["staging_column"] for m in fields.values()]
        tmp_files = []
        for n, (src, final) in enumerate(sources):
            table = f"merge_{n}"
            con.execute(f'CREATE TEMP TABLE {table} AS SELECT CAST(row_number() OVER () AS BIGINT) AS "__rn", * FROM read_parquet({lit(src.as_posix())})')
            cols = {r[0]: r[1] for r in con.execute(f"DESCRIBE {table}").fetchall()}
            for col in staging_cols:
                if col not in cols:
                    raise ConstructionStateError(f"{src.name} has no column {col}")
                con.execute(f'UPDATE {table} SET {q(col)} = CAST(a.val AS {cols[col]}) FROM applied a '
                            f'WHERE a.staging_col = {lit(col)} AND {table}."__rn" = a.row_ref')
            if ERRORS_COLUMN in cols:
                for target in targets:
                    tag = f"{target}: "
                    con.execute(
                        f"UPDATE {table} SET {q(ERRORS_COLUMN)} = nullif(array_to_string(list_filter("
                        f"string_split(coalesce({q(ERRORS_COLUMN)}, ''), '; '), x -> x <> '' AND NOT starts_with(x, {lit(tag)})), '; '), '') "
                        f'WHERE "__rn" IN (SELECT row_ref FROM applied WHERE target = {lit(target)})')
                if STATUS_COLUMN in cols:
                    con.execute(f"UPDATE {table} SET {q(STATUS_COLUMN)} = CASE WHEN {q(ERRORS_COLUMN)} IS NULL THEN 'OK' ELSE 'FAILED' END "
                                f'WHERE "__rn" IN (SELECT row_ref FROM applied)')
            tmp = final.with_suffix(".parquet.tmp")
            con.execute(f'COPY (SELECT * EXCLUDE ("__rn") FROM {table} ORDER BY "__rn") TO {lit(tmp.as_posix())} (FORMAT parquet)')
            tmp_files.append((tmp, final))
        return tmp_files

    def _write_lineage(self, con, run_id: int, user: str, at: datetime, filename: str, tmp_files) -> int:
        """One lineage row per merged cell, then swap the merged files in - in one transaction."""
        stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
        who = re.sub(r"[\r\n]+", " ", user)[:80]
        src_file = re.sub(r"[\r\n]+", " ", filename)[:120]
        msg_prefix = f"[CONSTRUCTED] by {who} at {stamp} via {src_file}; previous: "
        fd, csv_name = tempfile.mkstemp(prefix="construction_lineage_", suffix=".csv")
        os.close(fd)
        try:
            con.execute(
                f"COPY (SELECT {int(run_id)} AS run_id, pk AS source_pk_value, CAST(NULL AS VARCHAR) AS target_pk_value, "
                f"target AS target_column_name, old_val AS source_raw_value, val AS transformed_value, rule_id AS rule_applied_id, "
                f"false AS loaded_successfully, {lit(msg_prefix)} || coalesce(old_err, 'empty') AS error_message "
                f"FROM applied ORDER BY row_ref, fidx) TO {lit(Path(csv_name).as_posix())} (FORMAT csv, HEADER false)")
            with self._sf.begin() as session, open(csv_name, "r", encoding="utf-8", newline="") as fh:
                cursor = session.connection().connection.cursor()
                try:
                    cursor.copy_expert(
                        "COPY mig_cell_lineage (run_id, source_pk_value, target_pk_value, target_column_name, source_raw_value, "
                        "transformed_value, rule_applied_id, loaded_successfully, error_message) FROM STDIN WITH (FORMAT csv)", fh)
                    rows = int(cursor.rowcount)
                finally:
                    cursor.close()
                for tmp, final in tmp_files:  # inside the transaction: a failed swap rolls the lineage back
                    os.replace(tmp, final)
            return rows
        finally:
            os.remove(csv_name)
            for tmp, _ in tmp_files:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)

    def _count_pending(self, con, cdir: Path, tpl: Path, template_meta, spec_by_col) -> int:
        staging = self._base_staging_after(cdir)
        con.execute(f'CREATE OR REPLACE TEMP TABLE now_stg AS SELECT CAST(row_number() OVER () AS BIGINT) AS "__rn", * FROM read_parquet({lit(staging.as_posix())})')
        total = 0
        for pos, f in enumerate(template_meta["fields"]):
            col = q(f["staging_column"])
            total += int(con.execute(
                f'SELECT count(*) FROM tpl t JOIN now_stg g ON g."__rn" = t.row_ref WHERE t.{q("need_" + str(pos))} '
                f"AND (g.{col} IS NULL OR trim(CAST(g.{col} AS VARCHAR)) = '')").fetchone()[0])
        return total

    def _base_staging_after(self, cdir: Path) -> Path:
        constructed = cdir / "staging.parquet"
        return constructed if constructed.is_file() else cdir.parent / "staging.parquet"

    # ------------------------------------------------------------------ 3. re-validate
    def revalidate(self, run_id: int, user: str, dedup_config: Optional[DedupConfig] = None) -> RevalidationResult:
        if not user or not user.strip():
            raise ValueError("user must not be empty")
        with self._lock(run_id):
            mapping_set_id, phase, status = self._run(run_id)
            self._guard(phase, status, run_id)
            self._load_report(run_id)
            cdir = self._cdir(run_id)
            constructed = cdir / "staging.parquet"
            if not constructed.is_file():
                raise ConstructionStateError("no constructed data has been merged yet; upload a completed template first")

            deduped = False
            if dedup_config is not None:
                DeduplicationEngine(self._memory).deduplicate(constructed, cdir / "deduplicated.parquet", dedup_config)
                deduped = True
            validation_input = cdir / "deduplicated.parquet" if (cdir / "deduplicated.parquet").is_file() else constructed

            version = len(list(self.run_dir(run_id).glob("validation_c*"))) + 1
            report = self._engine.validate(mapping_set_id, validation_input, self.run_dir(run_id) / f"validation_c{version}", run_id=run_id)
            history = self.run_dir(run_id) / "validation_history"
            history.mkdir(exist_ok=True)
            current_json = self.run_dir(run_id) / "validation.json"
            shutil.copy(current_json, history / f"validation_before_c{version}.json")
            current_json.write_text(report.model_dump_json(), encoding="utf-8")
            defects = self._defects.record_defects(run_id, report)
            transformed_ok, total = self._refresh_run_counters(run_id, constructed)

            revoked = self._revoke_signoff(run_id, version)
            readiness = self._governance.evaluate_readiness(run_id)
            self._log(run_id, "revalidate", user.strip(), quality_score=report.quality_score, valid_records=report.valid_records,
                      defective_records=report.defective_records, open_defects=defects.total_open, gate=readiness.state.value,
                      signoff_revoked=revoked, deduplicated_again=deduped, records_transformed=transformed_ok,
                      records_failed=total - transformed_ok)
            return RevalidationResult(run_id=run_id, validation=report, defects=defects, readiness=readiness,
                                      signoff_revoked=revoked, deduplicated_again=deduped)

    def _refresh_run_counters(self, run_id: int, constructed: Path) -> tuple[int, int]:
        """Constructed values turn failed records into valid ones, so `records_transformed` / `records_failed`
        (set by the transformation stage) are recomputed from the merged staging. Not yet loaded here, so
        records_failed holds transformation failures only."""
        with bounded_connection(self._memory) as con:
            total, ok = con.execute(
                f"SELECT count(*), count(*) FILTER (WHERE coalesce({q(STATUS_COLUMN)}, 'OK') = 'OK') "
                f"FROM read_parquet({lit(constructed.as_posix())})").fetchone()
        with self._sf.begin() as s:
            s.execute(update(MigMigrationRun).where(MigMigrationRun.id == run_id).values(
                records_transformed=int(ok), records_failed=int(total - ok)))
        return int(ok), int(total)

    def _revoke_signoff(self, run_id: int, version: int) -> bool:
        """New data invalidates an earlier load sign-off (and any cockpit file built from the old data)."""
        run_dir = self.run_dir(run_id)
        revoked = False
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        signoff = run_dir / "signoff.json"
        if signoff.is_file():
            signoff.rename(run_dir / f"signoff.revoked.{stamp}.json")
            revoked = True
        cockpit = run_dir / "cockpit.xlsx"
        if cockpit.is_file():
            cockpit.rename(run_dir / f"cockpit.stale.{stamp}.xlsx")
        return revoked

    # ------------------------------------------------------------------ placeholder rules
    def add_placeholder_rules(self, mapping_set_id: int, user: str) -> list[PlaceholderRule]:
        """For mandatory SAP fields with no rule at all: add a STATIC_VALUE NULL rule so the pipeline
        reports them as missing and Data Construction can fill them. DRAFT mapping sets only."""
        created: list[PlaceholderRule] = []
        with self._sf.begin() as s:
            mapping = s.scalar(select(MapTableRule).where(MapTableRule.id == mapping_set_id).with_for_update())
            if mapping is None:
                raise NotFoundError(f"mapping set {mapping_set_id} does not exist")
            if mapping.status != MappingStatus.DRAFT:
                raise ConstructionStateError(f"mapping set {mapping_set_id} is {mapping.status.value}; placeholders can only be added to a DRAFT mapping set")
            object_name = s.get(CatTable, mapping.target_table_id).business_object
            mapped = set(s.scalars(select(MapFieldRule.target_column_id).where(MapFieldRule.mapping_set_id == mapping_set_id)))
            rows = s.execute(
                select(CatColumn, CatTable.table_name).join(CatTable, CatColumn.table_id == CatTable.id)
                .join(CatDataSource, CatTable.source_id == CatDataSource.id)
                .where(CatDataSource.target_type == TargetType.TARGET, CatTable.business_object == object_name,
                       CatColumn.is_mandatory.is_(True), CatColumn.column_name.not_in(GENERATED_KEYS))
                .order_by(CatTable.id, CatColumn.ordinal_position)).all()
            for column, table_name in rows:
                if column.id in mapped:
                    continue
                rule = MapFieldRule(
                    mapping_set_id=mapping_set_id, source_column_id=None, target_column_id=column.id,
                    rule_type=RuleType.STATIC_VALUE, transformation_logic="NULL", ai_suggested=False,
                    ai_reasoning=f"Construction placeholder created by {user}: no legacy source for this mandatory field; "
                                 "a data steward must construct the value.",
                    is_mandatory_target=True)
                s.add(rule)
                s.flush()
                created.append(PlaceholderRule(rule_id=rule.id, target=f"{table_name}.{column.column_name}"))
        return created
