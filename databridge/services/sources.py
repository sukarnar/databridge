"""Source objects and snapshots: uploads, files on shares, database tables, SQL queries and REST APIs."""

import array
import hashlib
import shutil
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import select

from databridge.config import settings
from databridge.core.db import session_scope
from databridge.core.models import Mapping, Snapshot, SourceObject
from databridge.ingest.profiling import detect_drift, profile_fields
from databridge.ingest.sheet_profile import SheetProfile, parse_file
from databridge.services import connections as conn_svc
from databridge.services.runs import track


def list_sources() -> list[SourceObject]:
    with session_scope() as s:
        return list(s.scalars(select(SourceObject).order_by(SourceObject.name)))


def get_source(source_id: int) -> SourceObject:
    with session_scope() as s:
        src = s.get(SourceObject, source_id)
        if not src:
            raise LookupError(f"Source {source_id} not found")
        return src


def delete_source(source_id: int) -> None:
    """Removes the source with its snapshots (rows and files). Refused while a mapping still reads it."""
    with session_scope() as s:
        src = s.get(SourceObject, source_id)
        if not src:
            return
        users = list(s.scalars(select(Mapping.name).where(Mapping.source_id == source_id)))
        if users:
            raise ValueError(f"\u201c{src.name}\u201d is used by the mapping(s) {', '.join(users)}. "
                             "Delete those mappings first (or point them at another source).")
        for snap in s.scalars(select(Snapshot).where(Snapshot.source_id == source_id)):
            s.delete(snap)
        s.flush()
        s.delete(src)
    shutil.rmtree(settings.data_dir / "snapshots" / str(source_id), ignore_errors=True)


def latest_snapshot(source_id: int) -> Snapshot | None:
    with session_scope() as s:
        return s.scalars(
            select(Snapshot).where(Snapshot.source_id == source_id).order_by(Snapshot.id.desc()).limit(1)
        ).first()


def list_snapshots(source_id: int) -> list[Snapshot]:
    with session_scope() as s:
        return list(s.scalars(select(Snapshot).where(Snapshot.source_id == source_id).order_by(Snapshot.id.desc())))


def load_snapshot(source_id: int, limit: int | None = None) -> pl.DataFrame:
    snap = latest_snapshot(source_id)
    if not snap:
        raise LookupError("Source has no data yet")
    lf = pl.scan_parquet(snap.parquet_path)
    return (lf.head(limit) if limit else lf).collect()


def _write_snapshot(source: SourceObject, df: pl.DataFrame, fields: list[dict[str, Any]],
                    file_name: str | None, file_hash: str | None, notes: list[str]) -> Snapshot:
    folder = settings.data_dir / "snapshots" / str(source.id)
    folder.mkdir(parents=True, exist_ok=True)
    with session_scope() as s:
        src = s.get(SourceObject, source.id)
        drift = detect_drift(src.fields, fields) if src.fields else None
        snap = Snapshot(source_id=src.id, file_name=file_name, file_hash=file_hash,
                        row_count=df.height, parquet_path="", notes=notes, drift=drift)
        s.add(snap)
        s.flush()
        path = folder / f"{snap.id}.parquet"
        df.write_parquet(path)
        snap.parquet_path = str(path.resolve())
        src.fields = fields
        src.latest_snapshot_id = snap.id
        return snap


def _find_by_hash(source_id: int, file_hash: str) -> Snapshot | None:
    with session_scope() as s:
        return s.scalars(select(Snapshot).where(Snapshot.source_id == source_id,
                                                Snapshot.file_hash == file_hash)).first()


# ------------------------------------------------------------------ uploads / files


def create_upload_source(name: str, filename: str, content: bytes, profile: SheetProfile) -> SourceObject:
    """First upload: saves the Sheet Profile with the source and ingests the file."""
    from sqlalchemy.exc import IntegrityError

    name = check_new_name(name)
    try:
        with session_scope() as s:
            src = SourceObject(name=name, kind="upload", object_ref={"file_name": filename},
                               sheet_profile=profile.to_dict(), fields=[])
            s.add(src)
            s.flush()
    except IntegrityError:  # created by someone else a moment ago
        raise ValueError(f"A source named \u201c{name}\u201d already exists. Choose another name.") from None
    try:
        ingest_file(src.id, filename, content, auto_publish=False)
    except BaseException:  # nothing half-created: fix the file or profile and try again
        delete_source(src.id)
        raise
    return get_source(src.id)


