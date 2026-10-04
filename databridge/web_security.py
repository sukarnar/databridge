"""Transport security for the studio and the API: HTTPS enforcement, secure websockets and security headers.

A pure ASGI middleware in front of everything (API, streaming API and the Flet studio):

* HTTPS only (when `https_required`): plain-HTTP page requests are redirected to `public_base_url`, other plain
  requests get 400, and websockets that did not arrive over TLS (ws:// instead of wss://) are refused. /health
  stays reachable over plain HTTP for container and deploy health checks.
* Websocket origin check against cross-site websocket hijacking: a browser page may only open a websocket to
  DataBridge if it was served by DataBridge itself (same host) or its origin is listed in `allowed_origins`.
  The studio websocket requires an Origin header (browsers always send one); the streaming API also accepts
  clients without one (scripts, services), which authenticate with an API key.
* Limits: open websockets in total and per client address, and per-path message sizes.
* Security headers: HSTS on HTTPS, nosniff, no framing (clickjacking), no referrer, no camera/mic/location.
* Refused connections are counted, logged and written to the audit log (at most once a minute per address and
  reason, so an attack can't flood the log).

Behind a proxy the scheme and client address come from X-Forwarded-Proto / X-Forwarded-For, which uvicorn only
believes from `trusted_proxies` (see databridge/serve.py).
"""

import ipaddress
import logging
import queue
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from databridge.config import settings

log = logging.getLogger("databridge.security")

STUDIO_WS = "/ws"
STREAM_WS = "/api/v1/stream"
EXEMPT_HTTP = {"/health"}  # reachable over plain HTTP (health checks inside the container / on the host)
SECURE_SCHEMES = {"https", "wss"}
STREAM_MAX_MESSAGE = 64 * 1024
# Studio messages are clicks, text and screen updates; files go over HTTPS (databridge/ui/uploads.py).
STUDIO_MAX_MESSAGE = 4 * 1024 * 1024


def studio_max_message() -> int:
    return STUDIO_MAX_MESSAGE


def max_message(path: str) -> int:
    """Largest websocket message accepted on this path. Enforced while reading frames (see serve.py), so an
    oversized message is refused before it is held in memory; the middleware checks again as a backstop."""
    return STREAM_MAX_MESSAGE if path == STREAM_WS else STUDIO_MAX_MESSAGE


def path_bucket(path: str) -> str:
    """Statistics per known websocket, everything else counted together (clients choose paths)."""
    return path if path in (STUDIO_WS, STREAM_WS) else "other"


def address_bucket(ip: str) -> str:
    """Per-address limits: an IPv6 client usually controls a whole /64, so count the /64."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(addr)


# ---------------------------------------------------------------- statistics (shown to admins)


@dataclass
class Stats:
    started: float = field(default_factory=time.time)
    open: Counter = field(default_factory=Counter)          # path bucket -> open websockets
    per_ip: Counter = field(default_factory=Counter)        # client address -> open websockets
    accepted: Counter = field(default_factory=Counter)      # path -> websockets accepted since start
    rejected: Counter = field(default_factory=Counter)      # reason -> refused since start
    recent: deque = field(default_factory=lambda: deque(maxlen=50))  # (time, ip, path, reason)
    insecure_http: int = 0                                  # plain-HTTP requests redirected or refused
    untrusted_proxy: int = 0                                # proxied HTTPS requests from an untrusted proxy
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"since": self.started, "open": dict(self.open), "open_total": sum(self.open.values()),
                    "accepted": dict(self.accepted), "rejected": dict(self.rejected),
                    "recent": list(self.recent)[::-1], "insecure_http": self.insecure_http,
                    "untrusted_proxy": self.untrusted_proxy}


STATS = Stats()
_last_audit: dict[tuple[str, str], float] = {}


def _audit(ip: str, path: str, reason: str, action: str = "security.ws_rejected", username: str = "") -> None:
    now = time.time()
    key = (ip, reason.split(":")[0])
    if now - _last_audit.get(key, 0) < 60:
        return
    _last_audit[key] = now
    if len(_last_audit) > 5000:  # don't grow without bound under a scan
        _last_audit.clear()
    try:
        _AUDIT_QUEUE.put_nowait((username, action, path, reason, ip))
    except queue.Full:  # under attack: the log line above is enough
        pass


_AUDIT_QUEUE: "queue.Queue[tuple]" = queue.Queue(maxsize=1000)


def _audit_worker() -> None:
    """Writes audit entries off the event loop: a slow database must not stall websocket handshakes."""
    from databridge.services.users import audit

    while True:
        entry = _AUDIT_QUEUE.get()
        try:
            audit(*entry)
        except Exception:  # the audit log must never break request handling
            log.exception("could not write audit entry")


threading.Thread(target=_audit_worker, name="security-audit", daemon=True).start()


_last_log: dict[tuple, float] = {}


def _log_throttled(key: tuple, msg: str, *args) -> None:
    """At most one log line a minute per key (an attacker must not be able to flood the log)."""
    now = time.time()
    if now - _last_log.get(key, 0) < 60:
        return
    if len(_last_log) > 5000:
        _last_log.clear()
    _last_log[key] = now
    log.warning(msg, *args)


def audit_throttled(ip: str, path: str, reason: str, action: str, username: str = "") -> None:
    _audit(ip, path, reason, action, username)


def reject_reason_counts() -> dict[str, int]:
    return STATS.snapshot()["rejected"]


# ---------------------------------------------------------------- origin rules


def _norm_origin(value: str) -> str | None:
    """scheme://host[:port] in lower case with default ports removed, or None if it isn't an http(s) origin."""
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:  # port out of range or not a number
        return None
    default = {"http": 80, "https": 443}[parts.scheme]
    host = parts.hostname.lower()
    if ":" in host:  # IPv6
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


