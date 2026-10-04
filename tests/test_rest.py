"""REST API sources: auth, pagination, formats, safety, snapshots/refresh/schedule, TLS - against a real server.

A fake API (FastAPI on uvicorn in a thread) plays every style a real API uses.
"""

import base64
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import polars as pl
import pytest
import uvicorn
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from databridge.connectors import rest
from databridge.connectors.rest import RestConnector, RestError
from databridge.engine.mapper import MappingSpec
from databridge.services import connections, endpoints, mappings, scheduler, sources, targets

ORDERS = [{"id": i, "customer": {"name": f"Customer {i}", "city": "Cary" if i % 2 else "Apex"},
           "amount": i * 10.5, "status": "open" if i % 3 else "closed"} for i in range(1, 24)]
STATE = {"version_rows": [{"id": 1, "v": "a"}], "tokens": set(), "token_calls": 0, "flaky": 0, "since": None}

api = FastAPI()


def _bad():
    return JSONResponse({"message": "unauthorized"}, status_code=401)


@api.get("/v1/orders")
def orders(page: int = 1, page_size: int = 10, x_api_key: str | None = Header(None)):
    if x_api_key != "k1":
        return _bad()
    chunk = ORDERS[(page - 1) * page_size: page * page_size]
    return {"data": {"items": chunk}, "meta": {"total_pages": -(-len(ORDERS) // page_size)}}


@api.get("/v1/offset")
def by_offset(offset: int = 0, limit: int = 10, api_key: str | None = None):
    if api_key != "k1":
        return _bad()
    return ORDERS[offset: offset + limit]


@api.get("/v1/cursor")
def by_cursor(cursor: str | None = None, authorization: str | None = Header(None)):
    if authorization != "Bearer tok":
        return _bad()
    start = int(cursor or 0)
    nxt = start + 7 if start + 7 < len(ORDERS) else None
    return {"results": ORDERS[start: start + 7], "next_cursor": str(nxt) if nxt else None}


@api.get("/v1/next")
def by_next(p: int = 1, authorization: str | None = Header(None)):
    if authorization != "Basic " + base64.b64encode(b"svc:pw").decode():
        return _bad()
    chunk = ORDERS[(p - 1) * 10: p * 10]
    return {"items": chunk, "links": {"next": f"/v1/next?p={p + 1}" if p * 10 < len(ORDERS) else None}}


@api.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    if form.get("grant_type") != "client_credentials" or form.get("client_id") != "cid" \
            or form.get("client_secret") != "csecret":
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    STATE["token_calls"] += 1
    t = f"t{STATE['token_calls']}"
    STATE["tokens"].add(t)
    return {"access_token": t, "token_type": "bearer", "expires_in": 3600}


@api.get("/v1/link")
def by_link(request: Request, p: int = 1, authorization: str | None = Header(None)):
    if not authorization or authorization.removeprefix("Bearer ") not in STATE["tokens"]:
        return _bad()
    headers = {"Link": f'<{request.url_for("by_link")}?p={p + 1}>; rel="next"'} if p * 10 < len(ORDERS) else {}
    return JSONResponse(ORDERS[(p - 1) * 10: p * 10], headers=headers)


@api.get("/v1/nested")
def nested():
    return {"orders": [
        {"id": 1, "customer": {"name": "Ann", "address": {"city": "Cary"}}, "tags": ["a", "b"], "code": 7,
         "lines": [{"sku": "X1", "qty": 2}, {"sku": "X2", "qty": 1}]},
        {"id": 2, "customer": {"name": "Bo", "address": {"city": "Apex"}}, "tags": [], "code": "B-7",
         "lines": []},
    ]}


@api.get("/v1/csv")
def as_csv():
    return PlainTextResponse("id;name;amount\n1;Ann;10.5\n2;Bo;\n", media_type="text/csv")


@api.get("/v1/xml")
def as_xml():
    xml = ("<?xml version='1.0'?><export><orders>"
           "<order id='1'><customer>Ann</customer><amount>10.5</amount><ship><city>Cary</city></ship></order>"
           "<order id='2'><customer>Bo</customer><amount>7</amount><ship><city>Apex</city></ship></order>"
           "</orders></export>")
    return Response(xml, media_type="application/xml")


@api.get("/v1/xml-bomb")
def xml_bomb():
    return Response('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><r>&lol;</r>',
                    media_type="application/xml")


@api.get("/v1/flaky")
def flaky():
    STATE["flaky"] += 1
    if STATE["flaky"] == 1:
        return JSONResponse({"message": "slow down"}, status_code=429, headers={"Retry-After": "0"})
    return [{"ok": True}]


@api.get("/v1/redirect-in")
def redirect_in():
    return RedirectResponse("/v1/offset?api_key=k1&limit=2", status_code=302)


@api.get("/v1/redirect-out")
def redirect_out():
    return RedirectResponse("http://evil.example/steal", status_code=302)


@api.get("/v1/evil-next")
def evil_next():
    return {"items": [{"id": 1}], "links": {"next": "http://evil.example/page2"}}


@api.get("/v1/same-cursor")
def same_cursor(cursor: str | None = None):
    return {"results": [{"id": 1}], "next_cursor": "again"}


@api.post("/v1/search")
async def search(request: Request):
    body = await request.json()
    return [o for o in ORDERS if o["status"] == body.get("status")]


@api.get("/v1/since")
def since(since: str = ""):
    STATE["since"] = since
    return [{"id": 1, "since": since}]


@api.get("/v1/versioned")
def versioned():
    return STATE["version_rows"]


@api.get("/v1/missing")
def missing():
    return JSONResponse({"detail": "no such thing"}, status_code=404)


@api.get("/v1/echo-secret")
def echo_secret(request: Request):
    return JSONResponse({"message": f"Invalid key {request.headers.get('authorization', '')}"}, status_code=401)


@api.get("/v1/gzip-bomb")
def gzip_bomb():
    import gzip

    return Response(gzip.compress(b"[" + b"0," * 1_500_000 + b"0]"), media_type="application/json",
                    headers={"Content-Encoding": "gzip"})


@api.get("/v1/maybe-empty")
def maybe_empty():
    return STATE.get("maybe_rows", [])


@api.get("/v1/ignores-page")
def ignores_page(page: int = 1):
    return [{"id": i} for i in range(5)]


@api.get("/v1/capped-offset")
def capped_offset(offset: int = 0, limit: int = 10):
    return ORDERS[offset: offset + min(limit, 5)]  # the API caps pages at 5 whatever you ask


@api.get("/v1/zero-pages")
def zero_pages(page: int = 0):
    last = 2  # pages 0, 1, 2; out of range returns the last page again
    return {"items": [{"page": min(page, last)}], "total_pages": 3}


@api.get("/v1/loop-redirect")
def loop_redirect():
    return RedirectResponse("/v1/loop-redirect", status_code=302)


@api.get("/v1/not-modified")
def not_modified():
    return Response(status_code=304)


@api.get("/v1/bom-csv")
def bom_csv():
    return Response("\ufeffid,name\n1,Ann\n".encode("utf-8"), media_type="text/csv; charset=utf-8")


@api.post("/oauth/weird")
def weird_token():
    return JSONResponse(["not", "an", "object"])


@api.get("/v1/changes")
def changes(since: str = ""):
    return STATE.get("changes", [])


@api.get("/busy/{rest:path}")
def always_busy(rest: str):
    STATE["busy_calls"] = STATE.get("busy_calls", 0) + 1
    return JSONResponse({"message": "slow down"}, status_code=429, headers={"Retry-After": "20"})


@api.get("/v1/slow-later")
async def slow_later(page: int = 1):
    import asyncio

    if page > 2:  # a preview (2 pages) is quick; reading everything takes a while
        await asyncio.sleep(4)
    return [{"id": page, "value": f"row {page}"}] if page <= 6 else []


@api.get("/test")
def dummy_test():
    return {"status": "ok", "method": "GET"}  # like https://dummyjson.com/test: one object, not a list


@api.get("/v1/slow")
async def slow(page: int = 1):
    import asyncio

    STATE["slow_live"] = STATE.get("slow_live", 0) + 1
    STATE["slow_peak"] = max(STATE.get("slow_peak", 0), STATE["slow_live"])
    await asyncio.sleep(0.15)
    STATE["slow_live"] -= 1
    return [{"id": page * 100 + i, "page": page} for i in range(5)] if page <= 10 else []


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture(autouse=True)
def fresh_tokens():
    rest.clear_token_cache()


def conn(base_url, **cfg) -> RestConnector:
    plain = {"base_url": base_url, **{k: v for k, v in cfg.items() if k not in RestConnector.secret_fields}}
    secret = {k: v for k, v in cfg.items() if k in RestConnector.secret_fields}
    return RestConnector(plain, secret)


# ------------------------------------------------------------------ auth + pagination


def test_api_key_header_page_pagination_and_records_path(base_url):
    c = conn(base_url, auth_type="api_key", api_key="k1")
    df = c.fetch({"path": "/v1/orders", "records_path": "data.items",
                  "pagination": {"type": "page", "param": "page", "size_param": "page_size", "size": 10}})
    assert df.height == 23 and df["id"].to_list() == list(range(1, 24))
    assert {"customer.name", "customer.city", "amount"} <= set(df.columns)  # nested objects flattened
    df = c.fetch({"path": "/v1/orders", "records_path": "data.items",  # stop by total pages, no page size
                  "pagination": {"type": "page", "total_path": "meta.total_pages"}})
    assert df.height == 23
    with pytest.raises(RestError, match="HTTP 401"):
        conn(base_url, auth_type="api_key", api_key="wrong").fetch({"path": "/v1/orders"})


def test_api_key_in_query_offset_pagination_and_max_rows(base_url):
    c = conn(base_url, auth_type="api_key", api_key="k1", api_key_in="query", api_key_name="api_key")
    ref = {"path": "/v1/offset", "pagination": {"type": "offset", "param": "offset", "size_param": "limit",
                                                  "size": 10}}
    assert c.fetch(ref).height == 23
    assert c.fetch({**ref, "max_rows": 15}).height == 15


def test_bearer_cursor_pagination(base_url):
    c = conn(base_url, auth_type="bearer", bearer_token="tok")
    df = c.fetch({"path": "/v1/cursor", "pagination": {"type": "cursor", "cursor_path": "next_cursor",
                                                        "param": "cursor"}})
    assert df.height == 23 and df["id"].n_unique() == 23


def test_basic_auth_next_url_pagination(base_url):
    c = conn(base_url, auth_type="basic", username="svc", password="pw")
    df = c.fetch({"path": "/v1/next", "pagination": {"type": "next_url", "next_path": "links.next"}})
    assert df.height == 23


def test_oauth2_client_credentials_link_header_and_token_reuse(base_url):
    STATE["tokens"].clear()
    c = conn(base_url, auth_type="oauth2", token_url=f"{base_url}/oauth/token", client_id="cid",
             client_secret="csecret")
    calls = STATE["token_calls"]
    df = c.fetch({"path": "/v1/link", "pagination": {"type": "link_header"}})
    assert df.height == 23 and STATE["token_calls"] == calls + 1  # one token for all pages
    STATE["tokens"].clear()  # the API revokes it: DataBridge gets a new token once and carries on
    assert c.fetch({"path": "/v1/link"}).height == 10 and STATE["token_calls"] == calls + 2
    bad = conn(base_url, auth_type="oauth2", token_url=f"{base_url}/oauth/token", client_id="cid",
               client_secret="nope")
    with pytest.raises(RestError, match="refused the client credentials"):
        bad.fetch({"path": "/v1/link"})


def test_oauth2_client_secret_as_basic(base_url):
    # our fake token endpoint only reads the form, so basic-auth style must fail there: proves it's not in the body
    c = conn(base_url, auth_type="oauth2", token_url=f"{base_url}/oauth/token", client_id="cid",
             client_secret="csecret", client_auth="basic")
    with pytest.raises(RestError, match="refused"):
        c.fetch({"path": "/v1/link"})


# ------------------------------------------------------------------ formats


def test_json_autodetect_flatten_explode_and_mixed_types(base_url):
    c = conn(base_url)
    df = c.fetch({"path": "/v1/nested"})  # finds the only list of objects ("orders") by itself
    assert df.height == 2 and df["customer.address.city"].to_list() == ["Cary", "Apex"]
    assert df["tags"].to_list() == ['["a", "b"]', "[]"]  # lists kept as JSON text
    assert df.schema["code"] == pl.Utf8  # 7 and "B-7" in one column -> text, not an error
    rows = c.fetch({"path": "/v1/nested", "records_path": "orders", "explode": "lines"})
    assert rows.height == 3 and rows["lines.sku"].to_list() == ["X1", "X2", None]
    assert rows["customer.name"].to_list() == ["Ann", "Ann", "Bo"]


def test_csv_and_xml(base_url):
    c = conn(base_url)
    df = c.fetch({"path": "/v1/csv"})
    assert df.columns == ["id", "name", "amount"] and df["amount"].to_list() == ["10.5", None]
    x = c.fetch({"path": "/v1/xml", "records_path": ".//order"})
    assert x.height == 2 and x["@id"].to_list() == ["1", "2"] and x["ship.city"].to_list() == ["Cary", "Apex"]
    assert x["customer"].to_list() == ["Ann", "Bo"]
    with pytest.raises(RestError, match="DOCTYPE"):
        c.fetch({"path": "/v1/xml-bomb", "records_path": "."})


def test_columns_keep_only_what_is_needed(base_url):
    c = conn(base_url, auth_type="api_key", api_key="k1")
    df = c.fetch({"path": "/v1/orders", "records_path": "data.items", "columns": ["id", "customer.name", "gone"]})
    assert df.columns == ["id", "customer.name", "gone"] and df["gone"].null_count() == df.height


def test_post_body_and_templates(base_url):
    with pytest.raises(RestError, match="POST is turned off"):  # read-only unless an admin allows POST
        conn(base_url).fetch({"method": "POST", "path": "/v1/search", "body": "{}"})
    c = conn(base_url, allow_post="yes")
    df = c.fetch({"method": "POST", "path": "/v1/search", "body": '{"status": "closed"}'})
    assert set(df["status"].to_list()) == {"closed"} and df.height == 7
    c.fetch({"path": "/v1/since", "params": {"since": "{{days_ago:7}}"}})
    assert STATE["since"] == (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    last = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    c.fetch({"path": "/v1/since", "params": {"since": "{{last_refresh}}"}, "_last_refresh": last})
    assert STATE["since"] == "2026-01-02T03:04:05+00:00"
    with pytest.raises(RestError, match="Unknown placeholder"):
        c.fetch({"path": "/v1/since", "params": {"since": "{{tomorrow}}"}})


# ------------------------------------------------------------------ safety and resilience


def test_requests_stay_on_the_connection_server(base_url):
    c = conn(base_url, auth_type="api_key", api_key="k1", api_key_in="query", api_key_name="api_key")
    assert c.fetch({"path": "/v1/redirect-in"}).height == 2  # same-server redirect: followed
    with pytest.raises(RestError, match="redirected to another server"):
        c.fetch({"path": "/v1/redirect-out"})
    with pytest.raises(RestError, match="only go to"):
        c.fetch({"path": "/v1/evil-next", "pagination": {"type": "next_url", "next_path": "links.next"}})
    with pytest.raises(RestError, match="only go to"):
        c.fetch({"path": "http://evil.example/data"})
    assert c.url_for("//evil.example/data").startswith(base_url + "/")  # scheme-relative: stays on our server


def test_requests_cannot_override_connection_identity(base_url):
    c = conn(base_url, auth_type="api_key", api_key="k1", secret_headers_json='{"apikey": "s"}')
    for header in ("Authorization", "host", "X-API-Key", "APIKEY", "Cookie"):
        with pytest.raises(RestError, match="set by the connection"):
            c.fetch({"path": "/v1/orders", "headers": {header: "x"}})


def test_retry_on_429_and_stop_on_repeating_cursor(base_url):
    STATE["flaky"] = 0
    c = conn(base_url)
    assert c.fetch({"path": "/v1/flaky"}).height == 1 and STATE["flaky"] == 2
    df = c.fetch({"path": "/v1/same-cursor", "pagination": {"type": "cursor", "cursor_path": "next_cursor"}})
    assert df.height == 1  # the same page again stops it (no endless loop, no duplicates)


def test_errors_are_readable_and_never_leak_secrets(base_url):
    c = conn(base_url, auth_type="api_key", api_key="super-secret-key", api_key_in="query")
    with pytest.raises(RestError) as e:
        c.fetch({"path": "/v1/missing"})
    assert "HTTP 404 (check the path): no such thing" in str(e.value) and "super-secret" not in str(e.value)
    dead = conn("http://127.0.0.1:9", auth_type="bearer", bearer_token="super-secret-key")
    with pytest.raises(RestError, match="Could not reach the API") as e:
        dead.fetch({"path": "/x"})
    assert "super-secret" not in str(e.value)
    assert not conn(base_url, auth_type="bearer", bearer_token="x").test().ok is None  # returns a result
    assert "base URL" in str(pytest.raises(RestError, conn, "ftp://x").value)


# ------------------------------------------------------------------ sources, refresh, mapping, schedule


def _connection(base_url, name="Orders API", **cfg):
    return connections.save_connection(name, "rest", {"base_url": base_url, **cfg})


def test_rest_source_lifecycle_refresh_unchanged_changed_and_republish(base_url):
    STATE["version_rows"] = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]
    c = _connection(base_url, name="Versioned API")
    src = sources.create_connector_source("Versioned", c.id, {"path": "/v1/versioned", "junk": 1,
                                                              "_last_refresh": "x"})
    assert src.kind == "rest" and src.object_ref == {"method": "GET", "path": "/v1/versioned"}  # cleaned
    assert [f["name"] for f in src.fields] == ["id", "v"]

    tgt = targets.save_target("Versioned target", [{"name": "id", "type": "integer", "required": True},
                                                   {"name": "v", "type": "string"}])
    m = mappings.create_mapping("Versioned map", src.id, tgt.id)
    mappings.save_spec(m.id, MappingSpec(rules=[{"target": "id", "formula": "[id]"},
                                                {"target": "v", "formula": "UPPER([v])"}]))
    mappings.publish(m.id)

    out = sources.refresh(src.id)  # same data: no new snapshot, nothing republished
    assert out["skipped"] and out["published"] == []
    assert sources.get_source(src.id).last_refresh_status == "unchanged"

    STATE["version_rows"] = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 3, "v": "c"}]
    out = sources.refresh(src.id)
    assert not out["skipped"] and out["rows"] == 3 and out["published"][0]["version"] == 2
    ds = mappings.dataset_for(m.id)
    assert pl.read_parquet(ds.parquet_path)["v"].to_list() == ["A", "B", "C"]

    STATE["version_rows"] = [{"id": 1, "v": "a", "extra": 1}]
    out = sources.refresh(src.id)
    assert out["drift"]["added"] == ["extra"]


def test_edit_request_and_failed_refresh_is_recorded(base_url):
    c = _connection(base_url, name="Edit API", auth_type="api_key", api_key="k1")
    src = sources.create_connector_source("Edit me", c.id, {"path": "/v1/orders", "records_path": "data.items"})
    assert src.fields and sources.load_snapshot(src.id).height == 10
    out = sources.update_request(src.id, {"path": "/v1/orders", "records_path": "data.items",
                                          "pagination": {"type": "page", "size_param": "page_size", "size": 10}})
    assert out["rows"] == 23
    with pytest.raises(RestError):
        sources.update_request(src.id, {"path": "/v1/orders", "method": "DELETE"})
    connections.save_connection("Edit API", "rest", {"base_url": base_url, "auth_type": "api_key",
                                                     "api_key": "wrong"}, c.id)
    with pytest.raises(RestError, match="401"):
        sources.refresh(src.id)
    s = sources.get_source(src.id)
    assert s.last_refresh_status == "failed" and "401" in s.last_refresh_message


def test_secrets_are_encrypted_at_rest(base_url):
    c = _connection(base_url, name="Secret API", auth_type="bearer", bearer_token="tok-very-secret")
    saved = connections.get_connection(c.id)
    assert "bearer_token" not in saved.config and "tok-very-secret" not in (saved.secret or "")
    result = connections.test_connection(c.id)  # the fake API has nothing at "/": reachable is enough
    assert result.ok and "Server reachable" in result.message


def test_schedule_runs_due_sources(base_url):
    STATE["version_rows"] = [{"id": 10, "v": "x"}]
    c = _connection(base_url, name="Scheduled API")
    src = sources.create_connector_source("Scheduled", c.id, {"path": "/v1/versioned"}, refresh_minutes=15)
    assert sources.get_source(src.id).refresh_minutes == 15
    with pytest.raises(ValueError, match="5 minutes"):
        sources.set_schedule(src.id, 1)
    now = datetime.now(timezone.utc)  # creating the source took the first snapshot
    assert src.id not in [s.id for s in sources.due_sources(now + timedelta(minutes=5))]
    later = now + timedelta(minutes=16)
    assert src.id in [s.id for s in sources.due_sources(later)]
    STATE["version_rows"] = [{"id": 10, "v": "y"}]
    ran = dict(scheduler.run_due(later))
    assert ran["Scheduled"] == "1 rows"
    assert dict(scheduler.run_due(later + timedelta(minutes=16)))["Scheduled"] == "unchanged"
    sources.set_schedule(src.id, None)
    assert src.id not in [s.id for s in sources.due_sources(now + timedelta(days=1))]


def test_openapi_browse_lists_get_operations(base_url):
    nodes = conn(base_url).browse()
    paths = {n.name for n in nodes}
    assert {"/v1/orders", "/v1/csv", "/v1/xml"} <= paths and "/oauth/token" not in paths  # GET only


def test_rest_source_via_api_refresh_and_listing(base_url):
    from fastapi.testclient import TestClient

    from databridge.api.runtime import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    client = TestClient(app)
    _, key = endpoints.create_api_key("rest-admin")
    c = _connection(base_url, name="API refresh")
    STATE["version_rows"] = [{"id": 5, "v": "q"}]
    src = sources.create_connector_source("API refreshed", c.id, {"path": "/v1/versioned"})
    listed = client.get("/api/v1/sources", params={"name": "API refreshed"}, headers={"X-API-Key": key}).json()
    assert listed[0]["kind"] == "rest" and listed[0]["load_with"] == f"/api/v1/sources/{src.id}/refresh"
    STATE["version_rows"] = [{"id": 5, "v": "r"}]
    r = client.post(f"/api/v1/sources/{src.id}/refresh", headers={"X-API-Key": key})
    assert r.status_code == 200 and r.json()["rows"] == 1 and r.json()["skipped"] is False


# ------------------------------------------------------------------ company TLS: custom CA and mutual TLS


def test_company_ca_and_mutual_tls(tmp_path):
    import ssl

    from tests.test_tls import _free_port as fp
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec

    from tests.test_tls import _cert, _name, _pem

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert("API CA", _name("API CA"), ca_key, ca_key.public_key(), ca=True)
    srv_key = ec.generate_private_key(ec.SECP256R1())
    srv = _cert("api.local", ca.subject, ca_key, srv_key.public_key(), sans=[x509.DNSName("localhost")])
    cli_key = ec.generate_private_key(ec.SECP256R1())
    cli = _cert("databridge", ca.subject, ca_key, cli_key.public_key(), client=True)
    paths = {}
    for k, v in {"ca": ca, "srv": srv, "srv_key": srv_key}.items():
        (tmp_path / f"{k}.pem").write_bytes(_pem(v))
        paths[k] = str(tmp_path / f"{k}.pem")
    port = fp()
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="error",
                                           ssl_certfile=paths["srv"], ssl_keyfile=paths["srv_key"],
                                           ssl_ca_certs=paths["ca"], ssl_cert_reqs=ssl.CERT_REQUIRED))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        c = _connection(f"https://localhost:{port}", name="TLS API")
        src_ref = {"path": "/v1/csv"}
        with pytest.raises(RestError, match="not trusted"):  # company CA unknown yet
            connections.connector_for(c.id).fetch(src_ref)
        info = connections.set_rest_certificates(c.id, ca_data=_pem(ca))
        assert info["ca"][0]["subject"].endswith("API CA")
        with pytest.raises(RestError):  # trusted now, but the server wants a client certificate
            connections.connector_for(c.id).fetch(src_ref)
        connections.set_rest_certificates(c.id, cert_data=_pem(cli), key_data=_pem(cli_key))
        assert connections.connector_for(c.id).fetch(src_ref).height == 2
        # saving the form again (it doesn't show certificates) keeps them
        connections.save_connection("TLS API", "rest", {"base_url": f"https://localhost:{port}",
                                                        "tls_verify": "custom"}, c.id)
        assert connections.connector_for(c.id).fetch(src_ref).height == 2
        saved = connections.get_connection(c.id)
        assert "client_key_pem" not in saved.config and "BEGIN" not in saved.secret  # key encrypted
        with pytest.raises(ValueError, match="does not belong"):
            connections.set_rest_certificates(c.id, cert_data=_pem(cli), key_data=_pem(srv_key))
    finally:
        server.should_exit = True