def ingest_file(source_id: int, filename: str, content: bytes, auto_publish: bool = True) -> dict[str, Any]:
    """Parses a new file with the source's saved profile, stores a snapshot and republishes mappings.

    Returns {snapshot_id, rows, drift, skipped, published: [...]}.
    """
    src = get_source(source_id)
    file_hash = hashlib.sha256(content).hexdigest()
    existing = _find_by_hash(source_id, file_hash)
    if existing:
        return {"snapshot_id": existing.id, "rows": existing.row_count, "skipped": True,
                "message": "Identical file already ingested", "drift": None, "published": []}
    upload_dir = settings.data_dir / "uploads" / str(source_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / f"{file_hash[:12]}_{Path(filename).name}").write_bytes(content)

    with track("ingest", f"{src.name} <- {filename}") as run:
        result = parse_file(content, filename, SheetProfile.from_dict(src.sheet_profile))
        snap = _write_snapshot(src, result.df, result.fields, filename, file_hash, result.notes)
        run.rows_in = run.rows_out = result.df.height
        drift = snap.drift or {}
        if drift.get("changed"):
            run.status = "warning"
            run.message = _drift_text(drift)
    published = _auto_publish(source_id, drift) if auto_publish else []
    return {"snapshot_id": snap.id, "rows": snap.row_count, "skipped": False, "drift": snap.drift,
            "notes": result.notes, "published": published}


def _drift_text(drift: dict[str, Any]) -> str:
    parts = []
    if drift.get("added"):
        parts.append("added " + ", ".join(drift["added"]))
    if drift.get("removed"):
        parts.append("removed " + ", ".join(drift["removed"]))
    if drift.get("renamed"):
        parts.append("renamed " + ", ".join(f"{r['from']}->{r['to']}" for r in drift["renamed"]))
    if drift.get("retyped"):
        parts.append("retyped " + ", ".join(f"{r['name']} {r['from']}->{r['to']}" for r in drift["retyped"]))
    return "Schema drift: " + "; ".join(parts)


def _auto_publish(source_id: int, drift: dict[str, Any]) -> list[dict[str, Any]]:
    from databridge.services import mappings as map_svc

    out = []
    for m in map_svc.mappings_for_source(source_id):
        if not (m.status == "published" and m.auto_publish):
            continue
        missing = [c for c in map_svc.referenced_source_columns(m) if c in set(drift.get("removed") or [])]
        if missing:
            out.append({"mapping": m.name, "status": "paused", "reason": f"mapped columns missing: {missing}"})
            continue
        ds = map_svc.publish(m.id, use_published_spec=True)
        out.append({"mapping": m.name, "status": "published", "version": ds.version, "rows": ds.row_count,
                    "rejected": ds.rejected_count, "run_id": ds.run_id})
    return out


def update_profile(source_id: int, profile: SheetProfile) -> None:
    with session_scope() as s:
        src = s.get(SourceObject, source_id)
        src.sheet_profile = profile.to_dict()


# ------------------------------------------------------------------ connector-backed sources


def create_connector_source(name: str, connection_id: int, ref: dict[str, Any],
                            profile: SheetProfile | None = None, refresh_minutes: int | None = None,
                            progress=None) -> SourceObject:
    """A table, view, SQL query, or a file on a connected file system."""
    name = check_new_name(name)
    conn = conn_svc.get_connection(connection_id)
    if conn.type == "database":
        kind = "sql" if ref.get("sql") else "table"
    elif conn.type == "rest":
        kind = "rest"
        ref = clean_request(ref)
    else:
        kind = "file"
    with session_scope() as s:
        if s.scalars(select(SourceObject).where(SourceObject.name == name)).first():
            raise ValueError(f"A source named \u201c{name}\u201d already exists. Choose another name.")
        src = SourceObject(name=name, kind=kind, connection_id=connection_id, object_ref=ref,
                           sheet_profile=profile.to_dict() if profile else None, fields=[],
                           refresh_minutes=None)  # scheduled only once the first snapshot exists (no race with it)
        s.add(src)
        s.flush()
    minutes = _minutes(refresh_minutes)
    cancel = getattr(progress, "cancel", None)
    try:
        refresh(src.id, auto_publish=False, progress=progress)
        if cancel is not None and cancel.is_set():  # cancelled after the last page: still add nothing
            raise ValueError("Cancelled")
    except BaseException:  # also native-library panics (not Exceptions): never leave a half-made source
        delete_source(src.id)  # nothing half-created: the user can fix the request and try again
        raise
    if minutes:
        with session_scope() as s:
            s.get(SourceObject, src.id).refresh_minutes = minutes
    return get_source(src.id)


