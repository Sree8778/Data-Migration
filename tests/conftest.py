"""Fixtures: an isolated test database, rebuilt per session and truncated per test."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from alembic import command
from alembic.config import Config

from src.db.models import Base
from src.db.session import create_db_engine, create_session_factory, get_test_database_url
from src.services.metadata_service import MetadataService


def _ensure_database_exists(url: str) -> None:
    parsed = make_url(url)
    admin = sa.create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.scalar(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": parsed.database}
            )
            if not exists:
                conn.execute(sa.text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def engine():
    url = get_test_database_url()
    assert make_url(url).database.endswith("_test"), "refusing to run tests against a non-test database"
    try:
        _ensure_database_exists(url)
    except sa.exc.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL not reachable (run `docker compose up -d`): {exc.orig}")
    eng = create_db_engine(url)
    # Build the schema exclusively through Alembic so the migrations are what gets tested.
    with eng.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def session_factory(engine):
    return create_session_factory(engine)


@pytest.fixture()
def service(engine, session_factory) -> MetadataService:
    """A MetadataService over a freshly emptied schema."""
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return MetadataService(session_factory)
