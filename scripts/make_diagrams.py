"""Architecture diagrams for the user guide (SVG sources + PNG renders).

    python scripts/make_diagrams.py [--roboto /path/to/Roboto-Regular.ttf --roboto-bold /path/to/Roboto-Bold.ttf]

Writes databridge/docs/guide/img/<name>.svg and .png (PNG rendered with Playwright's Chromium). The PNGs are what
the in-app guide and the README show; edit this script, not the images.
"""

import argparse
import asyncio
from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "databridge" / "docs" / "guide" / "img"

INK, MUTED = "#1b1b21", "#5f5f6b"
STYLES = {  # fill, stroke, title colour
    "app": ("#eef0fa", "#9fa8da", "#3f4a86"),
    "ui": ("#ffffff", "#515b92", "#3f4a86"),
    "store": ("#e7f4f1", "#7fbfb2", "#0f5e56"),
    "ext": ("#fdf3e3", "#e3b574", "#8a4b0b"),
    "people": ("#f4f4f6", "#b9b9c3", "#3a3a44"),
    "guard": ("#fbeef0", "#e2a3ae", "#8f2438"),
    "plain": ("#ffffff", "#c9cbd6", "#3a3a44"),
}


class Svg:
    def __init__(self, w: int, h: int, title: str):
        self.w, self.h, self.parts = w, h, []
        self.text(24, 38, title, 20, INK, weight=700)

    def text(self, x, y, s, size=13, color=INK, weight=400, anchor="start"):
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" '
                          f'text-anchor="{anchor}">{escape(s)}</text>')

    def box(self, x, y, w, h, title, lines=(), style="plain", r=10, title_size=14, dashed=False, center=False):
        fill, stroke, tcolor = STYLES[style]
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" '
                          f'stroke="{stroke}" stroke-width="1.5"{dash}/>')
        tx, anchor = (x + w / 2, "middle") if center else (x + 14, "start")
        self.text(tx, y + 24, title, title_size, tcolor, 700, anchor)
        for i, line in enumerate(lines):
            self.text(tx, y + 46 + i * 18, line, 12.5, MUTED, 400, anchor)

    def label(self, x, y, s, color=MUTED, size=12, anchor="middle", weight=400):
        self.text(x, y, s, size, color, weight, anchor)

    def arrow(self, points, color="#515b92", dashed=False, both=False, width=1.8):
        d = "M " + " L ".join(f"{x} {y}" for x, y in points)
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        start = ' marker-start="url(#tail)"' if both else ""
        self.parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{dash} '
                          f'marker-end="url(#head)"{start}/>')

    def render(self) -> str:
        defs = ('<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="#515b92"/></marker>'
                '<marker id="tail" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="#515b92"/></marker></defs>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" font-family="Roboto, \'Segoe UI\', Arial, sans-serif">'
                f'<rect width="100%" height="100%" rx="14" fill="#ffffff"/>{defs}{"".join(self.parts)}</svg>')


