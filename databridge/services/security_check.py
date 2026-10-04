"""Checks the transport security setup (HTTPS, secure websockets, proxy trust, certificates).

Shown to admins (Users > Security), logged at startup, and summarised as one word in /health so monitoring can
alert without the public health endpoint revealing any details.
"""

import ipaddress
import logging
import os
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from databridge.config import settings

log = logging.getLogger("databridge.security")
LEVELS = ("error", "warning", "info")


@dataclass
class Finding:
    level: str      # error | warning | info
    title: str
    detail: str
    fix: str = ""


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("container") is not None


def _local_host(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.endswith(".localhost")


def cert_days_left(path: str) -> tuple[int | None, str]:
    """(days until the certificate expires, subject) for a PEM file; (None, error) if unreadable."""
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(Path(path).read_bytes())
    except Exception as e:  # noqa: BLE001 - reported to the admin as is
        return None, str(e)
    not_after = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else \
        cert.not_valid_after.replace(tzinfo=timezone.utc)
    return (not_after - datetime.now(timezone.utc)).days, cert.subject.rfc4514_string()


def run() -> list[Finding]:
    from databridge.serve import check_tls_settings
    from databridge.web_security import STATS, _norm_origin

    out: list[Finding] = []
    base = urlsplit(settings.public_base_url)
    local = _local_host((base.hostname or "").lower())

    if base.scheme != "https" and not local:
        out.append(Finding("error", "Not encrypted",
                           f"The public address {settings.public_base_url} uses http, so the studio websocket is "
                           "ws:// and passwords, sessions and data travel unencrypted.",
                           "Put DataBridge behind the HTTPS proxy (Traefik or Nginx) or set up built-in TLS, then "
                           "set DATABRIDGE_PUBLIC_BASE_URL to the https:// address."))
    if base.scheme != "https" and settings.https_required:
        out.append(Finding("error", "HTTPS required but the address is http",
                           f"DATABRIDGE_REQUIRE_HTTPS is on, but DATABRIDGE_PUBLIC_BASE_URL is "
                           f"{settings.public_base_url}. Plain requests are refused and there is no https address to "
                           "send people to.",
                           "Set DATABRIDGE_PUBLIC_BASE_URL to the https:// address, or DATABRIDGE_REQUIRE_HTTPS=auto."))
    if base.scheme == "https" and not settings.https_required:
        out.append(Finding("warning", "HTTPS not enforced",
                           "DATABRIDGE_REQUIRE_HTTPS is off, so plain ws:// and http:// connections are accepted.",
                           "Set DATABRIDGE_REQUIRE_HTTPS=auto (the default) or true."))

    for problem in check_tls_settings():
        out.append(Finding("error", "Built-in TLS misconfigured", problem, "Fix the DATABRIDGE_TLS_* settings."))
    if settings.tls_enabled and Path(settings.tls_cert_file).is_file():
        days, subject = cert_days_left(settings.tls_cert_file)
        if days is None:
            out.append(Finding("error", "Certificate unreadable", subject))
        elif days < 0:
            out.append(Finding("error", "Certificate expired", f"{subject} expired {-days} day(s) ago.",
                               "Install a new certificate and restart."))
        elif days <= settings.ai_cert_warn_days:
            out.append(Finding("warning", "Certificate expires soon", f"{subject} expires in {days} day(s).",
                               "Renew it and restart DataBridge."))
        else:
            out.append(Finding("info", "Built-in TLS", f"{subject}, valid for {days} more days, TLS "
                               f"{settings.tls_min_version}+, client certificates: {settings.tls_client_cert}."))

    if settings.trusted_proxies.strip() == "*" and not _in_container() and not settings.tls_enabled \
            and settings.bind_host not in ("127.0.0.1", "::1", "localhost"):
        out.append(Finding("warning", "Proxy headers trusted from anyone",
                           f"DATABRIDGE_TRUSTED_PROXIES is * and the app listens on {settings.bind_host}. A client "
                           "that reaches the port directly could claim its connection was HTTPS.",
                           "List the proxy's address instead (e.g. 127.0.0.1 or 172.17.0.0/16), or make the port "
                           "reachable only from the proxy."))

    bad = [o.strip() for o in settings.allowed_origins.split(",") if o.strip() and not _norm_origin(o)]
    if bad:
        out.append(Finding("warning", "Ignored websocket origins",
                           f"Not valid origins (need scheme://host[:port], no wildcards): {', '.join(bad)}"))
    http_origins = [o for o in settings.allowed_origins.split(",") if o.strip().lower().startswith("http://")]
    if http_origins and settings.https_required:
        out.append(Finding("info", "http origins ignored",
                           f"With HTTPS required, pages served over http can't open websockets: "
                           f"{', '.join(o.strip() for o in http_origins)}"))

    stats = STATS.snapshot()
    if stats["untrusted_proxy"]:
        out.append(Finding("error", "Proxy not trusted",
                           f"{stats['untrusted_proxy']} request(s) came through an HTTPS proxy whose address is not in "
                           f"DATABRIDGE_TRUSTED_PROXIES ({settings.trusted_proxies}), so they were refused.",
                           "Add the proxy's address or network, e.g. 127.0.0.1 for Nginx on the same server or "
                           "172.16.0.0/12 for Traefik in Docker."))
    insecure = stats["rejected"].get("insecure", 0)
    if insecure:
        out.append(Finding("warning", "Plain ws:// connections refused",
                           f"{insecure} websocket(s) arrived without TLS since the last restart.",
                           "Check that the proxy sends X-Forwarded-Proto and is listed in "
                           "DATABRIDGE_TRUSTED_PROXIES, and that users open the https:// address."))
    origin = stats["rejected"].get("origin", 0)
    if origin:
        out.append(Finding("info", "Websockets from other sites refused",
                           f"{origin} connection(s) came from pages on other origins since the last restart "
                           "(possible cross-site websocket hijacking attempts; see Recent refusals).",
                           "If a legitimate portal embeds DataBridge, add its origin to DATABRIDGE_ALLOWED_ORIGINS."))
    if not any(f.level in ("error", "warning") for f in out):
        out.insert(0, Finding("info", "Secure", "HTTPS and secure websockets (wss://) are enforced." if
                              settings.https_required else "Local address: HTTPS is not required here."))
    return out


def summary(findings: list[Finding] | None = None) -> str:
    findings = run() if findings is None else findings
    for level in ("error", "warning"):
        if any(f.level == level for f in findings):
            return level
    return "ok"


def log_findings() -> None:
    for f in run():
        if f.level != "info":
            getattr(log, "error" if f.level == "error" else "warning")("%s: %s %s", f.title, f.detail, f.fix)
