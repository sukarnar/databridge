"""REST API connector: any HTTP endpoint that returns JSON, CSV or XML becomes a source.

A *connection* holds where and how to call: the base URL, authentication (API key, bearer token, basic,
OAuth2 client credentials), extra headers and TLS (company CA, client certificate). Secrets are encrypted.

A *request* (the source's object_ref) says what to fetch:

    {"method": "GET", "path": "/v1/orders", "params": {"status": "open", "since": "{{days_ago:7}}"},
     "headers": {}, "body": "",                      # JSON text, POST only
     "format": "auto",                               # auto | json | csv | xml
     "records_path": "data.items",                   # JSON: where the rows are; XML: element path (.//order)
     "explode": "",                                  # optional nested list to turn into rows (e.g. lines)
     "pagination": {"type": "page", ...},            # see PAGINATION
     "columns": ["id", "customer.name"],             # optional: keep only these columns
     "max_rows": 1000000, "max_pages": 1000}

Values may use {{today}}, {{yesterday}}, {{now}}, {{days_ago:N}} and {{last_refresh}} (time of the previous
successful refresh, empty the first time): handy for incremental loads.

Safety: requests only go to the connection's own origin (scheme, host and port of the base URL), so a "next"
link or a redirect can't send the credentials anywhere else. Paging stops at max_pages / max_rows and when a
cursor or next link repeats. 429 and temporary 5xx answers are retried with back-off (Retry-After honoured).
"""

import csv
from dataclasses import dataclass
import io
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from urllib.parse import urljoin, urlsplit

import httpx
import polars as pl
from pydantic import BaseModel, Field

from databridge.connectors.base import Connector, Node, TestResult

log = logging.getLogger("databridge.rest")
# httpx logs every request URL at INFO, which would include an API key sent as a query parameter
logging.getLogger("httpx").setLevel(logging.WARNING)

PAGINATION = {
    "none": "One call returns everything",
    "page": "Page number (?page=1, 2, 3 ...)",
    "offset": "Offset and limit (?offset=0&limit=100 ...)",
    "cursor": "Cursor or next token from the response",
    "next_url": "Next-page URL in the response body",
    "link_header": "Next-page URL in the Link header",
}
FORMATS = ("auto", "json", "csv", "xml")
RETRY_STATUS = {429, 502, 503, 504}
MAX_RETRIES = 3
CONNECT_TIMEOUT = 10.0
OPENAPI_TIMEOUT = 5.0
MAX_RESPONSE_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_ROWS = 1_000_000
DEFAULT_MAX_PAGES = 1000
FLATTEN_DEPTH = 6
COMMON_RECORD_KEYS = ("data", "items", "results", "records", "value", "rows", "entries", "list", "content")


class RestError(ValueError):
    """A problem the user can fix (shown as is, never contains secrets)."""


class Reply:
    """A fully read response (body size already checked)."""

    def __init__(self, status_code: int, headers, content: bytes, url: str, encoding: str | None):
        self.status_code, self.headers, self.content, self.url, self.encoding = (
            status_code, headers, content, url, encoding)

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)


class FetchStats:
    """Where a fetch spent its time: waiting for the API vs. DataBridge's own processing."""

    def __init__(self):
        self.started = time.monotonic()
        self.pages = self.rows = self.requests = 0
        self.api_seconds = self.processing_seconds = 0.0
        self.parallel = 1
        self._lock = threading.Lock()

    def add_api(self, seconds: float) -> None:
        with self._lock:
            self.requests += 1
            self.api_seconds += seconds

    def add_processing(self, seconds: float) -> None:
        with self._lock:
            self.processing_seconds += seconds

    def summary(self) -> str:
        elapsed = time.monotonic() - self.started
        per_page = self.api_seconds / self.requests if self.requests else 0
        text = f"{self.pages:,} page(s), {self.rows:,} rows in {elapsed:.1f} s"
        if self.requests:
            text += f" (API about {per_page:.2f} s per page"
            text += f", {self.parallel} at a time" if self.parallel > 1 else ""
            text += f"; processing {self.processing_seconds:.1f} s)"
        return text


