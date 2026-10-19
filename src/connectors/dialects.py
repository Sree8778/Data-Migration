"""Dialect-specific parts of the read-only connectors: how to connect, what the catalog queries look like, how to quote and limit.

PostgreSQL is exercised against a real server by the tests. Oracle, SQL Server and MySQL are written against their documented
catalog views and tested with a fake DB-API connection; they have not been run against real servers of those products.
"""

from __future__ import annotations

from typing import Any, Iterator

from src.connectors.base import FETCH_BATCH, ColumnDef, ConnectionSpec, Connector, ConnectorError


def _public_ca_bundle() -> str:
    """The Mozilla CA bundle shipped with `certifi` (it contains the DigiCert / Microsoft roots that Azure certificates chain to)."""
    import certifi
    return certifi.where()


def _ident(name: str, quote: str, close: str | None = None) -> str:
    close = close or quote
    return quote + name.replace(close, close + close) + close


# ====================================================================== PostgreSQL
class PostgresConnector(Connector):
    dialect, default_port, system_type, label = "postgresql", 5432, "POSTGRESQL", "PostgreSQL"
    _SYSTEM = ("pg_catalog", "information_schema")
    transactional_ddl = True

    def _type_sql(self, col: ColumnDef) -> str:
        return "INTEGER" if col.kind == "int" else f"VARCHAR({int(col.length or 255)})" if (col.length or 255) <= 10485760 else "TEXT"

    def natural_case(self, name: str) -> str:
        return name.lower()

    def _insert_many(self, cur: Any, sql: str, batch: list[tuple]) -> None:
        from psycopg2.extras import execute_values
        execute_values(cur, sql.split(" VALUES (")[0] + " VALUES %s", batch, page_size=len(batch))   # one multi-row INSERT per batch

    def _connect(self) -> Any:
        import psycopg2
        s = self.spec
        extra = {"sslrootcert": s.options["sslrootcert"]} if s.options.get("sslrootcert") else {}
        return psycopg2.connect(host=s.host, port=s.port, dbname=s.database, user=s.user, password=s.password,
                                connect_timeout=s.connect_timeout, sslmode=str(s.options.get("sslmode") or "prefer"),
                                application_name="erp-migration-accelerator", **extra)

    def _begin_read_only(self, conn: Any) -> None:
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")   # one consistent snapshot for count + extract

    def quote(self, identifier: str) -> str:
        return _ident(identifier, '"')

    def _schemas_sql(self, include_system: bool):
        extra = "" if include_system else " AND schema_name NOT IN ('pg_catalog','information_schema') AND schema_name NOT LIKE 'pg\\_%'"
        return f"SELECT schema_name FROM information_schema.schemata WHERE true{extra}", ()

    def _tables_sql(self, schema: str):
        return ("SELECT c.relname, CASE WHEN c.relkind IN ('v','m') THEN 'VIEW' ELSE 'TABLE' END, c.reltuples::bigint "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m')", (schema,))

    def _columns_sql(self, schema: str, table: str):
        return ("SELECT a.attname, format_type(a.atttypid, a.atttypmod), NULL, NOT a.attnotnull, COALESCE(bool_or(i.indisprimary), false), a.attnum "
                "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary AND a.attnum = ANY (i.indkey) "
                "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped GROUP BY a.attname, a.atttypid, a.atttypmod, a.attnotnull, a.attnum "
                "ORDER BY a.attnum", (schema, table))

    def _limit_sql(self, select_sql: str, limit: int) -> str:
        return f"{select_sql} LIMIT {int(limit)}"

    def _version(self, conn: Any) -> str:
        return str(self._query(conn, "SHOW server_version")[0][0])

    def _stream(self, conn: Any, sql: str):
        cur = conn.cursor(name="mig_extract")        # server-side cursor: memory stays flat for any table size
        cur.itersize = FETCH_BATCH
        cur.execute(sql)
        first = cur.fetchmany(FETCH_BATCH)           # the column names of a named cursor exist after the first fetch
        names = [d[0] for d in cur.description]

        def rows() -> Iterator[tuple]:
            try:
                batch = first
                while batch:
                    yield from batch
                    batch = cur.fetchmany(FETCH_BATCH)
            finally:
                cur.close()
        return names, rows()


# ====================================================================== Oracle
class OracleConnector(Connector):
    dialect, default_port, system_type, label = "oracle", 1521, "ORACLE", "Oracle Database"
    def _type_sql(self, col: ColumnDef) -> str:
        if col.kind == "int":
            return "NUMBER(10)"
        n = int(col.length or 255)
        return f"VARCHAR2({n} CHAR)" if n <= 4000 else "CLOB"

    def _placeholder(self, n: int) -> str:
        return f":{n}"

    def _rename_sql(self, schema: str, old: str, new: str) -> str:
        return f"ALTER TABLE {self.quote(schema)}.{self.quote(old)} RENAME TO {self.quote(new)}"

    def natural_case(self, name: str) -> str:
        return name.upper()

    _SYSTEM_USERS = ("SYS", "SYSTEM", "OUTLN", "DBSNMP", "APPQOSSYS", "CTXSYS", "XDB", "MDSYS", "ORDSYS", "WMSYS", "LBACSYS", "DVSYS", "GSMADMIN_INTERNAL",
                     "AUDSYS", "OJVMSYS", "ORDDATA", "ORDPLUGINS", "SI_INFORMTN_SCHEMA", "DIP", "ANONYMOUS", "XS$NULL", "APEX_030200", "APEX_PUBLIC_USER", "FLOWS_FILES")

    def _connect(self) -> Any:
        import oracledb
        oracledb.defaults.fetch_lobs = False         # LOB columns arrive as str / bytes instead of LOB handles
        oracledb.defaults.fetch_decimals = True      # NUMBER arrives as an exact Decimal, never a binary float
        s = self.spec
        return oracledb.connect(user=s.user, password=s.password, dsn=f"{s.host}:{s.port}/{s.database}", tcp_connect_timeout=s.connect_timeout)

    def _begin_read_only(self, conn: Any) -> None:
        cur = conn.cursor()
        try:
            cur.execute("SET TRANSACTION READ ONLY")   # read-consistent as of the first statement
        finally:
            cur.close()

    def quote(self, identifier: str) -> str:
        return _ident(identifier, '"')

    def _retryable(self, exc: Exception) -> bool:
        # ORA-01466: a read-only (read-consistent) transaction may not read a table whose definition changed in the same
        # second it started: freshly created or freshly granted tables. A moment later the same read succeeds.
        return "ORA-01466" in str(exc)

    def _schemas_sql(self, include_system: bool):
        if include_system:
            return "SELECT username FROM all_users", ()
        marks = ", ".join(f"'{u}'" for u in self._SYSTEM_USERS)
        return f"SELECT username FROM all_users WHERE username NOT IN ({marks}) AND username NOT LIKE 'APEX\\_%' ESCAPE '\\'", ()

    def _tables_sql(self, schema: str):
        return ("SELECT table_name, 'TABLE', num_rows FROM all_tables WHERE owner = :1 UNION ALL SELECT view_name, 'VIEW', NULL FROM all_views WHERE owner = :2",
                (schema, schema))

    def _columns_sql(self, schema: str, table: str):
        return ("SELECT c.column_name, c.data_type || CASE WHEN c.data_type LIKE '%CHAR%' THEN '(' || c.char_length || ')' "
                "WHEN c.data_type = 'NUMBER' AND c.data_precision IS NOT NULL THEN '(' || c.data_precision || ',' || NVL(c.data_scale, 0) || ')' END, "
                "CASE WHEN c.data_type LIKE '%CHAR%' THEN c.char_length END, CASE WHEN c.nullable = 'Y' THEN 1 ELSE 0 END, "
                "CASE WHEN pk.column_name IS NOT NULL THEN 1 ELSE 0 END, c.column_id "
                "FROM all_tab_columns c LEFT JOIN (SELECT cc.column_name FROM all_constraints k JOIN all_cons_columns cc ON cc.owner = k.owner AND cc.constraint_name = k.constraint_name "
                "WHERE k.constraint_type = 'P' AND k.owner = :1 AND k.table_name = :2) pk ON pk.column_name = c.column_name "
                "WHERE c.owner = :3 AND c.table_name = :4 ORDER BY c.column_id", (schema, table, schema, table))

    def _limit_sql(self, select_sql: str, limit: int) -> str:
        return f"SELECT * FROM ({select_sql}) WHERE ROWNUM <= {int(limit)}"

    def _version(self, conn: Any) -> str:
        return str(getattr(conn, "version", "") or "Oracle Database")

    def _stream(self, conn: Any, sql: str):
        cur = conn.cursor()
        cur.arraysize = FETCH_BATCH
        cur.execute(sql)
        names = [d[0] for d in cur.description]

        def rows() -> Iterator[tuple]:
            try:
                while True:
                    batch = cur.fetchmany(FETCH_BATCH)
                    if not batch:
                        return
                    yield from batch
            finally:
                cur.close()
        return names, rows()


# ====================================================================== SQL Server
class MssqlConnector(Connector):
    dialect, default_port, system_type, label = "mssql", 1433, "MSSQL", "Microsoft SQL Server"
    transactional_ddl = True

    def _type_sql(self, col: ColumnDef) -> str:
        if col.kind == "int":
            return "INT"
        n = int(col.length or 255)
        return f"NVARCHAR({n})" if n <= 4000 else "NVARCHAR(MAX)"

    def _begin_read_only(self, conn: Any) -> None:
        """Use snapshot isolation (one consistent view for the count and the extract, no blocking of writers) when the database has it switched on.
        Otherwise READ COMMITTED applies: a table that changes during the extract can then make the control total disagree, which is reported, never hidden."""
        cur = conn.cursor()
        try:
            cur.execute("SELECT snapshot_isolation_state FROM sys.databases WHERE name = DB_NAME()")
            row = cur.fetchone()
        finally:
            cur.close()
        conn.rollback()                                   # the check itself opened a transaction; the isolation level can only be chosen before one starts
        if row and int(row[0]) == 1:
            import pytds.extensions as ext
            conn.isolation_level = ext.ISOLATION_LEVEL_SNAPSHOT

    def _insert_many(self, cur: Any, sql: str, batch: list[tuple]) -> None:
        """The driver sends one statement per row with executemany (about 200 rows/s). One multi-row INSERT per chunk is far faster:
        SQL Server allows 1000 rows per VALUES list and 2100 parameters per statement."""
        head, width = sql.split(" VALUES (")[0], len(batch[0])
        per = max(1, min(1000, 2000 // width))
        marks = "(" + ", ".join(["%s"] * width) + ")"
        for i in range(0, len(batch), per):
            chunk = batch[i:i + per]
            cur.execute(f"{head} VALUES " + ", ".join([marks] * len(chunk)), [v for row in chunk for v in row])

    def _connect(self) -> Any:
        import pytds
        s = self.spec
        # this driver turns TLS on only when it is given a CA file, and Azure SQL refuses connections without TLS: with "encrypt" and no file of
        # its own, the public CA bundle (which holds the roots Azure certificates chain to) is used
        cafile = s.options.get("cafile") or (_public_ca_bundle() if s.options.get("encrypt") else None)
        return pytds.connect(dsn=s.host, port=s.port, database=s.database, user=s.user, password=s.password,
                             login_timeout=s.connect_timeout, timeout=300, autocommit=False, appname="erp-migration-accelerator",
                             cafile=cafile, validate_host=bool(s.options.get("validate_host", True)))

    def quote(self, identifier: str) -> str:
        return _ident(identifier, "[", "]")

    def _schemas_sql(self, include_system: bool):
        if include_system:
            return "SELECT name FROM sys.schemas", ()
        return "SELECT name FROM sys.schemas WHERE name NOT IN ('sys','INFORMATION_SCHEMA','guest') AND name NOT LIKE 'db[_]%'", ()

    def _tables_sql(self, schema: str):
        return ("SELECT t.name, 'TABLE', SUM(p.rows) FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
                "LEFT JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) WHERE s.name = %s GROUP BY t.name "
                "UNION ALL SELECT v.name, 'VIEW', NULL FROM sys.views v JOIN sys.schemas s ON s.schema_id = v.schema_id WHERE s.name = %s", (schema, schema))

    def _columns_sql(self, schema: str, table: str):
        return ("SELECT c.COLUMN_NAME, c.DATA_TYPE + CASE WHEN c.CHARACTER_MAXIMUM_LENGTH IS NOT NULL THEN '(' + CASE WHEN c.CHARACTER_MAXIMUM_LENGTH = -1 THEN 'max' "
                "ELSE CAST(c.CHARACTER_MAXIMUM_LENGTH AS varchar(10)) END + ')' ELSE '' END, c.CHARACTER_MAXIMUM_LENGTH, "
                "CASE WHEN c.IS_NULLABLE = 'YES' THEN 1 ELSE 0 END, CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END, c.ORDINAL_POSITION "
                "FROM INFORMATION_SCHEMA.COLUMNS c LEFT JOIN (SELECT k.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
                "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k ON k.CONSTRAINT_NAME = tc.CONSTRAINT_NAME AND k.TABLE_SCHEMA = tc.TABLE_SCHEMA "
                "WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s) pk ON pk.COLUMN_NAME = c.COLUMN_NAME "
                "WHERE c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s ORDER BY c.ORDINAL_POSITION", (schema, table, schema, table))

    def _limit_sql(self, select_sql: str, limit: int) -> str:
        return select_sql.replace("SELECT ", f"SELECT TOP ({int(limit)}) ", 1)

    def _version(self, conn: Any) -> str:
        return str(self._query(conn, "SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(40))")[0][0])


# ====================================================================== MySQL / MariaDB
class MysqlConnector(Connector):
    dialect, default_port, system_type, label = "mysql", 3306, "MYSQL", "MySQL / MariaDB"

    def _type_sql(self, col: ColumnDef) -> str:
        if col.kind == "int":
            return "INT"
        n = int(col.length or 255)
        return f"VARCHAR({n})" if n <= 16000 else "TEXT"

    def _table_suffix(self) -> str:
        return " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"

    def _rename_sql(self, schema: str, old: str, new: str) -> str:
        return f"RENAME TABLE {self.quote(schema)}.{self.quote(old)} TO {self.quote(schema)}.{self.quote(new)}"

    def _end_session(self, conn: Any) -> None:
        try:
            conn.close()          # the server rolls the transaction back; an explicit ROLLBACK would make the driver read a half-streamed table to its end
        except Exception:  # noqa: BLE001
            pass

    def _connect(self) -> Any:
        import pymysql
        s = self.spec
        tls: dict[str, Any] = {}
        if s.options.get("ssl") is False:
            tls = {"ssl_disabled": True}              # explicitly plain (the driver otherwise negotiates TLS whenever the server offers it)
        elif s.options.get("ssl") or s.options.get("ssl_ca"):
            verify = bool(s.options.get("ssl_verify", True))
            tls = {"ssl_ca": s.options.get("ssl_ca") or _public_ca_bundle(), "ssl_verify_cert": verify, "ssl_verify_identity": verify}
        return pymysql.connect(host=s.host, port=s.port, database=s.database or None, user=s.user, password=s.password,
                               connect_timeout=s.connect_timeout, charset="utf8mb4", autocommit=False, read_timeout=600, **tls)

    def _begin_read_only(self, conn: Any) -> None:
        cur = conn.cursor()
        try:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        finally:
            cur.close()

    def quote(self, identifier: str) -> str:
        return _ident(identifier, "`")

    def _schemas_sql(self, include_system: bool):
        if include_system:
            return "SELECT schema_name FROM information_schema.schemata", ()
        return "SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN ('mysql','information_schema','performance_schema','sys')", ()

    def _tables_sql(self, schema: str):
        return ("SELECT table_name, CASE WHEN table_type = 'VIEW' THEN 'VIEW' ELSE 'TABLE' END, table_rows FROM information_schema.tables WHERE table_schema = %s", (schema,))

    def _columns_sql(self, schema: str, table: str):
        return ("SELECT column_name, column_type, character_maximum_length, CASE WHEN is_nullable = 'YES' THEN 1 ELSE 0 END, "
                "CASE WHEN column_key = 'PRI' THEN 1 ELSE 0 END, ordinal_position FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position", (schema, table))

    def _limit_sql(self, select_sql: str, limit: int) -> str:
        return f"{select_sql} LIMIT {int(limit)}"

    def _version(self, conn: Any) -> str:
        return str(self._query(conn, "SELECT VERSION()")[0][0])

    def _stream(self, conn: Any, sql: str):
        import pymysql.cursors
        cur = conn.cursor(pymysql.cursors.SSCursor)   # unbuffered: rows are streamed, not loaded into memory
        cur.execute(sql)
        names = [d[0] for d in cur.description]

        def rows() -> Iterator[tuple]:
            done = False
            try:
                while True:
                    batch = cur.fetchmany(FETCH_BATCH)
                    if not batch:
                        done = True
                        return
                    yield from batch
            finally:
                if done:
                    cur.close()      # an unfinished unbuffered result is discarded with the connection; closing it would read it to the end
        return names, rows()


CONNECTORS: dict[str, type[Connector]] = {c.dialect: c for c in (PostgresConnector, OracleConnector, MssqlConnector, MysqlConnector)}


def connector_for(spec: ConnectionSpec, connect=None) -> Connector:
    cls = CONNECTORS.get(spec.dialect)
    if cls is None:
        raise ConnectorError(f"unsupported database type '{spec.dialect}' (supported: {', '.join(sorted(CONNECTORS))})")
    return cls(spec, connect)
