"""Container health check: `python -m databridge.healthcheck` (exit 0 = healthy).

Calls /health on this machine over http, or https when built-in TLS is on. The certificate is issued for the
public name, not for 127.0.0.1, so verification is skipped for this loopback-only call.
"""

import json
import ssl
import sys
import urllib.request

from databridge.config import settings


def main() -> int:
    scheme = "https" if settings.tls_enabled else "http"
    url = f"{scheme}://127.0.0.1:{settings.bind_port}/health"
    ctx = None
    if settings.tls_enabled:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if settings.tls_client_cert == "required":
            # Without a client certificate we can't call /health; a listening port is the best local signal.
            import socket

            try:
                socket.create_connection(("127.0.0.1", settings.bind_port), timeout=5).close()
                print("listening (mutual TLS required: /health not called)")
                return 0
            except OSError as e:
                print(f"unhealthy: {e}", file=sys.stderr)
                return 1
    try:
        with urllib.request.urlopen(url, timeout=5, context=ctx) as r:  # noqa: S310 - fixed local URL
            body = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        print(f"unhealthy: {e}", file=sys.stderr)
        return 1
    print(json.dumps(body))
    return 0 if body.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