# ------------------------------------------------------------------ review regressions


def test_secrets_never_reach_error_text_or_logs(base_url, caplog):
    import logging

    c = conn(base_url, auth_type="bearer", bearer_token="  tok-abc-123-secret \n")  # pasted with spaces
    assert c.cfg.bearer_token == "tok-abc-123-secret"
    with pytest.raises(RestError, match="control character") as e:
        conn(base_url, auth_type="bearer", bearer_token="tok\nX-Evil: 1")
    assert "Evil" not in str(e.value)
    with pytest.raises(RestError) as e:  # the API echoes the credential back in its error message
        c.fetch({"path": "/v1/echo-secret"})
    assert "tok-abc-123-secret" not in str(e.value) and "***" in str(e.value)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING  # no request URLs with keys


def test_decompressed_size_is_limited(base_url, monkeypatch):
    monkeypatch.setattr(rest, "MAX_RESPONSE_BYTES", 1024 * 1024)
    with pytest.raises(RestError, match="larger than"):
        conn(base_url).fetch({"path": "/v1/gzip-bomb"})  # ~9 KB on the wire, 3 MB unpacked


def test_paging_edge_cases(base_url):
    c = conn(base_url)
    df = c.fetch({"path": "/v1/ignores-page", "pagination": {"type": "page"}})
    assert df.height == 5  # same page again -> stop, no 1000 calls of duplicates
    df = c.fetch({"path": "/v1/capped-offset", "pagination": {"type": "offset", "size": 10}})
    assert df.height == 23  # the API's cap (5) is not mistaken for the end
    df = c.fetch({"path": "/v1/zero-pages", "records_path": "items",
                  "pagination": {"type": "page", "start": 0, "total_path": "total_pages"}})
    assert df["page"].to_list() == [0, 1, 2]
    with pytest.raises(RestError, match="more than 5 times"):
        c.fetch({"path": "/v1/loop-redirect"})
    with pytest.raises(RestError, match="HTTP 304"):
        c.fetch({"path": "/v1/not-modified"})
    assert c.fetch({"path": "/v1/bom-csv"}).columns == ["id", "name"]
    bad = conn(base_url, auth_type="oauth2", token_url=f"{base_url}/oauth/weird", client_id="a", client_secret="b")
    with pytest.raises(RestError, match="access_token"):
        bad.fetch({"path": "/v1/link"})


