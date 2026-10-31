"""Allow MongoDB as a source system type.

The system type is a VARCHAR guarded by a named CHECK constraint, so this widens the allowed values; no column is added or changed.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

OLD = "'ORACLE', 'SAP_ECC', 'SAP_S4HANA', 'MSSQL', 'POSTGRESQL', 'MYSQL', 'FLAT_FILE'"
NEW = "'ORACLE', 'SAP_ECC', 'SAP_S4HANA', 'MSSQL', 'POSTGRESQL', 'MYSQL', 'MONGODB', 'FLAT_FILE'"


def _recreate(values: str) -> None:
    op.execute("ALTER TABLE cat_data_sources DROP CONSTRAINT IF EXISTS ck_cat_data_sources_system_type")
    op.execute(f"ALTER TABLE cat_data_sources ADD CONSTRAINT ck_cat_data_sources_system_type CHECK (system_type IN ({values}))")


def upgrade() -> None:
    _recreate(NEW)


def downgrade() -> None:
    # A downgrade must never fail on data: MongoDB sources are relabelled (their connection_config still names the real dialect).
    op.execute("UPDATE cat_data_sources SET system_type = 'FLAT_FILE' WHERE system_type = 'MONGODB'")
    _recreate(OLD)
