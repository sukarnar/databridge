"""Secure websockets and HTTPS: enforcement, origin checks, limits, headers, streaming API, built-in TLS, proxies.

Most tests run a real uvicorn server (plain or TLS, via databridge.serve) and connect with the `websockets`
client, so the handshake, TLS versions, client certificates and close codes are the real thing.
"""

import json
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
from websockets.sync.client import connect

from databridge import serve, web_security
from databridge.api import stream
from databridge.api.runtime import health_router, router
from databridge.config import settings
from databridge.engine.mapper import MappingSpec
from databridge.ingest.sheet_profile import suggest_profile
from databridge.services import endpoints, events, mappings, security_check, sources, targets
from tests.test_tls import _cert, _name, _pem

# ------------------------------------------------------------------ app, data and servers


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.include_router(stream.router, prefix="/api/v1")
    app.include_router(health_router)

    @app.websocket("/ws")  # stands in for the Flet studio websocket
    async def studio(ws: WebSocket):
        await ws.accept()
        await ws.send_text("studio")
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            await ws.send_text(f"echo {len(msg.get('text') or msg.get('bytes') or '')}")

    app.add_middleware(web_security.TransportSecurity)
    return app


APP = _app()


@pytest.fixture(scope="module")
def published(sales_bytes_module, template_bytes_module):
    prof = suggest_profile(sales_bytes_module, "vendor_sales.xlsx")
    src = sources.create_upload_source("Stream sales", "vendor_sales.xlsx", sales_bytes_module, prof)
    tgt = targets.target_from_template("Stream orders", "target.xlsx", template_bytes_module)
    m = mappings.create_mapping("Stream sales to orders", src.id, tgt.id)
    mappings.save_spec(m.id, MappingSpec(rules=m.rules))
    mappings.publish(m.id)
    endpoints.save_endpoint({"slug": "stream-orders", "name": "Stream orders", "mapping_id": m.id,
                             "params": [{"name": "region", "column": "region", "op": "eq"}]})
    endpoints.save_endpoint({"slug": "stream-other", "name": "Other", "mapping_id": m.id})
    _, admin_key = endpoints.create_api_key("stream-admin")
    limited, limited_key = endpoints.create_api_key("stream-limited", ["stream-orders"])
    return {"mapping": m, "admin": admin_key, "limited": limited_key, "limited_id": limited.id}


@pytest.fixture(scope="module")
def sales_bytes_module():
    from tests.conftest import SAMPLES

    return (SAMPLES / "vendor_sales.xlsx").read_bytes()


@pytest.fixture(scope="module")
def template_bytes_module():
    from tests.conftest import SAMPLES

    return (SAMPLES / "target_customer_orders.xlsx").read_bytes()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(**overrides):
    """Starts APP with databridge.serve's configuration (TLS, proxy trust, ws limits) under these settings."""
    saved = {k: getattr(settings, k) for k in overrides}
    for k, v in overrides.items():
        setattr(settings, k, v)
    port = _free_port()
    config = serve.build_config("127.0.0.1", port, app=APP)
    config.log_level = "error"
    server = serve._Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "server did not start"
    return port, server, saved


@pytest.fixture
def run_server():
    started = []

    def go(**overrides):
        port, server, saved = _start(**overrides)
        started.append((server, saved))
        return port

    yield go
    for server, saved in reversed(started):  # reverse: a later start saved the earlier start's values
        server.should_exit = True
        for k, v in saved.items():
            setattr(settings, k, v)
    time.sleep(0.2)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("wss-pki")
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert("DataBridge Test CA", _name("DataBridge Test CA"), ca_key, ca_key.public_key(), ca=True)
    srv_key = ec.generate_private_key(ec.SECP256R1())
    srv = _cert("databridge.local", ca.subject, ca_key, srv_key.public_key(), sans=[x509.DNSName("localhost")])
    cli_key = ec.generate_private_key(ec.SECP256R1())
    cli = _cert("scheduler", ca.subject, ca_key, cli_key.public_key(), client=True)
    old_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    expired = (x509.CertificateBuilder().subject_name(_name("old")).issuer_name(ca.subject)
               .public_key(old_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(days=60)).not_valid_after(now - timedelta(days=2))
               .sign(ca_key, __import__("cryptography.hazmat.primitives.hashes", fromlist=["SHA256"]).SHA256()))
    paths = {}
    for k, v in {"ca": ca, "srv": srv, "srv_key": srv_key, "cli": cli, "cli_key": cli_key, "expired": expired,
                 "expired_key": old_key}.items():
        (d / f"{k}.pem").write_bytes(_pem(v))
        paths[k] = str(d / f"{k}.pem")
    return paths


