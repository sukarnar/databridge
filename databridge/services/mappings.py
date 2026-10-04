"""Mappings: create, edit, auto-map, preview and publish to versioned datasets."""

from typing import Any

import polars as pl
from sqlalchemy import func, select

from databridge.config import settings
from databridge.core.db import session_scope
from databridge.core.models import Dataset, Mapping
from databridge.engine.automap import Suggestion, suggest
from databridge.engine.formula import referenced_columns
from databridge.engine.mapper import MappingSpec, MapResult, run_mapping, source_columns_after_steps
from databridge.services import sources as src_svc
from databridge.services import targets as tgt_svc
from databridge.services.runs import track


def list_mappings() -> list[Mapping]:
    with session_scope() as s:
        return list(s.scalars(select(Mapping).order_by(Mapping.name)))


def get_mapping(mapping_id: int) -> Mapping:
    with session_scope() as s:
        m = s.get(Mapping, mapping_id)
        if not m:
            raise LookupError(f"Mapping {mapping_id} not found")
        return m


def mappings_for_source(source_id: int) -> list[Mapping]:
    with session_scope() as s:
        return list(s.scalars(select(Mapping).where(Mapping.source_id == source_id)))


def create_mapping(name: str, source_id: int, target_id: int, auto: bool = True) -> Mapping:
    name = (name or "").strip()
    if not name or len(name) > 160:
        raise ValueError("The mapping name must be 1 to 160 characters")
    src = src_svc.get_source(source_id)
    tgt = tgt_svc.get_target(target_id)
    rules = []
    if auto:
        rules = [{"target": s.target, "formula": f"[{s.source}]", "auto": True, "score": s.score}
                 for s in suggest(src.fields, tgt.fields)]
    with session_scope() as s:
        m = Mapping(name=name, source_id=source_id, target_id=target_id, rules=rules)
        s.add(m)
        s.flush()
        return m


def save_spec(mapping_id: int, spec: MappingSpec) -> Mapping:
    with session_scope() as s:
        m = s.get(Mapping, mapping_id)
        m.rules, m.row_steps, m.validations = spec.rules, spec.row_steps, spec.validations
        return m


def spec_of(m: Mapping, published: bool = False) -> MappingSpec:
    if published and m.published_spec:
        return MappingSpec.from_dict(m.published_spec)
    return MappingSpec(rules=m.rules, row_steps=m.row_steps, validations=m.validations)


def referenced_source_columns(m: Mapping) -> list[str]:
    cols: list[str] = []
    for r in spec_of(m, published=True).rules:
        for c in referenced_columns(r.get("formula", "")):
            if c not in cols:
                cols.append(c)
    return cols


def auto_map_suggestions(mapping_id: int) -> list[Suggestion]:
    m = get_mapping(mapping_id)
    src, tgt = src_svc.get_source(m.source_id), tgt_svc.get_target(m.target_id)
    cols = source_columns_after_steps([f["name"] for f in src.fields], m.row_steps)
    fields = [f for f in src.fields if f["name"] in cols] + [
        {"name": c, "type": "string"} for c in cols if c not in {f["name"] for f in src.fields}]
    mapped = {r["target"] for r in m.rules if r.get("formula")}
    return [s for s in suggest(fields, tgt.fields) if s.target not in mapped]


def preview(mapping_id: int, spec: MappingSpec | None = None, sample: int = 500) -> MapResult:
    m = get_mapping(mapping_id)
    tgt = tgt_svc.get_target(m.target_id)
    df = src_svc.load_snapshot(m.source_id, limit=sample)
    return run_mapping(df, spec or spec_of(m), tgt.fields)


def publish(mapping_id: int, use_published_spec: bool = False) -> Dataset:
    """Runs the mapping on the latest snapshot and writes a new dataset version."""
    m = get_mapping(mapping_id)
    tgt = tgt_svc.get_target(m.target_id)
    spec = spec_of(m, published=use_published_spec)
    with track("publish", m.name) as run:
        df = src_svc.load_snapshot(m.source_id)
        result = run_mapping(df, spec, tgt.fields)
        if result.rule_errors:
            raise ValueError("Fix formula errors before publishing: " +
                             "; ".join(f"{k}: {v}" for k, v in result.rule_errors.items()))
        with session_scope() as s:
            version = (s.scalar(select(func.max(Dataset.version)).where(Dataset.mapping_id == m.id)) or 0) + 1
        folder = settings.data_dir / "datasets" / str(m.id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"v{version}.parquet"
        result.valid.write_parquet(path)
        reject_path = None
        if result.rejects.height:
            rfolder = settings.data_dir / "rejects" / str(m.id)
            rfolder.mkdir(parents=True, exist_ok=True)
            reject_path = rfolder / f"v{version}.xlsx"
            result.rejects.write_excel(reject_path, worksheet="Rejected rows", autofit=True)
        run.rows_in, run.rows_out, run.rows_rejected = result.rows_in, result.valid.height, result.rejects.height
        with session_scope() as s:
            ds = Dataset(mapping_id=m.id, version=version, parquet_path=str(path.resolve()),
                         row_count=result.valid.height, rejected_count=result.rejects.height,
                         reject_path=str(reject_path.resolve()) if reject_path else None, run_id=run.id)
            s.add(ds)
            mm = s.get(Mapping, m.id)
            mm.status, mm.published_version = "published", version
            if not use_published_spec:
                mm.published_spec = spec.to_dict()
            s.flush()
    _announce(m, ds)
    return ds


def _announce(m: Mapping, ds: Dataset) -> None:
    """Tells streaming-API subscribers of every endpoint serving this mapping about the new version."""
    from databridge.core.models import Endpoint
    from databridge.services.events import publish_safely

    with session_scope() as s:
        endpoints = list(s.scalars(select(Endpoint).where(Endpoint.mapping_id == m.id, Endpoint.active.is_(True))))
    for ep in endpoints:
        publish_safely(f"endpoint:{ep.slug}", "dataset.published", {
            "endpoint": ep.slug, "mapping": m.name, "version": ds.version, "rows": ds.row_count,
            "rejected": ds.rejected_count, "run_id": ds.run_id, "published_at": ds.created_at,
            "served_version": ep.pinned_version or ds.version})


def list_datasets(mapping_id: int) -> list[Dataset]:
    with session_scope() as s:
        return list(s.scalars(select(Dataset).where(Dataset.mapping_id == mapping_id).order_by(Dataset.version.desc())))


def dataset_for(mapping_id: int, version: int | None = None) -> Dataset | None:
    with session_scope() as s:
        q = select(Dataset).where(Dataset.mapping_id == mapping_id)
        q = q.where(Dataset.version == version) if version else q.order_by(Dataset.version.desc())
        return s.scalars(q.limit(1)).first()


def delete_mapping(mapping_id: int) -> None:
    with session_scope() as s:
        m = s.get(Mapping, mapping_id)
        if m:
            s.delete(m)


def rejects_frame(dataset: Dataset) -> pl.DataFrame:
    if not dataset.reject_path:
        return pl.DataFrame()
    return pl.read_excel(dataset.reject_path)


def as_rows(df: pl.DataFrame, limit: int = 50) -> list[dict[str, Any]]:
    return df.head(limit).to_dicts()
