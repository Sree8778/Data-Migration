"""Delta analysis: which valid records are new, changed, unchanged or gone since an earlier run.

A second (third, fourth) run of the same migration should not start from zero. Every run is still validated and deduplicated against the WHOLE
population, so its defects, its gate and its certificate are complete; the delta only decides what is worth DELIVERING again. Two valid payloads
are compared record by record, on the target values (the fields SAP will receive, not the legacy columns):

    NEW        valid now, not valid in the baseline run
    CHANGED    valid in both, at least one target value differs (a content hash of the target fields)
    UNCHANGED  valid in both and identical: already delivered, nothing to do
    REMOVED    valid in the baseline, not valid now: say why (now rejected / now a duplicate / no longer in the source)

The records to deliver are written to delta_valid.parquet (with `_DELTA_KIND`); the keys that disappeared to delta_removed.parquet (with `_WHY`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.schemas.delta import DeltaCounts, DeltaReport
from src.services._duckdb_util import bounded_connection, lit, q
from src.services.db_loader_service import DbLoaderService

DELTA_KIND = "_DELTA_KIND"
REMOVED_WHY = "_WHY"
EXAMPLES = 5


class DeltaError(ValueError):
    """The two runs cannot be compared (different target fields, a missing payload)."""


class DeltaService:
    def __init__(self, loader: DbLoaderService, memory_limit_mb: int = 512) -> None:
        self._loader = loader
        self._memory = memory_limit_mb

    def compute(
        self, run_id: int, mapping_set_id: int, valid_path: str | Path, baseline_run_id: int, baseline_mapping_set_id: int, baseline_valid_path: str | Path,
        out_dir: Path, rejected_path: Optional[str] = None, duplicates_path: Optional[str] = None,
    ) -> DeltaReport:
        cur_path, base_path = Path(valid_path), Path(baseline_valid_path)
        for path, what in ((cur_path, f"run {run_id}"), (base_path, f"baseline run {baseline_run_id}")):
            if not path.is_file():
                raise DeltaError(f"{what} has no validated payload to compare")
        cur_cols = self._loader.target_columns(cur_path)
        base_cols = self._loader.target_columns(base_path)
        if cur_cols != base_cols:
            only = sorted(set(cur_cols) ^ set(base_cols))
            raise DeltaError(f"run {baseline_run_id} delivered different target fields than run {run_id} ({', '.join(only[:6])}"
                             f"{'...' if len(only) > 6 else ''}): the mapping changed, so run a full delivery instead")
        if not cur_cols:
            raise DeltaError("the payload holds no SAP target fields to compare")
        out_dir.mkdir(parents=True, exist_ok=True)
        delta_path, removed_path = out_dir / "delta_valid.parquet", out_dir / "delta_removed.parquet"

        def digest(path: Path) -> str:
            cols = ", ".join(f"coalesce(CAST({q(c)} AS VARCHAR), '')" for c in cur_cols)
            return f"SELECT CAST(\"_SRC_PK\" AS VARCHAR) AS k, md5(concat_ws(chr(31), {cols})) AS h FROM read_parquet({lit(path.as_posix())})"

        with bounded_connection(self._memory) as con:
            con.execute(f"CREATE TEMP TABLE cur AS {digest(cur_path)}")
            con.execute(f"CREATE TEMP TABLE base AS {digest(base_path)}")
            con.execute(
                "CREATE TEMP TABLE kinds AS SELECT coalesce(c.k, b.k) AS k, CASE WHEN b.k IS NULL THEN 'NEW' WHEN c.k IS NULL THEN 'REMOVED' "
                "WHEN c.h <> b.h THEN 'CHANGED' ELSE 'UNCHANGED' END AS kind FROM cur c FULL OUTER JOIN base b ON c.k = b.k")
            counts = dict(con.execute("SELECT kind, count(*) FROM kinds GROUP BY kind").fetchall())
            n_cur, n_base = (int(x) for x in con.execute("SELECT (SELECT count(*) FROM cur), (SELECT count(*) FROM base)").fetchone())
            examples = {kind: [r[0] for r in con.execute("SELECT k FROM kinds WHERE kind = ? ORDER BY k LIMIT ?", [kind, EXAMPLES]).fetchall()]
                        for kind in ("NEW", "CHANGED", "REMOVED") if counts.get(kind)}
            con.execute(
                f"COPY (SELECT v.*, kinds.kind AS {q(DELTA_KIND)} FROM read_parquet({lit(cur_path.as_posix())}) v JOIN kinds ON CAST(v.\"_SRC_PK\" AS VARCHAR) = kinds.k "
                f"WHERE kinds.kind IN ('NEW', 'CHANGED') ORDER BY kinds.k) TO {lit(delta_path.as_posix())} (FORMAT parquet)")
            why = "'NOT_IN_CURRENT_VALID'"
            joins = ""
            for alias, path, label in (("rj", rejected_path, "NOW_REJECTED"), ("dp", duplicates_path, "NOW_DUPLICATE")):
                if path and Path(path).is_file():
                    joins += f" LEFT JOIN (SELECT DISTINCT CAST(\"_SRC_PK\" AS VARCHAR) AS k FROM read_parquet({lit(Path(path).as_posix())})) {alias} ON {alias}.k = kinds.k"
                    why = f"CASE WHEN {alias}.k IS NOT NULL THEN '{label}' ELSE {why} END"
            # the nested CASEs put the last listed source first; both are exclusive in practice (a key is either rejected or a duplicate child)
            con.execute(f"COPY (SELECT kinds.k AS \"_SRC_PK\", {why} AS {q(REMOVED_WHY)} FROM kinds{joins} WHERE kinds.kind = 'REMOVED' ORDER BY kinds.k) "
                        f"TO {lit(removed_path.as_posix())} (FORMAT parquet)")
            reasons = dict(con.execute(f"SELECT {q(REMOVED_WHY)}, count(*) FROM read_parquet({lit(removed_path.as_posix())}) GROUP BY 1").fetchall())
        report = DeltaReport(
            run_id=run_id, baseline_run_id=baseline_run_id, mapping_set_id=mapping_set_id, baseline_mapping_set_id=baseline_mapping_set_id,
            checked_at=datetime.now(timezone.utc),
            counts=DeltaCounts(new=int(counts.get("NEW", 0)), changed=int(counts.get("CHANGED", 0)), unchanged=int(counts.get("UNCHANGED", 0)),
                               removed=int(counts.get("REMOVED", 0)), current_valid=n_cur, baseline_valid=n_base),
            removed_reasons={k: int(v) for k, v in reasons.items()}, examples=examples, columns_compared=len(cur_cols),
            delta_path=str(delta_path) if counts.get("NEW", 0) or counts.get("CHANGED", 0) else None,
            removed_path=str(removed_path) if counts.get("REMOVED", 0) else None)
        if report.delta_path is None:
            delta_path.unlink(missing_ok=True)
        if report.removed_path is None:
            removed_path.unlink(missing_ok=True)
        (out_dir / "delta.json").write_text(report.model_dump_json(indent=1), encoding="utf-8")
        return report