def test_truncation_is_flagged(base_url):
    c = _connection(base_url, name="Truncating API", auth_type="api_key", api_key="k1")
    src = sources.create_connector_source("Truncated", c.id, {
        "path": "/v1/orders", "records_path": "data.items", "max_rows": 15,
        "pagination": {"type": "page", "size_param": "page_size", "size": 10}})
    snap = sources.latest_snapshot(src.id)
    assert snap.row_count == 15 and any("Incomplete" in n for n in snap.notes)


def test_empty_answer_keeps_data_and_columns_and_incremental_append(base_url):
    STATE["maybe_rows"] = [{"id": 1, "v": "a"}]
    c = _connection(base_url, name="Maybe API")
    src = sources.create_connector_source("Maybe", c.id, {"path": "/v1/maybe-empty"})
    STATE["maybe_rows"] = []
    out = sources.refresh(src.id)
    assert out["skipped"] and out["message"] == "no new rows"
    assert [f["name"] for f in sources.get_source(src.id).fields] == ["id", "v"]
    assert conn(base_url).fetch({"path": "/v1/maybe-empty", "columns": ["id"]}).height == 0  # no fake null row

    STATE["changes"] = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]
    inc = sources.create_connector_source("Changes", c.id, {
        "path": "/v1/changes", "params": {"since": "{{last_refresh}}"},
        "incremental": {"mode": "append", "key": ["id"]}})
    STATE["changes"] = [{"id": 2, "v": "B"}, {"id": 3, "v": "c"}]  # 2 changed, 3 is new
    out = sources.refresh(inc.id)
    df = sources.load_snapshot(inc.id).sort("id")
    assert df.to_dicts() == [{"id": 1, "v": "a"}, {"id": 2, "v": "B"}, {"id": 3, "v": "c"}]


