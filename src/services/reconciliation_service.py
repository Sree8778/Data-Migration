"""Final reconciliation / audit proof: Source -> Transformed -> Validated -> Loaded.

The accounting identity checked for every run (all counts are source records):

    extracted = duplicate children + rejected by validation + loaded in SAP + failed in SAP + not attempted

Counts for the load come from the lineage outcome rows (`BUT000.PARTNER`), the extract/transform
counts from `mig_migration_runs`, and the duplicate / rejected / valid split from Module 5's
`ValidationReport` (pass it in; without it the report is flagged incomplete and cannot pass).
The report also contains independent cross-checks (counters vs lineage, unique SAP numbers,
segregation of duties on the mapping) and a deterministic cell-lineage spot check.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import duckdb
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import MapTableRule, MigMigrationRun
from src.schemas.sap import (
    CheckResult,
    DropOffLine,
    LineageSampleCheck,
    ReconCounts,
    ReconciliationReport,
)
from src.schemas.validation import Severity, ValidationReport
from src.services._duckdb_util import lit
from src.services.defect_service import DefectService
from src.services.metadata_service import NotFoundError
from src.services.sap_loader_service import PARTNER_COLUMN

_SAMPLE_PKS = 5


class ReconciliationService:
    def __init__(self, session_factory: sessionmaker[Session], sample_size: int = 20) -> None:
        self._session_factory = session_factory
        self._sample_size = sample_size

    # ------------------------------------------------------------------ report
    def generate_reconciliation_report(
        self, run_id: int, validation_report: Optional[ValidationReport] = None
    ) -> ReconciliationReport:
        with self._session_factory() as session:
            run = session.get(MigMigrationRun, run_id)
            if run is None:
                raise NotFoundError(f"migration run {run_id} does not exist")
            mapping = session.get(MapTableRule, run.mapping_set_id)
            facts = self._load_facts(session, run_id, run.mapping_set_id)
            sample = self._lineage_sample(session, run_id, run.mapping_set_id)
            load_errors = self._load_errors(session, run_id)
            run_info = dict(wave=run.wave_name, status=run.status.value, phase=run.current_phase.value,
                            extracted=run.records_extracted, transformed=run.records_transformed,
                            loaded_counter=run.records_loaded, failed_counter=run.records_failed)
            mapping_info = dict(id=mapping.id, version=mapping.version, created_by=mapping.created_by,
                                approved_by=mapping.approved_by, approved_at=mapping.approved_at)

        extracted, transformed_ok = run_info["extracted"], run_info["transformed"]
        loaded, load_failed = facts["loaded"], facts["failed"]
        attempted = loaded + load_failed
        complete = validation_report is not None

        dup = rejected = valid = not_attempted = None
        if validation_report is not None:
            dup = validation_report.duplicate_children_excluded
            rejected = validation_report.defective_records
            valid = validation_report.valid_records
            not_attempted = valid - attempted

        counts = ReconCounts(
            extracted=extracted, transformed_ok=transformed_ok, transform_failed=extracted - transformed_ok,
            duplicate_children=dup, validation_rejected=rejected, valid=valid, load_attempted=attempted,
            loaded=loaded, load_failed=load_failed, not_attempted=not_attempted,
        )
        dropoff = self._dropoff(counts, validation_report, facts)
        accounted = sum(line.count for line in dropoff)
        variance = extracted - accounted
        checks = self._checks(run_info, mapping_info, counts, facts, validation_report)

        return ReconciliationReport(
            run_id=run_id, wave_name=run_info["wave"], mapping_set_id=mapping_info["id"],
            mapping_version=mapping_info["version"], mapping_created_by=mapping_info["created_by"],
            mapping_approved_by=mapping_info["approved_by"], mapping_approved_at=mapping_info["approved_at"],
            run_status=run_info["status"], run_phase=run_info["phase"], counts=counts, dropoff=dropoff,
            accounted_rows=accounted, variance=variance, balanced=variance == 0, complete=complete,
            checks=checks, lineage_sample=sample,
            validation_failures={f.signature: f.violation_count for f in validation_report.failures} if validation_report else {},
            load_errors=load_errors,
            open_critical_defects=DefectService(self._session_factory).count_open(run_id, Severity.CRITICAL),
            generated_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------ pieces
    @staticmethod
    def _load_facts(session: Session, run_id: int, mapping_set_id: int) -> dict:
        row = session.execute(text(
            """
            SELECT count(DISTINCT source_pk_value) FILTER (WHERE loaded_successfully),
                   count(DISTINCT source_pk_value) FILTER (WHERE NOT loaded_successfully),
                   count(*) FILTER (WHERE loaded_successfully AND target_pk_value IS NULL),
                   count(DISTINCT target_pk_value) FILTER (WHERE loaded_successfully),
                   count(*) FILTER (WHERE loaded_successfully)
              FROM mig_cell_lineage WHERE run_id = :r AND target_column_name = :p
            """), {"r": run_id, "p": PARTNER_COLUMN}).one()
        stray = session.execute(text(
            """
            SELECT count(*) FROM mig_cell_lineage
             WHERE run_id = :r AND target_column_name <> :p AND loaded_successfully AND target_pk_value IS NULL
            """), {"r": run_id, "p": PARTNER_COLUMN}).scalar_one()
        failed_pks = [r[0] for r in session.execute(text(
            """
            SELECT source_pk_value FROM mig_cell_lineage
             WHERE run_id = :r AND target_column_name = :p AND NOT loaded_successfully
             ORDER BY id LIMIT :n
            """), {"r": run_id, "p": PARTNER_COLUMN, "n": _SAMPLE_PKS})]
        return {"loaded": int(row[0]), "failed": int(row[1]), "loaded_without_number": int(row[2]),
                "distinct_numbers": int(row[3]), "loaded_rows": int(row[4]), "stray_loaded": int(stray),
                "failed_sample": failed_pks}

    @staticmethod
    def _load_errors(session: Session, run_id: int) -> dict[str, int]:
        rows = session.execute(text(
            """
            SELECT error_message, count(*) FROM mig_cell_lineage
             WHERE run_id = :r AND target_column_name = :p AND NOT loaded_successfully AND error_message IS NOT NULL
             GROUP BY error_message ORDER BY count(*) DESC, error_message LIMIT 10
            """), {"r": run_id, "p": PARTNER_COLUMN}).all()
        return {msg: int(n) for msg, n in rows}

    def _lineage_sample(self, session: Session, run_id: int, mapping_set_id: int) -> LineageSampleCheck:
        pks = [r[0] for r in session.execute(text(
            """
            SELECT source_pk_value FROM mig_cell_lineage
             WHERE run_id = :r AND target_column_name = :p AND loaded_successfully
             ORDER BY md5(source_pk_value) LIMIT :n
            """), {"r": run_id, "p": PARTNER_COLUMN, "n": self._sample_size})]
        if not pks:
            return LineageSampleCheck(sampled=0, passed=0)
        rows = session.execute(text(
            """
            SELECT source_pk_value,
                   count(*)                                                         AS n_rows,
                   count(*) FILTER (WHERE target_column_name = :p)                  AS n_partner,
                   count(target_pk_value)                                           AS n_with_target,
                   count(DISTINCT target_pk_value)                                  AS n_targets,
                   bool_and(loaded_successfully)                                    AS all_loaded,
                   count(*) FILTER (WHERE rule_applied_id IS NOT NULL AND rule_applied_id NOT IN
                        (SELECT id FROM map_field_rules WHERE mapping_set_id = :m)) AS foreign_rules,
                   max(target_pk_value)                                             AS partner
              FROM mig_cell_lineage WHERE run_id = :r AND source_pk_value = ANY(:pks)
             GROUP BY source_pk_value ORDER BY md5(source_pk_value)
            """), {"p": PARTNER_COLUMN, "m": mapping_set_id, "r": run_id, "pks": pks}).all()
        failures: list[str] = []
        examples: list[str] = []
        passed = 0
        seen = {r[0] for r in rows}
        for missing in sorted(set(pks) - seen):
            failures.append(f"{missing}: no lineage rows found")
        for pk, n_rows, n_partner, n_target, n_targets, all_loaded, foreign, partner in rows:
            problems = []
            if n_partner != 1:
                problems.append(f"{n_partner} outcome rows (expected 1)")
            if n_target != n_rows or n_targets != 1:
                problems.append("rows disagree on the SAP partner number")
            if not all_loaded:
                problems.append("some rows not marked loaded")
            if foreign:
                problems.append(f"{foreign} rows reference rules outside the mapping set")
            if problems:
                failures.append(f"{pk}: " + "; ".join(problems))
            else:
                passed += 1
        for pk in pks[:3]:
            cell = session.execute(text(
                """
                SELECT target_column_name, source_raw_value, transformed_value, target_pk_value
                  FROM mig_cell_lineage
                 WHERE run_id = :r AND source_pk_value = :pk AND target_column_name <> :p
                 ORDER BY id LIMIT 1
                """), {"r": run_id, "pk": pk, "p": PARTNER_COLUMN}).first()
            if cell:
                examples.append(f"source {pk} -> SAP {cell[3]}: {cell[0]} '{cell[1]}' -> '{cell[2]}'")
        return LineageSampleCheck(sampled=len(pks), passed=passed, failures=failures, examples=examples)

    def _dropoff(self, c: ReconCounts, report: Optional[ValidationReport], facts: dict) -> list[DropOffLine]:
        lines = [DropOffLine(reason="LOADED", count=c.loaded,
                             explanation="Created in SAP with a business partner number.")]
        lines.append(DropOffLine(reason="SAP_LOAD_FAILED", count=c.load_failed, sample_pks=facts["failed_sample"],
                                 explanation="Sent to SAP but rejected; the SAP message is stored in the lineage."))
        if report is None:
            lines.append(DropOffLine(
                reason="NOT_LOADED_BEFORE_SAP", count=c.extracted - c.load_attempted,
                explanation="Duplicates and validation rejects combined (no validation report supplied)."))
            return lines
        lines.append(DropOffLine(
            reason="VALIDATION_REJECTED", count=c.validation_rejected, sample_pks=self._pks(report.rejected_path),
            explanation=("Failed pre-load validation (" + ", ".join(
                f"{f.signature}={f.violation_count}" for f in report.failures[:5]) + ("..." if len(report.failures) > 5 else "")
                + f"); {c.transform_failed} of these already failed in transformation.")))
        lines.append(DropOffLine(
            reason="DUPLICATE_CHILD", count=c.duplicate_children, sample_pks=self._pks(report.duplicates_path),
            explanation="Merged into a surviving master record by deduplication; intentionally not loaded."))
        lines.append(DropOffLine(
            reason="VALID_NOT_ATTEMPTED", count=c.not_attempted,
            explanation="Valid but never sent to SAP (load aborted or not run)."))
        return lines

    @staticmethod
    def _pks(path: Optional[str]) -> list[str]:
        if not path or not Path(path).is_file():
            return []
        rows = duckdb.connect().execute(
            f'SELECT CAST("_SRC_PK" AS VARCHAR) FROM read_parquet({lit(Path(path).as_posix())}) LIMIT {_SAMPLE_PKS}').fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def _checks(run: dict, mapping: dict, c: ReconCounts, facts: dict, report: Optional[ValidationReport]) -> list[CheckResult]:
        checks = [
            CheckResult(name="run_counter_loaded_matches_lineage", passed=run["loaded_counter"] == c.loaded,
                        detail=f"records_loaded={run['loaded_counter']}, lineage loaded records={c.loaded}"),
            CheckResult(name="run_counter_failed_matches_transform_plus_load",
                        passed=run["failed_counter"] == c.transform_failed + c.load_failed,
                        detail=f"records_failed={run['failed_counter']}, transform failed={c.transform_failed} "
                               f"+ SAP load failed={c.load_failed}"),
            CheckResult(name="every_loaded_record_has_sap_number", passed=facts["loaded_without_number"] == 0
                        and facts["stray_loaded"] == 0,
                        detail=f"{facts['loaded_without_number'] + facts['stray_loaded']} loaded lineage rows lack a target key"),
            CheckResult(name="sap_numbers_are_unique", passed=facts["distinct_numbers"] == facts["loaded_rows"],
                        detail=f"{facts['distinct_numbers']} distinct numbers for {facts['loaded_rows']} loaded records"),
            CheckResult(name="load_phase_reached", passed=run["phase"] == "LOAD" and run["status"] in ("SUCCEEDED", "FAILED"),
                        detail=f"phase={run['phase']}, status={run['status']}"),
            CheckResult(name="mapping_approved_with_segregation_of_duties",
                        passed=bool(mapping["approved_by"]) and mapping["approved_by"].strip().casefold()
                        != mapping["created_by"].strip().casefold(),
                        detail=f"created_by={mapping['created_by']}, approved_by={mapping['approved_by']}"),
            CheckResult(name="validation_report_supplied", passed=report is not None,
                        detail="valid / duplicate / rejected split available" if report else
                        "pass the Module 5 ValidationReport for a complete reconciliation"),
        ]
        if report is not None:
            checks += [
                CheckResult(name="validation_input_equals_extract", passed=report.input_records == c.extracted,
                            detail=f"validated {report.input_records} rows, extracted {c.extracted}"),
                CheckResult(name="validation_split_is_consistent",
                            passed=report.duplicate_children_excluded + report.valid_records + report.defective_records
                            == report.input_records,
                            detail=f"{report.duplicate_children_excluded} duplicates + {report.valid_records} valid + "
                                   f"{report.defective_records} rejected vs {report.input_records} input"),
                CheckResult(name="load_attempts_do_not_exceed_valid_records", passed=(c.not_attempted or 0) >= 0,
                            detail=f"{c.load_attempted} attempted of {c.valid} valid"),
            ]
        return checks

    # ------------------------------------------------------------------ export
    def export(self, report: ReconciliationReport, directory: str | Path) -> dict[str, str]:
        """Write the Markdown and HTML audit summaries; returns {format: path}."""
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        base = f"reconciliation_run_{report.run_id}"
        paths = {"markdown": out / f"{base}.md", "html": out / f"{base}.html"}
        paths["markdown"].write_text(render_markdown(report), encoding="utf-8")
        paths["html"].write_text(render_html(report), encoding="utf-8")
        return {k: str(v) for k, v in paths.items()}


# ---------------------------------------------------------------------- rendering
def _rows(report: ReconciliationReport) -> dict[str, list[list[str]]]:
    c = report.counts
    fmt = lambda v: "n/a" if v is None else f"{v:,}"  # noqa: E731
    return {
        "run": [["Run", str(report.run_id)], ["Wave", report.wave_name], ["Mapping set", f"{report.mapping_set_id} (v{report.mapping_version})"],
                ["Created by", report.mapping_created_by], ["Approved by", report.mapping_approved_by or "-"],
                ["Approved at", report.mapping_approved_at.isoformat() if report.mapping_approved_at else "-"],
                ["Run status / phase", f"{report.run_status} / {report.run_phase}"],
                ["Open CRITICAL defects", str(report.open_critical_defects)],
                ["Generated", report.generated_at.isoformat()]],
        "counts": [["Extracted from source", fmt(c.extracted)], ["Transformed OK", fmt(c.transformed_ok)],
                   ["Transformation failed", fmt(c.transform_failed)], ["Duplicate children", fmt(c.duplicate_children)],
                   ["Rejected by validation", fmt(c.validation_rejected)], ["Valid for load", fmt(c.valid)],
                   ["Sent to SAP", fmt(c.load_attempted)], ["Loaded in SAP", fmt(c.loaded)],
                   ["Failed in SAP", fmt(c.load_failed)], ["Valid but not attempted", fmt(c.not_attempted)]],
        "dropoff": [[d.reason, f"{d.count:,}", d.explanation, ", ".join(d.sample_pks) or "-"] for d in report.dropoff],
        "checks": [["PASS" if k.passed else "FAIL", k.name, k.detail] for k in report.checks],
    }


def render_markdown(report: ReconciliationReport) -> str:
    rows = _rows(report)

    def table(header: list[str], body: list[list[str]]) -> str:
        esc = lambda s: s.replace("|", "\\|")  # noqa: E731
        return "\n".join(["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
                         + ["| " + " | ".join(esc(x) for x in r) + " |" for r in body])

    verdict = "PASSED" if report.passed else "NOT PASSED"
    out = [f"# Migration Reconciliation - Run {report.run_id}", "",
           f"**Result: {verdict}** - variance {report.variance} row(s); "
           f"{'complete' if report.complete else 'INCOMPLETE (no validation report)'}", "",
           "## Run", table(["Item", "Value"], rows["run"]), "",
           "## Record counts", table(["Stage", "Records"], rows["counts"]), "",
           "## Drop-off accounting", table(["Reason", "Records", "Explanation", "Sample source keys"], rows["dropoff"]), "",
           f"Accounted rows: {report.accounted_rows:,} of {report.counts.extracted:,} extracted "
           f"(variance {report.variance}).", "",
           "## Control checks", table(["Result", "Check", "Detail"], rows["checks"]), "",
           "## Cell-lineage sample", f"{report.lineage_sample.passed} of {report.lineage_sample.sampled} sampled loaded records verified."]
    out += [f"- {e}" for e in report.lineage_sample.examples]
    out += [f"- FAILURE: {f}" for f in report.lineage_sample.failures]
    if report.validation_failures:
        out += ["", "## Validation failures by rule signature",
                table(["Signature", "Records"], [[k, f"{v:,}"] for k, v in report.validation_failures.items()])]
    if report.load_errors:
        out += ["", "## SAP load errors", table(["Message", "Records"], [[k, f"{v:,}"] for k, v in report.load_errors.items()])]
    out += ["", "## Sign-off", "", "| Role | Name | Date | Signature |", "|---|---|---|---|",
            "| Data owner | | | |", "| Data steward | | | |", "| Auditor | | | |", ""]
    return "\n".join(out)


def render_html(report: ReconciliationReport) -> str:
    rows = _rows(report)
    e = html.escape

    def table(header: list[str], body: list[list[str]]) -> str:
        head = "".join(f"<th>{e(h)}</th>" for h in header)
        trs = "".join("<tr>" + "".join(f"<td>{e(x)}</td>" for x in r) + "</tr>" for r in body)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{trs}</tbody></table>"

    verdict = "PASSED" if report.passed else "NOT PASSED"
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>Reconciliation - Run {report.run_id}</title>",
        "<style>body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#1a1a1a}"
        "table{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}th,td{border:1px solid #ccc;padding:.35rem .6rem;"
        "text-align:left;vertical-align:top}th{background:#f2f2f2}.pass{color:#0a6b2d}.fail{color:#b00020}</style></head><body>",
        f"<h1>Migration Reconciliation - Run {report.run_id}</h1>",
        f"<p><strong class='{'pass' if report.passed else 'fail'}'>Result: {verdict}</strong> - variance {report.variance} row(s); "
        f"{'complete' if report.complete else 'INCOMPLETE (no validation report)'}</p>",
        "<h2>Run</h2>", table(["Item", "Value"], rows["run"]),
        "<h2>Record counts</h2>", table(["Stage", "Records"], rows["counts"]),
        "<h2>Drop-off accounting</h2>", table(["Reason", "Records", "Explanation", "Sample source keys"], rows["dropoff"]),
        f"<p>Accounted rows: {report.accounted_rows:,} of {report.counts.extracted:,} extracted (variance {report.variance}).</p>",
        "<h2>Control checks</h2>", table(["Result", "Check", "Detail"], rows["checks"]),
        "<h2>Cell-lineage sample</h2>",
        f"<p>{report.lineage_sample.passed} of {report.lineage_sample.sampled} sampled loaded records verified.</p>",
        "<ul>" + "".join(f"<li>{e(x)}</li>" for x in report.lineage_sample.examples)
        + "".join(f"<li class='fail'>FAILURE: {e(x)}</li>" for x in report.lineage_sample.failures) + "</ul>",
    ]
    if report.validation_failures:
        parts += ["<h2>Validation failures by rule signature</h2>",
                  table(["Signature", "Records"], [[k, f"{v:,}"] for k, v in report.validation_failures.items()])]
    if report.load_errors:
        parts += ["<h2>SAP load errors</h2>", table(["Message", "Records"], [[k, f"{v:,}"] for k, v in report.load_errors.items()])]
    parts += ["<h2>Sign-off</h2>", table(["Role", "Name", "Date", "Signature"],
                                          [["Data owner", "", "", ""], ["Data steward", "", "", ""], ["Auditor", "", "", ""]]),
              "</body></html>"]
    return "".join(parts)