def overview() -> Svg:
    s = Svg(1280, 700, "DataBridge: system overview")
    # people and systems
    s.label(130, 72, "PEOPLE AND SYSTEMS", MUTED, 11.5, weight=700)
    s.box(30, 86, 200, 70, "Studio users", ["Browser: design, map, publish"], "people")
    s.box(30, 176, 200, 70, "Consumers", ["BI tools, apps, Excel (REST)"], "people")
    s.box(30, 266, 200, 70, "Schedulers", ["Stonebranch UAC, n8n, cron"], "people")
    # proxy
    s.box(280, 150, 150, 120, "HTTPS proxy", ["Traefik (Docker)", "or Nginx (native):", "https + wss, TLS 1.2+"], "plain",
          center=True)
    s.arrow([(230, 121), (255, 121), (255, 180), (280, 180)])
    s.arrow([(230, 211), (280, 211)])
    s.arrow([(230, 301), (255, 301), (255, 240), (280, 240)])
    # app container
    x0, bw, gap = 487, 160, 8
    s.box(470, 60, 530, 620, "DataBridge  (one Python process)", [], "app", r=14, title_size=15)
    s.box(x0, 100, 244, 78, "Studio UI", ["Flet: pages, Mapping Studio,", "workflow canvas"], "ui")
    s.box(x0 + 252, 100, 244, 78, "Broker API", ["REST /api/v1 (X-API-Key)", "wss:// streaming API: events, rows"], "ui")
    s.arrow([(430, 190), (450, 190), (450, 139), (x0, 139)])
    s.arrow([(430, 230), (460, 230), (460, 196), (x0 + 374, 196), (x0 + 374, 178)])
    s.label(735, 224, "SERVICES", MUTED, 11.5, weight=700)
    services = [
        ("Ingest", ["sheet profiles, type", "detection, drift"]),
        ("Connectors", ["files, databases, REST", "APIs; schedule, Explorer"]),
        ("Mapping engine", ["row steps, formulas", "→ Polars, checks"]),
        ("Publish", ["versioned datasets,", "reject reports"]),
        ("Endpoint queries", ["filters, paging (DuckDB)", "JSON / CSV / XLSX"]),
        ("Auth & security", ["roles, audit log, wss", "only, origin checks"]),
        ("AI gateway", ["keys, catalog, TLS,", "budgets, usage ledger"]),
        ("AI workflows", ["nodes, runs, review,", "REST trigger"]),
        ("Guardrails", ["PII masking, injection", "checks, output checks"]),
    ]
    for i, (title, lines) in enumerate(services):
        col, row = i % 3, i // 3
        style = "guard" if title == "Guardrails" else "plain"
        s.box(x0 + col * (bw + gap), 236 + row * 104, bw, 92, title, lines, style, title_size=13)
    s.box(x0, 556, 3 * bw + 2 * gap, 104, "Background", [
        "Scheduled source refreshes: REST APIs, tables, SQL, files (only changes kept)",
        "Company model health checks, certificate and outage alerts (Slack/Teams)",
        "Company LLM config file applied at every start"], "plain", title_size=13)
    # storage
    sx, sw = 1040, 220
    s.label(sx + sw / 2, 72, "STORAGE", MUTED, 11.5, weight=700)
    s.box(sx, 86, sw, 82, "Metadata database", ["PostgreSQL (Docker) or SQLite:", "mappings, users, audit, runs"],
          "store")
    s.box(sx, 184, sw, 100, "Data folder", ["Parquet snapshots & datasets,", "reject reports, AI run files,",
                                            "encryption key"], "store")
    s.arrow([(1000, 127), (sx, 127)])
    s.arrow([(1000, 234), (sx, 234)])
    # external
    s.label(sx + sw / 2, 322, "EXTERNAL SYSTEMS", MUTED, 11.5, weight=700)
    s.box(sx, 336, sw, 150, "Data sources", ["Read by Connectors (use", "read-only accounts)",
                                             "Shares, SFTP, S3/Azure/GCS", "Oracle, SQL Server, Postgres…",
                                             "REST APIs: JSON, CSV, XML"], "ext")
    s.box(sx, 506, sw, 150, "LLMs", ["Called by the AI gateway,", "only after the guardrails",
                                     "Company gateway & local", "models (internal network)",
                                     "Cloud providers (external)"], "ext")
    s.arrow([(1000, 411), (sx, 411)], color="#b36b12")
    s.arrow([(1000, 581), (sx, 581)], color="#b36b12")
    return s


def data_flow() -> Svg:
    s = Svg(1200, 360, "How data flows: from a file or table to an endpoint")
    stages = [
        ("Source", "ext", ["Spreadsheet upload, file", "on a share, table/SQL,", "REST API (JSON/CSV/XML)"]),
        ("Ingest", "plain", ["Sheet profile: header,", "skipped rows, types", "(same every version)"]),
        ("Snapshot", "store", ["Versioned Parquet,", "schema drift detected,", "duplicates skipped"]),
        ("Mapping", "app", ["Row steps → rules", "(formulas) → types", "→ data checks"]),
        ("Dataset", "store", ["Published version", "(Parquet) + reject", "report with reasons"]),
        ("Endpoint", "ui", ["Filters, paging,", "JSON / CSV / XLSX,", "API key or public"]),
    ]
    w, gap, x0, y = 168, 30, 24, 90
    for i, (title, style, lines) in enumerate(stages):
        x = x0 + i * (w + gap)
        s.box(x, y, w, 110, title, lines, style, title_size=15)
        s.label(x + w / 2, y - 12, f"{i + 1}", "#515b92", 13, weight=700)
        if i:
            s.arrow([(x - gap, y + 55), (x, y + 55)])
    # consumer + automation notes
    s.box(x0 + 5 * (w + gap), 240, w, 90, "Consumers", ["GET /api/v1/data/<slug>", "BI, apps, Excel"], "people")
    s.arrow([(x0 + 5 * (w + gap) + w / 2, 200), (x0 + 5 * (w + gap) + w / 2, 240)])
    s.box(x0, 240, 3 * w + 2 * gap, 90, "Automation", [
        "New file: POST /api/v1/ingest/<id>  ·  connection: POST /api/v1/sources/<id>/refresh",
        "→ snapshot → every mapping using the source republishes automatically",
        "The reply reports drift, paused mappings and rejected rows (with run_id)"], "people", title_size=14)
    s.arrow([(x0 + 1.5 * w, 240), (x0 + 1.5 * w, 200)], dashed=True)
    s.box(x0 + 3 * (w + gap), 240, 2 * w + gap, 90, "Rejected rows never block good ones", [
        "Failed conversions, missing required values and", "failed checks go to the reject report (View rejects)"],
        "guard", title_size=13)
    return s


