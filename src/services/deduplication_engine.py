"""Hybrid deduplication of a staging dataset: exact business keys, then fuzzy entity resolution.

Phase 1 (deterministic)  Rows with identical, non-empty *normalised* composite keys (tax id, email,
                         phone, ...) are linked. Done entirely in DuckDB window functions.
Phase 2 (fuzzy)          Normalised name (+ address) compared with rapidfuzz. Candidate pairs come
                         from blocking (shared name prefix or address prefix), compared block by
                         block with vectorised `rapidfuzz.process.cdist`. A pair matches when the
                         name similarity AND the weighted name+address score are >= threshold
                         (0.88). If either record has no address, the name alone decides. Digit tokens in the names
                         (and, when both exist, in the addresses) must be identical: "Plant 1" and
                         "Plant 2", or two house numbers, are different entities, not typos.
Clustering               Links from both phases are merged with scipy connected components, so a
                         cluster can mix exact and fuzzy evidence (chains A~B~C are merged).
Golden record            The member with the most populated columns survives; ties go to the
                         smallest primary key (deterministic).

Rows whose `_TRANSFORM_STATUS` is FAILED are excluded: they cannot become or merge into a master.
Output = the input staging columns + `dedup_cluster_id`, `is_duplicate_child`,
`surviving_master_pk` (a record's own pk when it is not a child) and `dedup_match_type`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from src.schemas.execution import ClusterSample, DedupConfig, DedupReport, ExactKey
from src.services._duckdb_util import DEFAULT_MEMORY_LIMIT_MB, bounded_connection, lit, q

OUTPUT_COLUMNS = ("dedup_cluster_id", "is_duplicate_child", "surviving_master_pk", "dedup_match_type")
_CHUNK_ROWS = 400
_MAX_SAMPLES = 20

LEGAL_SUFFIXES = (
    "INC", "INCORPORATED", "LLC", "LLP", "LP", "LTD", "LIMITED", "CORP", "CORPORATION", "CO", "COMPANY",
    "GMBH", "AG", "KG", "SA", "SAS", "SARL", "BV", "NV", "PLC", "PTE", "PVT", "PRIVATE", "OY", "AB", "SPA", "SRL",
)
_ADDRESS_ABBREVIATIONS = (
    ("STREET", "ST"), ("STRASSE", "ST"), ("STR", "ST"), ("AVENUE", "AVE"), ("ROAD", "RD"),
    ("DRIVE", "DR"), ("BOULEVARD", "BLVD"), ("SUITE", "STE"), ("FLOOR", "FL"),
)


def _digits(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\d+", text))


def _key_component(column: str, kind: str) -> str:
    c = f"CAST({q(column)} AS VARCHAR)"
    if kind == "alnum":
        expr = f"upper(regexp_replace({c}, '[^A-Za-z0-9]', '', 'g'))"
    elif kind == "email":
        expr = f"lower(trim({c}))"
    elif kind == "digits":
        expr = f"regexp_replace({c}, '[^0-9]', '', 'g')"
    else:
        expr = f"upper(regexp_replace(trim({c}), '\\s+', ' ', 'g'))"
    return f"nullif({expr}, '')"


def _name_norm_sql(column: str) -> str:
    base = f"trim(regexp_replace(regexp_replace(upper(CAST({q(column)} AS VARCHAR)), '[^\\p{{L}}\\p{{N}} ]', ' ', 'g'), '\\s+', ' ', 'g'))"
    stripped = base
    suffixes = "|".join(LEGAL_SUFFIXES)
    for _ in range(3):  # repeated: "CO LTD" needs two passes
        stripped = f"regexp_replace({stripped}, '\\b({suffixes})\\b', ' ', 'g')"
    stripped = f"trim(regexp_replace({stripped}, '\\s+', ' ', 'g'))"
    return f"CASE WHEN {stripped} = '' THEN {base} ELSE {stripped} END"


def _address_norm_sql(columns: list[str]) -> str:
    parts = ", ".join(f"coalesce(CAST({q(c)} AS VARCHAR), '')" for c in columns)
    expr = f"upper(concat_ws(' ', {parts}))"
    expr = f"regexp_replace({expr}, '[^\\p{{L}}\\p{{N}} ]', ' ', 'g')"
    for long, short in _ADDRESS_ABBREVIATIONS:
        expr = f"regexp_replace({expr}, '\\b{long}\\b', '{short}', 'g')"
    return f"trim(regexp_replace({expr}, '\\s+', ' ', 'g'))"


class DeduplicationEngine:
    def __init__(self, memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB, threads: int = 2) -> None:
        self._memory_limit_mb = memory_limit_mb
        self._threads = threads

    def deduplicate(
        self, staging_path: str | Path, output_path: str | Path, config: DedupConfig
    ) -> DedupReport:
        source, output = Path(staging_path), Path(output_path)
        if not source.is_file():
            raise FileNotFoundError(source)
        if output.suffix.lower() not in (".parquet", ".pq"):
            raise ValueError("output_path must be a .parquet file")
        output.parent.mkdir(parents=True, exist_ok=True)

        with bounded_connection(self._memory_limit_mb, self._threads) as con:
            columns = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({lit(source.as_posix())})").fetchall()]
            self._check_columns(config, columns)
            con.execute(
                f'CREATE TEMP TABLE stg AS SELECT CAST(row_number() OVER () - 1 AS BIGINT) AS "__id", * '
                f"FROM read_parquet({lit(source.as_posix())})"
            )
            total = int(con.execute("SELECT count(*) FROM stg").fetchone()[0])
            status = config.status_column
            eligible_sql = f"coalesce({q(status)}, 'OK') <> 'FAILED'" if status and status in columns else "true"
            eligible_rows = int(con.execute(f"SELECT count(*) FROM stg WHERE {eligible_sql}").fetchone()[0])

            edges_a: list[np.ndarray] = []
            edges_b: list[np.ndarray] = []
            edge_reason: list[np.ndarray] = []
            reasons: list[str] = []
            exact_counts: dict[str, int] = {}

            for key in config.exact_keys:  # ---- phase 1
                a, b = self._exact_edges(con, key, eligible_sql)
                exact_counts[key.name] = int(len(a))
                if len(a):
                    reasons.append(f"EXACT:{key.name}")
                    edges_a.append(a); edges_b.append(b)
                    edge_reason.append(np.full(len(a), len(reasons) - 1, dtype=np.int32))

            evaluated = matched_pairs = 0
            if config.name_column:  # ---- phase 2
                a, b, evaluated = self._fuzzy_edges(con, config, eligible_sql)
                matched_pairs = int(len(a))
                if len(a):
                    reasons.append("FUZZY")
                    edges_a.append(a); edges_b.append(b)
                    edge_reason.append(np.full(len(a), len(reasons) - 1, dtype=np.int32))

            clusters = self._cluster(total, edges_a, edges_b)
            comp_cols = config.completeness_columns or [
                c for c in columns if not c.startswith("_") and c != config.pk_column
            ]
            ids, completeness, pks = self._completeness(con, config, comp_cols)
            mapping, summary = self._golden_records(
                total, clusters, edges_a, edge_reason, reasons, ids, completeness, pks
            )

            con.register("dd", mapping)
            pk = q(config.pk_column)
            con.execute(
                f"COPY (SELECT stg.* EXCLUDE (\"__id\"), dd.cluster_id AS dedup_cluster_id, "
                f"coalesce(dd.is_child, false) AS is_duplicate_child, "
                f"coalesce(dd.master_pk, CAST(stg.{pk} AS VARCHAR)) AS surviving_master_pk, "
                f"dd.match_type AS dedup_match_type "
                f'FROM stg LEFT JOIN dd ON stg."__id" = dd."__id" ORDER BY stg."__id") '
                f"TO {lit(output.as_posix())} (FORMAT parquet)"
            )

        children = summary["children"]
        return DedupReport(
            input_rows=total,
            eligible_rows=eligible_rows,
            excluded_failed_rows=total - eligible_rows,
            duplicate_clusters=summary["clusters"],
            duplicate_children=children,
            surviving_records=eligible_rows - children,
            exact_match_clusters=summary["exact_clusters"],
            fuzzy_match_clusters=summary["fuzzy_clusters"],
            exact_edges_by_key=exact_counts,
            fuzzy_pairs_evaluated=int(evaluated),
            fuzzy_pairs_matched=matched_pairs,
            samples=summary["samples"],
            output_path=str(output),
        )

    # ------------------------------------------------------------------ validation
    @staticmethod
    def _check_columns(config: DedupConfig, columns: list[str]) -> None:
        wanted = [config.pk_column, *config.address_columns]
        if config.name_column:
            wanted.append(config.name_column)
        for key in config.exact_keys:
            wanted += key.columns
        missing = sorted({c for c in wanted if c not in columns})
        if missing:
            raise ValueError(f"staging dataset is missing columns: {missing}")
        clash = [c for c in OUTPUT_COLUMNS if c in columns]
        if clash:
            raise ValueError(f"staging dataset already has dedup output columns: {clash}")
        if config.completeness_columns:
            bad = [c for c in config.completeness_columns if c not in columns]
            if bad:
                raise ValueError(f"completeness columns not in staging dataset: {bad}")

    # ------------------------------------------------------------------ phase 1
    @staticmethod
    def _exact_edges(con, key: ExactKey, eligible_sql: str) -> tuple[np.ndarray, np.ndarray]:
        parts = [_key_component(c, key.normalizer) for c in key.columns]
        any_null = " OR ".join(f"{p} IS NULL" for p in parts)
        joined = ", ".join(parts)
        data = con.execute(
            f'SELECT a, b FROM (SELECT "__id" AS b, min("__id") OVER w AS a, count(*) OVER w AS c FROM ('
            f'SELECT "__id", CASE WHEN {any_null} THEN NULL ELSE concat_ws(chr(31), {joined}) END AS k '
            f"FROM stg WHERE {eligible_sql}) WHERE k IS NOT NULL WINDOW w AS (PARTITION BY k)) "
            f"WHERE c > 1 AND a <> b"
        ).fetchnumpy()
        return data["a"].astype(np.int64), data["b"].astype(np.int64)

    # ------------------------------------------------------------------ phase 2
    def _fuzzy_edges(self, con, config: DedupConfig, eligible_sql: str) -> tuple[np.ndarray, np.ndarray, int]:
        addr = _address_norm_sql(config.address_columns) if config.address_columns else "''"
        con.execute(
            f'CREATE TEMP TABLE fz AS SELECT "__id", nn, an, left(replace(nn, \' \', \'\'), 3) AS b1, '
            f"CASE WHEN an <> '' THEN left(replace(an, ' ', ''), 6) END AS b2 FROM ("
            f'SELECT "__id", {_name_norm_sql(config.name_column)} AS nn, {addr} AS an '
            f"FROM stg WHERE {eligible_sql} AND {q(config.name_column)} IS NOT NULL) WHERE nn <> ''"
        )
        threshold = config.fuzzy_threshold * 100.0
        weight = config.name_weight
        pairs: set[tuple[int, int]] = set()
        evaluated = 0
        for block_col in ("b1", "b2"):
            blocks = con.execute(
                f'SELECT list("__id" ORDER BY "__id"), list(nn ORDER BY "__id"), list(an ORDER BY "__id") '
                f"FROM fz WHERE {block_col} IS NOT NULL GROUP BY {block_col} HAVING count(*) > 1"
            ).fetchall()
            for ids, names, addrs in blocks:
                n = len(ids)
                evaluated += n * (n - 1) // 2
                for start in range(0, n, _CHUNK_ROWS):
                    chunk = names[start : start + _CHUNK_ROWS]
                    scores = process.cdist(chunk, names, scorer=fuzz.token_sort_ratio, dtype=np.float32, workers=1)
                    rows, cols = np.nonzero(scores >= threshold)
                    keep = cols > rows + start  # upper triangle only
                    for r, c in zip(rows[keep], cols[keep]):
                        i, j = start + int(r), int(c)
                        if _digits(names[i]) != _digits(names[j]):
                            continue  # "Plant 1" vs "Plant 2": numbers are identity, not typos
                        name_score = float(scores[r, c])
                        if addrs[i] and addrs[j]:
                            if _digits(addrs[i]) != _digits(addrs[j]):
                                continue  # different house number / postal code = different site
                            combined = weight * name_score + (1 - weight) * fuzz.token_sort_ratio(addrs[i], addrs[j])
                        else:
                            combined = name_score
                        if combined >= threshold:
                            pairs.add((int(ids[i]), int(ids[j])))
        if not pairs:
            return np.empty(0, np.int64), np.empty(0, np.int64), evaluated
        arr = np.array(sorted(pairs), dtype=np.int64)
        return arr[:, 0], arr[:, 1], evaluated

    # ------------------------------------------------------------------ clustering / golden record
    @staticmethod
    def _cluster(total: int, edges_a: list[np.ndarray], edges_b: list[np.ndarray]) -> np.ndarray:
        if not edges_a:
            return np.arange(total, dtype=np.int64)
        a, b = np.concatenate(edges_a), np.concatenate(edges_b)
        graph = coo_matrix((np.ones(len(a), dtype=np.int8), (a, b)), shape=(total, total))
        _, labels = connected_components(graph, directed=False)
        return labels.astype(np.int64)

    @staticmethod
    def _completeness(con, config: DedupConfig, columns: list[str]):
        score = " + ".join(
            f"CASE WHEN {q(c)} IS NULL OR trim(CAST({q(c)} AS VARCHAR)) = '' THEN 0 ELSE 1 END" for c in columns
        ) or "0"
        data = con.execute(
            f'SELECT "__id", CAST(({score}) AS INTEGER) AS comp, CAST({q(config.pk_column)} AS VARCHAR) AS pk '
            f'FROM stg ORDER BY "__id"'
        ).fetchnumpy()
        return data["__id"].astype(np.int64), data["comp"].astype(np.int64), np.asarray(data["pk"]).astype(str)

    @staticmethod
    def _golden_records(total, labels, edges_a, edge_reason, reasons, ids, completeness, pks):
        empty = pd.DataFrame({
            "__id": pd.Series(dtype="int64"), "cluster_id": pd.Series(dtype="int64"),
            "is_child": pd.Series(dtype="bool"), "master_pk": pd.Series(dtype="object"),
            "match_type": pd.Series(dtype="object"),
        })
        sizes = np.bincount(labels, minlength=int(labels.max()) + 1 if total else 1)
        in_cluster = sizes[labels] > 1
        if not in_cluster.any():
            return empty, {"clusters": 0, "children": 0, "exact_clusters": 0, "fuzzy_clusters": 0, "samples": []}

        member_ids = ids[in_cluster]
        member_labels = labels[member_ids]
        # master per cluster: highest completeness, then smallest pk (lexsort: last key is primary)
        order = np.lexsort((pks[member_ids], -completeness[member_ids], member_labels))
        sorted_ids, sorted_labels = member_ids[order], member_labels[order]
        first = np.r_[True, sorted_labels[1:] != sorted_labels[:-1]]
        master_of = dict(zip(sorted_labels[first].tolist(), sorted_ids[first].tolist()))

        # dense cluster numbers ordered by first appearance in the file
        uniq, first_pos = np.unique(member_labels, return_index=True)
        rank = {int(lbl): n + 1 for n, lbl in enumerate(uniq[np.argsort(first_pos)])}

        # evidence per cluster
        evidence: dict[int, set[str]] = {}
        if edges_a:
            ea, er = np.concatenate(edges_a), np.concatenate(edge_reason)
            for lbl, reason in np.unique(np.stack([labels[ea], er]), axis=1).T:
                evidence.setdefault(int(lbl), set()).add(reasons[int(reason)])

        match_type = {lbl: "+".join(sorted(ev)) for lbl, ev in evidence.items()}
        is_child = np.array([master_of[int(lbl)] != int(i) for lbl, i in zip(member_labels, member_ids)])
        frame = pd.DataFrame({
            "__id": member_ids,
            "cluster_id": [rank[int(lbl)] for lbl in member_labels],
            "is_child": is_child,
            "master_pk": [str(pks[master_of[int(lbl)]]) for lbl in member_labels],
            "match_type": [match_type[int(lbl)] for lbl in member_labels],
        }).sort_values("__id", kind="stable").reset_index(drop=True)

        fuzzy_clusters = sum(1 for ev in evidence.values() if "FUZZY" in ev)
        samples = []
        for lbl in sorted(rank, key=rank.get)[:_MAX_SAMPLES]:
            members = member_ids[member_labels == lbl]
            samples.append(ClusterSample(
                cluster_id=rank[lbl], master_pk=str(pks[master_of[lbl]]),
                member_pks=[str(pks[m]) for m in members], match_type=match_type[lbl],
            ))
        return frame, {
            "clusters": len(rank), "children": int(is_child.sum()),
            "exact_clusters": len(rank) - fuzzy_clusters, "fuzzy_clusters": fuzzy_clusters,
            "samples": samples,
        }
