"""Repository service for the metadata catalog and mapping-set containers.

The service owns its transactions: each public method runs in a single transaction and
returns Pydantic objects, so callers never hold ORM instances or sessions.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from src.db.enums import MappingStatus, TargetType
from src.db.models import CatColumn, CatDataSource, CatTable, MapTableRule
from src.schemas.metadata import (
    BulkColumnRegistration,
    ColumnCreate,
    ColumnRead,
    DataSourceCreate,
    DataSourceRead,
    MappingSetCreate,
    MappingSetRead,
    TableCreate,
    TableRead,
    TargetDictionary,
    TargetTableDictionary,
)


class MetadataServiceError(Exception):
    """Base class for service errors."""


class NotFoundError(MetadataServiceError):
    pass


class ConflictError(MetadataServiceError):
    pass


class MappingValidationError(MetadataServiceError):
    pass


class MetadataService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------ data sources
    def register_data_source(self, payload: DataSourceCreate) -> DataSourceRead:
        """Create the data source, or update it in place if `system_name` already exists."""
        with self._session_factory.begin() as session:
            source = session.scalar(
                select(CatDataSource).where(CatDataSource.system_name == payload.system_name)
            )
            if source is None:
                source = CatDataSource(**payload.model_dump())
                session.add(source)
            else:
                source.system_type = payload.system_type
                source.target_type = payload.target_type
                source.connection_config = payload.connection_config
            session.flush()
            session.refresh(source)
            return DataSourceRead.model_validate(source)

    # ------------------------------------------------------------ tables
    def register_table(self, payload: TableCreate) -> TableRead:
        """Create the table, or update its descriptive fields if (source_id, table_name) exists.

        `row_count_estimate` is only overwritten when a value is supplied.
        """
        with self._session_factory.begin() as session:
            if session.get(CatDataSource, payload.source_id) is None:
                raise NotFoundError(f"data source {payload.source_id} does not exist")
            table = session.scalar(
                select(CatTable).where(
                    CatTable.source_id == payload.source_id,
                    CatTable.table_name == payload.table_name,
                )
            )
            if table is None:
                table = CatTable(**payload.model_dump())
                session.add(table)
            else:
                table.business_name = payload.business_name
                table.description = payload.description
                table.business_object = payload.business_object
                if payload.row_count_estimate is not None:
                    table.row_count_estimate = payload.row_count_estimate
            session.flush()
            session.refresh(table)
            return TableRead.model_validate(table)

    # ------------------------------------------------------------ columns
    def bulk_register_columns(self, payload: BulkColumnRegistration) -> list[ColumnRead]:
        """Upsert columns of one table in a single statement.

        Structural attributes are always refreshed; profiling statistics (`sample_values`,
        `null_ratio`, `distinct_ratio`) are only overwritten when a new value is supplied.
        Returns the columns ordered by ordinal position.
        """
        with self._session_factory.begin() as session:
            if session.get(CatTable, payload.table_id) is None:
                raise NotFoundError(f"table {payload.table_id} does not exist")

            rows = [
                {
                    **col.model_dump(),
                    "table_id": payload.table_id,
                    "ordinal_position": col.ordinal_position if col.ordinal_position is not None else idx,
                }
                for idx, col in enumerate(payload.columns, start=1)
            ]
            stmt = pg_insert(CatColumn).values(rows)
            excluded = stmt.excluded
            stats = ("sample_values", "null_ratio", "distinct_ratio")
            update_cols = {
                name: excluded[name]
                for name in rows[0]
                if name not in ("table_id", "column_name") and name not in stats
            }
            update_cols.update({name: func.coalesce(excluded[name], CatColumn.__table__.c[name]) for name in stats})
            update_cols["updated_at"] = func.now()
            stmt = stmt.on_conflict_do_update(
                constraint="uq_cat_columns_table_id_column_name", set_=update_cols
            ).returning(CatColumn)

            result = session.scalars(
                stmt, execution_options={"populate_existing": True}
            ).all()
            return [
                ColumnRead.model_validate(c)
                for c in sorted(result, key=lambda c: (c.ordinal_position, c.id))
            ]

    # ------------------------------------------------------------ target dictionary
    def get_target_dictionary(self, object_name: str = "BUSINESS_PARTNER") -> TargetDictionary:
        """All TARGET-side tables (with columns) registered for a business object."""
        with self._session_factory() as session:
            tables = session.scalars(
                select(CatTable)
                .join(CatDataSource, CatTable.source_id == CatDataSource.id)
                .where(
                    CatDataSource.target_type == TargetType.TARGET,
                    CatTable.business_object == object_name,
                )
                .options(selectinload(CatTable.columns))
                .order_by(CatTable.id)
            ).all()
            if not tables:
                raise NotFoundError(f"no target tables registered for object '{object_name}'")
            return TargetDictionary(
                object_name=object_name,
                tables=[
                    TargetTableDictionary(
                        table=TableRead.model_validate(t),
                        columns=[ColumnRead.model_validate(c) for c in t.columns],
                    )
                    for t in tables
                ],
            )

    # ------------------------------------------------------------ mapping sets
    def create_mapping_set(self, payload: MappingSetCreate) -> MappingSetRead:
        """Create a DRAFT mapping container from a SOURCE/STAGING table to a TARGET table."""
        if payload.source_table_id == payload.target_table_id:
            raise MappingValidationError("source and target table must differ")
        try:
            with self._session_factory.begin() as session:
                source = self._load_table(session, payload.source_table_id)
                target = self._load_table(session, payload.target_table_id)
                if source.source.target_type == TargetType.TARGET:
                    raise MappingValidationError(
                        f"table {source.table_name} belongs to a TARGET system and cannot be a mapping source"
                    )
                if target.source.target_type != TargetType.TARGET:
                    raise MappingValidationError(
                        f"table {target.table_name} does not belong to a TARGET system"
                    )

                version = payload.version
                if version is None:
                    current_max = session.scalar(
                        select(func.max(MapTableRule.version)).where(
                            MapTableRule.source_table_id == source.id,
                            MapTableRule.target_table_id == target.id,
                        )
                    )
                    version = (current_max or 0) + 1

                rule = MapTableRule(
                    source_table_id=source.id,
                    target_table_id=target.id,
                    version=version,
                    status=MappingStatus.DRAFT,
                    created_by=payload.created_by,
                )
                session.add(rule)
                session.flush()
                session.refresh(rule)
                return MappingSetRead.model_validate(rule)
        except IntegrityError as exc:
            raise ConflictError(
                f"mapping set version {payload.version} already exists for this table pair"
            ) from exc

    @staticmethod
    def _load_table(session: Session, table_id: int) -> CatTable:
        table = session.scalar(
            select(CatTable).where(CatTable.id == table_id).options(selectinload(CatTable.source))
        )
        if table is None:
            raise NotFoundError(f"table {table_id} does not exist")
        return table