_refresh_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _source_lock(source_id: int) -> threading.Lock:
    with _locks_guard:
        return _refresh_locks.setdefault(source_id, threading.Lock())


NAME_MAX = 160  # SourceObject.name


def check_new_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the source a name")
    if len(name) > NAME_MAX:
        raise ValueError(f"The source name is {len(name)} characters; use at most {NAME_MAX}")
    with session_scope() as s:
        if s.scalars(select(SourceObject).where(SourceObject.name == name)).first():
            raise ValueError(f"A source named \u201c{name}\u201d already exists. Choose another name.")
    return name


def refresh(source_id: int, auto_publish: bool = True, progress=None) -> dict[str, Any]:
    """Pulls fresh data from the source's connection into a new snapshot (skipped when nothing changed).

    One refresh per source at a time: a scheduled refresh and a click on Refresh never overlap.
    """
    lock = _source_lock(source_id)
    if not lock.acquire(timeout=0.1):
        raise ValueError("This source is already being refreshed; try again when it finishes")
    started = datetime.now(timezone.utc)
    try:
        result = _refresh(source_id, auto_publish, progress)
    except BaseException as e:  # also native-library panics, so the Sources page shows the failure
        _record_refresh(source_id, started, "failed", str(e)[:1000] or type(e).__name__)
        raise
    else:
        status = "unchanged" if result.get("skipped") else "ok"
        _record_refresh(source_id, started, status, result.get("message") or f"{result['rows']:,} rows")
        return result
    finally:
        lock.release()


def _record_refresh(source_id: int, started: datetime, status: str, message: str) -> None:
    """last_refresh_at = when it finished (the schedule counts from there, so a slow refresh can't run back to
    back); last_refresh_ok_at = when a successful one started ({{last_refresh}}: no gap for incremental loads)."""
    with session_scope() as s:
        src = s.get(SourceObject, source_id)
        if src:
            src.last_refresh_at = datetime.now(timezone.utc)
            src.last_refresh_status, src.last_refresh_message = status, message
            if status != "failed":
                src.last_refresh_ok_at = started


def _uint64_bytes(hashes: pl.Series) -> bytes | memoryview:
    """The row hashes as raw 8-byte values (same bytes numpy's tobytes() gave, so old fingerprints still match),
    without needing numpy: Arrow's buffer directly, or a plain array as a fallback."""
    try:
        arr = hashes.to_arrow()
        if arr.null_count == 0 and sys.byteorder == "little":
            return memoryview(arr.buffers()[1])[arr.offset * 8:(arr.offset + len(arr)) * 8]
    except Exception:  # noqa: BLE001 - pyarrow missing or an unusual layout: use the portable way
        pass
    return array.array("Q", hashes.to_list()).tobytes()


def frame_hash(df: pl.DataFrame) -> str:
    """Fingerprint of the rows and columns, to skip snapshots of unchanged data (scheduled polling)."""
    h = hashlib.sha256(repr([(c, str(t)) for c, t in df.schema.items()]).encode())
    if df.height:
        h.update(_uint64_bytes(df.hash_rows(seed=0, seed_1=1, seed_2=2, seed_3=3)))
    return h.hexdigest()  # 64 characters: fits Snapshot.file_hash (PostgreSQL enforces the length)


