"""Runs DataBridge with the right transport settings: `python -m databridge.serve [--host H] [--port P]`

Use this instead of calling uvicorn directly. It applies the DATABRIDGE_* settings that uvicorn's own
command line doesn't know about:

* Built-in TLS (https:// and wss:// without a proxy) when DATABRIDGE_TLS_CERT_FILE and _KEY_FILE are set:
  TLS 1.2 or newer only, modern ciphers, optional client certificates (mutual TLS) from a company CA.
* Which proxies may set X-Forwarded-Proto / X-Forwarded-For (DATABRIDGE_TRUSTED_PROXIES). HTTPS enforcement
  relies on this: the app must know whether the browser's connection was encrypted.
* Websocket limits: largest message (studio uploads travel over the websocket, so upload limit + framing) and
  keep-alive pings that drop dead connections within about 40 seconds.
* One worker: the studio keeps each session in process memory.
"""

import argparse
import logging
import ssl
import sys
from pathlib import Path

import uvicorn

from databridge.config import settings

log = logging.getLogger("databridge.serve")
CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:!aNULL:!eNULL:!MD5:!DSS:!RC4:!3DES"
CLIENT_CERT = {"none": ssl.CERT_NONE, "optional": ssl.CERT_OPTIONAL, "required": ssl.CERT_REQUIRED}
MIN_VERSION = {"1.2": ssl.TLSVersion.TLSv1_2, "1.3": ssl.TLSVersion.TLSv1_3}


class TLSSettingsError(ValueError):
    pass


def check_tls_settings() -> list[str]:
    """Problems that would stop TLS from starting (empty = fine). Also used by the security check."""
    problems = []
    if bool(settings.tls_cert_file) != bool(settings.tls_key_file):
        problems.append("Set both DATABRIDGE_TLS_CERT_FILE and DATABRIDGE_TLS_KEY_FILE (or neither)")
    for name in ("tls_cert_file", "tls_key_file", "tls_client_ca_file"):
        value = getattr(settings, name)
        if value and not Path(value).is_file():
            problems.append(f"DATABRIDGE_{name.upper()}: file not found: {value}")
    if settings.tls_min_version not in MIN_VERSION:
        problems.append("DATABRIDGE_TLS_MIN_VERSION must be 1.2 or 1.3")
    if settings.tls_client_cert not in CLIENT_CERT:
        problems.append("DATABRIDGE_TLS_CLIENT_CERT must be none, optional or required")
    elif settings.tls_client_cert != "none" and not settings.tls_client_ca_file:
        problems.append("Client certificates need DATABRIDGE_TLS_CLIENT_CA_FILE (the CA that issues them)")
    if settings.tls_client_cert != "none" and not settings.tls_enabled:
        problems.append("Client certificates need built-in TLS (certificate and key files)")
    if not problems and settings.tls_enabled:
        try:
            build_ssl_context()
        except (ssl.SSLError, OSError) as e:
            problems.append(f"Certificate and key can't be used together: {e}")
    return problems


def build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = MIN_VERSION.get(settings.tls_min_version, ssl.TLSVersion.TLSv1_2)
    ctx.set_ciphers(CIPHERS)  # TLS 1.3 suites are always the strong defaults
    ctx.options |= ssl.OP_NO_COMPRESSION | ssl.OP_CIPHER_SERVER_PREFERENCE
    ctx.load_cert_chain(settings.tls_cert_file, settings.tls_key_file, password=settings.tls_key_password or None)
    ctx.verify_mode = CLIENT_CERT.get(settings.tls_client_cert, ssl.CERT_NONE)
    if settings.tls_client_ca_file:
        ctx.load_verify_locations(settings.tls_client_ca_file)
    return ctx


def ws_max_size() -> int:
    from databridge.web_security import STREAM_MAX_MESSAGE, STUDIO_MAX_MESSAGE

    return max(STUDIO_MAX_MESSAGE, STREAM_MAX_MESSAGE)


def _ws_protocol():
    """uvicorn's websocket protocol with a message size limit per path, applied while frames are read.

    A limit checked after uvicorn has buffered a message doesn't protect memory; here an oversized message on
    /api/v1/stream is refused (close 1009) as soon as its frame header says it is too large.
    """
    from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol

    from databridge.web_security import max_message

    class PathLimitedWebSocket(WebSocketsSansIOProtocol):
        def handle_connect(self, event):
            super().handle_connect(event)
            scope = getattr(self, "scope", None)
            if scope is not None:
                self.conn.max_message_size = max_message(scope["path"])

    return PathLimitedWebSocket


def build_config(host: str | None = None, port: int | None = None, app: str = "databridge.main:app") -> uvicorn.Config:
    problems = check_tls_settings()
    if problems:
        raise TLSSettingsError("; ".join(problems))
    config = uvicorn.Config(
        app, host=host or settings.bind_host, port=port or settings.bind_port, workers=1,
        proxy_headers=True, forwarded_allow_ips=settings.trusted_proxies or "127.0.0.1",
        ws=_ws_protocol(), ws_max_size=ws_max_size(), ws_ping_interval=20.0, ws_ping_timeout=20.0,
        server_header=False, log_level="info",
        **({"ssl_certfile": settings.tls_cert_file, "ssl_keyfile": settings.tls_key_file,
            "ssl_keyfile_password": settings.tls_key_password or None,
            "ssl_ca_certs": settings.tls_client_ca_file or None,
            "ssl_cert_reqs": CLIENT_CERT[settings.tls_client_cert], "ssl_ciphers": CIPHERS}
           if settings.tls_enabled else {}),
    )
    return config


class _Server(uvicorn.Server):
    """Replaces uvicorn's TLS context with ours after loading (minimum version, no compression)."""

    async def serve(self, sockets=None):
        if not self.config.loaded:
            self.config.load()
        if settings.tls_enabled:
            self.config.ssl = build_ssl_context()
        await super().serve(sockets)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m databridge.serve", description=__doc__.split("\n")[0])
    ap.add_argument("--host", help=f"address to listen on (default DATABRIDGE_BIND_HOST, {settings.bind_host})")
    ap.add_argument("--port", type=int, help=f"port (default DATABRIDGE_BIND_PORT, {settings.bind_port})")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = build_config(args.host, args.port)
    except TLSSettingsError as e:
        print(f"DataBridge can't start: {e}", file=sys.stderr)
        raise SystemExit(2) from e
    scheme = "https/wss" if settings.tls_enabled else "http/ws (put an HTTPS proxy in front)"
    log.info("DataBridge listening on %s:%s (%s, TLS >= %s, client certificates: %s)", config.host, config.port,
             scheme, settings.tls_min_version if settings.tls_enabled else "-", settings.tls_client_cert)
    _Server(config).run()


if __name__ == "__main__":
    main()
