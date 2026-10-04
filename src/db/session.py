"""Engine / session factory and schema lifecycle helpers (synchronous psycopg2)."""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import Base

DEFAULT_DATABASE_URL = "postgresql+psycopg2://migration:migration_dev_pw@localhost:5432/migration_catalog"
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg2://migration:migration_dev_pw@localhost:5432/migration_catalog_test"
)


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def get_test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def create_db_engine(url: str | None = None) -> Engine:
    """Bounded pool (max 10 connections) to conserve RAM; the dev container allows 50."""
    return create_engine(
        url or get_database_url(),
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_schema(engine: Engine) -> None:
    """Create all tables straight from the models. Scratch use only: real environments
    must be managed with `alembic upgrade head` so schema changes stay tracked."""
    Base.metadata.create_all(engine, checkfirst=True)


def drop_schema(engine: Engine) -> None:
    """Drop all catalog tables. Intended for tests and local resets only."""
    Base.metadata.drop_all(engine)
