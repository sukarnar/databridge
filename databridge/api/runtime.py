"""Broker REST API (/api/v1): what consumers and schedulers call."""

import io
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

from databridge.config import settings
from databridge.core.db import session_scope
from databridge.core.models import Dataset, Endpoint, Run
from databridge.services import endpoints as ep_svc
from databridge.services import mappings as map_svc
from databridge.services import sources as src_svc
from databridge.services import targets as tgt_svc

router = APIRouter()
RESERVED = {"page", "page_size", "format", "version"}


def _endpoint_or_404(slug: str) -> Endpoint:
    ep = ep_svc.get_endpoint_by_slug(slug)
    if not ep or not ep.active:
        raise HTTPException(404, f"Endpoint {slug!r} not found")
    return ep


def _check_admin(x_api_key: Optional[str]) -> None:
    """Admin routes (ingest, refresh, runs) require an active key with access to all endpoints ("*")."""
    fake = Endpoint(slug="__admin__", public=False)
    try:
        ep_svc.authorize(fake, x_api_key)
    except ep_svc.EndpointError as e:
        raise HTTPException(e.status, str(e)) from e


@router.get("/endpoints", summary="List published endpoints")
def list_endpoints() -> list[dict[str, Any]]:
    return [
        {"slug": e.slug, "name": e.name, "description": e.description, "public": e.public,
         "params": e.params, "formats": e.formats, "url": f"{settings.public_base_url}/api/v1/data/{e.slug}"}
        for e in ep_svc.list_endpoints() if e.active
    ]


