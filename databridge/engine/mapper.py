"""Mapping engine: row steps -> column rules -> cast to target types -> validation.

The same function powers the Studio's live preview (on a sample) and publishing (on all rows),
so what users see in the preview is what gets published.
"""

import re
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from databridge.core.types import base_type, to_polars
from databridge.engine.formula import FormulaError, compile_formula

ROW_ID = "__row"
ERRORS = "__errors"


@dataclass
class MappingSpec:
    rules: list[dict[str, Any]] = field(default_factory=list)  # [{target, formula}]
    row_steps: list[dict[str, Any]] = field(default_factory=list)
    validations: list[dict[str, Any]] = field(default_factory=list)  # [{field, kind, value}]

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "MappingSpec":
        d = d or {}
        return cls(
            rules=list(d.get("rules") or []),
            row_steps=list(d.get("row_steps") or []),
            validations=list(d.get("validations") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"rules": self.rules, "row_steps": self.row_steps, "validations": self.validations}


@dataclass
class MapResult:
    valid: pl.DataFrame  # target columns only
    rejects: pl.DataFrame  # __row, target columns as text, __errors
    cell_errors: dict[int, dict[str, str]]  # {row_index_in_output: {field: message}} for preview
    all_rows: pl.DataFrame  # every mapped row (valid + rejected) in source order, with __row
    rule_errors: dict[str, str]  # {target_field: formula error}
    rows_in: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {"rows_in": self.rows_in, "rows_out": self.valid.height, "rows_rejected": self.rejects.height}


# ------------------------------------------------------------------ row steps


def apply_row_steps(df: pl.DataFrame, steps: list[dict[str, Any]]) -> pl.DataFrame:
    for step in steps:
        kind = step.get("kind")
        if kind == "filter" and step.get("formula"):
            df = df.filter(compile_formula(step["formula"], set(df.columns)).fill_null(False))
        elif kind == "dedupe":
            cols = step.get("columns") or None
            df = df.unique(subset=cols, keep="first", maintain_order=True)
        elif kind == "sort" and step.get("column"):
            df = df.sort(step["column"], descending=bool(step.get("descending")), nulls_last=True)
        elif kind == "unpivot" and step.get("value_columns"):
            df = df.unpivot(
                on=step["value_columns"],
                index=step.get("id_columns") or [c for c in df.columns if c not in step["value_columns"]],
                variable_name=step.get("variable_name") or "attribute",
                value_name=step.get("value_name") or "value",
            )
    return df


def source_columns_after_steps(columns: list[str], steps: list[dict[str, Any]]) -> list[str]:
    """Column names visible to rules after row steps (unpivot changes them)."""
    cols = list(columns)
    for step in steps:
        if step.get("kind") == "unpivot" and step.get("value_columns"):
            ids = step.get("id_columns") or [c for c in cols if c not in step["value_columns"]]
            cols = ids + [step.get("variable_name") or "attribute", step.get("value_name") or "value"]
    return cols


# ------------------------------------------------------------------ run


def _cast(expr: pl.Expr, ctype: str) -> pl.Expr:
    b = base_type(ctype)
    dtype = to_polars(ctype)
    if b in {"integer", "float", "decimal"}:
        cleaned = expr.cast(pl.String).str.replace_all(r"[,$€£ ]", "")
        if b == "integer":
            return cleaned.cast(pl.Float64, strict=False).cast(pl.Int64, strict=False)
        return cleaned.cast(pl.Float64, strict=False).cast(dtype, strict=False)
    if b == "date":
        from databridge.engine.formula import _date_expr

        return _date_expr(expr, None)
    if b == "timestamp":
        return expr.cast(pl.String).str.to_datetime(strict=False, time_unit="us")
    if b == "boolean":
        s = expr.cast(pl.String).str.to_lowercase().str.strip_chars()
        return (
            pl.when(s.is_in(["true", "yes", "y", "1", "t"])).then(True)
            .when(s.is_in(["false", "no", "n", "0", "f"])).then(False)
            .otherwise(None)
        )
    return expr.cast(dtype, strict=False)


def run_mapping(
    source: pl.DataFrame,
    spec: MappingSpec,
    target_fields: list[dict[str, Any]],
) -> MapResult:
    df = apply_row_steps(source, spec.row_steps)
    rows_in = df.height
    df = df.with_row_index(ROW_ID, offset=1)
    available = set(df.columns)

    rules = {r["target"]: r.get("formula", "") for r in spec.rules if r.get("target")}
    rule_errors: dict[str, str] = {}
    raw_exprs: list[pl.Expr] = [pl.col(ROW_ID)]
    for f in target_fields:
        name = f["name"]
        formula = (rules.get(name) or "").strip()
        expr: pl.Expr = pl.lit(None)
        if formula:
            try:
                expr = compile_formula(formula, available)
            except FormulaError as e:
                rule_errors[name] = str(e)
        raw_exprs.append(expr.alias(name))

    raw = df.select(raw_exprs)
    typed = raw.select(
        [pl.col(ROW_ID)] + [_cast(pl.col(f["name"]), f.get("type", "string")).alias(f["name"]) for f in target_fields]
    )

    # Per-field error message expressions (first failing check wins per cell).
    extra = {}
    for v in spec.validations:
        extra.setdefault(v.get("field"), []).append(v)
    err_cols: list[pl.Expr] = []
    for f in target_fields:
        name = f["name"]
        t = pl.col(name)
        r = pl.col(f"__raw__{name}")
        checks: list[tuple[pl.Expr, str]] = []
        if name in rule_errors:
            checks.append((pl.lit(True), f"formula error: {rule_errors[name]}"))
        # conversion failure: had a value before casting, null after
        checks.append((r.is_not_null() & (r.cast(pl.String) != "") & t.is_null(),
                       f"cannot convert to {f.get('type', 'string')}"))
        if f.get("required"):
            checks.append((t.is_null() | (t.cast(pl.String).str.strip_chars() == ""), "required value missing"))
        for v in extra.get(name, []):
            kind, val = v.get("kind"), v.get("value")
            if kind == "regex" and val:
                checks.append((t.is_not_null() & ~t.cast(pl.String).str.contains(val), f"does not match {val}"))
            elif kind == "allowed" and val:
                allowed = [x.strip() for x in (val if isinstance(val, list) else str(val).split(","))]
                checks.append((t.is_not_null() & ~t.cast(pl.String).is_in(allowed), f"not one of {', '.join(allowed)}"))
            elif kind == "min" and val not in (None, ""):
                checks.append((t.is_not_null() & (t.cast(pl.Float64, strict=False) < float(val)), f"below minimum {val}"))
            elif kind == "max" and val not in (None, ""):
                checks.append((t.is_not_null() & (t.cast(pl.Float64, strict=False) > float(val)), f"above maximum {val}"))
            elif kind == "unique":
                checks.append((t.is_not_null() & t.is_duplicated(), "duplicate value"))
        expr = pl.lit(None, dtype=pl.String)
        for cond, msg in reversed(checks):
            expr = pl.when(cond.fill_null(False)).then(pl.lit(msg)).otherwise(expr)
        err_cols.append(expr.alias(f"__err__{name}"))

    checked = typed.with_columns(raw.select(pl.all().name.prefix("__raw__")).drop(f"__raw__{ROW_ID}")).with_columns(err_cols)
    err_names = [f"__err__{f['name']}" for f in target_fields]
    has_error = pl.any_horizontal([pl.col(c).is_not_null() for c in err_names]) if err_names else pl.lit(False)
    checked = checked.with_columns(has_error.alias("__bad"))

    names = [f["name"] for f in target_fields]
    valid = checked.filter(~pl.col("__bad")).select(names)
    bad = checked.filter(pl.col("__bad"))
    rejects = bad.select(
        [pl.col(ROW_ID).alias("source_row")]
        + [pl.col(f"__raw__{n}").cast(pl.String).alias(n) for n in names]
        + [pl.concat_str(
            [pl.when(pl.col(f"__err__{n}").is_not_null()).then(pl.lit(f"{n}: ") + pl.col(f"__err__{n}"))
             for n in names], separator="; ", ignore_nulls=True).alias("errors")]
    )

    cell_errors: dict[int, dict[str, str]] = {}
    err_rows = checked.select(err_names).to_dicts()
    for i, row in enumerate(err_rows):
        errs = {k.removeprefix("__err__"): v for k, v in row.items() if v}
        if errs:
            cell_errors[i] = errs
    all_rows = checked.select([ROW_ID] + names)
    return MapResult(valid=valid, rejects=rejects, cell_errors=cell_errors, all_rows=all_rows,
                     rule_errors=rule_errors, rows_in=rows_in)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "endpoint"
