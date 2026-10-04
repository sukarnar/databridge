"""Built-in schedule: refreshes connection-backed sources (REST APIs, tables, SQL, files) every N minutes.

Runs in a background thread of the (single) app process. Each due source is refreshed like a click on Refresh:
a new snapshot when the data changed (unchanged data is skipped), then published mappings republish. Failures
are recorded on the source (Sources page) and in the run history; the next attempt waits a full interval, so a
broken API isn't hammered. External schedulers (Stonebranch, n8n, cron) can keep using
POST /api/v1/sources/{id}/refresh instead.
"""

import logging
import threading
from datetime import datetime, timezone

from databridge.services import sources as src_svc

log = logging.getLogger("databridge.scheduler")
TICK_SECONDS = 30
_stop = threading.Event()
_thread: threading.Thread | None = None


def run_due(now: datetime | None = None) -> list[tuple[str, str]]:
    """Refreshes every due source once; returns (source name, outcome) pairs."""
    done = []
    for src in src_svc.due_sources(now):
        try:
            out = src_svc.refresh(src.id)
            outcome = "unchanged" if out.get("skipped") else f"{out['rows']:,} rows"
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:  # noqa: BLE001 - recorded on the source (native panics too); keep going
            outcome = f"failed: {e}"
            log.warning("Scheduled refresh of %s failed: %s", src.name, e)
        done.append((src.name, outcome))
    return done


def _loop() -> None:
    while not _stop.wait(TICK_SECONDS):
        try:
            run_due(datetime.now(timezone.utc))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001 - never let the scheduler die, not even on a native panic
            log.exception("scheduler tick failed")


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="source-scheduler", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
