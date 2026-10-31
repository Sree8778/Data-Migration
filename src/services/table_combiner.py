"""Combine landed tables with joins and filters into one new source table.

Real migrations rarely start from one table: a customer lives in a header table, a contacts table and a terms table. This service turns a STRUCTURED description (a base
table, joins on named columns, filters, the columns to keep) into one query over the landed snapshots, and refuses anything it cannot check:

  * there is no free-text SQL: aliases are validated names, columns must exist in the landed file (case-insensitively), operators come from a fixed list and every value
    is bound as a literal
  * join keys are compared as text the way the person chose (as they are / ignoring blanks around them / also ignoring case) and a blank key never matches another blank
  * before anything is created the combination is MEASURED: how many records of a table find no partner in the next, how many find several, how many rows result. A join that
    turns one base record into several rows (a header joined to its many contacts) is refused unless allowed, because it silently repeats customers
  * the result is landed exactly like a file or a database extract (a Parquet snapshot with a SHA-256 and a control total), and the description that produced it is kept with
    it, so the table can be rebuilt and shows where it came from
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.schemas.combine import (
    ALIAS, REF, CombinePreview, CombineSpec, FilterStep, JoinStats, JoinStep, OutputColumn)
from src.services._duckdb_util import bounded_connection, lit, q

MAX_OUTPUT_COLUMNS = 300


class CombineError(ValueError):
    """The description cannot be run as written (an unknown table or column, a join that does not connect, two columns with the same name)."""


@dataclass
class _Input:
    alias: str
    table_name: str
    path: Path
    columns: list[str]


@dataclass
class Plan:
    inputs: dict[str, _Input]  # by lower-cased alias, in the order base, joins
    from_sql: str  # "FROM ... JOIN ... ON ..." over the CTEs
    ctes: str
    where: str
    order: str
    outputs: list[tuple[str, str, str]]  # (alias, source column, output name)
    select_sql: str


def _norm(expression: str, match: str) -> str:
    text = f"CAST({expression} AS VARCHAR)"
    return {"exact": text, "trim": f"trim({text})", "trim_ci": f"upper(trim({text}))"}[match]


class TableCombiner:
    def __init__(self, memory_limit_mb: int = 1024) -> None:
        self._memory = memory_limit_mb

    # ------------------------------------------------------------------ planning (no data is read except the column names)
    def plan(self, spec: CombineSpec, tables: dict[int, tuple[str, Path]]) -> Plan:
        """`tables`: table id -> (table name, landed Parquet). Raises CombineError for anything that does not check out."""
        refs = [spec.base, *spec.joins]
        inputs: dict[str, _Input] = {}
        with bounded_connection(128) as con:
            for ref in refs:
                if ref.table_id not in tables or not tables[ref.table_id][1].is_file():
                    raise CombineError(f"table {ref.table_id} has no landed file to combine (land or import it first)")
                name, path = tables[ref.table_id]
                columns = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({lit(path.as_posix())})").fetchall()]
                inputs[ref.alias.lower()] = _Input(ref.alias, name, path, columns)

        def column(reference: str, allowed: Optional[set[str]] = None) -> tuple[_Input, str]:
            m = REF.match(reference or "")
            if not m:
                raise CombineError(f"'{reference}' is not a column reference: write it as alias.column, for example c.CUST_ID")
            alias = m.group(1).lower()
            if alias not in inputs:
                raise CombineError(f"unknown alias '{m.group(1)}' in '{reference}' (the aliases are {', '.join(i.alias for i in inputs.values())})")
            if allowed is not None and alias not in allowed:
                raise CombineError(f"'{reference}' refers to a table that is not joined yet at that point")
            wanted = m.group(2).strip().lower()
            actual = next((c for c in inputs[alias].columns if c.lower() == wanted), None)
            if actual is None:
                raise CombineError(f"table {inputs[alias].table_name} ({inputs[alias].alias}) has no column '{m.group(2)}'")
            return inputs[alias], actual

        ctes = ", ".join(f'{q(i.alias)} AS (SELECT *, row_number() OVER () AS "__rn" FROM read_parquet({lit(i.path.as_posix())}))' for i in inputs.values())
        from_sql = f"FROM {q(spec.base.alias)}"
        seen = {spec.base.alias.lower()}
        for step in spec.joins:
            conds = []
            for c in step.on:
                left, lcol = column(c.left, seen)
                right, rcol = column(c.right)
                if right.alias.lower() != step.alias.lower():
                    raise CombineError(f"in the join to {step.alias}, the right-hand column '{c.right}' must belong to {step.alias}")
                conds.append(f"{_norm(f'{q(left.alias)}.{q(lcol)}', spec.match)} = {_norm(f'{q(right.alias)}.{q(rcol)}', spec.match)} "
                             f"AND trim(CAST({q(left.alias)}.{q(lcol)} AS VARCHAR)) <> ''")
            from_sql += f" {'INNER' if step.type == 'inner' else 'LEFT'} JOIN {q(step.alias)} ON " + " AND ".join(conds)
            seen.add(step.alias.lower())

        where = " AND ".join(self._predicate(f, *column(f.column)) for f in spec.filters) or "TRUE"

        picks = spec.columns or []
        outputs: list[tuple[str, str, str]] = []
        if picks:
            for pick in picks:
                inp, col = column(pick.ref)
                outputs.append((inp.alias, col, (pick.name or col).strip()))
        else:
            counts: dict[str, int] = {}
            for inp in inputs.values():
                for col in inp.columns:
                    if col != "__rn":
                        counts[col.lower()] = counts.get(col.lower(), 0) + 1
            for inp in inputs.values():
                for col in inp.columns:
                    if col != "__rn":
                        outputs.append((inp.alias, col, f"{inp.alias}_{col}" if counts[col.lower()] > 1 else col))
        names = [o[2] for o in outputs]
        if len(outputs) > MAX_OUTPUT_COLUMNS:
            raise CombineError(f"the combination would have {len(outputs)} columns (at most {MAX_OUTPUT_COLUMNS}): choose the columns you need")
        if not outputs:
            raise CombineError("no columns were chosen")
        if len({n.lower() for n in names}) != len(names):
            dup = sorted({n for n in names if [x.lower() for x in names].count(n.lower()) > 1})
            raise CombineError(f"two output columns would be called {', '.join(dup)}: rename one of them")
        if any(not n or len(n) > 128 for n in names):
            raise CombineError("an output column name is empty or longer than 128 characters")
        select_sql = "SELECT " + ", ".join(f"{q(a)}.{q(c)} AS {q(n)}" for a, c, n in outputs)
        order = "ORDER BY " + ", ".join(f'{q(i.alias)}."__rn"' for i in inputs.values())
        return Plan(inputs=inputs, from_sql=from_sql, ctes=ctes, where=where, order=order, outputs=outputs, select_sql=select_sql)

    @staticmethod
    def _predicate(f: FilterStep, inp: _Input, col: str) -> str:
        ref = f"{q(inp.alias)}.{q(col)}"
        text = f"trim(CAST({ref} AS VARCHAR))"
        if f.op == "is_blank":
            return f"({ref} IS NULL OR {text} = '')"
        if f.op == "not_blank":
            return f"({ref} IS NOT NULL AND {text} <> '')"
        if f.op == "regex":
            return f"regexp_matches(coalesce({text}, ''), {lit(f.values[0])}{', ' + lit('i') if f.ignore_case else ''})"
        value = f"coalesce({text}, '')"
        if f.ignore_case:
            value = f"lower({value})"
        vals = [v.strip().lower() if f.ignore_case else v.strip() for v in f.values]
        return {"equals": f"{value} = {lit(vals[0])}" if vals else "", "not_equals": f"{value} <> {lit(vals[0])}" if vals else "",
                "in": f"{value} IN ({', '.join(lit(v) for v in vals)})", "contains": f"contains({value}, {lit(vals[0])})" if vals else "",
                "starts_with": f"starts_with({value}, {lit(vals[0])})" if vals else ""}[f.op]

    # ------------------------------------------------------------------ measuring and previewing
    def preview(self, spec: CombineSpec, tables: dict[int, tuple[str, Path]], sample_rows: int = 10) -> CombinePreview:
        plan = self.plan(spec, tables)
        warnings: list[str] = []
        with bounded_connection(self._memory) as con:
            for f in spec.filters:
                if f.op == "regex":
                    try:
                        con.execute(f"SELECT regexp_matches('', {lit(f.values[0])})")
                    except Exception as exc:  # noqa: BLE001 - DuckDB reports the RE2 error
                        raise CombineError(f"the pattern '{f.values[0]}' is not a valid regular expression: {str(exc).splitlines()[0][:140]}") from exc
            base_rows = int(con.execute(f"WITH {plan.ctes} SELECT count(*) FROM {q(spec.base.alias)}").fetchone()[0])
            stats: list[JoinStats] = []
            for i, step in enumerate(spec.joins):
                cond = " AND ".join(self._on(plan, spec, step))
                left_alias = self._left_alias(spec, step)
                left_rows, right_rows = (int(x) for x in con.execute(
                    f"WITH {plan.ctes} SELECT (SELECT count(*) FROM {q(left_alias)}), (SELECT count(*) FROM {q(step.alias)})").fetchone())
                none, several, most = con.execute(
                    f"WITH {plan.ctes} SELECT count(*) FILTER (WHERE m = 0), count(*) FILTER (WHERE m > 1), coalesce(max(m), 0) FROM ("
                    f'SELECT {q(left_alias)}."__rn", count({q(step.alias)}."__rn") AS m FROM {q(left_alias)} LEFT JOIN {q(step.alias)} ON {cond} GROUP BY 1)').fetchone()
                stats.append(JoinStats(alias=step.alias, table_name=plan.inputs[step.alias.lower()].table_name, type=step.type, left_alias=left_alias, left_rows=left_rows,
                                       right_rows=right_rows, left_rows_without_a_match=int(none), left_rows_with_several=int(several), most_partners=int(most)))
                lname, rname = plan.inputs[left_alias.lower()].table_name, plan.inputs[step.alias.lower()].table_name
                if none:
                    warnings.append(f"{none:,} of {left_rows:,} {lname} records find no partner in {rname}"
                                    + (" and are DROPPED by the inner join" if step.type == "inner" else " (they stay, with blank columns)"))
                if several:
                    warnings.append(f"{several:,} {lname} records find several partners in {rname} (up to {most})")
            after, distinct_base = (int(x) for x in con.execute(
                f'WITH {plan.ctes} SELECT count(*), count(DISTINCT {q(spec.base.alias)}."__rn") {plan.from_sql}').fetchone())
            out = int(con.execute(f"WITH {plan.ctes} SELECT count(*) {plan.from_sql} WHERE {plan.where}").fetchone()[0])
            blocked = None
            repeated = after - distinct_base
            if repeated > 0:
                blocked = (f"the joins turn {base_rows:,} {plan.inputs[spec.base.alias.lower()].table_name} records into {after:,} rows ({repeated:,} repeated): a record would appear "
                           "several times and could be migrated several times. Make the table with the many rows the base, narrow the join keys, or allow it explicitly")
                if spec.allow_fanout:
                    warnings.append(blocked.split(":")[0] + ": allowed, so records repeat")
                    blocked = None
            rows = con.execute(f"WITH {plan.ctes} {plan.select_sql} {plan.from_sql} WHERE {plan.where} {plan.order} LIMIT {int(sample_rows)}").fetchall()
        if base_rows == 0:
            blocked = blocked or "the base table has no rows"
        if out == 0 and not blocked:
            blocked = "the combination (with its filters) has no rows"
        if spec.filters and after - out:
            warnings.append(f"the filters remove {after - out:,} of {after:,} rows")
        sql = f"WITH {plan.ctes} {plan.select_sql} {plan.from_sql} WHERE {plan.where} {plan.order}"
        return CombinePreview(
            base_rows=base_rows, rows_after_joins=after, rows_out=out, removed_by_filters=after - out,
            columns=[OutputColumn(name=n, from_table=a, source_column=c) for a, c, n in plan.outputs], joins=stats, warnings=warnings, blocked=blocked,
            sample=[["" if v is None else str(v) for v in r] for r in rows], sql=sql)

    @staticmethod
    def _left_alias(spec: CombineSpec, step: JoinStep) -> str:
        m = REF.match(step.on[0].left)
        return next((j.alias for j in [spec.base, *spec.joins] if m and j.alias.lower() == m.group(1).lower()), spec.base.alias)

    def _on(self, plan: Plan, spec: CombineSpec, step: JoinStep) -> list[str]:
        out = []
        for c in step.on:
            lm, rm = REF.match(c.left), REF.match(c.right)
            lin, rin = plan.inputs[lm.group(1).lower()], plan.inputs[rm.group(1).lower()]
            lcol = next(x for x in lin.columns if x.lower() == lm.group(2).strip().lower())
            rcol = next(x for x in rin.columns if x.lower() == rm.group(2).strip().lower())
            out.append(f"{_norm(f'{q(lin.alias)}.{q(lcol)}', spec.match)} = {_norm(f'{q(rin.alias)}.{q(rcol)}', spec.match)} AND trim(CAST({q(lin.alias)}.{q(lcol)} AS VARCHAR)) <> ''")
        return out

    # ------------------------------------------------------------------ creating the table
    def materialise(self, spec: CombineSpec, tables: dict[int, tuple[str, Path]], out_csv: Path) -> tuple[int, list[str], CombinePreview]:
        """Write the combined rows to a UTF-8 CSV (the landing service turns it into the snapshot). Refuses a blocked combination."""
        preview = self.preview(spec, tables, sample_rows=5)
        if preview.blocked:
            raise CombineError(preview.blocked)
        plan = self.plan(spec, tables)
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with bounded_connection(self._memory) as con:
            con.execute(f"COPY (WITH {plan.ctes} {plan.select_sql} {plan.from_sql} WHERE {plan.where} {plan.order}) TO {lit(out_csv.as_posix())} "
                        f"(FORMAT csv, HEADER true, DELIMITER ',', QUOTE '\"')")
        return preview.rows_out, [o[2] for o in plan.outputs], preview