@router.get("/data/{slug}", summary="Query an endpoint's dataset")
def get_data(
    slug: str,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: Optional[int] = Query(None, ge=1),
    format: str = Query("json", pattern="^(json|csv|xlsx)$"),
    version: Optional[int] = Query(None, ge=1),
    x_api_key: Optional[str] = Header(None),
):
    ep = _endpoint_or_404(slug)
    try:
        ep_svc.authorize(ep, x_api_key)
        if format not in ep.formats:
            raise ep_svc.EndpointError(400, f"Format {format} not enabled for this endpoint")
        args = {k: v for k, v in request.query_params.items() if k not in RESERVED}
        result = ep_svc.query(ep, args, page, page_size, version, all_rows=format != "json")
    except ep_svc.EndpointError as e:
        raise HTTPException(e.status, str(e)) from e

    df = result["df"]
    headers = {"X-Dataset-Version": str(result["version"]), "X-Total-Count": str(result["total"])}
    if format == "csv":
        return Response(df.write_csv(), media_type="text/csv", headers={
            **headers, "Content-Disposition": f'attachment; filename="{slug}.csv"'})
    if format == "xlsx":
        buf = io.BytesIO()
        df.write_excel(buf, worksheet=slug[:31], autofit=True)
        return Response(buf.getvalue(), headers={
            **headers, "Content-Disposition": f'attachment; filename="{slug}.xlsx"'},
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    total, size = result["total"], result["page_size"]
    return {
        "endpoint": slug,
        "version": result["version"],
        "published_at": result["published_at"].isoformat() if result["published_at"] else None,
        "page": result["page"],
        "page_size": size,
        "total": total,
        "pages": (total + size - 1) // size if size else 1,
        "data": ep_svc.to_jsonable(df),
    }


@router.get("/data/{slug}/schema", summary="Columns and types an endpoint returns")
def get_schema(slug: str, x_api_key: Optional[str] = Header(None)):
    ep = _endpoint_or_404(slug)
    try:
        ep_svc.authorize(ep, x_api_key)
    except ep_svc.EndpointError as e:
        raise HTTPException(e.status, str(e)) from e
    m = map_svc.get_mapping(ep.mapping_id)
    tgt = tgt_svc.get_target(m.target_id)
    return {"endpoint": slug, "fields": tgt.fields, "params": ep.params}


@router.get("/sources", summary="List sources with their ids (for ingest and refresh)")
def list_sources(name: Optional[str] = Query(None, description="Exact source name"),
                 x_api_key: Optional[str] = Header(None)) -> list[dict[str, Any]]:
    _check_admin(x_api_key)
    out = []
    for src in src_svc.list_sources():
        if name is not None and src.name != name:
            continue
        action = ({"upload": f"/api/v1/ingest/{src.id}", "workflow": None}.get(src.kind, f"/api/v1/sources/{src.id}/refresh"))
        out.append({"id": src.id, "name": src.name, "kind": src.kind, "latest_snapshot_id": src.latest_snapshot_id,
                    "load_with": action})
    return out


@router.post("/ingest/{source_id}", summary="Upload a new file for a source; map and publish")
async def ingest(source_id: int, file: UploadFile = File(...), x_api_key: Optional[str] = Header(None)):
    _check_admin(x_api_key)
    content = await file.read()
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB")
    try:
        return src_svc.ingest_file(source_id, file.filename or "upload.xlsx", content)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/sources/{source_id}/refresh", summary="Pull fresh data from a connection-backed source")
def refresh_source(source_id: int, x_api_key: Optional[str] = Header(None)):
    _check_admin(x_api_key)
    try:
        return src_svc.refresh(source_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/runs/{run_id}", summary="Status of an ingest, extract or publish run")
def get_run(run_id: int, x_api_key: Optional[str] = Header(None)):
    _check_admin(x_api_key)
    with session_scope() as s:
        run = s.get(Run, run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        return {c: getattr(run, c) for c in ("id", "kind", "subject", "status", "started_at", "finished_at",
                                              "rows_in", "rows_out", "rows_rejected", "message")}


@router.get("/runs/{run_id}/rejects", summary="Download the reject report (XLSX) for a publish run")
def get_rejects(run_id: int, x_api_key: Optional[str] = Header(None)):
    _check_admin(x_api_key)
    with session_scope() as s:
        from sqlalchemy import select

        ds = s.scalars(select(Dataset).where(Dataset.run_id == run_id)).first()
    if not ds or not ds.reject_path:
        raise HTTPException(404, "No rejected rows for this run")
    return FileResponse(ds.reject_path, filename=f"rejects_run_{run_id}.xlsx")


# ------------------------------------------------------------------ AI workflows


def _check_workflow_key(slug: str, x_api_key: Optional[str]) -> None:
    """A key may run a workflow if it covers all endpoints ("*") or lists "workflow:<slug>"."""
    try:
        ep_svc.authorize(Endpoint(slug=f"workflow:{slug}", public=False), x_api_key)
    except ep_svc.EndpointError as e:
        raise HTTPException(e.status, str(e)) from e


@router.post("/workflows/{slug}/run", summary="Run a published AI workflow")
def run_workflow(slug: str, body: dict[str, Any] | None = None, x_api_key: Optional[str] = Header(None)):
    """Body: {"input": {...}, "limit": 0, "confirm": false}. Runs the published version as the workflow owner.

    Returns the output rows (up to 500), rows flagged or rejected by guardrails, and token usage.
    """
    from databridge.services import workflows as wf_svc

    _check_workflow_key(slug, x_api_key)
    wf = wf_svc.get_by_slug(slug)
    if not wf or not wf.published_spec:
        raise HTTPException(404, f"No published workflow {slug!r}")
    body = body or {}
    try:
        run = wf_svc.run(wf.id, None, body.get("input") or {}, trigger="api", row_limit=int(body.get("limit") or 0),
                         confirm=bool(body.get("confirm")))
    except wf_svc.NeedsConfirmation as e:
        raise HTTPException(409, {"message": str(e), "tokens_expected": e.estimate["tokens_expected"]}) from e
    except wf_svc.WorkflowError as e:
        raise HTTPException(422, str(e)) from e
    result = wf_svc.api_result(run)
    if run.status in ("failed", "blocked"):
        raise HTTPException(422 if run.status == "blocked" else 500, result)
    return result


@router.get("/ai-runs/{run_id}", summary="Status and results of an AI workflow run")
def get_ai_run(run_id: int, x_api_key: Optional[str] = Header(None)):
    from databridge.services import workflows as wf_svc

    try:
        run, _ = wf_svc.get_run(run_id)
    except wf_svc.WorkflowError as e:
        raise HTTPException(404, str(e)) from e
    _check_workflow_key(wf_svc.get_workflow(run.workflow_id).slug, x_api_key)
    return wf_svc.api_result(run)


health_router = APIRouter()


# Folder name of the running code: the release id in a native deploy (releases/<id>/databridge).
RELEASE = Path(__file__).resolve().parents[2].name


@health_router.get("/health", include_in_schema=False)
def health():
    from databridge.services import security_check

    # One word only: details are for admins (Users > Security), not for anyone who can reach /health.
    return {"status": "ok", "release": RELEASE, "security": security_check.summary()}