def allowed_origins() -> set[str]:
    out = {o for o in (_norm_origin(x) for x in settings.allowed_origins.split(",") if x.strip()) if o}
    base = _norm_origin(settings.public_base_url)
    if base:
        out.add(base)
    return out


def origin_allowed(origin: str | None, host_header: str | None, secure: bool) -> bool:
    """Same-origin (the page came from this host) or explicitly allowed. "null" and malformed origins never are."""
    norm = _norm_origin(origin or "")
    if not norm:
        return False
    if settings.https_required and not norm.startswith("https://"):
        return False  # a page served over plain HTTP must not drive a secure session
    if norm in allowed_origins():
        return True
    if host_header:
        scheme = "https" if secure else "http"
        return norm == _norm_origin(f"{scheme}://{host_header}")
    return False


# ---------------------------------------------------------------- headers


def security_headers(secure: bool) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
        # Only directives that can't break the Flutter web client: no framing, no <base> or plugin tricks.
        (b"content-security-policy", b"frame-ancestors 'none'; base-uri 'self'; object-src 'none'"),
    ]
    if secure and settings.https_required and settings.hsts_seconds > 0:
        headers.append((b"strict-transport-security", f"max-age={settings.hsts_seconds}".encode()))
    return headers


def _header(scope, name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return None


def _client_ip(scope) -> str:
    client = scope.get("client")
    return client[0] if client else "?"


# ---------------------------------------------------------------- middleware


class TransportSecurity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            return await self._http(scope, receive, send)
        if scope["type"] == "websocket":
            return await self._websocket(scope, receive, send)
        return await self.app(scope, receive, send)

    # ------------------------------------------------------------ HTTP
    async def _http(self, scope, receive, send):
        secure = scope.get("scheme") in SECURE_SCHEMES
        path = scope.get("path", "")
        if settings.https_required and not secure and path not in EXEMPT_HTTP:
            with STATS.lock:
                STATS.insecure_http += 1
            if (_header(scope, b"x-forwarded-proto") or "").lower() == "https":
                # A proxy did terminate TLS, but it isn't in trusted_proxies. Redirecting would loop forever.
                with STATS.lock:
                    STATS.untrusted_proxy += 1
                _log_throttled(("proxy", _client_ip(scope)), "HTTPS proxy at %s is not trusted: add it to "
                               "DATABRIDGE_TRUSTED_PROXIES", _client_ip(scope))
                return await self._respond(send, 400, b'{"detail":"DataBridge does not trust this proxy. An admin '
                                           b'must add its address to DATABRIDGE_TRUSTED_PROXIES."}',
                                           [(b"content-type", b"application/json")], secure)
            base = settings.public_base_url.rstrip("/")
            if scope.get("method") in ("GET", "HEAD") and base.lower().startswith("https://"):
                # Redirect to the configured address, never to the Host header (no open redirect). raw_path keeps
                # the request's own encoding (%3F stays %3F, non-ASCII stays percent-encoded).
                raw = scope.get("raw_path") or path.encode("utf-8")
                qs = scope.get("query_string", b"")
                target = base.encode("latin-1", "replace") + raw + (b"?" + qs if qs else b"")
                return await self._respond(send, 308, b"", [(b"location", target)], secure)
            return await self._respond(send, 400, b'{"detail":"HTTPS is required"}',
                                       [(b"content-type", b"application/json")], secure)

        extra = security_headers(secure)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [h for h in extra if h[0] not in names]
            await send(message)

        return await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _respond(send, status: int, body: bytes, headers: list, secure: bool):
        await send({"type": "http.response.start", "status": status,
                    "headers": headers + [(b"content-length", str(len(body)).encode())] + security_headers(secure)})
        await send({"type": "http.response.body", "body": body})

    # ------------------------------------------------------------ websockets
    def check_websocket(self, scope) -> str | None:
        """Why this websocket must be refused, or None."""
        path = scope.get("path", "")
        secure = scope.get("scheme") in SECURE_SCHEMES
        if settings.https_required and not secure:
            return "insecure: ws:// instead of wss://"
        origin = _header(scope, b"origin")
        if origin is None:
            if path != STREAM_WS:  # browsers always send Origin; only API clients may omit it
                return "origin: missing"
        elif not origin_allowed(origin, _header(scope, b"host"), secure):
            return f"origin: {origin[:120]} not allowed"
        ip = address_bucket(_client_ip(scope))
        bucket = path_bucket(path)
        with STATS.lock:
            # Separate budgets, so streaming clients (which may connect before authenticating) can't use up the
            # studio's connections, and the other way round.
            if bucket == STREAM_WS:
                if STATS.open[STREAM_WS] >= settings.stream_max_connections:
                    return "limit: too many streaming connections"
            elif STATS.open[STUDIO_WS] + STATS.open["other"] >= settings.ws_max_connections:
                return "limit: too many open websockets"
            if STATS.per_ip[ip] >= settings.ws_max_per_ip:
                return "limit: too many websockets from this address"
        return None

    async def _websocket(self, scope, receive, send):
        path, ip = scope.get("path", ""), _client_ip(scope)
        bucket, ip_bucket = path_bucket(path), address_bucket(ip)
        reason = self.check_websocket(scope)
        if reason:
            with STATS.lock:
                STATS.rejected[reason.split(":")[0]] += 1
                STATS.recent.append((time.time(), ip, path[:80], reason))
            _log_throttled(("ws", ip, reason.split(":")[0]), "websocket refused from %s to %s: %s", ip, path[:80],
                           reason)
            _audit(ip, path, reason)
            message = await receive()  # websocket.connect
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.close", "code": 1008, "reason": reason.split(":")[0]})
            return

        limit = max_message(path)
        closed = False

        async def limited_receive():
            nonlocal closed
            message = await receive()
            if message["type"] == "websocket.receive" and not closed:
                text = message.get("text")
                size = len(message.get("bytes") or b"") + (len(text.encode("utf-8", "surrogatepass")) if text else 0)
                if size > limit:
                    closed = True
                    log.warning("websocket from %s to %s closed: message of %d bytes (limit %d)", ip, path, size,
                                limit)
                    with STATS.lock:
                        STATS.rejected["message too large"] += 1
                        STATS.recent.append((time.time(), ip, path, f"message too large: {size} bytes"))
                    await send({"type": "websocket.close", "code": 1009, "reason": "message too large"})
                    return {"type": "websocket.disconnect", "code": 1009}
            return message

        async def guarded_send(message):
            if not closed:  # after we closed an oversized connection, the app's late messages are dropped
                await send(message)

        with STATS.lock:
            STATS.open[bucket] += 1
            STATS.per_ip[ip_bucket] += 1
            STATS.accepted[bucket] += 1
        try:
            await self.app(scope, limited_receive, guarded_send)
        finally:
            with STATS.lock:
                STATS.open[bucket] -= 1
                STATS.per_ip[ip_bucket] -= 1
                if STATS.per_ip[ip_bucket] <= 0:
                    del STATS.per_ip[ip_bucket]
                if STATS.open[bucket] <= 0:
                    del STATS.open[bucket]
