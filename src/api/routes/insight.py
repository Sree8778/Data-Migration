"""Visibility across cycles: how two runs (mock loads, a dress rehearsal, the production load) compare, and one dashboard of where the programme stands.

A migration is run several times before it counts. What the people responsible need is not one run's numbers but the direction: did the data get better between
mock load 1 and mock load 2, which defects are gone, which are new, what is left before the gate opens, and is anything stuck.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from src.api.context import AppContext
from src.api.deps import get_ctx
from src.db.enums import RunStatus
from src.db.models import CatDataSource, CatTable, MapTableRule, MigMigrationRun
from src.db.enums import TargetType

router = APIRouter(tags=["insight"])


class RunFigures(BaseModel):
    run_id: int
    wave_name: str
    mapping_set_id: int
    status: str
    started_at: Optional[datetime] = None
    input_records: int
    valid_records: int
    defective_records: int
    duplicate_children: int
    quality_score: Optional[float] = None
    gate: Optional[str] = None  # READY_FOR_LOAD / BLOCKED ...
    open_critical: Optional[int] = None


class DefectChange(BaseModel):
    signature: str
    before: int
    after: int
    change: int


class CycleComparison(BaseModel):
    base: RunFigures
    other: RunFigures
    quality_change: Optional[float] = None
    valid_change: int
    defective_change: int
    resolved: list[DefectChange]  # in the base run, gone in the other
    new: list[DefectChange]  # not in the base run, there in the other
    improved: list[DefectChange]
    worsened: list[DefectChange]
    unchanged: int
    verdict: str


class WaveStatus(BaseModel):
    wave: str
    runs: int
    latest_run: int
    status: str
    gate: Optional[str] = None
    quality_score: Optional[float] = None
    valid_records: Optional[int] = None
    open_critical: Optional[int] = None
    trend: list[Optional[float]]  # quality score of each run of the wave, oldest first


class TableQuality(BaseModel):
    table_id: int
    table_name: str
    score: float
    assessed_at: str
    change: Optional[float] = None
    high_findings: int


class Dashboard(BaseModel):
    generated_at: str
    tables: dict[str, int]
    mapping_sets: dict[str, int]
    runs: dict[str, int]
    waves: list[WaveStatus]
    data_quality: list[TableQuality]
    tasks: dict = {}
    attention: list[str]


def _figures(ctx: AppContext, run: MigMigrationRun, with_gate: bool = True) -> RunFigures:
    report = ctx.load_validation_report(run.id)
    gate, critical = None, None
    if with_gate and report is not None:
        try:
            readiness = ctx.governance.evaluate_readiness(run.id)
            gate, critical = readiness.state, readiness.open_critical_defects
        except Exception:  # noqa: BLE001 - a missing gate must not hide the figures
            pass
    return RunFigures(
        run_id=run.id, wave_name=run.wave_name, mapping_set_id=run.mapping_set_id, status=run.status.value, started_at=run.started_at,
        input_records=report.input_records if report else run.records_extracted, valid_records=report.valid_records if report else 0,
        defective_records=report.defective_records if report else run.records_failed, duplicate_children=report.duplicate_children_excluded if report else 0,
        quality_score=report.quality_score if report else None, gate=gate, open_critical=critical)


@router.get("/cycles/compare", response_model=CycleComparison)
def compare_cycles(base: int, other: int, ctx: AppContext = Depends(get_ctx)) -> CycleComparison:
    """Two runs side by side: the figures, the change in quality, and which defect groups were resolved, are new, improved or got worse."""
    with ctx.session_factory() as s:
        runs = {i: s.get(MigMigrationRun, i) for i in (base, other)}
        if any(r is None for r in runs.values()):
            raise HTTPException(404, f"run {base if runs[base] is None else other} does not exist")
        a, b = _figures(ctx, runs[base]), _figures(ctx, runs[other])
    if a.quality_score is None or b.quality_score is None:
        raise HTTPException(409, f"run {base if a.quality_score is None else other} has not been validated yet, so there is nothing to compare")
    before = {f.signature: f.violation_count for f in ctx.load_validation_report(base).failures}
    after = {f.signature: f.violation_count for f in ctx.load_validation_report(other).failures}
    resolved = [DefectChange(signature=k, before=v, after=0, change=-v) for k, v in sorted(before.items()) if k not in after]
    new = [DefectChange(signature=k, before=0, after=v, change=v) for k, v in sorted(after.items()) if k not in before]
    both = [(k, before[k], after[k]) for k in sorted(before) if k in after]
    improved = [DefectChange(signature=k, before=x, after=y, change=y - x) for k, x, y in both if y < x]
    worsened = [DefectChange(signature=k, before=x, after=y, change=y - x) for k, x, y in both if y > x]
    change = round(b.quality_score - a.quality_score, 2)
    if change > 0 and not new and not worsened:
        verdict = f"Better: quality up {change} points, {len(resolved)} defect group(s) resolved, nothing new or worse."
    elif change > 0:
        verdict = f"Better overall (+{change} points), but {len(new)} defect group(s) are new and {len(worsened)} got worse: look at those first."
    elif change < 0:
        verdict = f"Worse: quality down {abs(change)} points; {len(new)} new and {len(worsened)} worsened defect group(s)."
    else:
        verdict = "No change in quality between the two runs."
    return CycleComparison(base=a, other=b, quality_change=change, valid_change=b.valid_records - a.valid_records, defective_change=b.defective_records - a.defective_records,
                           resolved=resolved, new=new, improved=improved, worsened=worsened, unchanged=len(both) - len(improved) - len(worsened), verdict=verdict)


@router.get("/dashboard", response_model=Dashboard)
def dashboard(ctx: AppContext = Depends(get_ctx)) -> Dashboard:
    """Where the programme stands: tables, mapping sets by status, runs by status, each wave's latest run with its quality trend, the latest data quality assessment
    of every table, and the things that need a person (a stuck run, an interrupted delivery, a blocked gate, a worsening trend)."""
    with ctx.session_factory() as s:
        tables_total = s.scalar(select(func.count()).select_from(CatTable).join(CatDataSource, CatTable.source_id == CatDataSource.id).where(CatDataSource.target_type != TargetType.TARGET)) or 0
        table_rows = s.execute(select(CatTable.id, CatTable.table_name).join(CatDataSource, CatTable.source_id == CatDataSource.id).where(CatDataSource.target_type != TargetType.TARGET)).all()
        mapping = {st.value: n for st, n in s.execute(select(MapTableRule.status, func.count()).group_by(MapTableRule.status)).all()}
        runs_by_status = {st.value: n for st, n in s.execute(select(MigMigrationRun.status, func.count()).group_by(MigMigrationRun.status)).all()}
        all_runs = list(s.scalars(select(MigMigrationRun).order_by(MigMigrationRun.id)))
    landed = sum(1 for tid, _ in table_rows if ctx.source_file(tid) is not None)

    waves: list[WaveStatus] = []
    attention: list[str] = []
    for name in dict.fromkeys(r.wave_name for r in all_runs):
        in_wave = [r for r in all_runs if r.wave_name == name]
        latest = in_wave[-1]
        fig = _figures(ctx, latest)
        trend = [(ctx.load_validation_report(r.id).quality_score if ctx.load_validation_report(r.id) else None) for r in in_wave]
        waves.append(WaveStatus(wave=name, runs=len(in_wave), latest_run=latest.id, status=latest.status.value, gate=fig.gate, quality_score=fig.quality_score, valid_records=fig.valid_records,
                                open_critical=fig.open_critical, trend=trend))
        scored = [t for t in trend if t is not None]
        if len(scored) >= 2 and scored[-1] < scored[-2]:
            attention.append(f"wave {name}: quality fell from {scored[-2]} to {scored[-1]} between its last two runs")
        if fig.gate == "BLOCKED":
            attention.append(f"wave {name} (run {latest.id}) is blocked at the gate" + (f": {fig.open_critical} critical defect(s) open" if fig.open_critical else ""))
    if runs_by_status.get("RUNNING"):
        attention.append(f"{runs_by_status['RUNNING']} run(s) in RUNNING; one that survives a server restart is marked FAILED automatically")
    for p in (ctx.workspace / "runs").glob("run_*/db_loads_pending.json") if (ctx.workspace / "runs").is_dir() else []:
        attention.append(f"{p.parent.name}: a database delivery was interrupted and must be settled (POST /api/pipeline/recover-db)")

    quality: list[TableQuality] = []
    for tid, tname in table_rows:
        report = ctx.quality.latest(tid)
        if report:
            quality.append(TableQuality(table_id=tid, table_name=tname, score=report.overall_score, assessed_at=report.assessed_at, change=report.change,
                                        high_findings=sum(1 for f in report.findings if f.severity == "HIGH")))
            if report.change is not None and report.change < 0:
                attention.append(f"table {tname}: data quality fell {abs(report.change)} points at the last assessment")
    work = ctx.tasks.summary()
    if work["overdue"]:
        attention.append(f"{work['overdue']} stewardship task(s) are overdue")
    if work["unassigned"]:
        attention.append(f"{work['unassigned']} open task(s) have no owner")
    return Dashboard(generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), tables={"registered": int(tables_total), "landed": landed}, mapping_sets=mapping,
                     runs=runs_by_status, waves=waves, data_quality=sorted(quality, key=lambda t: t.score), tasks=work, attention=attention)
