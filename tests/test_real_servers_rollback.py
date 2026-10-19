"""Rollback of a delivery on the real Oracle, SQL Server and MySQL servers (and the PostgreSQL one through the API in test_rollback.py)."""

from __future__ import annotations

import pytest

from src.connectors.base import ColumnDef, ConnectorError
from src.connectors.dialects import connector_for
from tests.test_real_servers import server  # noqa: F401 - the parametrised fixture that skips a server that is not running


def test_rollback_removes_only_the_runs_rows_checks_the_counts_and_drops_an_emptied_table(server):
    w = connector_for(server.spec("rw"))
    cols = [ColumnDef("LEGACY_KEY", "text", 255, False), ColumnDef("MIG_RUN_ID", "int", nullable=False), ColumnDef("V", "text", 20)]
    table = w.natural_case("MIG_RB_T")
    run_col = w.natural_case("MIG_RUN_ID")
    run1 = [("a", 1, "x"), ("b", 1, "y")]

    def tables():
        return {t.name for t in w.tables(server.tgt_schema)}

    w.load_table(server.tgt_schema, "MIG_RB_T", cols, ["LEGACY_KEY"], iter(run1))
    out = w.rollback_delivery(server.tgt_schema, table, run_col, 1, expected_rows=2)
    assert (out.removed, out.remaining, out.table_dropped) == (2, 0, True) and table not in tables()       # emptied, so dropped

    w.load_table(server.tgt_schema, "MIG_RB_T", cols, ["LEGACY_KEY"], iter(run1))
    q = w.quote
    with w.session(read_only=False) as conn:                                                                   # a row another run delivered into the same table
        cur = conn.cursor()
        cur.execute(f"INSERT INTO {q(server.tgt_schema)}.{q(table)} ({q(w.natural_case('LEGACY_KEY'))}, {q(run_col)}, {q(w.natural_case('V'))}) "
                    f"VALUES ({w._placeholder(1)}, {w._placeholder(2)}, {w._placeholder(3)})", ("c", 2, "z"))
        cur.close()
        conn.commit()
    with pytest.raises(ConnectorError, match="changed since"):                                                  # the table does not hold what was delivered
        w.rollback_delivery(server.tgt_schema, table, run_col, 1, expected_rows=3)
    assert w.count(server.tgt_schema, table) == 3                                                               # a refused rollback changes nothing
    out = w.rollback_delivery(server.tgt_schema, table, run_col, 1, expected_rows=2)
    assert (out.removed, out.remaining, out.table_dropped) == (2, 1, False) and w.count(server.tgt_schema, table) == 1   # the other run's row stays
    with pytest.raises(ConnectorError, match="nothing to roll back"):
        w.rollback_delivery(server.tgt_schema, table, run_col, 1)
    with pytest.raises(ConnectorError, match="not found|cannot see|column"):
        w.rollback_delivery(server.tgt_schema, table, "NO_SUCH_COLUMN", 2)
    assert w.rollback_delivery(server.tgt_schema, table, run_col, 2).table_dropped and table not in tables()


def test_merge_applies_a_delta_keeps_before_images_verifies_values_and_rolls_back_to_the_exact_table(server):
    w = connector_for(server.spec("rw"))
    cols = [ColumnDef("LEGACY_KEY", "text", 255, False), ColumnDef("MIG_RUN_ID", "int", nullable=False), ColumnDef("NAME", "text", 40), ColumnDef("NOTE", "text", 60)]
    names = [w.natural_case(c.name) for c in cols]
    key, run_col = names[0], names[1]
    table = w.natural_case("MIG_MG_T")
    base = [(f"k{i}", 1, f"Name {i} M\u00fcller", f"note {i}") for i in range(1, 8)]
    w.load_table(server.tgt_schema, "MIG_MG_T", cols, ["LEGACY_KEY"], iter(base))
    original = w.fingerprint_table(server.tgt_schema, table, names, [key])
    try:
        delta = [("k2", 2, "Name 2 CHANGED", "note 2"), ("k3", 2, "Name 3 \u00c4ndered", "note 3"), ("k8", 2, "Brand new 8", "note 8"), ("k9", 2, "Brand new 9", None)]
        image: list[tuple] = []
        out = w.merge_table(server.tgt_schema, "MIG_MG_T", cols, "LEGACY_KEY", iter(delta), delete_keys=iter(["k5", "does_not_exist"]), batch_size=2, before_sink=image.extend)
        assert (out.inserted, out.updated, out.deleted) == (2, 2, 1) and (out.rows_before, out.rows_after) == (7, 8) and out.before_rows == 3
        assert out.values_verified and out.cells_compared == len(delta) * len(cols) and len(out.value_digest) == 16
        assert sorted(r[0] for r in image) == ["k2", "k3", "k5"] and dict((r[0], r[2]) for r in image)["k2"] == "Name 2 M\u00fcller"     # the OLD versions were kept
        assert w.count(server.tgt_schema, table) == 8
        assert original.differing_columns(w.fingerprint_table(server.tgt_schema, table, names, [key]))

        # a merge that fails halfway changes nothing
        def boom():
            yield ("k1", 3, "half way", "x")
            raise RuntimeError("the source went away")
        before = w.fingerprint_table(server.tgt_schema, table, names, [key])
        with pytest.raises(ConnectorError, match="rolled back"):
            w.merge_table(server.tgt_schema, "MIG_MG_T", cols, "LEGACY_KEY", boom(), batch_size=1)
        assert before.differing_columns(w.fingerprint_table(server.tgt_schema, table, names, [key])) == []
        with pytest.raises(ConnectorError, match="different columns"):
            w.merge_table(server.tgt_schema, "MIG_MG_T", cols[:3], "LEGACY_KEY", iter([("k1", 3, "x")]))

        # roll back: the table is exactly what it was before the merge
        with pytest.raises(ConnectorError, match="changed since"):
            w.rollback_merge(server.tgt_schema, table, cols, "LEGACY_KEY", run_col, 2, iter(image), expected_mine=5, expected_before=3)
        rb = w.rollback_merge(server.tgt_schema, table, cols, "LEGACY_KEY", run_col, 2, iter(image), expected_mine=4, expected_before=3)
        assert (rb.removed, rb.restored, rb.rows_before, rb.rows_after) == (4, 3, 8, 7)
        assert original.differing_columns(w.fingerprint_table(server.tgt_schema, table, names, [key])) == [] and original.digest == w.fingerprint_table(
            server.tgt_schema, table, names, [key]).digest
    finally:
        with w.session(read_only=False) as conn:
            w._drop(conn, server.tgt_schema, table)
            conn.commit()
