---
title: HTTPS and secure websockets
icon: SHIELD_OUTLINED
permission: manage_users
pages: []
keywords: https, tls, ssl, wss, websocket, secure, certificate, mutual tls, mtls, origin, hsts, proxy, traefik, nginx, x-forwarded-proto, security
---
# HTTPS and secure websockets (admins)

The studio runs over a websocket: every click and screen update travels through `/ws`. The [streaming API](guide:rest-api) uses a websocket too (`/api/v1/stream`). DataBridge makes sure both are **secure websockets (wss://)**, which means they are encrypted with TLS just like `https://`.

Check the current state under [Users](app:users) > **Security**. It shows the setup, any problems with the fix for each, the open connections and recently refused connections. `/health` reports one word (`ok`, `warning` or `error`) under `security`, so monitoring can alert without showing details to the public.

## What DataBridge enforces

When the public address (`DATABRIDGE_PUBLIC_BASE_URL`) starts with `https://`, HTTPS is required (`DATABRIDGE_REQUIRE_HTTPS=auto`):

| Protection | What happens |
|---|---|
| **HTTPS only** | `http://` pages are redirected to the https address. Other plain requests are refused. `/health` stays available for health checks. |
| **wss:// only** | Websockets that didn't arrive over TLS (`ws://`) are refused. |
| **Origin check** | A websocket is accepted only from DataBridge's own pages, or from sites listed in `DATABRIDGE_ALLOWED_ORIGINS`. This stops *cross-site websocket hijacking*: another website can't use a signed-in browser to drive the studio. |
| **Limits** | Open websockets: studio and streaming API have separate budgets, plus limits per address and per API key. Message size: studio 4 MB, streaming API 64 KB, refused while the message is still arriving. Files are uploaded over HTTPS, not over the websocket. Dead connections are dropped after about 40 seconds without a reply to a ping. |
| **Security headers** | HSTS (browsers remember to use https), no framing by other sites (clickjacking), `nosniff`, no referrer. |
| **Audit** | Refused connections appear in the audit log as `security.ws_rejected` (at most once a minute per address and reason). |

## Option 1: behind the HTTPS proxy (recommended)

Traefik (Docker) or Nginx (native install) holds the certificate and handles `https://` and `wss://`. The shipped configurations already pass websocket upgrades and `X-Forwarded-Proto`.

The proxy must be **trusted**: DataBridge believes `X-Forwarded-Proto: https` only from the addresses in `DATABRIDGE_TRUSTED_PROXIES`. The Docker image trusts private network ranges (its Docker network), and the native service trusts `127.0.0.1` and Docker networks. If you see *"DataBridge does not trust this proxy"*, add the proxy's address or network, for example `DATABRIDGE_TRUSTED_PROXIES=127.0.0.1,172.16.0.0/12`.

For TLS 1.2 or newer only, use the `databridge-tls` options in `deploy/native/traefik-dynamic.yml` (Traefik). With Nginx, certbot's settings already do this.

## Option 2: built-in TLS (no proxy)

For an internal server without Traefik or Nginx, DataBridge can serve `https://` and `wss://` itself:

```
DATABRIDGE_PUBLIC_BASE_URL=https://databridge.corp.example:8443
DATABRIDGE_TLS_CERT_FILE=/certs/databridge.crt    # certificate + intermediates, PEM
DATABRIDGE_TLS_KEY_FILE=/certs/databridge.key
DATABRIDGE_TLS_MIN_VERSION=1.2                    # or 1.3
```

Start it with `python -m databridge.serve --port 8443`. The Docker image and the native service already start this way. Only TLS 1.2 or newer and strong ciphers are accepted. If a setting is wrong, DataBridge stops with a clear message. When the certificate is close to expiring, the Security page warns you `DATABRIDGE_AI_CERT_WARN_DAYS` in advance.

**Mutual TLS** (client certificates from your company CA):

```
DATABRIDGE_TLS_CLIENT_CA_FILE=/certs/company-ca.pem
DATABRIDGE_TLS_CLIENT_CERT=required      # or optional
```

With `required`, every browser and script needs a certificate from that CA, on top of signing in or an API key. The native deploy script's health check has no client certificate, so use `optional` there, or keep mutual TLS at a proxy.

## Portals and other sites

If a company portal on another address opens DataBridge websockets, list it: `DATABRIDGE_ALLOWED_ORIGINS=https://portal.corp.example`. Use exact addresses; wildcards are ignored. With HTTPS required, `http://` origins are never accepted.

## Settings

| Variable | Default |
|---|---|
| `DATABRIDGE_REQUIRE_HTTPS` | `auto` |
| `DATABRIDGE_ALLOWED_ORIGINS` | (none; same-origin only) |
| `DATABRIDGE_TRUSTED_PROXIES` | `127.0.0.1` (Docker image: private ranges; native service: local + Docker networks) |
| `DATABRIDGE_HSTS_SECONDS` | one year |
| `DATABRIDGE_WS_MAX_CONNECTIONS` / `_WS_MAX_PER_IP` | 500 studio websockets / 30 per address (IPv6: per /64) |
| `DATABRIDGE_STREAM_MAX_CONNECTIONS` | 200 streaming connections (separate budget) |
| `DATABRIDGE_STREAM_MAX_PER_KEY` / `_STREAM_IDLE_MINUTES` | 10 / 30 |
| `DATABRIDGE_TLS_*` | built-in TLS, see above |

All settings are listed under [Server settings](guide:admin-settings).