class RestConfig(BaseModel):
    base_url: str = Field(..., description="e.g. https://api.example.com (requests may only go to this server)")
    auth_type: Literal["none", "api_key", "bearer", "basic", "oauth2"] = Field(
        "none", description="How DataBridge signs in to the API", json_schema_extra={"choices": {
            "none": "None (public API)", "api_key": "API key", "bearer": "Bearer token", "basic": "Basic (user name "
            "and password)", "oauth2": "OAuth2 client credentials"}})
    api_key_name: str = Field("X-API-Key", description="API key: header or query parameter name")
    api_key_in: Literal["header", "query"] = Field("header", description="API key: send in a header or the URL",
                                                   json_schema_extra={"choices": {"header": "Header",
                                                                                  "query": "Query parameter"}})
    api_key: Optional[str] = Field(None, description="API key value")
    bearer_token: Optional[str] = Field(None, description="Bearer token (sent as Authorization: Bearer ...)")
    username: Optional[str] = Field(None, description="Basic auth user name")
    password: Optional[str] = Field(None, description="Basic auth password")
    token_url: Optional[str] = Field(None, description="OAuth2 token URL (client credentials grant)")
    client_id: Optional[str] = Field(None, description="OAuth2 client ID")
    client_secret: Optional[str] = Field(None, description="OAuth2 client secret")
    scope: Optional[str] = Field(None, description="OAuth2 scope (optional)")
    audience: Optional[str] = Field(None, description="OAuth2 audience (optional, e.g. Auth0)")
    client_auth: Literal["body", "basic"] = Field("body", description="OAuth2: how the client ID and secret are sent",
                                                  json_schema_extra={"choices": {"body": "In the form body",
                                                                                 "basic": "As basic auth"}})
    headers_json: str = Field("{}", description='Extra headers as JSON, e.g. {"Accept": "application/json"}')
    secret_headers_json: Optional[str] = Field(None, description='Headers holding secrets, as JSON (stored '
                                               'encrypted), e.g. {"apikey": "..."}')
    allow_post: Literal["no", "yes"] = Field(
        "no", description="Let sources use POST (only for search-style APIs that read data with POST)",
        json_schema_extra={"choices": {"no": "GET only (read-only)", "yes": "Allow POST for searches"}})
    test_path: str = Field("", description="Path called by Test (blank = the base URL)")
    timeout_seconds: int = Field(60, description="Seconds to wait for each response")
    requests_per_minute: int = Field(0, description="Rate limit for this API (0 = no limit)")
    parallel_requests: int = Field(4, ge=1, le=8, description="Pages fetched at the same time with page-number or offset "
                                   "paging (1 to 8; 1 = one after another; cursors and next links are always one by one)")
    tls_verify: Literal["system", "custom", "off"] = Field(
        "system", description="How the server's certificate is checked (upload the company CA below)",
        json_schema_extra={"choices": {"system": "Public and OS-trusted CAs", "custom": "Company CA (uploaded)",
                                       "off": "Don't check (not recommended)"}})
    ca_pem: Optional[str] = Field(None, description="Company CA certificate (PEM), set by uploading")
    client_cert_pem: Optional[str] = Field(None, description="Client certificate (PEM), set by uploading")
    client_key_pem: Optional[str] = Field(None, description="Client key (PEM), set by uploading")


# ------------------------------------------------------------------ small helpers


def origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    port = parts.port or {"http": 80, "https": 443}.get(scheme, 0)
    return scheme, (parts.hostname or "").lower(), port


def get_path(data: Any, path: str) -> Any:
    """Value at a dot path (a.b.0.c); None when missing. Empty path = the data itself."""
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def render_template(value: Any, context: dict[str, Any]) -> Any:
    """Fills {{today}}, {{yesterday}}, {{now}}, {{days_ago:N}} and {{last_refresh}} in strings (recursively)."""
    if isinstance(value, dict):
        return {k: render_template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, context) for v in value]
    if not isinstance(value, str) or "{{" not in value:
        return value
    now = context.get("now") or datetime.now(timezone.utc)

    def sub(m: re.Match) -> str:
        name, _, arg = m.group(1).strip().partition(":")
        if name == "today":
            return now.date().isoformat()
        if name == "yesterday":
            return (now - timedelta(days=1)).date().isoformat()
        if name == "now":
            return now.replace(microsecond=0).isoformat()
        if name == "days_ago":
            return (now - timedelta(days=int(arg or 0))).date().isoformat()
        if name == "last_refresh":
            last = context.get("last_refresh")
            return last.replace(microsecond=0).isoformat() if last else ""
        raise RestError(f"Unknown placeholder {{{{{m.group(1)}}}}} (use today, yesterday, now, days_ago:N, "
                        "last_refresh)")

    return re.sub(r"\{\{([^}]*)\}\}", sub, value)


_encode = json.JSONEncoder(ensure_ascii=False, default=str).encode


def flatten(record: Any, prefix: str = "", depth: int = 0, out: dict | None = None) -> dict[str, Any]:
    """{"a": {"b": 1}, "tags": ["x"]} -> {"a.b": 1, "tags": '["x"]'}; lists stay as JSON text."""
    if out is None:
        out = {}
    if type(record) is dict:
        if not record and prefix:
            out[prefix] = None
        for k, v in record.items():
            key = (prefix + "." + k if type(k) is str else f"{prefix}.{k}") if prefix else (
                k if type(k) is str else str(k))
            t = type(v)
            if t is dict:
                if depth < FLATTEN_DEPTH:
                    flatten(v, key, depth + 1, out)
                else:
                    out[key] = _encode(v)
            elif t is list:
                out[key] = _encode(v)
            else:
                out[key] = v
    else:
        out[prefix or "value"] = _encode(record) if isinstance(record, list) else record
    return out


def explode(records: list[Any], path: str) -> list[dict[str, Any]]:
    """One row per element of the nested list at `path`; parent fields repeat, child fields get the path prefix."""
    rows = []
    for rec in records:
        children = get_path(rec, path) if isinstance(rec, dict) else None
        parent = _without(rec, path) if isinstance(rec, dict) else {"value": rec}
        if not isinstance(children, list) or not children:
            rows.append(parent)
            continue
        for child in children:
            row = dict(parent)
            row[path] = child
            rows.append(row)
    return rows


def _without(rec: dict, path: str) -> dict:
    head, _, rest = path.partition(".")
    if head not in rec:
        return dict(rec)
    copy = dict(rec)
    if rest and isinstance(copy[head], dict):
        copy[head] = _without(copy[head], rest)
    else:
        del copy[head]
    return copy


def to_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Rows -> DataFrame. Polars builds it (fast); rows it can't type are built column by column below."""
    if not rows:
        return pl.DataFrame()
    try:
        df = pl.from_dicts(rows, infer_schema_length=None, strict=True)
        odd = [c for c, t in df.schema.items() if t in (pl.Int128, pl.UInt64, pl.Object, pl.Null)
               or isinstance(t, (pl.List, pl.Struct, pl.Array))]
        if not odd:
            return df
    except Exception:  # noqa: BLE001 - mixed types Polars won't reconcile: use the careful path
        pass
    return _to_frame_by_column(rows)


