"""Object Explorer: browse any connection lazily and add tables, views, sheets or SQL as sources."""

import json
import threading

import flet as ft

from databridge.connectors.base import Node
from databridge.ingest.sheet_profile import SPREADSHEET_EXT, ext_of
from databridge.services import connections as conn_svc
from databridge.services import sources as src_svc
from databridge.ui.common import (background, DialogStatus, card, chip, close_dialog, df_table, dialog, empty_state,
                                  friendly_error, guarded, mounted, page_header, run_in_dialog, toast, type_badge)

ICONS = {"folder": ft.Icons.FOLDER, "file": ft.Icons.INSERT_DRIVE_FILE, "sheet": ft.Icons.GRID_ON,
         "schema": ft.Icons.SCHEMA, "table": ft.Icons.TABLE_ROWS, "view": ft.Icons.VIEW_LIST,
         "endpoint": ft.Icons.HTTP, "request": ft.Icons.HTTP}
SCHEDULES = [("", "Off (Refresh button or API)"), ("15", "Every 15 minutes"), ("60", "Every hour"),
             ("360", "Every 6 hours"), ("1440", "Every day")]


def _key(ref: dict) -> str:
    return json.dumps(ref, sort_keys=True)


class ExplorerView:
    def __init__(self, app, connection_id: int | None = None):
        self.app = app
        self.page = app.page
        self.conns = conn_svc.list_connections()
        self.conn_id = connection_id or (self.conns[0].id if self.conns else None)
        self.children: dict[str, list[Node]] = {}
        self.expanded: set[str] = set()
        self.selected: Node | None = None
        self.tree = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)
        self.detail = ft.Column(spacing=12, scroll=ft.ScrollMode.AUTO, expand=True)

    @property
    def conn(self):
        return next((c for c in self.conns if c.id == self.conn_id), None)

    def build(self) -> ft.Control:
        if not self.conns:
            return ft.Column([page_header("Explorer"), empty_state(
                ft.Icons.ACCOUNT_TREE, "No connections to explore",
                "Add a database or file system connection first.",
                ft.FilledButton("Add connection", on_click=lambda _: self.app.navigate("connections")))])
        picker = ft.Dropdown(
            label="Connection", value=str(self.conn_id), width=320, dense=True,
            options=[ft.DropdownOption(key=str(c.id), text=c.name) for c in self.conns],
            on_select=lambda e: self.app.navigate("explorer", connection_id=int(e.control.value)),
        )
        actions = [picker]
        if self.conn and self.conn.type == "database":
            actions.append(ft.OutlinedButton("Custom SQL", icon=ft.Icons.CODE, on_click=lambda _: self.show_sql()))
        if self.conn and self.conn.type == "rest":
            actions.append(ft.FilledButton("New request", icon=ft.Icons.ADD, on_click=lambda _: self.show_request()))
        # The page shows at once; the list fills in when the connection answers (a slow or unreachable server
        # must never freeze the app)
        looking = ("Looking for the API's OpenAPI description..." if self.conn and self.conn.type == "rest"
                   else "Loading...")
        self.tree.controls = [ft.Row([ft.ProgressRing(width=16, height=16, stroke_width=2),
                                      ft.Text(looking, size=12, color=ft.Colors.ON_SURFACE_VARIANT)], spacing=8)]
        self.page.run_thread(self._load_root_in_background)
        self.show_placeholder()
        return ft.Column([
            page_header("Explorer", "Pick an endpoint or build a request, preview the rows, then add them as a "
                        "source." if self.conn and self.conn.type == "rest" else
                        "Browse schemas, tables, views, folders, files and sheets. "
                        "Select one to see its columns and data.", actions),
            ft.Row([
                ft.Container(card(self.tree, padding=8), width=360),
                ft.Container(card(self.detail), expand=True),
            ], expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH, spacing=14),
        ], spacing=16, expand=True)

    # ------------------------------------------------------------ tree

    def load_root(self) -> None:
        self.children["root"] = conn_svc.browse(self.conn_id, None)
        self.render_tree()

    def _load_root_in_background(self) -> None:
        try:
            self.load_root()
        except Exception as e:  # noqa: BLE001 - shown in the tree instead of a blank page
            self.tree.controls = [ft.Text(f"Could not list this connection: {friendly_error(e)}", size=12,
                                          color=ft.Colors.ERROR, selectable=True)]
            if mounted(self.tree):
                self.tree.update()

    def render_tree(self) -> None:
        rows: list[ft.Control] = []

        def add(nodes: list[Node], depth: int):
            for n in nodes:
                k = _key(n.ref)
                is_open = k in self.expanded
                selected = self.selected is not None and _key(self.selected.ref) == k
                rows.append(ft.Container(
                    ft.Row([
                        ft.Container(width=depth * 16),
                        ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN if is_open else ft.Icons.KEYBOARD_ARROW_RIGHT,
                                size=18, opacity=1 if n.has_children else 0),
                        ft.Icon(ICONS.get(n.kind, ft.Icons.DESCRIPTION), size=18, color=ft.Colors.PRIMARY),
                        ft.Text(n.name, size=13, expand=True, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS,
                                weight=ft.FontWeight.W_600 if selected else None),
                        ft.Text(n.detail, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                    ], spacing=4),
                    height=32, padding=ft.Padding.symmetric(horizontal=6), border_radius=ft.BorderRadius.all(6),
                    bgcolor=ft.Colors.SECONDARY_CONTAINER if selected else None,
                    on_click=background(self.page, lambda _, node=n: self.click(node)), ink=True,
                ))
                if is_open:
                    add(self.children.get(k, []), depth + 1)

        add(self.children.get("root", []), 0)
        empty = ("No OpenAPI description found on this API. Use New request to call any endpoint."
                 if self.conn and self.conn.type == "rest" else
                 "Nothing here (check the file pattern or schema allowlist).")
        self.tree.controls = rows or [ft.Text(empty, size=12, color=ft.Colors.ON_SURFACE_VARIANT)]
        if mounted(self.tree):
            self.tree.update()

    def click(self, node: Node) -> None:
        if node.kind == "endpoint":  # from the API's OpenAPI description: fill in the request builder
            self.selected = node
            self.render_tree()
            self.show_request(node.ref)
            return
        k = _key(node.ref)
        if node.has_children:
            if k in self.expanded:
                self.expanded.discard(k)
            else:
                if k not in self.children:
                    self.children[k] = conn_svc.browse(self.conn_id, node.ref)
                self.expanded.add(k)
        readable = node.kind in {"table", "view", "sheet"} or (
            node.kind == "file" and ext_of(node.ref.get("path", "")) not in SPREADSHEET_EXT)
        if readable:
            self.selected = node
            self.show_detail(node)
        self.render_tree()

    # ------------------------------------------------------------ detail

    def show_placeholder(self) -> None:
        if self.conn and self.conn.type == "rest":
            self.detail.controls = [empty_state(
                ft.Icons.HTTP, "Call an endpoint",
                "Pick an endpoint on the left (when the API publishes an OpenAPI description) or click New "
                "request. Preview the rows, then add them as a source.",
                ft.FilledButton("New request", icon=ft.Icons.ADD, on_click=lambda _: self.show_request()))]
            return
        self.detail.controls = [empty_state(ft.Icons.TOUCH_APP, "Select an object",
                                            "Expand the tree on the left and pick a table, view or sheet.")]

    def show_request(self, initial: dict | None = None) -> None:
        from databridge.ui.views.rest_request import request_dialog

        def preview(ref: dict) -> None:
            node = Node(f"{ref.get('method', 'GET')} {ref.get('path', '')}", "request", ref)
            self.page.run_thread(guarded(self.page, lambda: self.show_detail(node, ref)))  # off the event loop

        request_dialog(self.page, "REST request", initial, preview)

    def show_detail(self, node: Node, ref: dict | None = None) -> None:
        ref = ref or node.ref
        self.detail.controls = [ft.ProgressRing()]
        self.detail.update()
        if self.conn and self.conn.type == "rest":  # one call: the columns come from the previewed rows
            from databridge.ingest.profiling import profile_fields

            try:
                df = conn_svc.preview(self.conn_id, ref, 200)
            except Exception:
                self.show_placeholder()
                self.detail.update()
                raise
            fields = profile_fields(df)
        else:
            fields = conn_svc.describe(self.conn_id, ref)
            df = conn_svc.preview(self.conn_id, ref, 100)
        field_rows = [ft.DataRow(cells=[
            ft.DataCell(ft.Text(f["name"], size=12, weight=ft.FontWeight.W_500)),
            ft.DataCell(type_badge(f["type"])),
            ft.DataCell(ft.Text(f.get("native_type", ""), size=11, color=ft.Colors.ON_SURFACE_VARIANT)),
            ft.DataCell(ft.Row(([chip("PK", ft.Colors.PRIMARY)] if f.get("pk") else [])
                               + ([chip(f"FK: {f['fk']}", ft.Colors.TEAL_600)] if f.get("fk") else []), spacing=4)),
            ft.DataCell(ft.Text("yes" if f.get("nullable", True) else "no", size=12)),
        ]) for f in fields]
        title = ref.get("sql", "")[:60] + "…" if ref.get("sql") else node.name
        default_name = node.name if not ref.get("sql") else "Custom query"
        is_rest = node.kind in ("request", "endpoint")
        if is_rest:
            default_name = (ref.get("path") or "request").strip("/").replace("/", " ") or "request"
        pages = (ref.get("pagination") or {}).get("type", "none")
        self.detail.controls = [
            ft.Row([
                ft.Icon(ICONS.get(node.kind, ft.Icons.CODE), color=ft.Colors.PRIMARY),
                ft.Text(title, size=18, weight=ft.FontWeight.W_600, expand=True, selectable=True),
                *([ft.OutlinedButton("Edit request", icon=ft.Icons.EDIT,
                                     on_click=lambda _: self.show_request(ref))] if is_rest else []),
                ft.FilledButton("Add as source", icon=ft.Icons.ADD, visible=self.app.can("design"),
                                on_click=lambda _: self.add_source(default_name, ref, fields)),
            ]),
            ft.Text(f"{len(fields)} columns · showing first {df.height} rows"
                    + (" (preview reads at most 2 pages; the source reads them all)" if is_rest and pages != "none"
                       else ""), size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.DataTable(columns=[ft.DataColumn(ft.Text(h, size=12, weight=ft.FontWeight.W_600))
                                  for h in ("Column", "Type", "Native type", "Keys", "Nullable")],
                         rows=field_rows, data_row_min_height=32, data_row_max_height=36, column_spacing=18),
            ft.Text("Preview", size=14, weight=ft.FontWeight.W_600),
            df_table(df, max_rows=100),
        ]
        self.detail.update()

    def add_source(self, default_name: str, ref: dict, fields: list[dict] | None = None) -> None:
        name = ft.TextField(label="Source name", value=f"{self.conn.name} · {default_name}"[:160], autofocus=True,
                            width=520, max_length=160)
        schedule = ft.Dropdown(label="Refresh", value="", width=520, dense=True,
                               options=[ft.DropdownOption(key=k, text=t) for k, t in SCHEDULES])
        is_rest = self.conn.type == "rest"
        boxes = [ft.Checkbox(label=f["name"], value=True, data=f["name"]) for f in (fields or [])] if is_rest else []
        pick = []
        if boxes:
            def set_all(value: bool):
                for b in boxes:
                    b.value = value
                    b.update()

            pick = [
                ft.Row([ft.Text("Columns to keep", size=13, weight=ft.FontWeight.W_600, expand=True),
                        ft.TextButton("All", on_click=lambda _: set_all(True)),
                        ft.TextButton("None", on_click=lambda _: set_all(False))], width=520),
                ft.Container(ft.Column(boxes, spacing=0, scroll=ft.ScrollMode.AUTO), height=180, width=520,
                             border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=8, padding=6),
                ft.Text("Unticked columns are not stored. You can still map only what you need later.",
                        size=11.5, color=ft.Colors.ON_SURFACE_VARIANT),
            ]

        status = DialogStatus(520)
        cancelled = threading.Event()
        running = {"on": False}

        def cancel(_):
            close_dialog(self.page)
            if running["on"]:
                cancelled.set()  # the fetch stops at its next step; the half-made source is removed
                toast(self.page, "Cancelled. No source was added.")

        cancel_btn = ft.TextButton("Cancel", on_click=cancel)
        add_btn = ft.FilledButton("Add source", icon=ft.Icons.ADD)

        async def save(_):
            try:
                self.app.require("design")
                src_name = src_svc.check_new_name(name.value)  # before any slow call
                final = dict(ref)
                if boxes:
                    keep = [b.data for b in boxes if b.value]
                    if not keep:
                        raise ValueError("Keep at least one column")
                    if len(keep) < len(boxes):
                        final["columns"] = keep
                minutes = int(schedule.value) if schedule.value else None
            except Exception as e:  # noqa: BLE001 - shown in the dialog
                status.error(friendly_error(e))
                return

            class Progress:
                """Page counts, plus what the connector is waiting on (connecting, retrying) and a cancel flag."""
                cancel = cancelled

                def __call__(self, pages: int, rows: int) -> None:
                    if pages > 1:
                        status.busy(f"Reading page {pages} ({rows:,} rows so far)...")

                @staticmethod
                def notice(text: str) -> None:
                    status.busy(text)

            def work():
                running["on"] = True
                try:
                    return src_svc.create_connector_source(src_name, self.conn_id, final, refresh_minutes=minutes,
                                                           progress=Progress())
                finally:
                    running["on"] = False

            def done(src) -> None:
                self.app.audit("source.create", src.name, f"from connection {self.conn.name}")
                snap = src_svc.latest_snapshot(src.id)
                rows = snap.row_count if snap else 0
                note = src_svc.get_source(src.id).last_refresh_message or ""
                timing = note.split(". ", 1)[1] if ". " in note and " page(s)" in note else ""
                close_dialog(self.page)
                toast(self.page, f"Source \u201c{src.name}\u201d added: {rows:,} rows, {len(src.fields)} columns"
                      + (f", refreshes every {minutes} min" if minutes else "")
                      + (f" ({timing})" if timing else "")
                      + ". Next: map it to a target under Mappings.")
                self.app.navigate("sources")

            await run_in_dialog(status, [add_btn, name, schedule, *boxes],
                                "Taking the first snapshot (reading every page)...", work, done, cancel=cancelled)

        add_btn.on_click = save
        dialog(self.page, "Add as source", ft.Column([
            name, *pick, schedule,
            ft.Text("DataBridge takes a snapshot now. Later refreshes (button, schedule or "
                    "POST /api/v1/sources/{id}/refresh) store a new snapshot only when the data changed, and "
                    "republish the mappings that use it.", size=12, color=ft.Colors.ON_SURFACE_VARIANT, width=520),
            status.view,
        ], tight=True, spacing=10, scroll=ft.ScrollMode.AUTO), [cancel_btn, add_btn], width=560)

    def show_sql(self) -> None:
        sql = ft.TextField(label="SELECT query", multiline=True, min_lines=8, max_lines=16,
                           text_style=ft.TextStyle(font_family="monospace", size=13),
                           hint_text="SELECT o.order_id, c.name, o.amount\nFROM orders o JOIN customers c ON c.id = o.customer_id")
        msg = ft.Text("Only a single read-only SELECT is allowed.", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

        def run(_):
            connector = conn_svc.connector_for(self.conn_id)
            try:
                connector.validate_sql(sql.value or "")  # type: ignore[attr-defined]
            finally:
                connector.close()
            close_dialog(self.page)
            node = Node("Custom query", "view", {"sql": sql.value})
            self.selected = None
            self.show_detail(node, {"sql": sql.value})

        dialog(self.page, "Custom SQL", ft.Column([sql, msg], tight=True, spacing=8), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Validate and preview", on_click=background(self.page, run)),
        ], width=640)
