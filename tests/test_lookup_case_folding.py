"""The second million-row dataset (a German-style export) found that a lookup entry for "Großbritannien" never matched the value "Großbritannien": the lookup keys are upper-cased
by Python ("GROSSBRITANNIEN") and the data by DuckDB, which turns the German sharp s into the capital sharp s ("GROẞBRITANNIEN"). Both sides now fold the same way."""

from __future__ import annotations

import duckdb
import pytest

from src.services._duckdb_util import fold
from src.services.lookup_service import normalize_lookup_key

VALUES = ["Großbritannien", "  großbritannien ", "GROSSBRITANNIEN", "Österreich", "Türkei", "Deutschland", "straße", "Côte d'Ivoire", "España", "Ελλάδα", "日本", ""]


@pytest.mark.parametrize("value", VALUES)
def test_the_sql_key_is_the_python_key(value):
    assert duckdb.connect().execute(f"SELECT {fold('CAST(? AS VARCHAR)')}", [value]).fetchone()[0] == normalize_lookup_key(value)
