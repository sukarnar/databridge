"""Request builder for REST API sources: what to call, where the rows are, how to page through them."""

import json
from collections.abc import Callable
from typing import Any

import flet as ft

from databridge.connectors.rest import FORMATS, PAGINATION, RestError, parse_lines
from databridge.ui.common import DialogStatus, close_dialog, dialog, guarded, mounted, run_in_dialog

W = 660

# pagination type -> fields: (key, label, default, hint)
PAGE_FIELDS: dict[str, list[tuple[str, str, str, str]]] = {
    "page": [("param", "Page parameter", "page", ""), ("size_param", "Page size parameter", "", "e.g. page_size"),
             ("size", "Page size", "", "largest the API allows"), ("start", "First page", "1", ""),
             ("total_path", "Total pages path", "", "e.g. meta.total_pages")],
    "offset": [("param", "Offset parameter", "offset", ""), ("size_param", "Limit parameter", "limit", ""),
               ("size", "Page size", "100", "largest the API allows"), ("total_path", "Total rows path", "", "e.g. total")],
    "cursor": [("cursor_path", "Next cursor in response", "next_cursor", "e.g. meta.next_cursor"),
               ("param", "Cursor parameter", "cursor", "sent back on the next call")],
    "next_url": [("next_path", "Next-page link in response", "next", "e.g. links.next")],
    "link_header": [],
    "none": [],
}
SPEED_TIP = ("Faster refreshes: set the page size to the largest the API allows (fewer calls). Page-number and "
             "offset paging fetch several pages at once (Parallel requests on the connection).")
PLACEHOLDERS = ("Values can use {{today}}, {{yesterday}}, {{now}}, {{days_ago:7}} and {{last_refresh}} (time of the "
                "previous successful refresh, empty the first time) for incremental loads.")


def _lines(d: dict | None, sep: str) -> str:
    return "\n".join(f"{k}{sep}{v}" for k, v in (d or {}).items())