def test_failed_first_load_and_bad_edit_leave_nothing_broken(base_url):
    c = _connection(base_url, name="Strict API", auth_type="api_key", api_key="k1")
    with pytest.raises(RestError):
        sources.create_connector_source("Strict", c.id, {"path": "/v1/missing"})
    assert "Strict" not in [s.name for s in sources.list_sources()]  # the name is free to try again
    src = sources.create_connector_source("Strict", c.id, {"path": "/v1/orders", "records_path": "data.items"})
    with pytest.raises(RestError, match="404"):
        sources.update_request(src.id, {"path": "/v1/missing"})
    assert sources.get_source(src.id).object_ref["path"] == "/v1/orders"  # the working request is kept


def test_target_from_source_columns_and_mapping(base_url):
    c = _connection(base_url, name="Target API", auth_type="api_key", api_key="k1")
    src = sources.create_connector_source("For target", c.id, {"path": "/v1/orders", "records_path": "data.items"})
    tgt = targets.target_from_source("From source", src.id)
    assert [(f["name"], f["type"]) for f in tgt.fields] == [
        ("id", "integer"), ("customer_name", "string"), ("customer_city", "string"), ("amount", "float"),
        ("status", "string")]
    m = mappings.create_mapping("From source map", src.id, tgt.id)
    assert {r["target"] for r in m.rules} == {"id", "customer_name", "customer_city", "amount", "status"}
    assert mappings.preview(m.id).stats["rows_out"] == 10
    only = targets.target_from_source("Two fields", src.id, ["id", "status"])
    assert [f["name"] for f in only.fields] == ["id", "status"]
    assert targets.field_name("Order Date") == "order_date" and targets.field_name("1st") == "f_1st"