def _refresh(source_id: int, auto_publish: bool, progress=None) -> dict[str, Any]:
    src = get_source(source_id)
    if src.kind == "upload" or not src.connection_id:
        raise ValueError("Uploaded sources refresh by uploading a new file (POST /api/v1/ingest/{source_id})")
    connector = conn_svc.connector_for(src.connection_id)
    try:
        if src.kind == "file":
            content = connector.read_bytes(src.object_ref)  # type: ignore[attr-defined]
            profile = SheetProfile.from_dict(src.sheet_profile) if src.sheet_profile else None
            if profile is None and src.object_ref.get("sheet"):
                profile = SheetProfile(sheet=src.object_ref["sheet"])
                from databridge.ingest.sheet_profile import suggest_profile

                profile = suggest_profile(content, src.object_ref["path"], src.object_ref["sheet"])
                update_profile(source_id, profile)
            connector.close()
            return ingest_file(source_id, src.object_ref["path"].split("/")[-1], content, auto_publish)

        if progress is not None and hasattr(connector, "progress"):
            connector.progress = progress  # (pages, rows) while reading, e.g. for the Add-as-source dialog
            # optional extras on the same object: .notice(text) (connecting, retrying) and .cancel (an Event)
            connector.notice = getattr(progress, "notice", None)
            connector.cancel = getattr(progress, "cancel", None)
        ref = dict(src.object_ref)
        if src.kind == "rest":
            ref["_last_refresh"] = _aware(src.last_refresh_ok_at)  # for {{last_refresh}} in the request
        timing = ""
        with track("extract", src.name) as run:
            frames = list(connector.read(ref))
            stats = getattr(connector, "stats", None)
            timing = stats.summary() if src.kind == "rest" and stats is not None else ""
            if timing:
                run.message = timing
            df = pl.concat(frames, how="vertical_relaxed") if frames else pl.DataFrame()
            run.rows_in = df.height
            latest = latest_snapshot(source_id)
            notes = []
            if src.kind == "rest":
                incremental = (src.object_ref.get("incremental") or {})
                if df.height == 0 and latest is not None:
                    # Nothing new (typical for {{last_refresh}} requests): keep the data and the columns we have
                    run.rows_out = 0
                    run.message = "The API returned no rows; the previous snapshot is kept"
                    return {"snapshot_id": latest.id, "rows": latest.row_count, "skipped": True, "drift": None,
                            "published": [], "message": "no new rows", "timing": timing}
                if incremental.get("mode") == "append" and latest is not None:
                    df, note = _append(latest, df, incremental.get("key") or [])
                    notes.append(note)
                truncated = getattr(connector, "truncated", "")
                if truncated:
                    notes.append(f"Incomplete: {truncated} (raise Max rows / Max pages in the request)")
            digest = frame_hash(df)
            if latest is not None and latest.file_hash == digest:
                run.rows_out = 0
                run.message = "No changes since the last snapshot; nothing republished" + (
                    f". {timing}" if timing else "")
                return {"snapshot_id": latest.id, "rows": latest.row_count, "skipped": True, "drift": None,
                        "published": [], "message": "unchanged" + (f". {timing}" if timing else ""),
                        "timing": timing}
            fields = profile_fields(df)
            snap = _write_snapshot(src, df, fields, None, digest, notes)
            run.rows_out = df.height
            if snap.drift and snap.drift.get("changed"):
                run.status, run.message = "warning", _drift_text(snap.drift)
            elif any(n.startswith("Incomplete") for n in notes):
                run.status, run.message = "warning", "; ".join(notes + ([timing] if timing else []))
    finally:
        connector.close()
    published = _auto_publish(source_id, snap.drift or {}) if auto_publish else []
    out = {"snapshot_id": snap.id, "rows": snap.row_count, "skipped": False, "drift": snap.drift,
           "published": published}
    if timing:
        out["timing"] = timing
        out["message"] = f"{snap.row_count:,} rows. {timing}"
    return out


def _append(latest, new: pl.DataFrame, key: list[str]) -> tuple[pl.DataFrame, str]:
    """Incremental REST loads: previous rows + new rows; with key columns, a new row replaces the old one."""
    old = pl.read_parquet(latest.parquet_path)
    combined = pl.concat([old, new], how="diagonal_relaxed")
    if key:
        missing = [k for k in key if k not in combined.columns]
        if missing:
            raise ValueError(f"Key column(s) not in the data: {', '.join(missing)}")
        combined = combined.unique(subset=key, keep="last", maintain_order=True)
    return combined, f"Appended {new.height:,} new rows to {old.height:,}" + (
        f" (matched on {', '.join(key)})" if key else "")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ------------------------------------------------------------------ REST requests and schedules

