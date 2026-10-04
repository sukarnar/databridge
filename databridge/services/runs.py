"""Run records for auditing ingests, extracts and publishes."""

from contextlib import contextmanager
from collections.abc import Iterator

from sqlalchemy import select

from databridge.core.db import session_scope
from databridge.core.models import Run, utcnow


@contextmanager
def track(kind: str, subject: str) -> Iterator[Run]:
    """Creates a Run, yields it for the caller to fill counters, then saves status."""
    with session_scope() as s:
        run = Run(kind=kind, subject=subject[:200], status="running")  # column limit (PostgreSQL enforces it)
        s.add(run)
        s.flush()
    _emit("run.started", run)
    try:
        yield run
        if run.status == "running":
            run.status = "warning" if run.rows_rejected else "ok"
    except Exception as e:
        run.status, run.message = "failed", f"{type(e).__name__}: {e}"
        raise
    finally:
        run.finished_at = utcnow()
        with session_scope() as s:
            s.merge(run)
        _emit("run.finished", run)


def _emit(event: str, run: Run) -> None:
    from databridge.services.events import publish_safely

    publish_safely("runs", event, {c: getattr(run, c) for c in (
        "id", "kind", "subject", "status", "started_at", "finished_at", "rows_in", "rows_out", "rows_rejected",
        "message")})


def recent_runs(limit: int = 50) -> list[Run]:
    with session_scope() as s:
        return list(s.scalars(select(Run).order_by(Run.id.desc()).limit(limit)))