def _to_frame_by_column(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Mixed types per column: all ints -> Int64, numbers -> Float64, bools -> Boolean, else text."""
    columns: dict[str, None] = {}
    for r in rows:
        for k in r:
            columns.setdefault(k, None)
    data: dict[str, pl.Series] = {}
    for col in columns:
        values = [r.get(col) for r in rows]
        present = [v for v in values if v is not None]
        types = {type(v) for v in present}
        if types and types <= {bool}:
            data[col] = pl.Series(col, values, dtype=pl.Boolean)
        elif types and types <= {int}:
            try:
                data[col] = pl.Series(col, values, dtype=pl.Int64)
            except (OverflowError, pl.exceptions.PolarsError):
                data[col] = pl.Series(col, [None if v is None else str(v) for v in values], dtype=pl.Utf8)
        elif types and types <= {int, float}:
            data[col] = pl.Series(col, [None if v is None else float(v) for v in values], dtype=pl.Float64)
        else:
            data[col] = pl.Series(col, [None if v is None else (
                json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)) for v in values],
                dtype=pl.Utf8)
    return pl.DataFrame(data)


def find_records(data: Any) -> Any:
    """Where the rows probably are when no records path is given."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in COMMON_RECORD_KEYS:
            v = data.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                inner = find_records(v)
                if isinstance(inner, list):
                    return inner
        lists = [v for v in data.values() if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)]
        if len(lists) == 1:
            return lists[0]
        return [data]
    return [data]


