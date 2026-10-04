"""The in-app user guide: Markdown topics in databridge/docs/guide, filtered by role, with generated sections.

Each topic file starts with front matter:

    ---
    title: Mapping Studio
    icon: COMPARE_ARROWS          # a flet Icons name
    permission: view              # only roles with this permission see the topic
    pages: [mappings, studio]     # opening Documentation from these pages shows this topic
    keywords: map, arrows, ...    # extra search terms
    ---

Placeholders filled at render time (so reference material can't go stale):
    {{functions}}  formula functions, from the formula engine
    {{settings}}   server settings (environment variables), from the settings model
    {{api_base}}   this server's public URL
    {{api_host}}   the same without the scheme (for wss:// examples)

Links in topics: [text](guide:<slug>) opens another topic, [text](app:<page>) opens a page of the tool.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from databridge.config import Settings, settings
from databridge.core.auth import can

GUIDE_DIR = Path(__file__).resolve().parents[1] / "docs" / "guide"

SETTING_HELP: dict[str, str] = {
    "data_dir": "Folder for snapshots, datasets, uploads and (with SQLite) the database",
    "database_url": "SQLAlchemy URL of the metadata database; blank = SQLite file in the data folder",
    "secret_key": "Encryption key for stored passwords and API keys; blank = generated into the data folder. Keep it safe",
    "max_upload_mb": "Largest file that can be uploaded",
    "preview_rows": "Rows shown in previews",
    "default_page_size": "Rows per page for endpoints that don't set one",
    "max_page_size": "Largest page_size a caller may request",
    "public_base_url": "The address users and consumers use (shown in endpoint URLs and this guide)",
    "no_cdn": "Serve the studio's web assets from the server instead of a CDN (closed networks)",
    "session_hours": "How long a normal sign-in lasts",
    "remember_days": "How long 'Keep me signed in' lasts",
    "idle_minutes": "Sign out after this much inactivity",
    "max_failed_logins": "Failed sign-ins before an account locks",
    "lockout_minutes": "How long a locked account stays locked",
    "min_password_length": "Minimum password length",
    "admin_username": "First admin, created at startup only when there are no users",
    "admin_password": "Password of that first admin (must be changed at first sign-in)",
    "ai_enabled": "Turn AI features on or off",
    "ai_request_timeout": "Seconds to wait for a model reply",
    "ai_monthly_token_limit": "Monthly tokens per user (0 = unlimited)",
    "ai_log_content": "Store prompt and reply text in the usage ledger",
    "ai_allow_private_urls": "Allow personal API keys to call private addresses (off = public https only)",
    "ai_ca_bundle": "Extra CA certificate file trusted for all AI calls (company root CA)",
    "ai_config_file": "Company LLM configuration (YAML) applied at every start",
    "ai_health_minutes": "Minutes between health checks of company models (0 = off)",
    "ai_alert_webhook": "Slack or Teams incoming-webhook URL for health alerts",
    "ai_cert_warn_days": "Warn this many days before a certificate expires (AI endpoints and built-in TLS)",
    "source_scheduler": "Run the built-in schedule for sources (off = refresh only by button or API)",
    "require_https": "auto = HTTPS and wss:// required when the public address is https; or true / false",
    "allowed_origins": "Other web origins (https://portal.example.com) allowed to open websockets; same-origin always is",
    "trusted_proxies": "Proxy addresses or networks whose X-Forwarded-Proto/For headers are believed; * = any",
    "hsts_seconds": "Strict-Transport-Security max-age sent over HTTPS (0 = off)",
    "ws_max_connections": "Open studio websockets in total (about one per open browser tab)",
    "stream_max_connections": "Open streaming-API websockets in total (a separate budget from the studio)",
    "ws_max_per_ip": "Open websockets per client address",
    "stream_max_per_key": "Streaming API connections per API key",
    "stream_idle_minutes": "Close streaming connections with no subscription and no messages after this long",
    "bind_host": "Address python -m databridge.serve listens on",
    "bind_port": "Port python -m databridge.serve listens on",
    "tls_cert_file": "Built-in TLS: certificate (PEM, with intermediates). Serves https:// and wss:// without a proxy",
    "tls_key_file": "Built-in TLS: private key (PEM)",
    "tls_key_password": "Password of the private key, if it has one",
    "tls_min_version": "Oldest TLS version accepted: 1.2 or 1.3",
    "tls_client_ca_file": "CA that issues client certificates (mutual TLS)",
    "tls_client_cert": "Client certificates: none, optional or required (needs the CA file)",
}


@dataclass
class Topic:
    slug: str
    title: str
    icon: str
    permission: str
    pages: list[str]
    keywords: list[str]
    body: str
    order: int = 0
    headings: list[str] = field(default_factory=list)


def _parse(path: Path) -> Topic:
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if m:
        meta = yaml.safe_load(m.group(1)) or {}
        body = text[m.end():]
    order, _, slug = path.stem.partition("-")
    keywords = meta.get("keywords") or ""
    return Topic(slug=slug, title=meta.get("title") or slug, icon=meta.get("icon") or "ARTICLE",
                 permission=meta.get("permission") or "view", pages=list(meta.get("pages") or []),
                 keywords=[k.strip() for k in (keywords.split(",") if isinstance(keywords, str) else keywords)],
                 body=body.strip(), order=int(order) if order.isdigit() else 999,
                 headings=re.findall(r"^#{2,3} (.+)$", body, re.M))


@lru_cache(maxsize=1)
def _all() -> tuple[Topic, ...]:
    return tuple(sorted((_parse(p) for p in GUIDE_DIR.glob("*.md")), key=lambda t: t.order))


def topics(role: str) -> list[Topic]:
    """Topics this role may read, in guide order."""
    return [t for t in _all() if can(role, t.permission)]


def get(slug: str, role: str) -> Topic | None:
    return next((t for t in topics(role) if t.slug == slug), None)


def topic_for_page(page_key: str | None, role: str) -> Topic | None:
    """The topic that explains a page of the tool (for 'help for this page')."""
    if not page_key:
        return None
    return next((t for t in topics(role) if page_key in t.pages), None)


def _functions_table() -> str:
    from databridge.engine.formula import FUNCTIONS

    rows = ["| Function | What it does |", "|---|---|"]
    for name in sorted(FUNCTIONS):
        doc = FUNCTIONS[name].doc.replace("|", "/")
        rows.append(f"| `{name}` | {doc} |")
    return "\n".join(rows)


def _settings_table() -> str:
    rows = ["| Variable | Default | What it does |", "|---|---|---|"]
    for name, f in Settings.model_fields.items():
        default = f.default
        shown = "(blank)" if default in ("", None) else str(default)
        if name in ("secret_key", "admin_password"):
            shown = "(blank)"
        rows.append(f"| `DATABRIDGE_{name.upper()}` | {shown} | {SETTING_HELP.get(name, '')} |")
    return "\n".join(rows)


def _page_permission(key: str) -> str | None:
    from databridge.ui.app import App  # the menu decides which pages exist and who may open them

    return next((perm for k, *_, perm in App.NAV if k == key), None)


def render(topic: Topic, role: str | None = None) -> str:
    """Markdown for a topic. With a role, links to topics or pages that role can't open become plain text."""
    body = topic.body
    if role:
        visible = {t.slug for t in topics(role)}

        def link(m):
            text, kind, target = m.group(1), m.group(2), m.group(3)
            if kind == "guide" and target in visible:
                return m.group(0)
            if kind == "app" and (perm := _page_permission(target)) and can(role, perm):
                return m.group(0)
            return text

        body = re.sub(r"\[([^\]]+)\]\((guide|app):([\w-]+)\)", link, body)
    if "{{functions}}" in body:
        body = body.replace("{{functions}}", _functions_table())
    if "{{settings}}" in body:
        body = body.replace("{{settings}}", _settings_table())
    body = body.replace("{{api_host}}", settings.public_base_url.split("://", 1)[-1].rstrip("/"))
    return body.replace("{{api_base}}", settings.public_base_url.rstrip("/"))


def _plain(markdown: str) -> str:
    text = re.sub(r"```.*?```", " ", markdown, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[#>*`|_]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def search(query: str, role: str, limit: int = 12) -> list[tuple[Topic, str]]:
    """Topics matching every word of the query, best first, with a short snippet around the first match."""
    words = [w.lower() for w in re.findall(r"\w+", query or "") if len(w) > 1]
    if not words:
        return []
    results = []
    for t in topics(role):
        plain = _plain(render(t, role))
        hay_title, hay_kw, hay_body = t.title.lower(), " ".join(t.keywords).lower(), plain.lower()
        if not all(w in hay_title or w in hay_kw or w in hay_body for w in words):
            continue
        score = sum(10 * (w in hay_title) + 5 * (w in hay_kw) + min(hay_body.count(w), 5) for w in words)
        score += sum(3 for h in t.headings if any(w in h.lower() for w in words))
        pos = min((hay_body.find(w) for w in words if w in hay_body), default=0)
        start = max(0, pos - 60)
        snippet = ("..." if start else "") + plain[start:start + 180].strip() + "..."
        results.append((score, t, snippet))
    results.sort(key=lambda r: -r[0])
    return [(t, s) for _, t, s in results[:limit]]