def test_parallel_pages_keep_order_and_report_timing(base_url):
    c = conn(base_url, parallel_requests=4)
    STATE["slow_peak"] = 0
    started = time.monotonic()
    df = c.fetch({"path": "/v1/slow", "pagination": {"type": "page"}})
    fast = time.monotonic() - started
    assert df["page"].to_list() == [p for p in range(1, 11) for _ in range(5)]  # in order, nothing twice
    assert STATE["slow_peak"] > 1
    assert c.stats.pages == 10 and c.stats.rows == 50 and c.stats.parallel == 4
    assert "10 page(s), 50 rows" in c.stats.summary() and "4 at a time" in c.stats.summary()

    one = conn(base_url, parallel_requests=1)
    time.sleep(0.4)  # let pages asked for past the end finish
    STATE["slow_peak"] = 0
    started = time.monotonic()
    assert one.fetch({"path": "/v1/slow", "pagination": {"type": "page"}}).height == 50
    assert STATE["slow_peak"] == 1 and time.monotonic() - started > fast * 1.8

    cut = conn(base_url, parallel_requests=4).fetch({"path": "/v1/slow", "pagination": {"type": "page"},
                                                    "max_rows": 12})
    assert cut["id"].to_list() == [100, 101, 102, 103, 104, 200, 201, 202, 203, 204, 300, 301]

    # offset paging in parallel, and the refresh message carries the timing
    df = conn(base_url, auth_type="api_key", api_key_name="api_key", api_key_in="query", api_key="k1").fetch(
        {"path": "/v1/offset", "pagination": {"type": "offset", "size": 5}})
    assert df["id"].to_list() == list(range(1, 24))
    src = sources.create_connector_source("Slow pages", _connection(base_url, name="Slow API").id,
                                          {"path": "/v1/slow", "pagination": {"type": "page"}})
    assert "10 page(s), 50 rows" in sources.get_source(src.id).last_refresh_message