def parse_xml(content: bytes, record_path: str) -> list[dict[str, Any]]:
    head = content[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in content[:65536].lower():
        raise RestError("XML with a DOCTYPE or entity declarations is not accepted (unsafe)")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise RestError(f"The response is not valid XML: {e}") from e
    path = record_path.strip() or "./*"
    if path.startswith("/"):
        path = "." + path
    try:
        elements = [root] if path in (".", root.tag) else root.findall(path)
    except SyntaxError as e:
        raise RestError(f"Invalid XML record path {record_path!r}: {e}") from e
    return [_element_record(el) for el in elements]


def _tag(el) -> str:
    return el.tag.split("}", 1)[-1] if isinstance(el.tag, str) else str(el.tag)


def _element_record(el, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in el.attrib.items():
        out[f"{prefix}@{k.split('}', 1)[-1]}"] = v
    children = list(el)
    if not children:
        text = (el.text or "").strip()
        if text or not out:
            out[prefix.rstrip(".") or "value"] = text or None
        return out
    counts: dict[str, int] = {}
    for c in children:
        counts[_tag(c)] = counts.get(_tag(c), 0) + 1
    for c in children:
        name = _tag(c)
        key = f"{prefix}{name}"
        if counts[name] > 1:  # repeated elements: keep as a JSON list
            out.setdefault(key, [])
            out[key].append(_element_record(c, "", depth + 1) if list(c) or c.attrib else (c.text or "").strip())
        elif list(c) and depth < FLATTEN_DEPTH:
            out.update(_element_record(c, key + ".", depth + 1))
        else:
            out.update(_element_record(c, key, depth + 1) if c.attrib else {key: (c.text or "").strip() or None})
    return {k: (json.dumps(v, default=str) if isinstance(v, list) else v) for k, v in out.items()}


def parse_csv(content: bytes, encoding: str | None) -> list[dict[str, Any]]:
    text = content.decode(encoding or "utf-8-sig", errors="replace").lstrip("\ufeff")
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [{k: (v if v != "" else None) for k, v in row.items() if k is not None} for row in reader]


# ------------------------------------------------------------------ OAuth2 token cache

_tokens: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


def clear_token_cache() -> None:
    with _token_lock:
        _tokens.clear()


# ------------------------------------------------------------------ connector


class RestConnector(Connector):
    type_name = "rest"
    label = "REST API (JSON, CSV or XML over HTTP)"
    config_model = RestConfig
    secret_fields = {"api_key", "bearer_token", "password", "client_secret", "secret_headers_json",
                     "client_key_pem"}
    form_exclude = {"ca_pem", "client_cert_pem", "client_key_pem"}  # set through the certificate uploads
    capabilities = {"preview", "request"}

    def __init__(self, config: dict[str, Any], secrets: dict[str, Any] | None = None):
        super().__init__(config, secrets)
        self.cfg: RestConfig
        base = self.cfg.base_url.strip()
        if not re.match(r"^https?://[^/\s]+", base, re.I):
            raise RestError("The base URL must start with http:// or https:// and name a server")
        for name in ("api_key", "bearer_token", "username", "password", "client_id", "client_secret"):
            value = getattr(self.cfg, name)
            if isinstance(value, str):
                value = value.strip()  # pasted values often carry a trailing space or newline
                if re.search(r"[\x00-\x1f\x7f]", value):
                    raise RestError(f"The {name.replace('_', ' ')} contains a line break or control character")
                setattr(self.cfg, name, value)
        for text, what in ((self.cfg.headers_json, "Extra headers"), (self.cfg.secret_headers_json, "Secret headers")):
            for k, v in self._headers_from(text, what).items():
                if re.search(r"[\x00-\x1f\x7f]", k + v):
                    raise RestError(f"{what}: {k} contains a line break or control character")
        self.base = base.rstrip("/") + "/"
        self.origin = origin(self.base)
        self.truncated = ""
        self.progress = None  # optional callable(pages_so_far, rows_so_far)
        self.notice = None  # optional callable(text): what it is doing now (connecting, retrying, waiting)
        self.cancel = None  # optional threading.Event: set it to stop between requests
        self._said_connecting = False
        self.retries = MAX_RETRIES  # a preview uses fewer: someone is waiting for it
        self._lock = threading.Lock()
        self.stats = FetchStats()
        self._client: httpx.Client | None = None
        self._last_request = 0.0

    def protected_headers(self) -> set[str]:
        """Headers a request may not set: they carry the connection's identity or change where it goes."""
        names = {"host", "authorization", "cookie", "proxy-authorization"}
        if self.cfg.auth_type == "api_key" and self.cfg.api_key_in == "header":
            names.add((self.cfg.api_key_name or "x-api-key").lower())
        names |= {k.lower() for k in self._headers_from(self.cfg.secret_headers_json, "Secret headers")}
        return names

    # ------------------------------------------------------------ http plumbing
    def _ssl(self):
        from databridge.ai import tls

        return tls.build_context(verify=self.cfg.tls_verify, ca_pem=self.cfg.ca_pem,
                                 client_cert_pem=self.cfg.client_cert_pem, client_key_pem=self.cfg.client_key_pem)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            read = float(self.cfg.timeout_seconds or 60)
            # A server that can't be reached should fail in seconds, not after the (long) read timeout
            self._client = httpx.Client(verify=self._ssl(), timeout=httpx.Timeout(read, connect=min(CONNECT_TIMEOUT, read)),
                                        follow_redirects=False,
                                        headers={"User-Agent": "DataBridge", "Accept":
                                                 "application/json, text/csv, application/xml;q=0.9, */*;q=0.5"})
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _headers_from(self, text: str | None, what: str) -> dict[str, str]:
        if not text or not text.strip():
            return {}
        try:
            data = json.loads(text)
        except ValueError as e:
            raise RestError(f"{what} must be JSON, e.g. {{\"Accept\": \"application/json\"}}") from e
        if not isinstance(data, dict):
            raise RestError(f"{what} must be a JSON object")
        return {str(k): str(v) for k, v in data.items()}

    def url_for(self, path_or_url: str) -> str:
        """Absolute URL for a path (relative to the base URL) or a URL; refuses other servers."""
        target = (path_or_url or "").strip()
        if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I):
            url = target
        else:
            url = urljoin(self.base, target.lstrip("/")) if target else self.base.rstrip("/")
        if origin(url) != self.origin:
            raise RestError(f"Requests may only go to {self.origin[0]}://{self.origin[1]}:{self.origin[2]} "
                            "(the connection's server), not to another server")
        return url

    def _oauth_token(self, force: bool = False) -> str:
        c = self.cfg
        if not (c.token_url and c.client_id and c.client_secret):
            raise RestError("OAuth2 needs a token URL, client ID and client secret")
        key = json.dumps([c.token_url, c.client_id, c.client_secret, c.scope, c.audience])
        with _token_lock:
            cached = _tokens.get(key)
        if cached and not force and cached[1] > time.time():
            return cached[0]
        form = {"grant_type": "client_credentials"}
        if c.scope:
            form["scope"] = c.scope
        if c.audience:
            form["audience"] = c.audience
        auth = None
        if c.client_auth == "basic":
            auth = httpx.BasicAuth(c.client_id, c.client_secret)
        else:
            form.update(client_id=c.client_id, client_secret=c.client_secret)
        try:
            r = self.client.post(c.token_url, data=form, auth=auth, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise RestError(f"Could not reach the token URL: {self._friendly(e)}") from None
        if r.status_code >= 400:
            raise RestError(f"The token URL refused the client credentials (HTTP {r.status_code}). "
                            "Check the client ID, secret and scope.")
        r = Reply(r.status_code, r.headers, r.content[:1_000_000], str(r.url), r.encoding)
        try:
            body = r.json()
            token = body["access_token"]
            if not isinstance(token, str) or not token:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise RestError("The token URL did not return an access_token") from None
        try:
            lifetime = float(body.get("expires_in") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600.0
        with _token_lock:
            _tokens[key] = (token, time.time() + max(30.0, lifetime - 60))
        return token

    def _auth(self, headers: dict[str, str], params: dict[str, Any], force_token: bool = False):
        c = self.cfg
        if c.auth_type == "api_key":
            if not c.api_key:
                raise RestError("Enter the API key")
            if c.api_key_in == "query":
                params[c.api_key_name or "api_key"] = c.api_key
            else:
                headers[c.api_key_name or "X-API-Key"] = c.api_key
        elif c.auth_type == "bearer":
            if not c.bearer_token:
                raise RestError("Enter the bearer token")
            headers["Authorization"] = f"Bearer {c.bearer_token}"
        elif c.auth_type == "basic":
            if not c.username:
                raise RestError("Enter the user name")
            return httpx.BasicAuth(c.username, c.password or "")
        elif c.auth_type == "oauth2":
            headers["Authorization"] = f"Bearer {self._oauth_token(force_token)}"
        return None

    def _say(self, text: str) -> None:
        if self.notice is not None:
            try:
                self.notice(text)
            except Exception:  # noqa: BLE001 - feedback must never break a fetch
                log.debug("notice callback failed", exc_info=True)

    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise RestError("Cancelled")

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.cancel is not None:
            self.cancel.wait(seconds)
            self._check_cancel()
        else:
            time.sleep(seconds)

    @property
    def host(self) -> str:
        return httpx.URL(self.cfg.base_url).host or self.cfg.base_url

    def _throttle(self) -> None:
        """Spaces requests to the rate limit, also when pages are fetched in parallel."""
        rpm = self.cfg.requests_per_minute or 0
        if rpm <= 0:
            return
        with self._lock:
            slot = max(time.monotonic(), self._last_request + 60.0 / rpm)
            self._last_request = slot
        wait = slot - time.monotonic()
        if wait > 1:
            self._say(f"Keeping to {rpm} requests per minute; next call in {wait:.0f} s...")
        self._sleep(wait)

    def _friendly(self, exc: BaseException) -> str:
        from databridge.ai import tls

        return tls.friendly_error(exc) or f"{type(exc).__name__}: {exc}"

    def secret_values(self) -> list[str]:
        """Every secret this connection could put on the wire (for scrubbing error text)."""
        c = self.cfg
        values = [c.api_key, c.bearer_token, c.password, c.client_secret]
        values += list(self._headers_from(c.secret_headers_json, "Secret headers").values())
        if c.username and c.password:
            import base64

            values.append(base64.b64encode(f"{c.username}:{c.password}".encode()).decode())
        with _token_lock:
            values += [t for t, _ in _tokens.values()]
        return sorted({v for v in values if v and len(v) >= 4}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for v in self.secret_values():
            text = text.replace(v, "***")
        return text

    def error(self, text: str) -> RestError:
        return RestError(self.redact(text))

    def request(self, method: str, url: str, params: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None, body: str | None = None, retries: int | None = None,
                timeout: float | None = None) -> "Reply":
        """One call with auth, retries on 429/5xx and same-server redirects only. Error text never holds secrets."""
        url = self.url_for(url)
        merged = {**self._headers_from(self.cfg.headers_json, "Extra headers"),
                  **self._headers_from(self.cfg.secret_headers_json, "Secret headers"), **(headers or {})}
        content = None
        if body and method.upper() != "GET":
            try:
                json.loads(body)
                merged.setdefault("Content-Type", "application/json")
            except ValueError:
                pass
            content = body.encode("utf-8")
        retries = self.retries if retries is None else retries
        refreshed = False
        attempt = 0
        redirects = 0
        while True:
            self._check_cancel()
            if not self._said_connecting:
                self._said_connecting = True
                self._say(f"Calling {self.host}...")
            q = dict(params or {})
            h = dict(merged)
            auth = self._auth(h, q, force_token=refreshed)
            self._throttle()
            # Merge into the URL's own query (a redirect or next link carries its query; params add to it)
            target = httpx.URL(url).copy_merge_params(q) if q else httpx.URL(url)
            try:
                r = self._send(method.upper(), target, h, content, auth, timeout)
            except httpx.ConnectTimeout:
                if attempt < min(1, retries):  # once more, then give up: waiting longer rarely helps
                    attempt += 1
                    self._say(f"Could not connect to {self.host} yet; trying once more...")
                    self._sleep(1)
                    continue
                raise RestError(
                    f"Could not connect to {self.host} within {self.client.timeout.connect:.0f} seconds. The "
                    "DataBridge server may not be able to reach it: check its internet access, firewall, proxy "
                    "and DNS (in Docker, a missing IPv6 route is a common cause).") from None
            except httpx.TimeoutException:
                if attempt < retries:
                    attempt += 1
                    self._say(f"No answer from {self.host} after {self.cfg.timeout_seconds} s; trying again "
                              f"({attempt + 1} of {MAX_RETRIES + 1})...")
                    self._sleep(min(2 ** attempt, 10))
                    continue
                raise RestError(f"The API did not answer within {self.cfg.timeout_seconds} seconds") from None
            except httpx.HTTPError as e:
                raise self.error(f"Could not reach the API: {self._friendly(e)}") from None
            if 300 <= r.status_code < 400:
                location = r.headers.get("location")
                if r.status_code not in (301, 302, 303, 307, 308) or not location:
                    raise RestError(f"HTTP {r.status_code} from the API with nothing to follow; check the path")
                if redirects >= 5:
                    raise RestError("The API redirected more than 5 times; check the path")
                target = urljoin(r.url, location)
                target = str(httpx.URL(target).copy_remove_param(self.cfg.api_key_name)) \
                    if self.cfg.auth_type == "api_key" and self.cfg.api_key_in == "query" else target
                if origin(target) != self.origin:
                    raise RestError("The API redirected to another server; DataBridge does not follow it "
                                    "(your credentials would go there too). Use that server's URL instead.")
                url, redirects, params = target, redirects + 1, None  # the redirect URL has its own query
                if r.status_code in (301, 302, 303):
                    method, content = "GET", None
                continue
            if r.status_code == 401 and self.cfg.auth_type == "oauth2" and not refreshed:
                refreshed = True  # token revoked or expired early: get a new one once
                continue
            if r.status_code in RETRY_STATUS and attempt < retries:
                attempt += 1
                retry_after = r.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 2 ** attempt
                self._say(f"The API is busy (HTTP {r.status_code}); trying again in {min(delay, 30):.0f} s "
                          f"({attempt + 1} of {MAX_RETRIES + 1})...")
                self._sleep(min(delay, 30))
                continue
            if r.status_code >= 400:
                raise self.error(self._status_message(r))
            return r

    def _send(self, method: str, target: httpx.URL, headers: dict, content: bytes | None, auth,
              timeout: float | None = None) -> "Reply":
        """Sends and reads the body with a size limit counted after decompression (no zip bombs)."""
        started = time.monotonic()
        try:
            if timeout is not None:
                return self._send_timed(method, target, headers, content, auth, timeout)
            return self._send_timed(method, target, headers, content, auth)
        finally:
            self.stats.add_api(time.monotonic() - started)

    def _send_timed(self, method: str, target: httpx.URL, headers: dict, content: bytes | None, auth,
                    timeout: float | None = None) -> "Reply":
        extra = {"timeout": timeout} if timeout is not None else {}
        with self.client.stream(method, target, headers=headers, content=content, auth=auth, **extra) as r:
            if 300 <= r.status_code < 400:
                return Reply(r.status_code, r.headers, b"", str(r.url), r.encoding)
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise RestError(f"The response is larger than {MAX_RESPONSE_BYTES // (1024 * 1024)} MB; "
                                    "use pagination or narrower parameters")
                chunks.append(chunk)
            return Reply(r.status_code, r.headers, b"".join(chunks), str(r.url), r.encoding)

    @staticmethod
    def _status_message(r: "Reply") -> str:
        hint = {401: "check the credentials", 403: "the credentials are not allowed to read this",
                404: "check the path", 405: "check the method (GET or POST)", 429: "rate limited; try later or "
                "lower requests per minute"}.get(r.status_code, "")
        detail = ""
        try:
            body = r.json()
            detail = str(body.get("message") or body.get("error_description") or body.get("error") or
                         body.get("detail") or "")[:200] if isinstance(body, dict) else ""
        except ValueError:
            detail = r.text[:200].strip() if r.headers.get("content-type", "").startswith("text/plain") else ""
        return f"HTTP {r.status_code}" + (f" ({hint})" if hint else "") + (f": {detail}" if detail else "")

    # ------------------------------------------------------------ parsing
    @staticmethod
    def detect_format(r: "Reply", wanted: str) -> str:
        if wanted in ("json", "csv", "xml"):
            return wanted
        ctype = r.headers.get("content-type", "").lower()
        if "json" in ctype:
            return "json"
        if "csv" in ctype or "text/tab-separated" in ctype:
            return "csv"
        if "xml" in ctype:
            return "xml"
        head = r.content[:200].lstrip()
        if head[:1] in (b"{", b"["):
            return "json"
        if head[:1] == b"<":
            return "xml"
        return "csv"

    def records_of(self, r: "Reply", ref: dict[str, Any]) -> tuple[list[dict[str, Any]], Any]:
        """(rows as flat dicts, parsed JSON body or None)."""
        fmt = self.detect_format(r, ref.get("format") or "auto")
        path = (ref.get("records_path") or "").strip()
        if fmt == "json":
            try:
                body = r.json()
            except ValueError:
                raise RestError("The response is not valid JSON (set the format, or check the path)") from None
            data = get_path(body, path) if path else find_records(body)
            if path and data is None:
                raise RestError(f"Records path {path!r} not found in the response")
            records = data if isinstance(data, list) else [data]
            if ref.get("explode"):
                records = explode(records, ref["explode"])
            return [flatten(rec) for rec in records], body
        if fmt == "xml":
            return parse_xml(r.content, path), None
        return parse_csv(r.content, r.encoding), None

    # ------------------------------------------------------------ paging
    def pages(self, ref: dict[str, Any], limit: int | None = None) -> Iterator[list[dict[str, Any]]]:
        """Yields the rows of each page, following the request's pagination, until done or a limit is hit.

        Page-number and offset paging fetch several pages at once (parallel_requests); cursors and next links
        depend on the previous page and are fetched one by one.
        """
        context = {"last_refresh": ref.get("_last_refresh"), "now": datetime.now(timezone.utc)}
        method = (ref.get("method") or "GET").upper()
        if method not in ("GET", "POST"):
            raise RestError("Method must be GET or POST")
        if method == "POST" and self.cfg.allow_post != "yes":
            raise RestError("POST is turned off for this connection. An admin can allow it (Connections > edit > "
                            "Allow POST) for APIs that search with POST; DataBridge never needs it to read data.")
        path = render_template(ref.get("path") or "", context)
        params = {str(k): v for k, v in render_template(dict(ref.get("params") or {}), context).items()
                  if v not in (None, "")}
        headers = {str(k): str(v) for k, v in render_template(dict(ref.get("headers") or {}), context).items()}
        blocked = [k for k in headers if k.lower() in self.protected_headers()]
        if blocked:
            raise RestError(f"Header {blocked[0]} is set by the connection, not by a request")
        body = render_template(ref.get("body") or "", context)
        pg = dict(ref.get("pagination") or {"type": "none"})
        kind = pg.get("type") or "none"
        if kind not in PAGINATION:
            raise RestError(f"Unknown pagination {kind!r}")
        job = _Job(method=method, url=self.url_for(path), params=params, headers=headers, body=body, ref=ref,
                   pg=pg, kind=kind, max_rows=int(limit or ref.get("max_rows") or DEFAULT_MAX_ROWS),
                   max_pages=int(ref.get("max_pages") or DEFAULT_MAX_PAGES), size=int(pg.get("size") or 0),
                   first_page=int(pg["start"]) if pg.get("start") not in (None, "") else 1,
                   first_offset=int(pg.get("start_offset") or 0))
        self.truncated = ""
        self.stats = FetchStats()
        parallel = max(1, min(int(self.cfg.parallel_requests or 1), 8))
        if kind in ("page", "offset") and parallel > 1:
            yield from self._numbered_pages(job, parallel)
        else:
            yield from self._sequential_pages(job)

    def _get_page(self, job: "_Job", url: str, query: dict) -> tuple[list[dict[str, Any]], Any, "Reply"]:
        r = self.request(job.method, url, query, job.headers, job.body)
        started = time.monotonic()
        rows, parsed = self.records_of(r, job.ref)
        self.stats.add_processing(time.monotonic() - started)
        return rows, parsed, r

    def _query(self, job: "_Job", index: int, offset: int | None = None) -> dict:
        """Parameters for page `index` (0-based) of page-number or offset paging."""
        q = dict(job.params)
        pg = job.pg
        if job.kind == "page":
            q[pg.get("param") or "page"] = job.first_page + index
            if job.size and pg.get("size_param"):
                q[pg["size_param"]] = job.size
        elif job.kind == "offset":
            q[pg.get("param") or "offset"] = job.first_offset + index * job.size if offset is None else offset
            if job.size:
                q[pg.get("size_param") or "limit"] = job.size
        return q

    @staticmethod
    def _page_key(rows: list[dict[str, Any]]) -> tuple:
        """Cheap identity of a page, to notice an API that returns the same page whatever is asked."""
        return (len(rows), json.dumps(rows[0], sort_keys=True, default=str),
                json.dumps(rows[-1], sort_keys=True, default=str))

    def _emit(self, job: "_Job", rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Rows to yield (cut at max_rows), updating counters; None when nothing is left to take."""
        self._check_cancel()
        room = job.max_rows - job.total_rows
        if room <= 0:
            return None
        taken = rows[:room]
        job.total_rows += len(taken)
        self.stats.pages += 1
        self.stats.rows = job.total_rows
        if len(rows) > room or (job.total_rows >= job.max_rows and job.kind != "none"):
            self.truncated = f"stopped at the maximum of {job.max_rows:,} rows"
        if self.progress is not None:
            self.progress(self.stats.pages, job.total_rows)
        return taken

    def _numbered_pages(self, job: "_Job", parallel: int) -> Iterator[list[dict[str, Any]]]:
        """Page-number / offset paging with up to `parallel` requests in flight, results kept in order."""
        from collections import deque
        from concurrent.futures import ThreadPoolExecutor

        # Offsets can only be computed ahead when the API really returns `size` rows per page: check page 1
        if job.kind == "offset" and not job.size:
            yield from self._sequential_pages(job)
            return
        pool = ThreadPoolExecutor(parallel, thread_name_prefix="rest-page")
        inflight: deque = deque()
        next_index, total_pages, last_key = 0, None, None
        stride_known = job.kind == "page"
        self.stats.parallel = parallel
        try:
            while True:
                want = parallel if stride_known else 1
                while (len(inflight) < want and next_index < job.max_pages
                       and (total_pages is None or next_index < total_pages)):
                    inflight.append((next_index, pool.submit(self._get_page, job, job.url,
                                                             self._query(job, next_index))))
                    next_index += 1
                if not inflight:
                    if next_index >= job.max_pages and (total_pages is None or next_index < total_pages):
                        self.truncated = f"stopped after the maximum of {job.max_pages:,} pages"
                    return
                index, future = inflight.popleft()
                if total_pages is not None and index >= total_pages:
                    return  # asked for before the total was known
                rows, parsed, _ = future.result()
                if index == 0:
                    total = get_path(parsed, job.pg["total_path"]) if job.pg.get("total_path") and parsed is not None \
                        else None
                    if _num(total) is not None:
                        total_pages = _num(total) if job.kind == "page" else -(-_num(total) // job.size)
                    if job.kind == "offset":
                        if len(rows) != job.size:  # the API caps pages: offsets must follow what it returns
                            taken = self._emit(job, rows) if rows else None
                            if taken:
                                yield taken
                            if rows and job.total_rows < job.max_rows:
                                yield from self._sequential_pages(job, start_offset=job.first_offset + len(rows),
                                                                  pages_done=1, last_key=self._page_key(rows))
                            return
                        stride_known = True
                if not rows:
                    return
                key = self._page_key(rows)
                if key == last_key:
                    return
                last_key = key
                taken = self._emit(job, rows)
                if taken:
                    yield taken
                if job.total_rows >= job.max_rows:
                    return
                if job.kind == "offset" and len(rows) < job.size:
                    return  # a short page after full ones: the end
        finally:
            for _, f in inflight:
                f.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

    def _sequential_pages(self, job: "_Job", start_offset: int | None = None, pages_done: int = 0,
                          last_key: tuple | None = None) -> Iterator[list[dict[str, Any]]]:
        kind, pg = job.kind, job.pg
        url, params = job.url, dict(job.params)
        page_index = 0
        offset = job.first_offset if start_offset is None else start_offset
        seen: set[str] = set()
        for n in range(pages_done, job.max_pages):
            if kind == "page":
                q = self._query(job, page_index)
            elif kind == "offset":
                q = self._query(job, 0, offset=offset)
            else:
                q = dict(params)
            rows, parsed, r = self._get_page(job, url, q)
            if kind != "none" and rows:
                key = self._page_key(rows)
                if key == last_key:
                    return
                last_key = key
            if rows:
                taken = self._emit(job, rows)
                if taken:
                    yield taken
            if kind == "none" or not rows or job.total_rows >= job.max_rows:
                return
            total = get_path(parsed, pg["total_path"]) if pg.get("total_path") and parsed is not None else None
            # Only an empty page or a known total ends page/offset paging: a short page may just be the API's
            # own page-size cap (asking for 100, getting 50), not the end.
            if kind == "page":
                if _num(total) is not None and page_index + 1 >= _num(total):
                    return
                page_index += 1
            elif kind == "offset":
                offset += len(rows)
                if _num(total) is not None and offset >= _num(total):
                    return
            elif kind == "cursor":
                cursor = get_path(parsed, pg.get("cursor_path") or "next_cursor") if parsed is not None else None
                if cursor in (None, "", False) or str(cursor) in seen:
                    return
                seen.add(str(cursor))
                params[pg.get("param") or "cursor"] = cursor
            elif kind in ("next_url", "link_header"):
                nxt = (get_path(parsed, pg.get("next_path") or "next") if kind == "next_url" and parsed is not None
                       else _link_next(r.headers.get("link", "")) if kind == "link_header" else None)
                if not nxt or not isinstance(nxt, str):
                    return
                nxt = urljoin(r.url, nxt)
                if nxt in seen:
                    return
                seen.add(nxt)
                url = self.url_for(nxt)  # refuses other servers
                params = {}  # the next link carries its own query
        self.truncated = f"stopped after the maximum of {job.max_pages:,} pages"
        log.warning("REST source %s", self.truncated)

    def fetch(self, ref: dict[str, Any], limit: int | None = None) -> pl.DataFrame:
        rows: list[dict[str, Any]] = []
        for page in self.pages(ref, limit):
            rows.extend(page)
        df = to_frame(rows)
        cols = [c for c in (ref.get("columns") or []) if c]
        if cols and df.height == 0:
            return pl.DataFrame(schema={c: pl.Utf8 for c in cols})
        if cols:
            missing = [c for c in cols if c not in df.columns]
            df = df.select([c for c in cols if c in df.columns])
            for c in missing:  # keep the shape stable; drift detection will point it out
                df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias(c))
            df = df.select(cols)
        return df

    # ------------------------------------------------------------ Connector contract
    def test(self) -> TestResult:
        try:
            r = self.request("GET", self.cfg.test_path or "")
        except RestError as e:
            text = str(e)
            if text.startswith("HTTP ") and not text.startswith(("HTTP 401", "HTTP 403", "HTTP 5")):
                # The server answered and didn't reject the credentials; the test path just isn't an endpoint
                status = re.match(r"HTTP \d+", text).group(0)
                return TestResult(True, f"Server reachable ({status} at the test path; set a test path to check "
                                  "a real endpoint)")
            return TestResult(False, text)
        return TestResult(True, f"Connected: HTTP {r.status_code}, {r.headers.get('content-type', 'no type')}",
                          {"status": r.status_code})

    def browse(self, ref: dict[str, Any] | None = None) -> list[Node]:
        """GET operations from the API's OpenAPI document, when it has one at a usual place."""
        if ref:
            return []
        # A quick look only: one try per place, a short timeout, and no waiting on busy or unreachable servers
        for path in ("openapi.json", "swagger.json", "v3/api-docs", "api-docs", "openapi.yaml"):
            try:
                r = self.request("GET", path, retries=0, timeout=OPENAPI_TIMEOUT)
                spec = r.json() if "yaml" not in path else __import__("yaml").safe_load(r.text)
            except RestError as e:
                if not str(e).startswith(("HTTP 400", "HTTP 404", "HTTP 405", "HTTP 406", "HTTP 410")):
                    # can't connect, timed out, busy (429/5xx), not allowed: stop looking, don't keep the user waiting
                    return []
                continue
            except Exception:  # noqa: BLE001 - not a spec here, try the next usual place
                continue
            if isinstance(spec, dict) and isinstance(spec.get("paths"), dict):
                nodes = []
                for p, ops in sorted(spec["paths"].items()):
                    if isinstance(ops, dict) and "get" in ops:
                        summary = (ops["get"] or {}).get("summary") or ""
                        nodes.append(Node(p, "endpoint", {"method": "GET", "path": p}, False, summary[:60]))
                return nodes[:500]
        return []

    def describe(self, ref: dict[str, Any]) -> list[dict[str, Any]]:
        from databridge.ingest.profiling import profile_fields

        return profile_fields(self.preview(ref, 200))

    def preview(self, ref: dict[str, Any], limit: int = 100) -> pl.DataFrame:
        saved, self.retries = self.retries, min(self.retries, 1)
        try:
            return self.fetch({**ref, "max_pages": min(int(ref.get("max_pages") or 2), 2)}, limit=limit)
        finally:
            self.retries = saved

    def read(self, ref: dict[str, Any], batch_size: int = 50_000) -> Iterator[pl.DataFrame]:
        yield self.fetch(ref)


@dataclass
class _Job:
    """One fetch: the rendered request and its paging state."""
    method: str
    url: str
    params: dict
    headers: dict
    body: str
    ref: dict
    pg: dict
    kind: str
    max_rows: int
    max_pages: int
    size: int
    first_page: int
    first_offset: int
    total_rows: int = 0


def _num(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _link_next(header: str) -> str | None:
    for part in header.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;(.*)', part)
        if m and re.search(r'rel\s*=\s*"?next"?', m.group(2), re.I):
            return m.group(1)
    return None


def parse_lines(text: str, sep: str = "=") -> dict[str, str]:
    """"name=value" (or "Name: value") per line -> dict; for the request builder."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, s, v = line.partition(sep)
        if not s:
            raise RestError(f"Write each line as name{sep}value: {line[:60]}")
        out[k.strip()] = v.strip()
    return out
