"""ASGI entry point: broker API at /api/v1, streaming API at /api/v1/stream (wss), Flet studio at /.

Run:  python -m databridge.serve        (applies TLS, proxy trust and websocket limits; see databridge/serve.py)
"""

import logging

import flet.fastapi as flet_fastapi

from databridge import __version__
from databridge.api.runtime import health_router, router
from databridge.api.stream import router as stream_router
from databridge.config import settings
from databridge.core.db import init_db
from databridge.services import users
from databridge.ui import uploads as studio_uploads
from databridge.ui import web_assets
from databridge.ui.app import main as studio_main
from databridge.web_security import TransportSecurity

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

init_db()
users.bootstrap_admin()
users.purge_expired_sessions()

if settings.ai_config_file:  # company LLM configuration kept in a file (see databridge/services/ai_config.py)
    from databridge.services import ai_config

    try:
        for line in ai_config.apply_file(settings.ai_config_file):
            logging.getLogger("databridge").info("AI config: %s", line)
    except ai_config.ConfigError as e:
        logging.getLogger("databridge").error("AI config file not applied: %s", e)

from databridge.services import ai_health  # noqa: E402

ai_health.start()

from databridge.services import scheduler  # noqa: E402

if settings.source_scheduler:
    scheduler.start()  # refreshes sources that have a schedule (Sources > Schedule)

from databridge.services import security_check  # noqa: E402

security_check.log_findings()

app = flet_fastapi.FastAPI(
    title="DataBridge Broker API",
    version=__version__,
    description="Query published datasets, ingest files and trigger refreshes. "
    "Send your key in the X-API-Key header.",
    docs_url="/api/docs",
    openapi_url="/api/v1/openapi.json",
    redoc_url=None,
)
app.include_router(router, prefix="/api/v1", tags=["broker"])
app.include_router(stream_router, prefix="/api/v1")
app.include_router(health_router)

# The studio is mounted last so API routes take precedence.
app.mount(
    "/",
    flet_fastapi.app(
        studio_main,
        app_name="DataBridge",
        app_short_name="DataBridge",
        app_description="Map source data to targets and serve it over REST",
        assets_dir=str(web_assets.prepare(settings.data_dir / "web")),  # DataBridge logo instead of Flet's
        max_upload_size=settings.max_upload_mb * 1024 * 1024,
        # Files are uploaded over HTTPS with signed, expiring URLs (not over the websocket): databridge/ui/uploads.py
        upload_dir=studio_uploads.prepare(),
        secret_key=studio_uploads.SECRET,
        no_cdn=settings.no_cdn,
    ),
)

# Outermost layer: HTTPS enforcement, websocket origin checks and limits, security headers.
app.add_middleware(TransportSecurity)
