"""Database connector: any SQLAlchemy dialect, plus generic ODBC and JDBC-style URLs.

Browsing uses SQLAlchemy's Inspector. Reads use Polars `read_database` in batches.
Custom SQL must be a single SELECT (checked with sqlglot).
"""

import importlib.util
from collections.abc import Iterator
from typing import Any, Literal, Optional

import polars as pl
import sqlglot
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine

from databridge.connectors.base import Connector, Node, TestResult
from databridge.core.types import from_polars, from_sqlalchemy

# dialect -> (SQLAlchemy drivername, python module to check, pip package, sqlglot dialect)
DIALECTS: dict[str, tuple[str, str | None, str, str]] = {
    "postgresql": ("postgresql+psycopg", "psycopg", "psycopg[binary]", "postgres"),
    "mysql": ("mysql+pymysql", "pymysql", "pymysql", "mysql"),
    "mariadb": ("mariadb+pymysql", "pymysql", "pymysql", "mysql"),
    "mssql": ("mssql+pyodbc", "pyodbc", "pyodbc", "tsql"),
    "oracle": ("oracle+oracledb", "oracledb", "oracledb", "oracle"),
    "sqlite": ("sqlite", None, "(built in)", "sqlite"),
    "duckdb": ("duckdb", "duckdb_engine", "duckdb-engine", "duckdb"),
    "db2": ("db2+ibm_db", "ibm_db_sa", "ibm_db_sa", "postgres"),
    "snowflake": ("snowflake", "snowflake.sqlalchemy", "snowflake-sqlalchemy", "snowflake"),
    "redshift": ("redshift+psycopg2", "sqlalchemy_redshift", "sqlalchemy-redshift", "redshift"),
    "bigquery": ("bigquery", "sqlalchemy_bigquery", "sqlalchemy-bigquery", "bigquery"),
    "hana": ("hana", "sqlalchemy_hana", "sqlalchemy-hana", "postgres"),
    "teradata": ("teradatasql", "teradatasqlalchemy", "teradatasqlalchemy", "teradata"),
    "odbc": ("mssql+pyodbc", "pyodbc", "pyodbc", "tsql"),
}


def installed_dialects() -> dict[str, bool]:
    out = {}
    for name, (_, module, _, _) in DIALECTS.items():
        out[name] = module is None or importlib.util.find_spec(module.split(".")[0]) is not None
    return out


class DatabaseConfig(BaseModel):
    dialect: Literal[tuple(DIALECTS)] = Field("postgresql", description="Database type")  # type: ignore[valid-type]
    host: Optional[str] = Field(None, description="Host name")
    port: Optional[int] = Field(None, description="Port")
    database: Optional[str] = Field(None, description="Database / service name / file path (SQLite)")
    username: Optional[str] = Field(None, description="User (read-only account recommended)")
    password: Optional[str] = Field(None, description="Password")
    options: str = Field("", description="Extra URL query options, e.g. driver=ODBC Driver 18 for SQL Server")
    url_override: Optional[str] = Field(None, description="Full SQLAlchemy URL (overrides the fields above)")
    schema_allowlist: str = Field("", description="Comma-separated schemas to show (blank = all)")
    statement_timeout_s: int = Field(60, description="Query timeout in seconds")


