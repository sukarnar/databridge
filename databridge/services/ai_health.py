"""Health checks for company model endpoints, with alerts.

Every DATABRIDGE_AI_HEALTH_MINUTES (default 15; 0 = off) each enabled endpoint is probed: models list (with the
company or discovery key), latency, and certificate expiry (server certificate, and the client certificate for
mutual TLS). Results are stored on the endpoint (last check + recent history) and shown in AI > Models.

Alerts go to the audit log and, if DATABRIDGE_AI_ALERT_WEBHOOK is set, to a webhook as {"text": ...} (Slack and
Microsoft Teams incoming webhooks accept this). An alert fires when an endpoint changes state (ok -> failed,
failed -> ok) and when a certificate is within DATABRIDGE_AI_CERT_WARN_DAYS of expiry (once a day).
"""

import logging
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from cryptography import x509

from databridge.ai import tls
from databridge.config import settings
from databridge.core.db import session_scope
from databridge.core.models import ModelEndpoint
from databridge.services import llm
from databridge.services.users import audit

log = logging.getLogger("databridge.ai_health")
HISTORY = 48
SLOW_MS = 5000
_thread: threading.Thread | None = None
_stop = threading.Event()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def server_cert_days(ep: ModelEndpoint) -> int | None:
    """Days until the endpoint's server certificate expires (None for http or when it can't be read)."""
    u = urlparse(ep.base_url)
    if u.scheme != "https" or not u.hostname:
        return None
    ctx = llm.tls_context_for(ep)
    if ctx is True:
        ctx = ssl.create_default_context()
    try:
        with socket.create_connection((u.hostname, u.port or 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=u.hostname) as ssock:
                der = ssock.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError, llm.AIError):
        return None
    if not der:
        return None
    cert = x509.load_der_x509_certificate(der)
    return (cert.not_valid_after_utc - _now()).days


def check(ep: ModelEndpoint) -> dict:
    """Probes one endpoint. status: ok | warning | failed."""
    started = time.perf_counter()
    status, message, models = llm.probe_endpoint(ep)
    latency = int((time.perf_counter() - started) * 1000)
    result = {"checked_at": _now().isoformat(timespec="seconds"), "latency_ms": latency, "message": message,
              "models": len(models) if models is not None else None}
    certs = []
    if status != "failed":
        days = server_cert_days(ep)
        if days is not None:
            certs.append(("server certificate", days))
    for c in tls.describe(ep.client_cert_pem)[:1]:
        certs.append(("client certificate", c.days_left))
    for c in tls.describe(ep.ca_pem)[:1]:
        certs.append(("CA certificate", c.days_left))
    if certs:
        name, days = min(certs, key=lambda c: c[1])
        result["cert_days"], result["cert"] = days, name
    warnings = []
    if status == "reachable":
        warnings.append("reachable, but models can't be listed without a key")
    if latency > SLOW_MS and status != "failed":
        warnings.append(f"slow ({latency / 1000:.1f} s)")
    if result.get("cert_days") is not None and result["cert_days"] <= settings.ai_cert_warn_days:
        warnings.append(f"{result['cert']} expires in {result['cert_days']} days" if result["cert_days"] >= 0
                        else f"{result['cert']} has expired")
    result["status"] = "failed" if status == "failed" else ("warning" if warnings else "ok")
    if warnings and status != "failed":
        result["message"] = "; ".join(warnings)
    return result


def _alert(text: str) -> None:
    audit("health-check", "ai.endpoint.alert", "", text[:500])
    log.warning(text)
    if settings.ai_alert_webhook:
        try:
            httpx.post(settings.ai_alert_webhook, json={"text": f"DataBridge: {text}"}, timeout=10)
        except httpx.HTTPError as e:
            log.error("Alert webhook failed: %s", e)


def run_checks(endpoint_id: int | None = None) -> list[tuple[str, dict]]:
    """Checks enabled endpoints (or one), stores results and sends alerts. Returns (name, result) pairs."""
    out = []
    for ep in llm.list_endpoints(include_disabled=False):
        if endpoint_id and ep.id != endpoint_id:
            continue
        try:
            result = check(ep)
        except Exception as e:  # noqa: BLE001 - a check must never crash the loop
            result = {"checked_at": _now().isoformat(timespec="seconds"), "status": "failed",
                      "message": f"{type(e).__name__}: {e}", "latency_ms": 0}
        previous = (ep.health or {}).get("status")
        with session_scope() as s:
            row = s.get(ModelEndpoint, ep.id)
            history = list(row.health_history or [])[-(HISTORY - 1):]
            history.append({"at": result["checked_at"], "status": result["status"], "ms": result["latency_ms"]})
            last_cert_alert = (row.health or {}).get("cert_alerted_on")
            row.health = {**result, "cert_alerted_on": last_cert_alert}
            row.health_history = history
            row.last_test_ok = result["status"] != "failed"
            row.last_test_message = result["message"]
        if previous and previous != result["status"] and "failed" in (previous, result["status"]):
            _alert(f"{ep.name} is {'DOWN' if result['status'] == 'failed' else 'back up'}: {result['message']}")
        days = result.get("cert_days")
        today = _now().date().isoformat()
        if days is not None and days <= settings.ai_cert_warn_days and last_cert_alert != today:
            _alert(f"{ep.name}: {result['cert']} " + (f"expires in {days} days" if days >= 0 else "has expired"))
            with session_scope() as s:
                row = s.get(ModelEndpoint, ep.id)
                row.health = {**(row.health or {}), "cert_alerted_on": today}
        out.append((ep.name, result))
    return out


def problems() -> list[str]:
    """Current endpoint problems for banners (failed endpoints, expiring certificates)."""
    issues = []
    for ep in llm.list_endpoints(include_disabled=False):
        h = ep.health or {}
        if h.get("status") == "failed":
            issues.append(f"{ep.name} is failing: {h.get('message', '')}")
        elif h.get("cert_days") is not None and h["cert_days"] <= settings.ai_cert_warn_days:
            issues.append(f"{ep.name}: {h.get('cert')} expires in {h['cert_days']} days")
    return issues


def _loop() -> None:
    _stop.wait(20)  # let the app finish starting
    while not _stop.is_set():
        try:
            run_checks()
        except Exception:  # noqa: BLE001
            log.exception("AI health check round failed")
        _stop.wait(max(1, settings.ai_health_minutes) * 60)


def start() -> None:
    """Starts the background checker once per process (the app runs one worker)."""
    global _thread
    if settings.ai_health_minutes <= 0 or not settings.ai_enabled or (_thread and _thread.is_alive()):
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="ai-health", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
