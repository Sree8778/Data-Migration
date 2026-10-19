"""Alembic is the single source of schema truth: migrations must match the ORM models."""

from __future__ import annotations

import pytest
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
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0002"


def test_alembic_downgrade_and_upgrade_cycle(engine):
    from alembic import command
    from alembic.config import Config
    from src.db.session import get_test_database_url

    url = get_test_database_url()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

    # Downgrade to base
    command.downgrade(cfg, "base")
    with engine.connect() as conn:
        # Table exists but has no stamped version row
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) is None

    # Upgrade back to head
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0002"



def test_downgrade_with_postgresql_and_mysql_sources_present_relabels_them_instead_of_failing(engine):
    from alembic import command
    from alembic.config import Config
    from src.db.session import get_test_database_url

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", get_test_database_url().replace("%", "%%"))
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM cat_tables; DELETE FROM cat_data_sources"))
        conn.execute(sa.text("INSERT INTO cat_data_sources (system_name, system_type, target_type, connection_config) VALUES "
                             "('PG_SRC', 'POSTGRESQL', 'SOURCE', '{\"dialect\": \"postgresql\"}'), ('MY_SRC', 'MYSQL', 'SOURCE', '{}')"))
    command.downgrade(cfg, "0001")
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0001"
        rows = dict(conn.execute(sa.text("SELECT system_name, system_type FROM cat_data_sources")).all())
        assert rows == {"PG_SRC": "FLAT_FILE", "MY_SRC": "FLAT_FILE"}
        with pytest.raises(sa.exc.DBAPIError):
            conn.execute(sa.text("INSERT INTO cat_data_sources (system_name, system_type, target_type, connection_config) VALUES ('X', 'POSTGRESQL', 'SOURCE', '{}')"))
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM cat_data_sources"))
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0002"