class DatabaseConnector(Connector):
    type_name = "database"
    label = "Database (Oracle, SQL Server, Postgres, MySQL, ...)"
    config_model = DatabaseConfig
    secret_fields = {"password", "url_override"}
    capabilities = {"browse", "preview", "incremental", "pushdown"}

    def __init__(self, config: dict[str, Any], secrets: dict[str, Any] | None = None):
        super().__init__(config, secrets)
        self.cfg: DatabaseConfig
        self._engine: Engine | None = None

    # -------------------------------------------------------------- engine

    def url(self) -> URL | str:
        c = self.cfg
        if c.url_override:
            return c.url_override
        drivername = DIALECTS[c.dialect][0]
        query: dict[str, str] = {}
        for part in filter(None, (p.strip() for p in c.options.split("&"))):
            k, _, v = part.partition("=")
            query[k.strip()] = v.strip()
        if c.dialect == "oracle" and c.database and "service_name" not in query:
            query["service_name"] = c.database
            database = None
        else:
            database = c.database
        return URL.create(drivername, username=c.username or None, password=c.password or None,
                          host=c.host or None, port=c.port or None, database=database, query=query)

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(self.url(), pool_pre_ping=True, pool_size=5, max_overflow=5) \
                if self.cfg.dialect != "sqlite" else create_engine(self.url())
        return self._engine

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    @property
    def sql_dialect(self) -> str:
        return DIALECTS[self.cfg.dialect][3]

    def _quote(self, *parts: str | None) -> str:
        prep = self.engine.dialect.identifier_preparer
        return ".".join(prep.quote(p) for p in parts if p)

    def _allowed_schema(self, schema: str | None) -> bool:
        allow = [s.strip().lower() for s in self.cfg.schema_allowlist.split(",") if s.strip()]
        return not allow or (schema or "").lower() in allow

    # -------------------------------------------------------------- contract

    def test(self) -> TestResult:
        module = DIALECTS[self.cfg.dialect][1]
        if module and importlib.util.find_spec(module.split(".")[0]) is None:
            return TestResult(False, f"Driver not installed: pip install {DIALECTS[self.cfg.dialect][2]}")
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1" if self.cfg.dialect != "oracle" else "SELECT 1 FROM DUAL"))
                version = conn.dialect.server_version_info
            return TestResult(True, f"Connected ({self.cfg.dialect} {'.'.join(map(str, version or ())) or ''})".strip())
        except Exception as e:  # noqa: BLE001
            return TestResult(False, f"{type(e).__name__}: {str(e).splitlines()[0]}")

    def browse(self, ref: dict[str, Any] | None = None) -> list[Node]:
        insp = inspect(self.engine)
        ref = ref or {}
        if "schema" not in ref:
            try:
                schemas = insp.get_schema_names()
            except NotImplementedError:
                schemas = [None]
            default = insp.default_schema_name
            nodes = []
            for s in schemas:
                if s is not None and s.lower() in {"information_schema", "pg_catalog", "sys"}:
                    continue
                if self._allowed_schema(s):
                    label = s or "(default)"
                    nodes.append(Node(label, "schema", {"schema": s}, has_children=True,
                                      detail="default" if s == default else ""))
            return nodes
        schema = ref.get("schema")
        nodes = [Node(t, "table", {"schema": schema, "table": t}) for t in sorted(insp.get_table_names(schema=schema))]
        nodes += [Node(v, "view", {"schema": schema, "table": v}) for v in sorted(insp.get_view_names(schema=schema))]
        return nodes

    def describe(self, ref: dict[str, Any]) -> list[dict[str, Any]]:
        if ref.get("sql"):
            df = self.preview(ref, limit=100)
            return [{"name": n, "type": from_polars(t), "nullable": True} for n, t in df.schema.items()]
        insp = inspect(self.engine)
        schema, table = ref.get("schema"), ref["table"]
        pk = set((insp.get_pk_constraint(table, schema=schema) or {}).get("constrained_columns") or [])
        fks: dict[str, str] = {}
        try:
            for fk in insp.get_foreign_keys(table, schema=schema):
                for col, rcol in zip(fk["constrained_columns"], fk["referred_columns"]):
                    fks[col] = f"{fk['referred_table']}.{rcol}"
        except NotImplementedError:
            pass
        fields = []
        for col in insp.get_columns(table, schema=schema):
            fields.append({
                "name": col["name"],
                "type": from_sqlalchemy(col["type"]),
                "native_type": str(col["type"]),
                "nullable": bool(col.get("nullable", True)),
                "pk": col["name"] in pk,
                "fk": fks.get(col["name"]),
            })
        return fields

    def validate_sql(self, sql: str) -> str:
        """Returns normalized SQL if it is a single read-only SELECT, else raises ValueError."""
        try:
            statements = [s for s in sqlglot.parse(sql, read=self.sql_dialect) if s is not None]
        except sqlglot.errors.ParseError as e:
            raise ValueError(f"SQL does not parse: {e}") from e
        if len(statements) != 1:
            raise ValueError("Only one statement is allowed")
        stmt = statements[0]
        if not isinstance(stmt, (sqlglot.exp.Select, sqlglot.exp.Union, sqlglot.exp.Intersect, sqlglot.exp.Except)):
            raise ValueError("Only SELECT queries are allowed")
        forbidden = (sqlglot.exp.Insert, sqlglot.exp.Update, sqlglot.exp.Delete, sqlglot.exp.Drop,
                     sqlglot.exp.Create, sqlglot.exp.Alter, sqlglot.exp.Command)
        if any(stmt.find(f) for f in forbidden):
            raise ValueError("Only read-only SELECT queries are allowed")
        return sql.strip().rstrip(";")

    def _select_sql(self, ref: dict[str, Any], limit: int | None = None) -> str:
        if ref.get("sql"):
            base = self.validate_sql(ref["sql"])
            if limit is None:
                return base
            inner = sqlglot.parse_one(base, read=self.sql_dialect).subquery("q")
            return sqlglot.select("*").from_(inner).limit(int(limit)).sql(dialect=self.sql_dialect)
        target = self._quote(ref.get("schema"), ref["table"])
        stmt = sqlglot.select("*").from_(sqlglot.parse_one(target, read=self.sql_dialect, into=sqlglot.exp.Table))
        if ref.get("where"):
            stmt = stmt.where(ref["where"])
        if limit is not None:
            stmt = stmt.limit(int(limit))
        return stmt.sql(dialect=self.sql_dialect)

    def preview(self, ref: dict[str, Any], limit: int = 100) -> pl.DataFrame:
        sql = self._select_sql(ref, limit)
        with self.engine.connect() as conn:
            return pl.read_database(text(sql), connection=conn, infer_schema_length=None)

    def read(self, ref: dict[str, Any], batch_size: int = 50_000) -> Iterator[pl.DataFrame]:
        sql = self._select_sql(ref)
        with self.engine.connect().execution_options(stream_results=True) as conn:
            yield from pl.read_database(text(sql), connection=conn, iter_batches=True,
                                        batch_size=batch_size, infer_schema_length=None)