def test_single_object_unreachable_server_feedback_and_cancel(base_url, monkeypatch):
    # one object (like https://dummyjson.com/test) under every paging style: exactly one row, quickly
    for pagination in (None, {"type": "page"}, {"type": "offset", "size": 100}, {"type": "cursor"}):
        ref = {"path": "/test", **({"pagination": pagination} if pagination else {})}
        assert conn(base_url).fetch(ref).to_dicts() == [{"status": "ok", "method": "GET"}], pagination

    # a server that can't be reached fails in seconds with a hint, not after minutes of retries
    monkeypatch.setattr(rest, "CONNECT_TIMEOUT", 0.2)

    def no_connect(*a, **k):
        raise httpx.ConnectTimeout("timed out")

    c = conn("https://unreachable.example")
    said = []
    c.notice = said.append
    monkeypatch.setattr(c, "_send_timed", no_connect)
    started = time.monotonic()
    with pytest.raises(RestError, match="Could not connect to unreachable.example.*internet access"):
        c.fetch({"path": "/test"})
    assert time.monotonic() - started < 5
    assert said[0] == "Calling unreachable.example..." and "trying once more" in said[1]

    # busy answers say what they are waiting for
    STATE["flaky"] = 0
    c = conn(base_url)
    said = []
    c.notice = said.append
    c.fetch({"path": "/v1/flaky"})
    assert any("busy (HTTP" in t for t in said)

    # cancel stops between pages, and a cancelled Add-as-source leaves nothing behind
    class Progress:
        cancel = threading.Event()

        def __call__(self, pages, rows):
            if pages >= 2:
                self.cancel.set()

        notice = staticmethod(lambda text: None)

    connection = _connection(base_url, name="Cancel API")
    with pytest.raises(RestError, match="Cancelled"):
        sources.create_connector_source("Cancelled source", connection.id,
                                        {"path": "/v1/slow", "pagination": {"type": "page"}},
                                        refresh_minutes=15, progress=Progress())
    assert not [s for s in sources.list_sources() if s.name == "Cancelled source"]

    class LateCancel:  # cancelled once every page is read: still nothing is added
        cancel = threading.Event()

        def __call__(self, pages, rows):
            self.cancel.set()  # reported after the only page: the fetch has no more checks to hit

        notice = staticmethod(lambda text: None)

    with pytest.raises(Exception, match="Cancelled"):
        sources.create_connector_source("Late cancel", connection.id, {"path": "/test"}, progress=LateCancel())
    assert not [s for s in sources.list_sources() if s.name == "Late cancel"]
    src = sources.create_connector_source("Scheduled after first load", connection.id, {"path": "/test"},
                                          refresh_minutes=15)
    assert src.refresh_minutes == 15