def request_dialog(page: ft.Page, title: str, initial: dict[str, Any] | None,
                   on_done: Callable[[Any], None], action: str = "Preview",
                   show_columns: bool = False, work: Callable[[dict[str, Any]], Any] | None = None,
                   busy_message: str = "Working...") -> None:
    """With `work`, it runs inside the dialog (progress and errors shown there) and on_done gets its result;
    without, the dialog closes and on_done gets the request."""
    ref = dict(initial or {})
    pg = dict(ref.get("pagination") or {"type": "none"})
    method = ft.Dropdown(label="Method", value=ref.get("method", "GET"), width=110, dense=True,
                         options=[ft.DropdownOption(key=m, text=m) for m in ("GET", "POST")])
    path = ft.TextField(label="Path *", value=ref.get("path", ""), dense=True, expand=True,
                        hint_text="/v1/orders", helper="Relative to the connection's base URL")
    params = ft.TextField(label="Query parameters", value=_lines(ref.get("params"), "="), multiline=True,
                          min_lines=2, max_lines=5, dense=True, width=W, hint_text="status=open\nsince={{days_ago:7}}",
                          helper="One name=value per line")
    headers = ft.TextField(label="Headers", value=_lines(ref.get("headers"), ": "), multiline=True, min_lines=1,
                           max_lines=4, dense=True, width=W, hint_text="Accept: application/json",
                           helper="One Name: value per line (secrets belong in the connection, not here)")
    body = ft.TextField(label="Body (JSON)", value=ref.get("body", ""), multiline=True, min_lines=2, max_lines=8,
                        dense=True, width=W, text_style=ft.TextStyle(font_family="monospace", size=12),
                        visible=ref.get("method", "GET") == "POST")
    fmt = ft.Dropdown(label="Response format", value=ref.get("format", "auto"), width=170, dense=True,
                      options=[ft.DropdownOption(key=f, text=f.upper() if f != "auto" else "Detect") for f in FORMATS])
    records = ft.TextField(label="Records path", value=ref.get("records_path", ""), dense=True, expand=True,
                           helper="JSON: data.items (blank = find the list). XML: .//order")
    explode = ft.TextField(label="One row per item of", value=ref.get("explode", ""), dense=True, width=W,
                           helper="Optional nested list, e.g. lines: each line becomes a row with its order's fields")
    pg_type = ft.Dropdown(label="Pagination", value=pg.get("type", "none"), width=W, dense=True,
                          options=[ft.DropdownOption(key=k, text=v) for k, v in PAGINATION.items()])
    pg_fields = ft.Row(wrap=True, spacing=10, run_spacing=10, width=W)
    pg_controls: dict[str, ft.TextField] = {}
    max_rows = ft.TextField(label="Max rows", value=str(ref.get("max_rows", "")), dense=True, width=150,
                            hint_text="1000000", keyboard_type=ft.KeyboardType.NUMBER)
    max_pages = ft.TextField(label="Max pages", value=str(ref.get("max_pages", "")), dense=True, width=150,
                             hint_text="1000", keyboard_type=ft.KeyboardType.NUMBER)
    inc = dict(ref.get("incremental") or {})
    mode = ft.Dropdown(label="Each refresh", value=inc.get("mode", "replace"), width=300, dense=True,
                       options=[ft.DropdownOption(key="replace", text="Replaces the data"),
                                ft.DropdownOption(key="append", text="Adds to the data (incremental)")])
    key = ft.TextField(label="Match rows on", value=", ".join(inc.get("key") or []), dense=True, width=350,
                       hint_text="id", helper="Key columns: a new row replaces the old one",
                       visible=inc.get("mode") == "append")

    def mode_changed(_):
        key.visible = mode.value == "append"
        key.update()

    mode.on_select = mode_changed
    columns = ft.TextField(label="Columns to keep", value=", ".join(ref.get("columns") or []), dense=True, width=W,
                           helper="Comma-separated, e.g. id, customer.name (blank = all)", visible=show_columns)
    error = ft.Text("", color=ft.Colors.ERROR, size=12, visible=False, width=W)

    def render_pagination(_=None):
        kind = pg_type.value or "none"
        pg_controls.clear()
        for key, label, default, hint in PAGE_FIELDS.get(kind, []):
            current = pg.get(key, default) if pg.get("type") == kind else default
            pg_controls[key] = ft.TextField(label=label, value="" if current in (None, "") else str(current),
                                            dense=True, width=205, hint_text=hint or None)
        pg_fields.controls = list(pg_controls.values())
        if mounted(pg_fields):
            pg_fields.update()

    def method_changed(_):
        body.visible = method.value == "POST"
        body.update()

    method.on_select = method_changed
    pg_type.on_select = render_pagination
    render_pagination()

    def collect() -> dict[str, Any]:
        if not (path.value or "").strip():
            raise RestError("Enter the path, e.g. /v1/orders")
        out: dict[str, Any] = {"method": method.value or "GET", "path": path.value.strip(),
                               "format": fmt.value or "auto"}
        p = parse_lines(params.value or "", "=")
        h = parse_lines(headers.value or "", ":")
        if p:
            out["params"] = p
        if h:
            out["headers"] = h
        if out["method"] == "POST" and (body.value or "").strip():
            try:
                json.loads(body.value)
            except ValueError as e:
                raise RestError(f"The body is not valid JSON: {e}") from None
            out["body"] = body.value.strip()
        if (records.value or "").strip():
            out["records_path"] = records.value.strip()
        if (explode.value or "").strip():
            out["explode"] = explode.value.strip()
        kind = pg_type.value or "none"
        if kind != "none":
            pag: dict[str, Any] = {"type": kind}
            for key, ctl in pg_controls.items():
                v = (ctl.value or "").strip()
                if v:
                    if key in ("size", "start"):
                        if not v.lstrip("-").isdigit():
                            raise RestError(f"{ctl.label} must be a whole number")
                        v = int(v)
                    pag[key] = v
            out["pagination"] = pag
        for ctl, key in ((max_rows, "max_rows"), (max_pages, "max_pages")):
            v = (ctl.value or "").strip()
            if v:
                if not v.isdigit() or int(v) < 1:
                    raise RestError(f"{ctl.label} must be a positive whole number")
                out[key] = int(v)
        if mode.value == "append":
            out["incremental"] = {"mode": "append",
                                  "key": [k.strip() for k in (key.value or "").split(",") if k.strip()]}
        cols = [c.strip() for c in (columns.value or "").split(",") if c.strip()]
        if cols:
            out["columns"] = cols
        elif not show_columns and ref.get("columns"):
            out["columns"] = ref["columns"]
        return out

    status = DialogStatus(W)
    cancel_btn = ft.TextButton("Cancel", on_click=lambda _: close_dialog(page))
    go_btn = ft.FilledButton(action, icon=ft.Icons.PLAY_ARROW)

    async def done(_):
        try:
            new_ref = collect()
        except RestError as e:
            error.value, error.visible = str(e), True
            error.update()
            return
        error.visible = False
        error.update()
        if work is None:
            close_dialog(page)
            on_done(new_ref)
            return

        def finished(result) -> None:
            close_dialog(page)
            on_done(result)

        await run_in_dialog(status, [go_btn, cancel_btn], busy_message, lambda: work(new_ref), finished)

    go_btn.on_click = done

    content = ft.Column([
        ft.Container(height=2),  # room for the first row's floating labels
        ft.Row([method, path], width=W, vertical_alignment=ft.CrossAxisAlignment.START),
        params, headers, body,
        ft.Row([fmt, records], width=W, vertical_alignment=ft.CrossAxisAlignment.START),
        explode, pg_type, pg_fields,
        ft.Text(SPEED_TIP, size=11.5, color=ft.Colors.ON_SURFACE_VARIANT, width=W),
        ft.Row([max_rows, max_pages], spacing=10),
        ft.Row([mode, key], spacing=10, vertical_alignment=ft.CrossAxisAlignment.START),
        columns,
        ft.Text(PLACEHOLDERS, size=11.5, color=ft.Colors.ON_SURFACE_VARIANT, width=W),
        error,
        status.view,
    ], spacing=12, scroll=ft.ScrollMode.AUTO, tight=True)
    dialog(page, title, content, [cancel_btn, go_btn], width=W + 40, height=620)
