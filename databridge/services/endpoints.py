"""Endpoints and API keys, and the DuckDB query that serves endpoint requests."""

import re
from datetime import datetime, timezone
from typing import Any

import duckdb
import polars as pl
from sqlalchemy import select

from databridge.config import settings
from databridge.core.db import session_scope
from databridge.core.models import ApiKey, Endpoint
from databridge.core.security import hash_key, new_api_key
from databridge.services import mappings as map_svc

OPS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "contains": "ILIKE", "in": "IN"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,78}$")


class EndpointError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def list_endpoints() -> list[Endpoint]:
    with session_scope() as s:
        return list(s.scalars(select(Endpoint).order_by(Endpoint.slug)))


def get_endpoint_by_slug(slug: str) -> Endpoint | None:
    with session_scope() as s:
        return s.scalars(select(Endpoint).where(Endpoint.slug == slug)).first()


def save_endpoint(values: dict[str, Any], endpoint_id: int | None = None) -> Endpoint:
    slug = values["slug"].strip().lower()
    if not SLUG_RE.match(slug):
        raise ValueError("Slug must be 2-79 characters: lowercase letters, digits and dashes")
    params = []
    for p in values.get("params") or []:
        if p.get("name") and p.get("column"):
            if p.get("op", "eq") not in OPS:
                raise ValueError(f"Unknown operator {p.get('op')}")
            params.append({"name": p["name"], "column": p["column"], "op": p.get("op", "eq")})
    with session_scope() as s:
        ep = s.get(Endpoint, endpoint_id) if endpoint_id else Endpoint()
        ep.slug, ep.name = slug, values.get("name") or slug
        ep.description = values.get("description", "")
        ep.mapping_id = int(values["mapping_id"])
        ep.params = params
        ep.public = bool(values.get("public"))
        ep.page_size = int(values.get("page_size") or settings.default_page_size)
        ep.formats = values.get("formats") or ["json", "csv", "xlsx"]
        ep.active = bool(values.get("active", True))
        ep.pinned_version = values.get("pinned_version")
        s.add(ep)
        s.flush()
        return ep


def delete_endpoint(endpoint_id: int) -> None:
    with session_scope() as s:
        ep = s.get(Endpoint, endpoint_id)
        if ep:
            s.delete(ep)


# ------------------------------------------------------------------ API keys


def create_api_key(name: str, endpoints: list[str] | None = None) -> tuple[ApiKey, str]:
    raw, prefix, digest = new_api_key()
    with session_scope() as s:
        key = ApiKey(name=name, prefix=prefix, key_hash=digest, endpoints=endpoints or ["*"])
        s.add(key)
        s.flush()
        return key, raw


def list_api_keys() -> list[ApiKey]:
    with session_scope() as s:
        return list(s.scalars(select(ApiKey).order_by(ApiKey.id.desc())))


def revoke_api_key(key_id: int) -> None:
    with session_scope() as s:
        k = s.get(ApiKey, key_id)
        if k:
            k.active = False


def active_key(raw_key: str | None) -> ApiKey | None:
    """The active API key for this raw value (used by the streaming API to re-check keys while connected)."""
    if not raw_key:
        return None
    with session_scope() as s:
        return s.scalars(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key), ApiKey.active.is_(True))).first()


def authorize(endpoint: Endpoint, raw_key: str | None) -> None:
    if endpoint.public:
        return
    if not raw_key:
        raise EndpointError(401, "Missing X-API-Key header")
    with session_scope() as s:
        key = s.scalars(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key), ApiKey.active.is_(True))).first()
        if not key:
            raise EndpointError(401, "Invalid API key")
        if "*" not in key.endpoints and endpoint.slug not in key.endpoints:
            raise EndpointError(403, f"Key not allowed for endpoint {endpoint.slug}")
        key.last_used_at = datetime.now(timezone.utc)