def _client_ctx(pki, cert=False, max_version=None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=pki["ca"])
    if cert:
        ctx.load_cert_chain(pki["cli"], pki["cli_key"])
    if max_version:
        ctx.maximum_version = max_version
    return ctx


def _recv(ws, timeout=5) -> dict:
    return json.loads(ws.recv(timeout=timeout))


def _recv_type(ws, kind, timeout=5) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        msg = _recv(ws, timeout=max(0.1, end - time.time()))
        if msg["type"] == kind:
            return msg
    raise AssertionError(f"no {kind} message")


def _refused(fn) -> int:
    with pytest.raises(InvalidStatus) as e:
        fn()
    return e.value.response.status_code


# ------------------------------------------------------------------ headers and HTTPS enforcement


def test_security_headers_and_no_hsts_over_http(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    r = TestClient(APP).get("/health")
    assert r.status_code == 200
    h = r.headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in h["content-security-policy"] and h["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" not in h


def test_https_required_redirects_and_refuses_plain_http(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://databridge.example.com")
    plain = TestClient(APP, follow_redirects=False)
    r = plain.get("/api/v1/endpoints?x=1", headers={"Host": "evil.example"})
    assert r.status_code == 308
    assert r.headers["location"] == "https://databridge.example.com/api/v1/endpoints?x=1"  # never the Host header
    assert plain.post("/api/v1/ingest/1").status_code == 400
    assert plain.get("/health").status_code == 200  # container / deploy health checks stay on plain HTTP
    secure = TestClient(APP, base_url="https://databridge.example.com")
    r = secure.get("/api/v1/endpoints")
    assert r.status_code == 200 and r.headers["strict-transport-security"] == "max-age=31536000"
    r = plain.get("/api/v1/endpoints", headers={"X-Forwarded-Proto": "https"})  # proxy not in trusted_proxies
    assert r.status_code == 400 and "TRUSTED_PROXIES" in r.text  # explained, not an endless redirect
    assert any(f.title == "Proxy not trusted" for f in security_check.run())
    monkeypatch.setattr(settings, "require_https", "false")
    assert plain.get("/api/v1/endpoints").status_code == 200


def test_origin_rules(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://db.example.com")
    monkeypatch.setattr(settings, "allowed_origins", "https://portal.example.com:8443, not-an-origin, https://*.x")
    ok = web_security.origin_allowed
    assert ok("https://db.example.com", "db.example.com", True)
    assert ok("https://DB.example.com:443", "whatever", True)  # normalised, configured address
    assert ok("https://portal.example.com:8443", None, True)
    assert ok("https://other.example.com", "other.example.com", True)  # same-origin by Host
    assert not ok("https://evil.example", "db.example.com", True)
    assert not ok("http://db.example.com", "db.example.com", False)  # https required: http pages can't
    assert not ok("null", "db.example.com", True) and not ok("", "db.example.com", True)
    assert not ok("https://db.example.com.evil.example", "db.example.com", True)
    findings = security_check.run()
    assert any(f.title == "Ignored websocket origins" and "not-an-origin" in f.detail for f in findings)


# ------------------------------------------------------------------ websockets over a real server


def test_plain_ws_refused_when_https_required(run_server, published):
    port = run_server(public_base_url="https://databridge.example.com")
    before = web_security.STATS.snapshot()["rejected"].get("insecure", 0)
    status = _refused(lambda: connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}"))
    assert status == 403
    assert web_security.STATS.snapshot()["rejected"]["insecure"] == before + 1
    assert any(f.title == "Plain ws:// connections refused" for f in security_check.run())


def test_proxy_headers_only_from_trusted_proxies(run_server, published):
    headers = {"X-Forwarded-Proto": "https"}
    origin = "https://databridge.example.com"
    port = run_server(public_base_url=origin, trusted_proxies="127.0.0.1")
    with connect(f"ws://127.0.0.1:{port}/ws", origin=origin, additional_headers=headers) as ws:
        assert ws.recv(timeout=5) == "studio"  # proxy on 127.0.0.1 says the browser used TLS: accepted
    port = run_server(public_base_url=origin, trusted_proxies="10.9.9.9")
    status = _refused(lambda: connect(f"ws://127.0.0.1:{port}/ws", origin=origin, additional_headers=headers))
    assert status == 403  # a client that isn't the proxy can't claim https


def test_cross_site_websocket_hijacking_blocked(run_server):
    port = run_server(public_base_url="http://localhost:8000")
    url = f"ws://127.0.0.1:{port}/ws"
    assert _refused(lambda: connect(url, origin="https://evil.example")) == 403
    assert _refused(lambda: connect(url)) == 403  # studio socket without Origin: not a browser page of ours
    with connect(url, origin=f"http://127.0.0.1:{port}") as ws:  # same origin
        assert ws.recv(timeout=5) == "studio"
    time.sleep(0.5)  # audit entries are written by a background thread
    entries = [e for e in __import__("databridge.services.users", fromlist=["x"]).recent_audit(50)
               if e.action == "security.ws_rejected"]
    assert entries and entries[0].ip == "127.0.0.1"


def test_connection_limits_per_ip(run_server):
    port = run_server(public_base_url="http://localhost:8000", ws_max_per_ip=2)
    origin = f"http://127.0.0.1:{port}"
    with connect(f"ws://127.0.0.1:{port}/ws", origin=origin), connect(f"ws://127.0.0.1:{port}/ws", origin=origin):
        assert _refused(lambda: connect(f"ws://127.0.0.1:{port}/ws", origin=origin)) == 403
    time.sleep(0.3)
    with connect(f"ws://127.0.0.1:{port}/ws", origin=origin) as c:  # slot freed after close
        assert c.recv(timeout=5) == "studio"


def test_studio_message_size_follows_upload_limit(run_server):
    port = run_server(public_base_url="http://localhost:8000", max_upload_mb=1)
    limit = web_security.studio_max_message()
    with connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}", max_size=None) as ws:
        ws.recv(timeout=5)
        ws.send(b"x" * (limit - 10))  # a file just under the limit still gets through
        assert ws.recv(timeout=5) == f"echo {limit - 10}"
    with connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}", max_size=None) as ws:
        ws.recv(timeout=5)
        ws.send(b"x" * (limit + 10))
        with pytest.raises(ConnectionClosed) as e:
            ws.recv(timeout=5)
        assert e.value.rcvd.code == 1009


# ------------------------------------------------------------------ streaming API


def test_stream_auth(run_server, published):
    port = run_server(public_base_url="http://localhost:8000")
    url = f"ws://127.0.0.1:{port}/api/v1/stream"
    with connect(url, additional_headers={"X-API-Key": published["admin"]}) as ws:  # scripts: header
        assert _recv(ws)["type"] == "welcome"
    with connect(url, origin=f"http://127.0.0.1:{port}") as ws:  # browsers: first message
        ws.send(json.dumps({"type": "auth", "api_key": published["limited"]}))
        assert _recv(ws)["key"] == "stream-limited"
    with connect(url, additional_headers={"X-API-Key": "dbk_wrong"}) as ws:
        with pytest.raises(ConnectionClosed) as e:
            ws.recv(timeout=5)
        assert e.value.rcvd.code == 4401
    with connect(url) as ws:
        ws.send(json.dumps({"type": "subscribe", "topics": ["runs"]}))  # not authenticated first
        with pytest.raises(ConnectionClosed) as e:
            ws.recv(timeout=5)
        assert e.value.rcvd.code == 4401
    assert _refused(lambda: connect(url, origin="https://evil.example")) == 403  # browsers from other sites


def test_stream_events_and_permissions(run_server, published):
    port = run_server(public_base_url="http://localhost:8000")
    url = f"ws://127.0.0.1:{port}/api/v1/stream"
    with connect(url, additional_headers={"X-API-Key": published["limited"]}) as ws:
        _recv(ws)
        ws.send(json.dumps({"type": "subscribe", "topics": ["endpoint:stream-orders", "endpoint:stream-other",
                                                            "runs", "bogus"]}))
        sub = _recv_type(ws, "subscribed")
        assert sub["topics"] == ["endpoint:stream-orders"]
        assert set(sub["denied"]) == {"endpoint:stream-other", "runs", "bogus"}
    with connect(url, additional_headers={"X-API-Key": published["admin"]}) as ws:
        _recv(ws)
        ws.send(json.dumps({"type": "subscribe", "topics": ["endpoint:stream-orders", "runs"]}))
        assert len(_recv_type(ws, "subscribed")["topics"]) == 2
        time.sleep(0.2)
        ds = mappings.publish(published["mapping"].id)  # e.g. after an ingest
        got = {}
        while len(got) < 3:
            msg = _recv_type(ws, "event")
            got.setdefault(msg["event"], msg)
        assert got["run.started"]["data"]["kind"] == "publish"
        assert got["run.finished"]["data"]["status"] in ("ok", "warning")
        pub = got["dataset.published"]
        assert pub["topic"] == "endpoint:stream-orders" and pub["data"]["version"] == ds.version
        assert pub["data"]["run_id"] == ds.run_id


def test_stream_query_in_chunks_and_cancel(run_server, published):
    port = run_server(public_base_url="http://localhost:8000")
    url = f"ws://127.0.0.1:{port}/api/v1/stream"
    with connect(url, additional_headers={"X-API-Key": published["limited"]}) as ws:
        _recv(ws)
        ws.send(json.dumps({"type": "query", "id": "q1", "endpoint": "stream-orders", "chunk_size": 2}))
        rows, chunks = [], 0
        while True:
            msg = _recv(ws)
            if msg["type"] == "rows":
                assert msg["id"] == "q1" and msg["seq"] == chunks and len(msg["data"]) <= 2
                rows += msg["data"]
                chunks += 1
            elif msg["type"] == "end":
                break
        assert msg["total"] == len(rows) == 6 and msg["chunks"] == chunks == 3
        ws.send(json.dumps({"type": "query", "id": "q2", "endpoint": "stream-orders", "params": {"region": "East"}}))
        assert {r["region"] for r in _recv_type(ws, "rows")["data"]} == {"East"}
        _recv_type(ws, "end")
        ws.send(json.dumps({"type": "query", "id": "q3", "endpoint": "stream-other"}))
        err = _recv_type(ws, "error")
        assert err["id"] == "q3" and err["status"] == 403
        ws.send("not json")
        assert _recv_type(ws, "error")["message"] == "messages must be JSON"
        ws.send(json.dumps({"type": "ping"}))
        assert _recv_type(ws, "pong")


def test_stream_limits(run_server, published):
    port = run_server(public_base_url="http://localhost:8000", stream_max_per_key=1)
    url = f"ws://127.0.0.1:{port}/api/v1/stream"
    headers = {"X-API-Key": published["admin"]}
    with connect(url, additional_headers=headers) as first:
        _recv(first)
        with connect(url, additional_headers=headers) as second:
            with pytest.raises(ConnectionClosed) as e:
                second.recv(timeout=5)
            assert e.value.rcvd.code == 4429
        first.send("x" * (web_security.STREAM_MAX_MESSAGE + 1))
        with pytest.raises(ConnectionClosed) as e:
            while True:
                first.recv(timeout=5)
        assert e.value.rcvd.code == 1009
    time.sleep(0.3)
    with connect(url, additional_headers=headers) as again:  # slot released after both closed
        assert _recv(again)["type"] == "welcome"


def test_revoked_key_ends_stream(run_server, published, monkeypatch):
    monkeypatch.setattr(stream, "HEARTBEAT_SECONDS", 0.2)
    monkeypatch.setattr(stream, "RECHECK_SECONDS", 0.2)
    _, raw = endpoints.create_api_key("stream-temp", ["stream-orders"])
    port = run_server(public_base_url="http://localhost:8000")
    with connect(f"ws://127.0.0.1:{port}/api/v1/stream", additional_headers={"X-API-Key": raw}) as ws:
        _recv(ws)
        key = endpoints.active_key(raw)
        endpoints.revoke_api_key(key.id)
        with pytest.raises(ConnectionClosed) as e:
            while True:
                ws.recv(timeout=5)
        assert e.value.rcvd.code == 4401


def test_slow_consumer_is_disconnected():
    import asyncio

    async def scenario():
        sub = events.subscribe(events.Subscriber(loop=asyncio.get_running_loop(), queue=asyncio.Queue(2)))
        sub.topics.add("runs")
        for i in range(5):
            events.publish("runs", "run.finished", {"id": i})
        await asyncio.sleep(0.05)
        events.unsubscribe(sub)
        return sub

    sub = asyncio.run(scenario())
    assert sub.overflowed and sub.queue.qsize() == 2


# ------------------------------------------------------------------ built-in TLS


def test_builtin_tls_serves_wss(run_server, published, pki):
    port = run_server(public_base_url="https://localhost", tls_cert_file=pki["srv"], tls_key_file=pki["srv_key"])
    ctx = _client_ctx(pki)
    with connect(f"wss://localhost:{port}/api/v1/stream", ssl=ctx,
                 additional_headers={"X-API-Key": published["admin"]}) as ws:
        assert _recv(ws)["type"] == "welcome"
    with connect(f"wss://localhost:{port}/ws", ssl=ctx, origin=f"https://localhost:{port}") as ws:
        assert ws.recv(timeout=5) == "studio"
    # TLS 1.1 and older are refused
    old = _client_ctx(pki)
    old.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    old.maximum_version = ssl.TLSVersion.TLSv1_1
    with pytest.raises((ssl.SSLError, OSError)):
        connect(f"wss://localhost:{port}/api/v1/stream", ssl=old, open_timeout=5)


def test_tls_13_only(run_server, published, pki):
    port = run_server(public_base_url="https://localhost", tls_cert_file=pki["srv"], tls_key_file=pki["srv_key"],
                      tls_min_version="1.3")
    with pytest.raises((ssl.SSLError, OSError)):
        connect(f"wss://localhost:{port}/api/v1/stream", ssl=_client_ctx(pki, max_version=ssl.TLSVersion.TLSv1_2),
                open_timeout=5)
    with connect(f"wss://localhost:{port}/api/v1/stream", ssl=_client_ctx(pki),
                 additional_headers={"X-API-Key": published["admin"]}) as ws:
        assert _recv(ws)["type"] == "welcome"


def test_mutual_tls_required(run_server, published, pki):
    port = run_server(public_base_url="https://localhost", tls_cert_file=pki["srv"], tls_key_file=pki["srv_key"],
                      tls_client_ca_file=pki["ca"], tls_client_cert="required")
    url = f"wss://localhost:{port}/api/v1/stream"
    with pytest.raises((ssl.SSLError, OSError, WebSocketException)):
        with connect(url, ssl=_client_ctx(pki), additional_headers={"X-API-Key": published["admin"]},
                     open_timeout=5) as ws:
            ws.recv(timeout=5)  # TLS 1.3 reports a missing client certificate after the handshake
    with connect(url, ssl=_client_ctx(pki, cert=True), additional_headers={"X-API-Key": published["admin"]}) as ws:
        assert _recv(ws)["type"] == "welcome"


def test_tls_settings_checked(pki, monkeypatch):
    monkeypatch.setattr(settings, "tls_cert_file", pki["srv"])
    monkeypatch.setattr(settings, "tls_key_file", "")
    assert any("both" in p for p in serve.check_tls_settings())
    monkeypatch.setattr(settings, "tls_key_file", pki["cli_key"])  # key of another certificate
    assert any("can't be used together" in p for p in serve.check_tls_settings())
    with pytest.raises(serve.TLSSettingsError):
        serve.build_config("127.0.0.1", 1, app=APP)
    monkeypatch.setattr(settings, "tls_key_file", pki["srv_key"])
    monkeypatch.setattr(settings, "tls_client_cert", "required")
    assert any("CLIENT_CA_FILE" in p for p in serve.check_tls_settings())
    monkeypatch.setattr(settings, "tls_client_cert", "none")
    monkeypatch.setattr(settings, "tls_min_version", "1.0")
    assert any("1.2 or 1.3" in p for p in serve.check_tls_settings())
    monkeypatch.setattr(settings, "tls_min_version", "1.2")
    assert serve.check_tls_settings() == []
    cfg = serve.build_config("127.0.0.1", 1, app=APP)
    assert cfg.ws_max_size == web_security.studio_max_message() and cfg.ws_ping_interval == 20
    assert cfg.forwarded_allow_ips == settings.trusted_proxies


# ------------------------------------------------------------------ security check


def test_security_check_findings(monkeypatch, pki):
    def titles():
        return {f.title: f.level for f in security_check.run()}

    monkeypatch.setattr(settings, "public_base_url", "http://databridge.example.com")
    assert titles()["Not encrypted"] == "error" and security_check.summary() == "error"
    monkeypatch.setattr(settings, "public_base_url", "https://databridge.example.com")
    monkeypatch.setattr(settings, "require_https", "false")
    assert titles()["HTTPS not enforced"] == "warning"
    monkeypatch.setattr(settings, "require_https", "auto")
    monkeypatch.setattr(settings, "tls_cert_file", pki["expired"])
    monkeypatch.setattr(settings, "tls_key_file", pki["expired_key"])
    assert titles()["Certificate expired"] == "error"
    monkeypatch.setattr(settings, "tls_cert_file", pki["srv"])
    monkeypatch.setattr(settings, "tls_key_file", pki["srv_key"])
    monkeypatch.setattr(settings, "ai_cert_warn_days", 60)
    assert titles()["Certificate expires soon"] == "warning"  # test certificate is valid for 30 days
    monkeypatch.setattr(settings, "ai_cert_warn_days", 7)
    assert "Built-in TLS" in titles()
    monkeypatch.setattr(settings, "tls_cert_file", "")
    monkeypatch.setattr(settings, "tls_key_file", "")
    monkeypatch.setattr(settings, "trusted_proxies", "*")
    monkeypatch.setattr(security_check, "_in_container", lambda: False)
    assert titles()["Proxy headers trusted from anyone"] == "warning"


def test_health_reports_one_word(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "http://databridge.example.com")
    monkeypatch.setattr(settings, "require_https", "false")
    body = TestClient(APP).get("/health").json()
    assert body["security"] == "error" and set(body) == {"status", "release", "security"}


# ------------------------------------------------------------------ review regressions


def test_oversized_stream_frame_refused_while_reading(run_server, published):
    port = run_server(public_base_url="http://localhost:8000")
    with connect(f"ws://127.0.0.1:{port}/api/v1/stream", max_size=None) as ws:  # not even authenticated
        ws.send(b"x" * (5 * 1024 * 1024))
        with pytest.raises(ConnectionClosed) as e:
            while True:
                ws.recv(timeout=5)
        assert e.value.rcvd.code == 1009


def test_bad_origin_port_is_refused_not_crash(run_server, monkeypatch):
    port = run_server(public_base_url="http://localhost:8000", allowed_origins="https://portal:99999")
    assert _refused(lambda: connect(f"ws://127.0.0.1:{port}/ws", origin="https://x:99999")) == 403
    assert any(f.title == "Ignored websocket origins" for f in security_check.run())  # config typo reported


def test_unknown_websocket_paths_share_one_counter(run_server):
    port = run_server(public_base_url="http://localhost:8000")
    for i in range(3):
        try:
            with connect(f"ws://127.0.0.1:{port}/x{i}", origin=f"http://127.0.0.1:{port}"):
                pass
        except WebSocketException:
            pass
    keys = set(web_security.STATS.snapshot()["accepted"])
    assert keys <= {"/ws", "/api/v1/stream", "other"}


def test_redirect_keeps_raw_path(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://db.example.com")
    plain = TestClient(APP, follow_redirects=False)
    assert plain.get("/a%3Fb").headers["location"] == "https://db.example.com/a%3Fb"
    r = plain.get("/%C4%80")
    assert r.status_code == 308 and r.headers["location"] == "https://db.example.com/%C4%80"


def test_require_https_with_http_address_does_not_loop(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    monkeypatch.setattr(settings, "require_https", "true")
    assert TestClient(APP, follow_redirects=False).get("/api/v1/endpoints").status_code == 400
    assert any(f.title == "HTTPS required but the address is http" for f in security_check.run())


def test_stream_budget_is_separate_from_studio(run_server, published):
    port = run_server(public_base_url="http://localhost:8000", stream_max_connections=1)
    with connect(f"ws://127.0.0.1:{port}/api/v1/stream"):  # an unauthenticated client holds the only slot
        assert _refused(lambda: connect(f"ws://127.0.0.1:{port}/api/v1/stream")) == 403
        with connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
            assert ws.recv(timeout=5) == "studio"  # the studio still works


def test_address_buckets():
    assert web_security.address_bucket("2001:db8::1") == web_security.address_bucket("2001:db8::ffff") \
        == "2001:db8::/64"
    assert web_security.address_bucket("::ffff:10.0.0.1") == "10.0.0.1"
    assert web_security.address_bucket("10.0.0.1") == "10.0.0.1"


def test_stream_bad_queries_always_answered(run_server, published):
    port = run_server(public_base_url="http://localhost:8000")
    with connect(f"ws://127.0.0.1:{port}/api/v1/stream", additional_headers={"X-API-Key": published["admin"]}) as ws:
        _recv(ws)
        for i, bad in enumerate([{"version": "99999999999999999999999"}, {"chunk_size": 1e400},
                                 {"chunk_size": "x"}, {"params": [1]}]):
            ws.send(json.dumps({"type": "query", "id": f"b{i}", "endpoint": "stream-orders", **bad}))
            err = _recv_type(ws, "error")
            assert err["id"] == f"b{i}" and err["status"] == 400 and "Traceback" not in err["message"]
        ws.send(json.dumps({"type": "query", "id": "c", "endpoint": "stream-orders", "chunk_size": 1}))
        ws.send(json.dumps({"type": "cancel", "id": "c"}))
        end = time.time() + 10
        while time.time() < end:
            msg = _recv(ws, timeout=10)
            if msg.get("id") == "c" and msg["type"] in ("cancelled", "end"):
                break
        else:
            raise AssertionError("query neither cancelled nor finished")
        ws.send(json.dumps({"type": "query", "id": "d", "endpoint": "stream-orders", "chunk_size": 1}))
        assert _recv_type(ws, "rows")["id"] == "d"  # cancel while it is streaming
        ws.send(json.dumps({"type": "cancel", "id": "d"}))
        while (msg := _recv(ws))["type"] not in ("cancelled", "end"):
            pass
        ws.send(json.dumps({"type": "ping"}))
        assert _recv_type(ws, "pong")  # connection still fine


def test_stream_message_rate_limit(run_server, published):
    port = run_server(public_base_url="http://localhost:8000")
    with connect(f"ws://127.0.0.1:{port}/api/v1/stream", additional_headers={"X-API-Key": published["admin"]}) as ws:
        _recv(ws)
        try:
            for _ in range(stream.MESSAGES_PER_10S + 1):  # one too many; then only read (see note)
                ws.send(json.dumps({"type": "ping"}))
            while True:
                ws.recv(timeout=5)
        except ConnectionClosed:
            pass
        # Had the client kept sending after the server closed, TCP could reset before it read the close frame.
        assert ws.protocol.close_code == 1008


def test_batch_query_reads_in_chunks(published):
    ep = endpoints.get_endpoint_by_slug("stream-orders")
    bq = endpoints.BatchQuery(ep, {"region": "East"})
    try:
        total = bq.count()
        assert total >= 2
        bq.start()
        first = bq.next_rows(1)
        rest = bq.next_rows(1000)
        assert len(first) == 1 and len(rest) == total - 1 and bq.next_rows(5) == []
        assert {r["region"] for r in first + rest} == {"East"}
    finally:
        bq.close()


def test_nginx_passes_forwarded_headers_in_every_location():
    import re
    from pathlib import Path

    conf = (Path(__file__).resolve().parents[1] / "deploy" / "native" / "nginx-databridge.conf").read_text()
    blocks = re.findall(r"location[^{]*\{(.*?)\n    \}", conf, re.S)
    assert len(blocks) >= 4
    for block in blocks:  # nginx drops server-level proxy_set_header in a location that sets its own
        for header in ("Host", "X-Forwarded-For", "X-Forwarded-Proto"):
            assert f"proxy_set_header {header}" in block, (header, block[:80])


def test_studio_files_never_travel_over_the_websocket():
    """Uploads use signed HTTPS URLs (databridge/ui/uploads.py); the websocket only takes small messages."""
    from pathlib import Path

    from databridge.ui.uploads import _safe_name

    ui = Path(__file__).resolve().parents[1] / "databridge" / "ui"
    offenders = [str(p) for p in ui.rglob("*.py") if "with_data=True" in p.read_text()]
    assert offenders == []
    assert _safe_name("../../etc/passwd") == "passwd" and _safe_name("C:\\x\\Q1 <sales>.xlsx") == "Q1 _sales_.xlsx"