def test_openapi_lookup_is_a_quick_probe(base_url):
    STATE["busy_calls"] = 0
    started = time.monotonic()
    assert conn(f"{base_url}/busy/").browse() == []  # a rate-limited API: one call, no 20 s waits
    assert time.monotonic() - started < 3 and STATE["busy_calls"] == 1
    assert conn(base_url).browse()  # the fake API's own openapi.json is still found


def test_delete_source_removes_snapshots_and_refuses_while_mapped(base_url):
    connection = _connection(base_url, name="Delete API")
    src = sources.create_connector_source("To delete", connection.id, {"path": "/test"})
    folder = sources.latest_snapshot(src.id).parquet_path
    target = targets.target_from_source("To delete target", src.id)
    m = mappings.create_mapping("To delete map", src.id, target.id)
    with pytest.raises(ValueError, match="used by the mapping"):
        sources.delete_source(src.id)
    mappings.delete_mapping(m.id)
    sources.delete_source(src.id)
    assert src.id not in [s.id for s in sources.list_sources()]
    assert not Path(folder).exists()


class FakePanic(BaseException):
    """Like pyo3's PanicException (e.g. Polars without numpy): a BaseException, not an Exception."""


def test_fingerprint_needs_no_numpy_and_a_native_panic_never_hangs_the_dialog(base_url, monkeypatch):
    import array
    import asyncio
    import hashlib

    df = pl.DataFrame({"id": [1, 2, 3], "name": ["a", None, "c"]}).slice(1)  # a slice: non-zero Arrow offset
    h = hashlib.sha256(repr([(c, str(t)) for c, t in df.schema.items()]).encode())
    h.update(array.array("Q", df.hash_rows(seed=0, seed_1=1, seed_2=2, seed_3=3).to_list()).tobytes())
    assert sources.frame_hash(df) == h.hexdigest()  # same bytes numpy gave: old fingerprints still match

    # a panic during the first snapshot: no half-made source, and the dialog shows an error instead of spinning
    def panic(*a, **k):
        raise FakePanic("Failed to access NumPy array API capsule")

    monkeypatch.setattr(sources, "frame_hash", panic)
    connection = _connection(base_url, name="Panic API")
    with pytest.raises(FakePanic):
        sources.create_connector_source("Panicked", connection.id, {"path": "/test"})
    assert not [s for s in sources.list_sources() if s.name == "Panicked"]

    from databridge.ui import common

    shown = {}

    class Status:
        def busy(self, text):
            shown["busy"] = text

        def error(self, text):
            shown["error"] = text

    asyncio.run(common.run_in_dialog(Status(), [], "Working...",
                                     lambda: sources.create_connector_source("Panicked", connection.id,
                                                                             {"path": "/test"}),
                                     lambda result: shown.setdefault("success", result)))
    assert "FakePanic" in shown["error"] and "success" not in shown


