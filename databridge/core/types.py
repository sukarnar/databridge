"""Canonical type system.

Every source (Excel, CSV, Oracle, SQL Server, ...) is normalized to these types so the
Mapping Studio can apply one set of compatibility rules everywhere.
"""

import re
from typing import Any

import polars as pl

CANONICAL_TYPES = [
    "string",
    "integer",
    "decimal",
    "float",
    "boolean",
    "date",
    "timestamp",
    "json",
    "binary",
]

_DECIMAL_RE = re.compile(r"decimal\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", re.I)


def base_type(ctype: str) -> str:
    """'decimal(12,2)' -> 'decimal'."""
    return ctype.split("(")[0].strip().lower() if ctype else "string"


def to_polars(ctype: str) -> pl.DataType:
    b = base_type(ctype)
    if b == "decimal":
        m = _DECIMAL_RE.fullmatch(ctype.strip())
        if m:
            return pl.Decimal(int(m.group(1)), int(m.group(2)))
        return pl.Float64()
    return {
        "string": pl.String(),
        "integer": pl.Int64(),
        "float": pl.Float64(),
        "boolean": pl.Boolean(),
        "date": pl.Date(),
        "timestamp": pl.Datetime("us"),
        "json": pl.String(),
        "binary": pl.Binary(),
    }.get(b, pl.String())


def from_polars(dtype: pl.DataType) -> str:
    if dtype.is_integer():
        return "integer"
    if isinstance(dtype, pl.Decimal):
        return f"decimal({dtype.precision or 38},{dtype.scale})"
    if dtype.is_float():
        return "float"
    if dtype == pl.Boolean:
        return "boolean"
    if dtype == pl.Date:
        return "date"
    if isinstance(dtype, pl.Datetime):
        return "timestamp"
    if dtype == pl.Binary:
        return "binary"
    if isinstance(dtype, (pl.Struct, pl.List)):
        return "json"
    return "string"


def from_sqlalchemy(sa_type: Any) -> str:
    """Map a SQLAlchemy column type (from Inspector.get_columns) to a canonical type."""
    name = type(sa_type).__name__.upper()
    precision = getattr(sa_type, "precision", None)
    scale = getattr(sa_type, "scale", None)
    if name in {"NUMERIC", "DECIMAL", "NUMBER", "MONEY", "SMALLMONEY"}:
        if scale in (0, None) and precision is not None and name == "NUMBER":
            return "integer"
        if precision is not None and scale is not None:
            return "integer" if scale == 0 else f"decimal({precision},{scale})"
        return "decimal(38,10)" if name != "NUMBER" else "float"
    if "INT" in name:
        return "integer"
    if name in {"FLOAT", "REAL", "DOUBLE", "DOUBLE_PRECISION", "BINARY_DOUBLE", "BINARY_FLOAT"}:
        return "float"
    if name in {"BOOLEAN", "BIT"}:
        return "boolean"
    if name == "DATE":
        return "date"
    if "TIMESTAMP" in name or "DATETIME" in name:
        return "timestamp"
    if name in {"JSON", "JSONB"}:
        return "json"
    if name in {"BLOB", "BYTEA", "VARBINARY", "BINARY", "RAW", "LARGEBINARY", "IMAGE"}:
        return "binary"
    return "string"


# Compatibility of converting a value of type `src` into type `tgt`.
_OK = {
    ("integer", "decimal"),
    ("integer", "float"),
    ("decimal", "float"),
    ("date", "timestamp"),
    ("boolean", "integer"),
}
_INCOMPATIBLE = {
    ("date", "integer"),
    ("date", "decimal"),
    ("date", "float"),
    ("date", "boolean"),
    ("timestamp", "integer"),
    ("timestamp", "decimal"),
    ("timestamp", "float"),
    ("timestamp", "boolean"),
    ("boolean", "date"),
    ("boolean", "timestamp"),
    ("integer", "date"),
    ("float", "date"),
    ("decimal", "date"),
}


def compatibility(src: str, tgt: str) -> str:
    """Returns 'ok', 'lossy' or 'incompatible'."""
    s, t = base_type(src), base_type(tgt)
    if s == t or t == "string":
        return "ok"
    if (s, t) in _OK:
        return "ok"
    if s in {"json", "binary"} or t in {"json", "binary"}:
        return "incompatible"
    if (s, t) in _INCOMPATIBLE:
        return "incompatible"
    return "lossy"
