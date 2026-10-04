"""Target schemas: from a template workbook, from a source's fields, or defined by hand."""

from typing import Any

from sqlalchemy import select

from databridge.core.db import session_scope
from databridge.core.models import TargetSchema
from databridge.core.types import CANONICAL_TYPES, base_type
from databridge.ingest.sheet_profile import parse_file


def list_targets() -> list[TargetSchema]:
    with session_scope() as s:
        return list(s.scalars(select(TargetSchema).order_by(TargetSchema.name)))


def get_target(target_id: int) -> TargetSchema:
    with session_scope() as s:
        t = s.get(TargetSchema, target_id)
        if not t:
            raise LookupError(f"Target schema {target_id} not found")
        return t


def _clean_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for f in fields:
        name = str(f.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        t = f.get("type") or "string"
        if base_type(t) not in CANONICAL_TYPES:
            t = "string"
        out.append({"name": name, "type": t, "required": bool(f.get("required")),
                    "description": f.get("description", "")})
    return out


def save_target(name: str, fields: list[dict[str, Any]], origin: str = "manual",
                target_id: int | None = None) -> TargetSchema:
    name = (name or "").strip()
    if not name or len(name) > 160:
        raise ValueError("The target name must be 1 to 160 characters")
    with session_scope() as s:
        t = s.get(TargetSchema, target_id) if target_id else TargetSchema(name=name, origin=origin)
        t.name, t.fields = name, _clean_fields(fields)
        s.add(t)
        s.flush()
        return t


def target_from_template(name: str, filename: str, content: bytes) -> TargetSchema:
    """Header row of a template workbook becomes the field list; types come from any sample rows."""
    result = parse_file(content, filename)
    fields = [{"name": f["name"], "type": f["type"] if f.get("distinct") else "string", "required": False}
              for f in result.fields]
    return save_target(name, fields, origin="template")


def delete_target(target_id: int) -> None:
    with session_scope() as s:
        t = s.get(TargetSchema, target_id)
        if t:
            s.delete(t)


def field_name(column: str) -> str:
    """customer.name -> customer_name, "Order Date" -> order_date (a clean API field name)."""
    import re

    name = re.sub(r"[^0-9a-zA-Z]+", "_", column).strip("_").lower()
    return name if name and not name[0].isdigit() else f"f_{name}"


def target_from_source(name: str, source_id: int, columns: list[str] | None = None) -> TargetSchema:
    """A target with one field per source column (or the chosen ones), same types: a quick start for mapping."""
    from databridge.core.types import CANONICAL_TYPES, base_type
    from databridge.services.sources import get_source

    src = get_source(source_id)
    fields, seen = [], set()
    for f in src.fields:
        if columns and f["name"] not in columns:
            continue
        fname = field_name(f["name"])
        while fname in seen:
            fname += "_2"
        seen.add(fname)
        ftype = f.get("type") or "string"
        fields.append({"name": fname, "type": ftype if base_type(ftype) in CANONICAL_TYPES else "string"})
    if not fields:
        raise ValueError("The source has no columns yet; refresh it first")
    return save_target(name, fields, origin=f"source:{src.name}")