# ------------------------------------------------------------------ query


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def query(endpoint: Endpoint, args: dict[str, str], page: int = 1, page_size: int | None = None,
          version: int | None = None, all_rows: bool = False) -> dict[str, Any]:
    """Filters, paginates and returns a Polars frame plus paging info."""
    if not endpoint.active:
        raise EndpointError(404, "Endpoint is not active")
    ds = map_svc.dataset_for(endpoint.mapping_id, version or endpoint.pinned_version)
    if not ds:
        raise EndpointError(404, "No published dataset yet for this endpoint")
    where, binds = [], [ds.parquet_path]
    for p in endpoint.params:
        if p["name"] not in args or args[p["name"]] in (None, ""):
            continue
        value, op = args[p["name"]], OPS[p["op"]]
        col = _qi(p["column"])
        if p["op"] == "contains":
            where.append(f"CAST({col} AS VARCHAR) ILIKE ?")
            binds.append(f"%{value}%")
        elif p["op"] == "in":
            items = [v.strip() for v in value.split(",") if v.strip()]
            where.append(f"CAST({col} AS VARCHAR) IN ({', '.join('?' for _ in items)})")
            binds.extend(items)
        else:
            where.append(f"{col} {op} ?")
            binds.append(value)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    size = min(int(page_size or endpoint.page_size), settings.max_page_size)
    page = max(1, int(page))
    con = duckdb.connect()
    try:
        total = con.execute(f"SELECT COUNT(*) FROM read_parquet(?){clause}", binds).fetchone()[0]
        sql = f"SELECT * FROM read_parquet(?){clause}"
        if not all_rows:
            sql += f" LIMIT {size} OFFSET {(page - 1) * size}"
        df = con.execute(sql, binds).pl()
    except duckdb.Error as e:
        raise EndpointError(400, f"Query failed: {e}") from e
    finally:
        con.close()
    return {"df": df, "total": int(total), "page": page, "page_size": size, "version": ds.version,
            "published_at": ds.created_at}


class BatchQuery:
    """An endpoint query read in batches (streaming API): memory stays at one batch whatever the dataset size.

    Each method is blocking; call them from a worker thread. `interrupt()` may be called from another thread to
    stop a running statement.
    """

    def __init__(self, endpoint: Endpoint, args: dict[str, str], version: int | None = None):
        if not endpoint.active:
            raise EndpointError(404, "Endpoint is not active")
        ds = map_svc.dataset_for(endpoint.mapping_id, version or endpoint.pinned_version)
        if not ds:
            raise EndpointError(404, "No published dataset yet for this endpoint")
        self.version = ds.version
        where, self.binds = [], [ds.parquet_path]
        for p in endpoint.params:
            if p["name"] not in args or args[p["name"]] in (None, ""):
                continue
            value, col = args[p["name"]], _qi(p["column"])
            if p["op"] == "contains":
                where.append(f"CAST({col} AS VARCHAR) ILIKE ?")
                self.binds.append(f"%{value}%")
            elif p["op"] == "in":
                items = [v.strip() for v in value.split(",") if v.strip()]
                where.append(f"CAST({col} AS VARCHAR) IN ({', '.join('?' for _ in items)})")
                self.binds.extend(items)
            else:
                where.append(f"{col} {OPS[p['op']]} ?")
                self.binds.append(value)
        self.clause = f" WHERE {' AND '.join(where)}" if where else ""
        self.con = duckdb.connect(config={"threads": 2, "memory_limit": "512MB"})
        self.columns: list[str] = []

    def count(self) -> int:
        try:
            return int(self.con.execute(f"SELECT COUNT(*) FROM read_parquet(?){self.clause}", self.binds).fetchone()[0])
        except duckdb.Error as e:
            raise EndpointError(400, "Query failed") from e

    def start(self) -> None:
        try:
            self.con.execute(f"SELECT * FROM read_parquet(?){self.clause}", self.binds)
        except duckdb.Error as e:
            raise EndpointError(400, "Query failed") from e
        self.columns = [d[0] for d in self.con.description]

    def next_rows(self, n: int) -> list[dict[str, Any]]:
        rows = self.con.fetchmany(n)
        return [{c: _json_value(v) for c, v in zip(self.columns, row)} for row in rows]

    def interrupt(self) -> None:
        try:
            self.con.interrupt()
        except Exception:  # noqa: BLE001 - already finished or closed
            pass

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:  # noqa: BLE001
            pass


def _json_value(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if type(v).__name__ == "Decimal":
        return float(v)
    return v


def to_jsonable(df: pl.DataFrame) -> list[dict[str, Any]]:
    out = []
    for row in df.to_dicts():
        out.append({k: (v.isoformat() if hasattr(v, "isoformat") else float(v) if type(v).__name__ == "Decimal" else v)
                    for k, v in row.items()})
    return out