REQUEST_KEYS = {"method", "path", "params", "headers", "body", "format", "records_path", "explode", "pagination",
                "columns", "max_rows", "max_pages", "incremental"}


def clean_request(ref: dict[str, Any]) -> dict[str, Any]:
    """Only known request keys, validated (no internal values such as _last_refresh are stored)."""
    from databridge.connectors.rest import FORMATS, PAGINATION, RestError

    out = {k: v for k, v in ref.items() if k in REQUEST_KEYS and v not in (None, "", [], {})}
    out["method"] = (out.get("method") or "GET").upper()
    if out["method"] not in ("GET", "POST"):
        raise RestError("Method must be GET or POST")
    if out.get("format", "auto") not in FORMATS:
        raise RestError(f"Format must be one of {', '.join(FORMATS)}")
    pg = out.get("pagination") or {}
    if pg and pg.get("type", "none") not in PAGINATION:
        raise RestError("Unknown pagination type")
    for key in ("max_rows", "max_pages"):
        if key in out:
            out[key] = max(1, int(out[key]))
    inc = out.get("incremental") or {}
    if inc:
        if inc.get("mode") not in ("replace", "append"):
            raise RestError("Incremental mode must be replace or append")
        if inc["mode"] == "replace":
            out.pop("incremental")
        else:
            out["incremental"] = {"mode": "append", "key": [str(k) for k in inc.get("key") or [] if str(k).strip()]}
    return out


def update_request(source_id: int, ref: dict[str, Any], actor: str = "") -> dict[str, Any]:
    """Changes what a REST source fetches, then refreshes it (mappings republish if the data changed)."""
    src = get_source(source_id)
    if src.kind != "rest":
        raise ValueError("Only REST sources have a request to edit")
    clean = clean_request(ref)
    # Try the new request first: a bad edit must not replace a working one
    connector = conn_svc.connector_for(src.connection_id)
    try:
        connector.preview(clean, 20)
    finally:
        connector.close()
    with session_scope() as s:
        row = s.get(SourceObject, source_id)
        row.object_ref = clean
        row.last_refresh_ok_at = None  # a changed request starts with a full load ({{last_refresh}} empty)
    if actor:
        from databridge.services.users import audit

        audit(actor, "source.request", src.name, f"{clean.get('method')} {clean.get('path', '')}")
    return refresh(source_id)


def _minutes(value) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    minutes = int(value)
    if minutes < 5:
        raise ValueError("Refresh at most every 5 minutes")
    return minutes


def set_schedule(source_id: int, minutes: int | None, actor: str = "") -> None:
    src = get_source(source_id)
    if not src.connection_id:
        raise ValueError("Only sources that read from a connection can refresh on a schedule")
    with session_scope() as s:
        s.get(SourceObject, source_id).refresh_minutes = _minutes(minutes)
    if actor:
        from databridge.services.users import audit

        audit(actor, "source.schedule", src.name, f"every {minutes} min" if minutes else "off")


def next_refresh(src: SourceObject) -> datetime | None:
    if not src.refresh_minutes or not src.connection_id:
        return None
    from datetime import timedelta

    last = _aware(src.last_refresh_at)
    return (last + timedelta(minutes=src.refresh_minutes)) if last else datetime.now(timezone.utc)


def due_sources(now: datetime | None = None) -> list[SourceObject]:
    now = now or datetime.now(timezone.utc)
    return [s for s in list_sources() if (nxt := next_refresh(s)) is not None and nxt <= now]


# ------------------------------------------------------------------ data classification (AI egress rules)


def set_classification(source_id: int, classification: dict[str, str], actor: str) -> None:
    """Saves {column: public | internal | pii | confidential}; "internal" (the default) is not stored."""
    from databridge.ai.guardrails import LEVELS
    from databridge.services.users import audit

    clean = {c: lvl for c, lvl in classification.items() if lvl in LEVELS and lvl != "internal"}
    with session_scope() as s:
        src = s.get(SourceObject, source_id)
        if not src:
            raise LookupError(f"Source {source_id} not found")
        src.classification = clean or None
        name = src.name
    audit(actor, "source.classify", name, ", ".join(f"{c}={lvl}" for c, lvl in sorted(clean.items())) or "cleared")