def test_upload_with_a_taken_name_is_a_clear_error_and_leaves_nothing_behind():
    from databridge.ingest.sheet_profile import SheetProfile

    csv = b"id,name\n1,Ann\n"
    sources.create_upload_source("Employees dup", "employees.csv", csv, SheetProfile())
    before = len(sources.list_sources())
    with pytest.raises(ValueError, match="already exists"):
        sources.create_upload_source("Employees dup", "employees.csv", csv, SheetProfile())
    with pytest.raises(Exception):
        sources.create_upload_source("Broken upload", "broken.xlsx", b"not a workbook", SheetProfile())
    assert len(sources.list_sources()) == before


def test_file_connection_paths_are_forgiving_and_errors_readable(tmp_path):
    folder = tmp_path / "HR files"
    folder.mkdir()
    (folder / "employees.csv").write_text("id,name\n1,Ann\n")
    # quoted Windows-style "Copy as path" of the file itself: folder + that file
    quoted = '"' + str(folder / "employees.csv") + '"'
    c = connections.save_connection("Local HR", "filesystem", {"protocol": "file", "root": quoted})
    assert c.config["root"] == folder.as_posix() and c.config["file_pattern"] == "employees.csv"
    nodes = connections.browse(c.id)
    assert [n.name for n in nodes] == ["employees.csv"]
    with pytest.raises(ValueError, match="was not found on the DataBridge server"):
        connections.save_connection("Typo", "filesystem", {"protocol": "file", "root": str(tmp_path / "nope")})
    with pytest.raises(ValueError, match=r"^Root: "):
        connections.save_connection("Blank", "filesystem", {"protocol": "file", "root": "  "})
    from databridge.connectors.filesystem import FileSystemConfig, FileSystemConnector
    assert FileSystemConfig(protocol="smb", root=r"share\HR\exports").root == "share/HR/exports"
    conn_ = FileSystemConnector({"protocol": "file", "root": "C:/Users/S/HR"})
    assert conn_._full("C:/Users/S/HR/a.xlsx") == "C:/Users/S/HR/a.xlsx"
    assert conn_._full("c:/users/s/hr/a.xlsx") == "c:/users/s/hr/a.xlsx"
    assert conn_._full("a.xlsx") == "C:/Users/S/HR/a.xlsx"
