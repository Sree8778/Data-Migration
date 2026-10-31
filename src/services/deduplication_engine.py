"""Hybrid deduplication of a staging dataset: exact business keys, then fuzzy entity resolution.

Phase 1 (deterministic)  Rows with identical, non-empty *normalised* composite keys (tax id, email,
                         phone, ...) are linked. Done entirely in DuckDB window functions.
Phase 2 (fuzzy)          Normalised name (+ address) compared with rapidfuzz. Candidate pairs come
                         from blocking (shared name prefix or address prefix), compared block by
                         block with vectorised `rapidfuzz.process.cdist`. A pair matches when the
                         name similarity AND the weighted name+address score are >= threshold
                         (0.88). If either record has no address, the name alone decides. Digit tokens in the names
                         must be identical ("Plant 1" and "Plant 2" are different entities, not typos). In the
                         addresses the numbers (house no., postal code) must not CONFLICT: two different house
                         numbers are different sites, but a number that one record simply lacks is missing data.
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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from src.schemas.execution import ClusterSample, DedupConfig, DedupReport, ExactKey
from src.services._duckdb_util import DEFAULT_MEMORY_LIMIT_MB, bounded_connection, lit, q

HIGH_MERGE_RATE = 0.25   # more than this share of the eligible records merged away is more likely loose matching than that many real duplicates
OUTPUT_COLUMNS = ("dedup_cluster_id", "is_duplicate_child", "surviving_master_pk", "dedup_match_type", "dedup_match_strength", "dedup_enriched_columns")
_MAX_REVIEW_PAIRS = 100_000   # the review list is for people: beyond this many pairs it only counts
_STRENGTH = {4: "REVIEWED", 3: "EXACT", 2: "NAME_ADDRESS", 1: "NAME_ONLY"}
_CHUNK_ROWS = 400
_STREET_AGREEMENT = 60.0   # below this similarity two street names are different streets (a typo or an abbreviation stays far above it)
_MAX_SAMPLES = 20

LEGAL_SUFFIXES = (
    "INC", "INCORPORATED", "LLC", "LLP", "LP", "LTD", "LIMITED", "CORP", "CORPORATION", "CO", "COMPANY",
    "GMBH", "AG", "KG", "SA", "SAS", "SARL", "BV", "NV", "PLC", "PTE", "PVT", "PRIVATE", "OY", "AB", "SPA", "SRL",
    "KK", "SL",
)
_ADDRESS_ABBREVIATIONS = (
    ("STREET", "ST"), ("STRASSE", "ST"), ("STR", "ST"), ("AVENUE", "AVE"), ("ROAD", "RD"),
    ("DRIVE", "DR"), ("BOULEVARD", "BLVD"), ("SUITE", "STE"), ("FLOOR", "FL"),
)


def _digits(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\d+", text))


_PLACEHOLDER_IDS = ("NONE", "NULL", "NA", "NOTAPPLICABLE", "UNKNOWN", "TBD", "PENDING", "APPLIEDFOR", "TEST", "DUMMY")


def _key_component(column: str, kind: str) -> str:
    """Normalised key value, or NULL when the value cannot identify anyone: an e-mail that is not an address,
    a phone number with fewer than 6 digits or one repeated digit, a tax id that is too short, one repeated
    character or a known placeholder. Such values are shared by unrelated records and must never link them."""
    c = f"CAST({q(column)} AS VARCHAR)"
    if kind == "alnum":
        v = f"upper(regexp_replace({c}, '[^A-Za-z0-9]', '', 'g'))"
        valid = f"length({v}) >= 5 AND {v} <> repeat(left({v}, 1), length({v})) AND {v} NOT IN ({', '.join(repr(x) for x in _PLACEHOLDER_IDS)})"
        expr = f"CASE WHEN {valid} THEN {v} END"
    elif kind == "email":
        v = f"lower(trim({c}))"
        expr = f"CASE WHEN regexp_full_match({v}, '[^@\\s]+@[^@\\s]+\\.[a-z]{{2,}}') THEN {v} END"
    elif kind == "digits":
        v = f"regexp_replace({c}, '[^0-9]', '', 'g')"
        expr = f"CASE WHEN length({v}) >= 6 AND {v} <> repeat(left({v}, 1), length({v})) THEN {v} END"
    else:
        expr = f"upper(regexp_replace(trim({c}), '\\s+', ' ', 'g'))"
    return f"nullif({expr}, '')"


def _fold_sql(expression: str) -> str:
    """Upper-case with the accents folded: the German letters are transliterated (Müller = Mueller, Straße = Strasse), every other accent is dropped (Çelik = Celik). Without
    this, an accent changes the ALPHABETICAL ORDER of the words, and a comparison that sorts the words ("EVA ÇELIK" against "CELIK EVA") scores two spellings of one name 44."""
    text = f"upper({expression})"
    for a, b in (("Ä", "AE"), ("Ö", "OE"), ("Ü", "UE"), ("ẞ", "SS"), ("ß", "SS")):
        text = f"replace({text}, '{a}', '{b}')"
    return f"strip_accents({text})"


def _name_norm_sql(column: str) -> str:
    # periods are dropped (L.L.C. -> LLC, K.K. -> KK) before other punctuation becomes a space
    undotted = f"regexp_replace({_fold_sql(f'CAST({q(column)} AS VARCHAR)')}, '\\.', '', 'g')"
    base = f"trim(regexp_replace(regexp_replace({undotted}, '[^\\p{{L}}\\p{{N}} ]', ' ', 'g'), '\\s+', ' ', 'g'))"
    stripped = base
    suffixes = "|".join(LEGAL_SUFFIXES)
    for _ in range(3):  # repeated: "CO LTD" needs two passes
        stripped = f"regexp_replace({stripped}, '\\b({suffixes})\\b', ' ', 'g')"
    stripped = f"trim(regexp_replace({stripped}, '\\s+', ' ', 'g'))"
    return f"CASE WHEN {stripped} = '' THEN {base} ELSE {stripped} END"


def _address_norm_sql(columns: list[str]) -> str:
    parts = ", ".join(f"coalesce(CAST({q(c)} AS VARCHAR), '')" for c in columns)
    expr = _fold_sql(f"concat_ws(' ', {parts})")
    expr = f"regexp_replace({expr}, '[^\\p{{L}}\\p{{N}} ]', ' ', 'g')"
    for long, short in _ADDRESS_ABBREVIATIONS:
        expr = f"regexp_replace({expr}, '\\b{long}\\b', '{short}', 'g')"
    return f"trim(regexp_replace({expr}, '\\s+', ' ', 'g'))"


@dataclass
class _Fuzzy:
    a: np.ndarray
    b: np.ndarray
    weak: np.ndarray                      # per edge: decided on the name alone (a record had no address)
    evaluated: int = 0
    vetoed: int = 0
    limited_blocks: int = 0
    possible: dict = field(default_factory=dict)   # (id, id) -> combined score, the review band
    possible_overflow: int = 0
    common_held: int = 0                   # pairs that matched but were sent to review because the name is a common one
    street_held: int = 0                   # pairs that matched but were sent to review because the two street names are plainly different


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
            has_status = bool(status and status in columns)
            eligible_sql = f"coalesce({q(status)}, 'OK') <> 'FAILED'" if has_status and not config.include_failed_rows else "true"
            eligible_rows = int(con.execute(f"SELECT count(*) FROM stg WHERE {eligible_sql}").fetchone()[0])

            edges_a: list[np.ndarray] = []
            edges_b: list[np.ndarray] = []
            edge_reason: list[np.ndarray] = []
            edge_weak: list[np.ndarray] = []
            fuzzy = None
            reasons: list[str] = []
            exact_counts: dict[str, int] = {}
            ignored_keys: dict[str, int] = {}

            veto_codes = self._veto_codes(con, config, eligible_sql, total)   # per tax-number key: a code per row (-1 = none), to refuse links between different ones
            exact_vetoed = 0
            for key in config.exact_keys:  # ---- phase 1
                a, b, ignored = self._exact_edges(con, key, eligible_sql, config.max_key_group)
                if veto_codes and not key.vetoes_fuzzy and len(a):
                    keep = np.ones(len(a), dtype=bool)
                    for codes in veto_codes:
                        keep &= ~((codes[a] >= 0) & (codes[b] >= 0) & (codes[a] != codes[b]))
                    exact_vetoed += int(len(a) - keep.sum())
                    a, b = a[keep], b[keep]
                exact_counts[key.name] = int(len(a))
                if ignored:
                    ignored_keys[key.name] = ignored
                if len(a):
                    reasons.append(f"EXACT:{key.name}")
                    edges_a.append(a); edges_b.append(b)
                    edge_reason.append(np.full(len(a), len(reasons) - 1, dtype=np.int32))
                    edge_weak.append(np.zeros(len(a), dtype=bool))

            evaluated = matched_pairs = vetoed = limited = 0
            if config.name_column:  # ---- phase 2
                fuzzy = self._fuzzy_edges(con, config, eligible_sql, veto_codes)
                a, b, evaluated, vetoed, limited = fuzzy.a, fuzzy.b, fuzzy.evaluated, fuzzy.vetoed, fuzzy.limited_blocks
                matched_pairs = int(len(a))
                if len(a):
                    reasons.append("FUZZY")
                    edges_a.append(a); edges_b.append(b)
                    edge_reason.append(np.full(len(a), len(reasons) - 1, dtype=np.int32))
                    edge_weak.append(fuzzy.weak)

            reviewed_merges, reviewed_separations, decided = self._apply_decisions(con, config, total, edges_a, edges_b, edge_reason, edge_weak, reasons)
            tax_reasons = {f"EXACT:{k.name}" for k in config.exact_keys if k.vetoes_fuzzy}
            clusters, chain_refused = self._cluster_checked(total, edges_a, edges_b, edge_reason, edge_weak, reasons, veto_codes, tax_reasons)
            conflicting_groups = self._conflicting_groups(config, clusters, veto_codes)
            comp_cols = config.completeness_columns or [
                c for c in columns if not c.startswith("_") and c != config.pk_column
            ]
            ids, completeness, pks = self._completeness(con, config, comp_cols, penalise_failed=has_status and config.include_failed_rows)
            mapping, summary = self._golden_records(
                total, clusters, edges_a, edge_reason, edge_weak, reasons, ids, completeness, pks
            )
            possible_pairs, possible_path = self._write_possible(con, config, fuzzy, clusters, output, decided)

            con.register("dd", mapping)
            pk = q(config.pk_column)
            fill_cols = self._fill_columns(config, columns, comp_cols) if config.survivorship == "fill_blanks" and summary["children"] else []
            enriched_cells = 0
            if fill_cols:
                enriched_cells = self._prepare_fill(con, config, fill_cols, has_status)
            replaced = {c: f"CASE WHEN dd.cluster_id IS NOT NULL AND NOT dd.is_child AND fl.\"__f_{i}\" IS NOT NULL AND (stg.{q(c)} IS NULL OR trim(CAST(stg.{q(c)} AS VARCHAR)) = '') "
                           f"THEN fl.\"__f_{i}\" ELSE stg.{q(c)} END AS {q(c)}" for i, c in enumerate(fill_cols)}
            select_cols = ", ".join(replaced.get(c, f"stg.{q(c)}") for c in columns)
            enriched_sql = ("concat_ws(';', " + ", ".join(
                f"CASE WHEN dd.cluster_id IS NOT NULL AND NOT dd.is_child AND fl.\"__f_{i}\" IS NOT NULL AND (stg.{q(c)} IS NULL OR trim(CAST(stg.{q(c)} AS VARCHAR)) = '') THEN {lit(c)} END"
                for i, c in enumerate(fill_cols)) + ")") if fill_cols else "CAST(NULL AS VARCHAR)"
            con.execute(
                f"COPY (SELECT {select_cols}, dd.cluster_id AS dedup_cluster_id, "
                f"coalesce(dd.is_child, false) AS is_duplicate_child, "
                f"coalesce(dd.master_pk, CAST(stg.{pk} AS VARCHAR)) AS surviving_master_pk, "
                f"dd.match_type AS dedup_match_type, dd.strength AS dedup_match_strength, nullif({enriched_sql}, '') AS dedup_enriched_columns "
                f'FROM stg LEFT JOIN dd ON stg."__id" = dd."__id" {"LEFT JOIN fl ON fl.cluster_id = dd.cluster_id" if fill_cols else ""} ORDER BY stg."__id") '
                f"TO {lit(output.as_posix())} (FORMAT parquet)"
            )

        children = summary["children"]
        warnings = self._warnings(children, eligible_rows, conflicting_groups, limited, summary["weak_clusters"], possible_pairs, enriched_cells)
        if not any(k.vetoes_fuzzy for k in config.exact_keys) and config.name_column:
            warnings.append("No tax / VAT number column is part of the matching, so two companies with the same name and address cannot be told apart by their tax numbers, and a shared "
                            "phone number or e-mail address links them. If the table has such a column, name it in the matching setup (an exact key with 'vetoes_fuzzy').")
        if chain_refused:
            warnings.append(f"{chain_refused:,} link(s) were NOT made because they would have put two different tax numbers into one group (they were reached through a record "
                            "that has no tax number). The stronger evidence was kept; the refused links are not in the review list.")
        common_held = fuzzy.common_held if fuzzy is not None else 0
        street_held = fuzzy.street_held if fuzzy is not None else 0
        if street_held:
            warnings.append(f"{street_held:,} pair(s) matched on name, city, postal code and house number but have plainly different STREET names. They were NOT merged: both records "
                            "load, and the pairs are in possible_duplicates.parquet for a person to decide.")
        if common_held:
            warnings.append(f"{common_held:,} pair(s) matched on a COMMON name (shared by {config.common_name_count} or more records) with nothing but a city to back it. "
                            "They were NOT merged: both records load, and the pairs are in possible_duplicates.parquet for a person to decide.")
        if exact_vetoed:
            warnings.append(f"{exact_vetoed:,} shared e-mail / phone link(s) were NOT made because the two records carry different tax numbers: two legal entities.")
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
            ignored_shared_keys=ignored_keys,
            fuzzy_pairs_evaluated=int(evaluated),
            fuzzy_pairs_matched=matched_pairs,
            fuzzy_pairs_vetoed=vetoed,
            common_name_pairs_held_back=common_held,
            street_conflict_pairs_held_back=street_held,
            exact_links_vetoed=exact_vetoed,
            chain_links_refused=chain_refused,
            fuzzy_blocks_limited=limited,
            enriched_cells=enriched_cells,
            reviewed_merges=reviewed_merges,
            reviewed_separations=reviewed_separations,
            possible_duplicate_pairs=possible_pairs,
            possible_duplicates_path=possible_path,
            weak_match_clusters=summary["weak_clusters"],
            samples=summary["samples"],
            output_path=str(output),
            warnings=warnings,
        )

    @staticmethod
    def _conflicting_groups(config: DedupConfig, clusters: np.ndarray, veto_codes: list[np.ndarray]) -> int:
        """Merged groups that hold records with DIFFERENT values of a veto key (two tax numbers): reached through a chain of name matches or through a record that has no tax
        number. The pair-by-pair veto cannot see these, so they are counted for a person to check. Worked out on numeric arrays: no row is turned into a Python object."""
        if not veto_codes or len(clusters) == 0:
            return 0
        sizes = np.bincount(clusters)
        if sizes.max() < 2:   # nothing was merged
            return 0
        in_group = sizes[clusters] >= 2
        found: set[int] = set()
        for codes in veto_codes:
            ids = np.nonzero((codes >= 0) & in_group)[0]
            if not len(ids):
                continue
            distinct = np.unique(np.column_stack([clusters[ids], codes[ids]]), axis=0)          # one row per (group, tax number)
            groups, counts = np.unique(distinct[:, 0], return_counts=True)
            found.update(groups[counts > 1].tolist())
        return len(found)

    @staticmethod
    def _warnings(children: int, eligible: int, conflicting_groups: int, limited_blocks: int = 0, weak_clusters: int = 0, possible_pairs: int = 0, enriched_cells: int = 0) -> list[str]:
        out = []
        if eligible and children / eligible > HIGH_MERGE_RATE:
            out.append(f"{children:,} of {eligible:,} records ({children / eligible:.0%}) were merged away as duplicates. That is unusually many and more often means "
                       "loose matching (many similar names) than real duplicates: look at the cluster samples and, if needed, raise fuzzy_threshold before relying on this.")
        if conflicting_groups:
            out.append(f"{conflicting_groups:,} merged group(s) contain records with different tax numbers (linked through a chain of similar names or a shared "
                       "e-mail / phone). Two tax numbers are two legal entities: check these groups before the merged records are dropped.")
        if weak_clusters:
            out.append(f"{weak_clusters:,} merged group(s) rest on the name alone (one of the records had no address to confirm it). Common names and persons are where "
                       "wrong merges happen: check them (dedup_match_strength = NAME_ONLY) before the merged records are dropped.")
        if possible_pairs:
            out.append(f"{possible_pairs:,} pair(s) look like duplicates but not strongly enough to merge. They were NOT merged: both records load. "
                       "possible_duplicates.parquet lists them for a person to decide.")
        if enriched_cells:
            out.append(f"{enriched_cells:,} field(s) of surviving records were filled from a duplicate (survivorship = fill_blanks); each such row lists them in dedup_enriched_columns.")
        if limited_blocks:
            out.append(f"{limited_blocks:,} very large group(s) of look-alike names or addresses were compared only with their nearest neighbours by name (max_block_size), "
                       "not every record with every other; a duplicate whose name sorts far from its twin in such a group may have been missed.")
        return out

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
    def _key_value_sql(key: ExactKey) -> str:
        """The normalised composite key of a row, NULL when any part cannot identify anyone."""
        parts = [_key_component(c, key.normalizer) for c in key.columns]
        any_null = " OR ".join(f"{p} IS NULL" for p in parts)
        return f"CASE WHEN {any_null} THEN NULL ELSE concat_ws(chr(31), {', '.join(parts)}) END"

    @staticmethod
    def _exact_edges(con, key: ExactKey, eligible_sql: str, max_group: int) -> tuple[np.ndarray, np.ndarray, int]:
        """Link rows sharing a valid key value. A value shared by more than `max_group` rows is a placeholder
        (a group mailbox, 'n/a'), not a person: it links nobody and is counted instead."""
        con.execute(
            f'CREATE OR REPLACE TEMP TABLE kv AS SELECT "__id", k, min("__id") OVER w AS a, count(*) OVER w AS c FROM ('
            f'SELECT "__id", {DeduplicationEngine._key_value_sql(key)} AS k '
            f"FROM stg WHERE {eligible_sql}) WHERE k IS NOT NULL WINDOW w AS (PARTITION BY k)"
        )
        ignored = int(con.execute(f"SELECT count(DISTINCT k) FROM kv WHERE c > {int(max_group)}").fetchone()[0])
        data = con.execute(f'SELECT a, "__id" AS b FROM kv WHERE c > 1 AND c <= {int(max_group)} AND a <> "__id"').fetchnumpy()
        return data["a"].astype(np.int64), data["b"].astype(np.int64), ignored

    @staticmethod
    def _blocks(cursor, batch: int = 128):
        """The blocks of a query result, a few at a time: a table of a million rows has hundreds of thousands of names that must not all be Python strings at once."""
        while True:
            rows = cursor.fetchmany(batch)
            if not rows:
                return
            yield from rows

    @staticmethod
    def _veto_codes(con, config: DedupConfig, eligible_sql: str, total: int) -> list[np.ndarray]:
        """For each veto key (the tax number): an array with a code per row id, -1 where the row has no usable value, so two rows can be compared with numpy."""
        out = []
        for key in [k for k in config.exact_keys if k.vetoes_fuzzy]:
            data = con.execute(
                f'SELECT "__id", dense_rank() OVER (ORDER BY k) - 1 AS code FROM (SELECT "__id", {DeduplicationEngine._key_value_sql(key)} AS k FROM stg WHERE {eligible_sql}) WHERE k IS NOT NULL'
            ).fetchnumpy()
            codes = np.full(total, -1, dtype=np.int64)
            codes[data["__id"].astype(np.int64)] = data["code"].astype(np.int64)
            out.append(codes)
        return out

    # ------------------------------------------------------------------ phase 2
    def _fuzzy_edges(self, con, config: DedupConfig, eligible_sql: str, veto_codes: list[np.ndarray]) -> _Fuzzy:
        addr = _address_norm_sql(config.address_columns) if config.address_columns else "''"
        # Strong address evidence is a street, a house number or a postal code. A city alone is shared by every customer of that city: it is not evidence that two records are one.
        strong_cols = [c for c in config.address_columns if "CITY" not in c.upper()]
        strong = " OR ".join(f"coalesce(trim(CAST({q(c)} AS VARCHAR)), '') <> ''" for c in strong_cols) if strong_cols else "true"
        # Blocking keys, three passes (a pair only has to share ONE of them): the start of the name with its words in alphabetical order ("Müller, Hans" and "Hans Müller" meet,
        # as the comparison itself ignores word order), the start of the address, and the house number + postal code (the digits of the address: very selective, so it finds
        # the same site even when the name starts differently and the street is written another way, in a table where "Hauptstraße" or "Müller" alone is a huge block).
        con.execute(
            f'CREATE TEMP TABLE fz AS SELECT "__id", sn AS nn, ns, an, st, '
            f"CAST(hash(array_to_string(regexp_extract_all(sn, '[0-9]+'), ' ')) % 1000000007 AS BIGINT) AS ndc, "
            f"CAST(hash(array_to_string(dg, ' ')) % 1000000007 AS BIGINT) AS adc, CAST((CASE WHEN an <> '' THEN 1 ELSE 0 END) + (CASE WHEN st THEN 2 ELSE 0 END) + 4 * len(dg) AS BIGINT) AS fl, "
            f"left(replace(sn, ' ', ''), 3) AS b1, "
            f"CASE WHEN an <> '' THEN left(replace(an, ' ', ''), 6) END AS b2, "
            f"CASE WHEN len(dg) >= 2 OR (len(dg) = 1 AND len(dg[1]) >= 4) THEN array_to_string(dg, ' ') END AS b3 FROM ("
            f"SELECT \"__id\", array_to_string(list_sort(string_split(nn, ' ')), ' ') AS sn, nn AS ns, an, st, list_sort(regexp_extract_all(an, '[0-9]+')) AS dg FROM ("
            f'SELECT "__id", {_name_norm_sql(config.name_column)} AS nn, {addr} AS an, ({strong}) AS st '
            f"FROM stg WHERE {eligible_sql} AND {q(config.name_column)} IS NOT NULL) WHERE nn <> '')"
        )
        common = {r[0] for r in con.execute(f"SELECT nn FROM fz GROUP BY nn HAVING count(*) >= {int(config.common_name_count)}").fetchall()}
        held: set[tuple[int, int]] = set()
        threshold = config.fuzzy_threshold * 100.0
        review_cut = min(config.review_threshold, config.fuzzy_threshold) * 100.0
        weight = config.name_weight
        pairs: dict[tuple[int, int], bool] = {}   # pair -> decided on the name alone
        possible: dict[tuple[int, int], float] = {}
        possible_overflow = 0
        evaluated = limited_blocks = 0
        vetoed = 0   # pairs that matched on the name but carry different tax numbers. Counted, never stored: on a million customers there are ~100 million of them
        for block_col in ("b1", "b2", "b3"):
            blocks = self._blocks(con.execute(
                f'SELECT list("__id" ORDER BY "__id"), list(nn ORDER BY "__id"), list(an ORDER BY "__id"), list(fl ORDER BY "__id"), list(ndc ORDER BY "__id"), list(adc ORDER BY "__id"), '
                f'list({"ns" if block_col == "b3" else "CAST(NULL AS VARCHAR)"} ORDER BY "__id") '
                f"FROM fz WHERE {block_col} IS NOT NULL GROUP BY {block_col} HAVING count(*) > 1"
            ))
            for ids, names, addrs, flags, ndc, adc, raws in blocks:
                n = len(ids)
                banded = n > config.max_block_size
                if banded:
                    # An oversized block (a very common name prefix or street) compared all-against-all grows with n squared. Sort it by name
                    # and compare each record only with its nearest neighbours: similar names are adjacent, so the near-duplicates are still met.
                    order = sorted(range(n), key=names.__getitem__)
                    ids, names, addrs, flags, ndc, adc, raws = ([col[k] for k in order] for col in (ids, names, addrs, flags, ndc, adc, raws))
                    limited_blocks += 1
                else:
                    evaluated += n * (n - 1) // 2
                id_arr = np.asarray(ids, dtype=np.int64)
                _, b1_arr = np.unique([n.replace(" ", "")[:3] for n in names], return_inverse=True)   # which names share their first blocking key
                ndc_a, adc_a, flag_a = np.asarray(ndc, dtype=np.int64), np.asarray(adc, dtype=np.int64), np.asarray(flags, dtype=np.int64)
                adl_a = flag_a >> 2                                                  # how many numbers (house number, postal code) the address holds
                sts = ((flag_a & 2) > 0).tolist()                                    # a street / house number / postal code (not just a city) on this record
                backed = ((flag_a & 1) > 0) & ((flag_a & 2) > 0)
                for start in range(0, n, _CHUNK_ROWS):
                    chunk = names[start : start + _CHUNK_ROWS]
                    end = min(n, start + len(chunk) + config.max_block_size // 4) if banded else n
                    if banded:
                        evaluated += sum(end - i - 1 for i in range(start, start + len(chunk)))   # the pairs actually compared: each row with the later rows up to `end`
                    # score_cutoff: a pair below the review band is not scored to the end (its entry is 0), which gives the same result as scoring it, much sooner
                    scores = process.cdist(chunk, names[start:end], scorer=fuzz.token_sort_ratio, dtype=np.float32, workers=self._threads, score_cutoff=review_cut)
                    if block_col == "b3":
                        # The block is the same house number + postal code, so it is small and the extra comparison is cheap: a space typed in the wrong place ("Schmid tHolzbau")
                        # or left out changes the words, which the word-sorting comparison above cannot see through.
                        joined = [x.replace(" ", "") for x in raws]   # the words in their original order
                        scores = np.maximum(scores, process.cdist(joined[start : start + len(chunk)], joined[start:end], scorer=fuzz.ratio, dtype=np.float32, workers=self._threads, score_cutoff=review_cut))
                    rows, cols = np.nonzero(scores >= review_cut)
                    keep = cols > rows  # upper triangle only (columns are counted from `start`)
                    rows, cols = rows[keep], cols[keep] + start
                    if veto_codes and len(rows):
                        # Both carry a tax number and the numbers differ: two legal entities, whatever their names look like. Decided for the whole chunk at once on numeric arrays:
                        # on a large table nearly all similar-name pairs are of this kind, and each one as a Python object was most of the time and of the memory.
                        ia, ib = id_arr[start + rows], id_arr[cols]
                        bad = np.zeros(len(rows), dtype=bool)
                        for codes in veto_codes:
                            bad |= (codes[ia] >= 0) & (codes[ib] >= 0) & (codes[ia] != codes[ib])
                        if bad.any():
                            # a pair that shares its first blocking key was met in the first pass already: it is counted there, once
                            vetoed += int((bad if block_col == "b1" else bad & (b1_arr[start + rows] != b1_arr[cols])).sum())
                            rows, cols = rows[~bad], cols[~bad]
                    if len(rows):
                        # The cheap decisions, for the whole chunk at once (a hash can only fail to drop a pair, never drop a wrong one; the loop below decides the rest). On a table of
                        # people nearly every similar-name pair is a different person at a different address, and one Python iteration each was most of the time.
                        ia, ib = start + rows, cols
                        drop = ndc_a[ia] != ndc_a[ib]                                                         # "Plant 1" vs "Plant 2"
                        both = backed[ia] & backed[ib]
                        drop |= both & (adl_a[ia] == adl_a[ib]) & (adl_a[ia] > 0) & (adc_a[ia] != adc_a[ib])   # a different house number / postal code = a different site
                        rows, cols = rows[~drop], cols[~drop]
                    for r, c in zip(rows, cols):
                        i, j = start + int(r), int(c)
                        if _digits(names[i]) != _digits(names[j]):
                            continue  # "Plant 1" vs "Plant 2": numbers are identity, not typos
                        name_score = float(scores[r, c - start])
                        name_only = not (addrs[i] and addrs[j]) or not (sts[i] and sts[j])   # no street / house number / postal code on both sides: the name is all there is
                        if not name_only:
                            if _digits(addrs[i]) == _digits(addrs[j]):
                                address_score = fuzz.token_sort_ratio(addrs[i], addrs[j])
                            else:
                                di, dj = Counter(_digits(addrs[i])), Counter(_digits(addrs[j]))  # multisets: "78 ... LS1 78" is not "80 ... LS1 78"
                                if di - dj and dj - di:
                                    continue  # a different house number / postal code = a different site
                                # one record merely lacks a number (postal code, house no.): missing is not conflicting
                                address_score = fuzz.token_set_ratio(addrs[i], addrs[j])
                            combined = weight * name_score + (1 - weight) * address_score
                        else:
                            combined = name_score
                        pair = (min(int(ids[i]), int(ids[j])), max(int(ids[i]), int(ids[j])))
                        if combined >= threshold and name_only and (names[i] in common or names[j] in common):
                            held.add(pair)                                                # a common name and no street: not enough to say it is the same customer
                            if pair in possible or len(possible) < _MAX_REVIEW_PAIRS:
                                possible[pair] = max(possible.get(pair, 0.0), combined)
                            else:
                                possible_overflow += 1
                            continue
                        if combined >= threshold:
                            pairs[pair] = pairs.get(pair, True) and name_only    # any pair of passes that had an address makes the evidence stronger
                        elif combined >= review_cut:                              # Fellegi-Sunter's middle band: not merged, a person decides
                            if pair in possible or len(possible) < _MAX_REVIEW_PAIRS:
                                possible[pair] = max(possible.get(pair, 0.0), combined)
                            else:
                                possible_overflow += 1
        street_held = self._street_conflicts(con, config, pairs, possible, threshold)
        if not pairs:
            return _Fuzzy(np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, bool), evaluated, vetoed, limited_blocks, possible, possible_overflow, len(held), street_held)
        keys = sorted(pairs)
        arr = np.array(keys, dtype=np.int64)
        return _Fuzzy(arr[:, 0], arr[:, 1], np.array([pairs[k] for k in keys], dtype=bool), evaluated, vetoed, limited_blocks, possible, possible_overflow, len(held), street_held)

    @staticmethod
    def _street_conflicts(con, config: DedupConfig, pairs: dict, possible: dict, threshold: float) -> int:
        """Two records with the same name, city, postal code and house number but streets that are plainly different ("Goethestraße 95" and "Eichenring 95") are probably two
        people who share a common name and a coincidence of numbers: the name carries most of the score, so the street is checked on its own. Such a pair is moved from the
        merges to the review list. Street spellings ("Hauptstr." / "Hauptstraße", a typo) stay far above the cut."""
        street_cols = [c for c in config.address_columns if any(t in c.upper() for t in ("STREET", "STRASSE", "STRAS"))]
        if not pairs or not street_cols:
            return 0
        needed = np.unique(np.array(list(pairs), dtype=np.int64).ravel())
        con.register("need_street", pd.DataFrame({"i": needed}))
        street = dict(con.execute(f'SELECT s."__id", {_address_norm_sql(street_cols)} FROM stg s JOIN need_street n ON n.i = s."__id"').fetchall())
        con.unregister("need_street")
        moved = 0
        for pair in list(pairs):
            a, b = street.get(pair[0], ""), street.get(pair[1], "")
            if a and b and max(fuzz.token_sort_ratio(a, b), fuzz.ratio(a.replace(" ", ""), b.replace(" ", ""))) < _STREET_AGREEMENT:
                del pairs[pair]
                possible[pair] = max(possible.get(pair, 0.0), threshold)
                moved += 1
        return moved

    @staticmethod
    def _fill_columns(config: DedupConfig, columns: list[str], comp_cols: list[str]) -> list[str]:
        """The staging value columns a master may inherit from its duplicates: never the name (it identifies the record), the keys, the status or anything internal."""
        blocked = {config.pk_column, config.name_column, config.status_column, *OUTPUT_COLUMNS}
        blocked |= {c for k in config.exact_keys for c in k.columns}
        wanted = config.survivorship_columns if config.survivorship_columns else comp_cols
        return [c for c in wanted if c in columns and c not in blocked and not c.startswith(("_", "SRC_", "dedup_"))]

    @staticmethod
    def _prepare_fill(con, config: DedupConfig, fill_cols: list[str], has_status: bool) -> int:
        """Temp table `fl`: per group, for each fill column the value of the best duplicate that has one (a good record before a failed one, then the most complete, then the
        smallest key). Returns how many cells of masters will be filled."""
        good = f"coalesce(stg.{q(config.status_column)}, 'OK') <> 'FAILED'" if has_status else "true"
        firsts = ", ".join(
            f"first(CAST(stg.{q(c)} AS VARCHAR) ORDER BY ({good}) DESC, stg.\"__comp\" DESC, CAST(stg.{q(config.pk_column)} AS VARCHAR)) "
            f"FILTER (WHERE dd.is_child AND stg.{q(c)} IS NOT NULL AND trim(CAST(stg.{q(c)} AS VARCHAR)) <> '' AND {good}) AS \"__f_{i}\"" for i, c in enumerate(fill_cols))
        score = " + ".join(f"CASE WHEN stg.{q(c)} IS NULL OR trim(CAST(stg.{q(c)} AS VARCHAR)) = '' THEN 0 ELSE 1 END" for c in fill_cols) or "0"
        con.execute(f'CREATE TEMP TABLE stg_c AS SELECT stg.*, CAST(({score}) AS INTEGER) AS "__comp" FROM stg')
        con.execute(f'CREATE TEMP TABLE fl AS SELECT dd.cluster_id, {firsts} FROM stg_c AS stg JOIN dd ON stg."__id" = dd."__id" WHERE dd.cluster_id IS NOT NULL GROUP BY dd.cluster_id')
        counts = ", ".join(
            f"count_if(NOT dd.is_child AND fl.\"__f_{i}\" IS NOT NULL AND (stg.{q(c)} IS NULL OR trim(CAST(stg.{q(c)} AS VARCHAR)) = ''))" for i, c in enumerate(fill_cols))
        row = con.execute(f"SELECT {counts} FROM stg JOIN dd ON stg.\"__id\" = dd.\"__id\" JOIN fl ON fl.cluster_id = dd.cluster_id").fetchone()
        return int(sum(row or []))

    @staticmethod
    def _apply_decisions(con, config: DedupConfig, total: int, edges_a: list, edges_b: list, edge_reason: list, edge_weak: list, reasons: list):
        """Bring the people's decisions into the graph before it is clustered: a pair they said is the same entity becomes an edge (evidence REVIEWED), a pair they
        said is different loses every edge between its two records, whatever evidence it had. -> (merges, separations found in this data, all decided pairs)."""
        if not config.must_link and not config.cannot_link:
            return 0, 0, set()
        wanted = {pk for pair in (*config.must_link, *config.cannot_link) for pk in pair}
        rows = con.execute(f'SELECT CAST({q(config.pk_column)} AS VARCHAR), "__id" FROM stg WHERE CAST({q(config.pk_column)} AS VARCHAR) IN (SELECT unnest(?))', [sorted(wanted)]).fetchall()
        id_of = {pk: int(i) for pk, i in rows}
        norm = lambda a, b: (min(id_of[a], id_of[b]), max(id_of[a], id_of[b]))
        cannot = {norm(a, b) for a, b in config.cannot_link if a in id_of and b in id_of and a != b}
        must = {norm(a, b) for a, b in config.must_link if a in id_of and b in id_of and a != b} - cannot
        separations = 0
        if cannot:
            blocked = np.array(sorted(lo * total + hi for lo, hi in cannot), dtype=np.int64)
            for i in range(len(edges_a)):
                lo, hi = np.minimum(edges_a[i], edges_b[i]), np.maximum(edges_a[i], edges_b[i])
                hit = np.isin(lo * total + hi, blocked)
                if hit.any():
                    separations += int(hit.sum())
                    keep = ~hit
                    edges_a[i], edges_b[i], edge_reason[i], edge_weak[i] = edges_a[i][keep], edges_b[i][keep], edge_reason[i][keep], edge_weak[i][keep]
        if must:
            reasons.append("REVIEWED:MERGE")
            ordered = sorted(must)
            edges_a.append(np.array([x for x, _ in ordered], dtype=np.int64))
            edges_b.append(np.array([y for _, y in ordered], dtype=np.int64))
            edge_reason.append(np.full(len(ordered), len(reasons) - 1, dtype=np.int32))
            edge_weak.append(np.zeros(len(ordered), dtype=bool))
        return len(must), len(cannot), cannot | must

    def _write_possible(self, con, config: DedupConfig, fuzzy: Optional[_Fuzzy], clusters: np.ndarray, output: Path, decided: Optional[set] = None):
        """The review band as a file a person can work through: pairs that ended up in the same group anyway (through other evidence) are left out."""
        target = output.with_name("possible_duplicates.parquet")
        target.unlink(missing_ok=True)
        if fuzzy is None or not fuzzy.possible:
            return 0, None
        keep = [(a, b, s) for (a, b), s in fuzzy.possible.items() if clusters[a] != clusters[b] and (a, b) not in (decided or ())]   # a decided pair is no longer a question
        total = len(keep) + fuzzy.possible_overflow
        if not keep:
            return 0, None
        con.register("pp", pd.DataFrame(keep, columns=["__a", "__b", "score"]))
        pk, name = q(config.pk_column), q(config.name_column)
        con.execute(
            f'COPY (SELECT CAST(a.{pk} AS VARCHAR) AS pk_a, CAST(b.{pk} AS VARCHAR) AS pk_b, CAST(a.{name} AS VARCHAR) AS name_a, CAST(b.{name} AS VARCHAR) AS name_b, '
            f'fa.an AS address_a, fb.an AS address_b, round(pp.score, 1) AS similarity '
            f'FROM pp JOIN stg a ON a."__id" = pp."__a" JOIN stg b ON b."__id" = pp."__b" '
            f'JOIN fz fa ON fa."__id" = pp."__a" JOIN fz fb ON fb."__id" = pp."__b" ORDER BY pp.score DESC, pk_a) TO {lit(target.as_posix())} (FORMAT parquet)')
        con.unregister("pp")
        return total, str(target)

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
    def _cluster_checked(total, edges_a, edges_b, edge_reason, edge_weak, reasons, veto_codes, tax_reasons) -> tuple[np.ndarray, int]:
        """Connected groups, except that a link which would put two DIFFERENT tax numbers into one group is refused: two legal entities must not become one because a record
        with no tax number matched both of them. Only the (few) groups that would hold two tax numbers are redone, link by link, strongest evidence first (tax number, other exact
        key, address-backed name match, name-only match); a link a person decided is never refused. The edge lists are filtered in place."""
        labels = DeduplicationEngine._cluster(total, edges_a, edges_b)
        if not veto_codes or not edges_a:
            return labels, 0
        sizes = np.bincount(labels)
        bad: set[int] = set()
        for codes in veto_codes:
            ids = np.nonzero((codes >= 0) & (sizes[labels] >= 2))[0]
            if len(ids):
                distinct = np.unique(np.column_stack([labels[ids], codes[ids]]), axis=0)
                groups, counts = np.unique(distinct[:, 0], return_counts=True)
                bad.update(groups[counts > 1].tolist())
        if not bad:
            return labels, 0
        lengths = [len(x) for x in edges_a]
        a, b = np.concatenate(edges_a), np.concatenate(edges_b)
        reason, weak = np.concatenate(edge_reason), np.concatenate(edge_weak)
        reviewed = np.array([r.startswith("REVIEWED") for r in reasons])[reason]
        is_tax = np.array([r in tax_reasons for r in reasons])[reason]
        is_exact = np.array([r.startswith("EXACT") for r in reasons])[reason]
        priority = np.where(reviewed, 0, np.where(is_tax, 1, np.where(is_exact, 2, 3 + weak.astype(np.int8))))
        idx = np.nonzero(np.isin(labels[a], np.array(sorted(bad))))[0]
        idx = idx[np.argsort(priority[idx], kind="stable")]
        parent: dict[int, int] = {}
        held: dict[int, list[int]] = {}

        def find(x: int) -> int:
            root = x
            while parent.get(root, root) != root:
                root = parent[root]
            while parent.get(x, x) != root:
                parent[x], x = root, parent[x]
            return root

        def codes_of(root: int) -> list[int]:
            if root not in held:
                held[root] = [int(c[root]) for c in veto_codes]
            return held[root]

        refused = np.zeros(len(a), dtype=bool)
        for e in idx:
            rx, ry = find(int(a[e])), find(int(b[e]))
            if rx == ry:
                continue
            cx, cy = codes_of(rx), codes_of(ry)
            if not reviewed[e] and any(p >= 0 and q >= 0 and p != q for p, q in zip(cx, cy)):
                refused[e] = True
                continue
            parent[ry] = rx
            held[rx] = [p if p >= 0 else q for p, q in zip(cx, cy)]
        nodes = np.unique(np.concatenate([a[idx], b[idx]]))
        base = int(labels.max()) + 1
        for node in nodes:
            labels[node] = base + find(int(node))
        _, labels = np.unique(labels, return_inverse=True)
        keep, start = ~refused, 0
        for i, n in enumerate(lengths):
            part = keep[start : start + n]
            edges_a[i], edges_b[i], edge_reason[i], edge_weak[i] = edges_a[i][part], edges_b[i][part], edge_reason[i][part], edge_weak[i][part]
            start += n
        return labels.astype(np.int64), int(refused.sum())

    @staticmethod
    def _completeness(con, config: DedupConfig, columns: list[str], penalise_failed: bool = False):
        score = " + ".join(
            f"CASE WHEN {q(c)} IS NULL OR trim(CAST({q(c)} AS VARCHAR)) = '' THEN 0 ELSE 1 END" for c in columns
        ) or "0"
        if penalise_failed:   # a record that failed transformation never outranks a good member of its group, however many columns it fills
            score = f"({score}) - CASE WHEN coalesce({q(config.status_column)}, 'OK') = 'FAILED' THEN 100000 ELSE 0 END"
        data = con.execute(
            f'SELECT "__id", CAST(({score}) AS INTEGER) AS comp, CAST({q(config.pk_column)} AS VARCHAR) AS pk '
            f'FROM stg ORDER BY "__id"'
        ).fetchnumpy()
        return data["__id"].astype(np.int64), data["comp"].astype(np.int64), np.asarray(data["pk"]).astype(str)

    @staticmethod
    def _golden_records(total, labels, edges_a, edge_reason, edge_weak, reasons, ids, completeness, pks):
        empty = pd.DataFrame({
            "__id": pd.Series(dtype="int64"), "cluster_id": pd.Series(dtype="int64"),
            "is_child": pd.Series(dtype="bool"), "master_pk": pd.Series(dtype="object"),
            "match_type": pd.Series(dtype="object"), "strength": pd.Series(dtype="object"),
        })
        sizes = np.bincount(labels, minlength=int(labels.max()) + 1 if total else 1)
        in_cluster = sizes[labels] > 1
        if not in_cluster.any():
            return empty, {"clusters": 0, "children": 0, "exact_clusters": 0, "fuzzy_clusters": 0, "weak_clusters": 0, "samples": []}

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
        strength: dict[int, int] = {}      # the strongest kind of evidence that holds each group together: a shared key > name + address > the name alone
        if edges_a:
            exact_edge = np.array([r.startswith("EXACT") for r in reasons])[er]
            reviewed_edge = np.array([r.startswith("REVIEWED") for r in reasons])[er]
            edge_rank = np.where(reviewed_edge, 4, np.where(exact_edge, 3, np.where(np.concatenate(edge_weak), 1, 2)))
            for lbl, rk in pd.DataFrame({"l": labels[ea], "r": edge_rank}).groupby("l")["r"].max().items():
                strength[int(lbl)] = int(rk)
        is_child = np.array([master_of[int(lbl)] != int(i) for lbl, i in zip(member_labels, member_ids)])
        frame = pd.DataFrame({
            "__id": member_ids,
            "cluster_id": [rank[int(lbl)] for lbl in member_labels],
            "is_child": is_child,
            "master_pk": [str(pks[master_of[int(lbl)]]) for lbl in member_labels],
            "match_type": [match_type[int(lbl)] for lbl in member_labels],
            "strength": [_STRENGTH[strength[int(lbl)]] for lbl in member_labels],
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
            "weak_clusters": sum(1 for lbl in rank if strength.get(lbl) == 1),
            "samples": samples,
        }
