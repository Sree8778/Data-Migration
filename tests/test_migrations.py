"""Alembic is the single source of schema truth: migrations must match the ORM models."""

from __future__ import annotations

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from src.db.models import Base


def test_alembic_head_matches_models(engine):
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


def test_alembic_version_is_stamped(engine):
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0001"
