"""Alembic environment. URL precedence: -x/config `sqlalchemy.url` > DATABASE_URL > project default."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from src.db import models  # noqa: F401  (registers all tables on Base.metadata)
from src.db.models import Base
from src.db.session import create_db_engine, get_database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_database_url()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_url())
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