def ai_path() -> Svg:
    s = Svg(1200, 400, "How an AI call works: every call passes the guardrails and the gateway")
    s.box(24, 90, 170, 150, "Callers", ["Playground", "AI suggest (mapping)", "Workflow LLM nodes",
                                        "REST: workflow runs"], "people")
    s.box(234, 70, 250, 190, "Guardrails: before sending", [
        "Column classification", "(PII / confidential, follows data)", "Network rules: block confidential",
        "for external models", "PII masking / pseudonymizing", "Prompt-injection check, <data> tags",
        "Row, token and budget limits"], "guard")
    s.box(524, 70, 250, 190, "Model gateway", [
        "Role + model approval (catalog)", "Key: company key, your issued key,", "or your personal cloud key",
        "TLS: company CA, mutual TLS", "Monthly budget per user", "Usage ledger (no text by default)",
        "Audit log"], "app")
    s.box(814, 70, 170, 190, "LLM", ["Company gateway", "Local: Ollama, vLLM,", "LM Studio (internal)", "",
                                     "Cloud providers", "(external)"], "ext")
    s.box(1014, 70, 170, 190, "Guardrails: after", ["JSON schema + retry", "Formula rules", "No invented values",
                                                    "PII leak check", "Banned words, length"], "guard")
    s.arrow([(194, 165), (234, 165)])
    s.arrow([(484, 165), (524, 165)])
    s.arrow([(774, 165), (814, 165)])
    s.arrow([(984, 165), (1014, 165)])
    s.box(234, 290, 950, 80, "Outcome per row", [
        "Pass → result columns  ·  Flag → kept with an ai_review note  ·  Reject → review table with the reason  ·  "
        "Stop → run blocked", "Test runs keep the exact (masked) prompts and replies for review"], "plain",
        title_size=14)
    s.arrow([(1099, 260), (1099, 290)])
    return s


def deployment() -> Svg:
    s = Svg(1200, 510, "Deployment on a Linux server (e.g. Hostinger VPS), deployed from GitHub")
    s.box(24, 70, 250, 110, "GitHub", ["Push to main → Actions run the", "tests, then deploy over SSH",
                                       "to option A or B (repository", "variable DEPLOY_MODE)"], "people")
    s.box(24, 210, 250, 100, "Users & consumers", ["https://databridge.<domain>", "studio + /api/v1, through the", "proxy of either option"], "people")
    # docker panel
    s.box(320, 60, 420, 380, "Option A: Docker", [], "app", r=14, title_size=15)
    s.box(340, 100, 380, 58, "Traefik (existing)", ["https + wss, Let's Encrypt, TLS 1.2+, rate limit on /api"], "plain")
    s.box(340, 180, 180, 110, "app container", ["image from GHCR", "databridge.serve, 1 worker", "volume: /data"], "ui")
    s.box(540, 180, 180, 110, "postgres container", ["internal network only", "volume: postgres-data"], "store")
    s.box(340, 310, 380, 110, "Deploy steps", ["build & push image → copy compose over SSH →",
                                               "pull & up -d → /health check → keep previous",
                                               "image tag for rollback"], "plain", title_size=13)
    s.arrow([(430, 158), (430, 180)])
    s.arrow([(520, 235), (540, 235)])
    # native panel
    s.box(770, 60, 410, 380, "Option B: native (no Docker)", [], "app", r=14, title_size=15)
    s.box(790, 100, 370, 58, "Nginx + certbot", ["https + wss, TLS 1.2+, proxy to 127.0.0.1:8000"], "plain")
    s.box(790, 180, 175, 110, "systemd service", ["databridge.serve (uv venv)", "releases/<id> +", "current symlink"], "ui")
    s.box(985, 180, 175, 110, "Storage", ["SQLite or PostgreSQL", "shared/data, shared/.env", "nightly backup"],
          "store")
    s.box(790, 310, 370, 110, "Deploy steps", ["rsync release → uv sync → switch symlink →",
                                               "restart → /health must report the new release,",
                                               "else automatic rollback"], "plain", title_size=13)
    s.arrow([(880, 158), (880, 180)])
    s.arrow([(965, 235), (985, 235)])
    s.label(160, 345, "Same app either way: configuration in", MUTED, 12)
    s.label(160, 363, ".env (DATABRIDGE_*), company LLMs in", MUTED, 12)
    s.label(160, 381, "an optional YAML config file", MUTED, 12)
    s.box(24, 400, 250, 88, "No proxy?", ["Built-in TLS: DATABRIDGE_TLS_*", "serves https + wss itself,", "optional mutual TLS"], "plain",
          title_size=13)
    return s


