"""Column profiling and schema drift detection."""

from typing import Any

import polars as pl
from rapidfuzz import fuzz

from databridge.core.types import from_polars


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def profile_fields(df: pl.DataFrame, sample_size: int = 5) -> list[dict[str, Any]]:
    """Type, null %, distinct count, min/max and sample values per column."""
    fields: list[dict[str, Any]] = []
    n = df.height
    for name, dtype in df.schema.items():
        s = df.get_column(name)
        nulls = s.null_count()
        non_null = s.drop_nulls()
        info: dict[str, Any] = {
            "name": name,
            "type": from_polars(dtype),
            "nullable": nulls > 0,
            "null_pct": round(100 * nulls / n, 1) if n else 0.0,
            "distinct": int(non_null.n_unique()) if len(non_null) else 0,
            "samples": [_jsonable(v) for v in non_null.unique(maintain_order=True).head(sample_size).to_list()],
        }
        if len(non_null) and (dtype.is_numeric() or dtype.is_temporal()):
            info["min"] = _jsonable(non_null.min())
            info["max"] = _jsonable(non_null.max())
        fields.append(info)
    return fields


def detect_drift(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> dict[str, Any]:
    """Compares two field lists. Renames are matched by fuzzy name similarity (>= 85)."""
    old_by = {f["name"]: f for f in old}
    new_by = {f["name"]: f for f in new}
    removed = [n for n in old_by if n not in new_by]
    added = [n for n in new_by if n not in old_by]
    renamed = []
    for r in list(removed):
        best = max(added, key=lambda a: fuzz.ratio(r.lower(), a.lower()), default=None)
        if best and fuzz.ratio(r.lower(), best.lower()) >= 85:
            renamed.append({"from": r, "to": best})
            removed.remove(r)
            added.remove(best)
    retyped = [
        {"name": n, "from": old_by[n]["type"], "to": new_by[n]["type"]}
        for n in old_by
        if n in new_by and old_by[n]["type"] != new_by[n]["type"]
    ]
    return {
        "added": added,
        "removed": removed,
        "renamed": renamed,
        "retyped": retyped,
        "changed": bool(added or removed or renamed or retyped),
    }