def workflow_triage() -> Svg:
    """The ticket triage workflow of the guide's examples (docs/guide/26-workflow-examples.md)."""
    s = Svg(1280, 430, "Example workflow: triage support tickets")
    y, h = 96, 132
    s.box(24, y, 190, h, "Tickets", ["Dataset rows", "Source: Support tickets", "filter: note not blank"], "store")
    s.box(254, y, 190, h, "Prepare", ["Transform", "short_note =", "LEFT([note], 800)", "keep LEN(note) >= 10"], "app")
    s.box(484, y, 250, h, "Classify ticket", ["LLM · one call per row", "sends: ticket_id, product,", "channel, short_note",
                                              "JSON: category, urgency,", "summary, ticket_ref"], "ext")
    s.box(774, y, 170, h, "Urgent?", ["Router", "[urgency] >= 4"], "app")
    s.box(1004, 60, 252, 96, "Urgent tickets", ["Output dataset", "new snapshot each run"], "ui")
    s.box(1004, 196, 252, 96, "Other tickets", ["Output dataset", "new snapshot each run"], "ui")
    mid = y + h // 2
    for x1, x2 in ((214, 254), (444, 484), (734, 774)):
        s.arrow([(x1, mid), (x2, mid)])
    s.arrow([(944, mid - 20), (974, mid - 20), (974, 108), (1004, 108)], color="#2e7d32")
    s.arrow([(944, mid + 20), (974, mid + 20), (974, 244), (1004, 244)], color="#e65100")
    s.label(990, 98, "yes", "#2e7d32", 12, weight=700)
    s.label(990, 262, "no", "#e65100", 12, weight=700)
    # rows flowing, under the chain
    s.label(119, y + h + 26, "180 rows", MUTED, 12.5)
    s.label(349, y + h + 26, "176 rows (+ short_note)", MUTED, 12.5)
    s.label(609, y + h + 26, "176 rows (+ 4 answer columns)", MUTED, 12.5)
    s.label(859, y + h + 26, "31 yes · 145 no", MUTED, 12.5)
    s.box(484, 290, 250, 116, "Guardrails on this node", ["email, customer: PII, not sent", "rule: urgency 1 to 5",
                                                          "ticket_ref must = ticket_id", "injection: flag"], "guard",
          title_size=13)
    s.arrow([(609, 290), (609, y + h + 36)], dashed=True)
    s.box(24, 290, 420, 116, "Review table (every run)", ["Flagged rows: kept, with an ai_review note",
                                                           "Rejected rows: left the flow, with node and reason",
                                                           "Nothing disappears silently"], "plain", title_size=13)
    return s


DIAGRAMS = {"architecture-overview": overview, "architecture-data-flow": data_flow,
            "architecture-ai": ai_path, "architecture-deployment": deployment, "workflow-triage": workflow_triage}


async def render_png(svgs: dict[str, str], fonts: str) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(device_scale_factor=2)
        for name, svg in svgs.items():
            await page.set_content(f"<html><head><style>{fonts} body{{margin:0;background:#fff}}</style></head>"
                                   f"<body>{svg}</body></html>")
            await page.wait_for_timeout(200)
            await page.locator("svg").screenshot(path=str(OUT / f"{name}.png"), omit_background=False)
        await browser.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roboto", help="Roboto-Regular.ttf (optional; embeds the studio's font when rendering)")
    ap.add_argument("--roboto-bold", help="Roboto-Bold.ttf")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    fonts = ""
    if args.roboto and args.roboto_bold:
        fonts = (f"@font-face{{font-family:Roboto;src:url('file://{args.roboto}');font-weight:400}}"
                 f"@font-face{{font-family:Roboto;src:url('file://{args.roboto_bold}');font-weight:700}}")
    svgs = {}
    for name, build in DIAGRAMS.items():
        svgs[name] = build().render()
        (OUT / f"{name}.svg").write_text(svgs[name], encoding="utf-8")
    asyncio.run(render_png(svgs, fonts))
    print("written to", OUT)


if __name__ == "__main__":
    main()
