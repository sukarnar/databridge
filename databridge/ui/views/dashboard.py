"""Home: counts, a quick-start checklist and recent runs."""

import flet as ft

from databridge.services import endpoints, mappings, runs, sources, targets
from databridge.ui.common import RUN_COLORS, card, chip, page_header


class DashboardView:
    def __init__(self, app):
        self.app = app

    def _stat(self, label: str, value: int, icon, key: str) -> ft.Control:
        return ft.Container(
            card(ft.Row([
                ft.Icon(icon, color=ft.Colors.PRIMARY, size=28),
                ft.Column([ft.Text(str(value), size=24, weight=ft.FontWeight.W_700),
                           ft.Text(label, size=12, color=ft.Colors.ON_SURFACE_VARIANT)], spacing=0),
            ], spacing=14)),
            expand=True, on_click=lambda _: self.app.navigate(key), ink=True,
            border_radius=ft.BorderRadius.all(10),
        )

    def build(self) -> ft.Control:
        srcs, tgts, maps = sources.list_sources(), targets.list_targets(), mappings.list_mappings()
        eps, keys = endpoints.list_endpoints(), endpoints.list_api_keys()
        published = [m for m in maps if m.status == "published"]

        steps = [
            ("Add a source", "Upload a spreadsheet, or pick a table or file in the Explorer.", bool(srcs), "sources"),
            ("Define a target", "Upload a template workbook with just the header row, or define fields.", bool(tgts), "targets"),
            ("Map and publish", "Draw arrows from source to target fields, check the preview, publish.", bool(published), "mappings"),
            ("Create an endpoint", "Expose the published data as a REST endpoint.", bool(eps), "endpoints"),
            ("Issue an API key", "Give consumers a key to call the endpoint.", bool(keys), "keys"),
        ]
        step_rows = []
        for i, (title, text, done, key) in enumerate(steps, 1):
            if key == "keys" and not self.app.can("manage_keys"):
                continue
            step_rows.append(ft.ListTile(
                leading=ft.Icon(ft.Icons.CHECK_CIRCLE if done else ft.Icons.RADIO_BUTTON_UNCHECKED,
                                color=ft.Colors.GREEN_600 if done else ft.Colors.OUTLINE),
                title=ft.Text(f"{i}. {title}", weight=ft.FontWeight.W_600),
                subtitle=ft.Text(text, size=12),
                trailing=ft.TextButton("Open", on_click=lambda _, k=key: self.app.navigate(k)),
                dense=True,
            ))

        recent = runs.recent_runs(10)
        run_rows = [
            ft.DataRow(cells=[
                ft.DataCell(ft.Text(r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "", size=12)),
                ft.DataCell(ft.Text(r.kind, size=12)),
                ft.DataCell(ft.Text(r.subject, size=12, width=260, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
                ft.DataCell(chip(r.status, RUN_COLORS.get(r.status, ft.Colors.OUTLINE))),
                ft.DataCell(ft.Text(f"{r.rows_out:,} / {r.rows_rejected:,}", size=12)),
            ]) for r in recent
        ]
        runs_table = ft.DataTable(
            columns=[ft.DataColumn(ft.Text(h, size=12, weight=ft.FontWeight.W_600))
                     for h in ("Started", "Kind", "Subject", "Status", "Rows out / rejected")],
            rows=run_rows, data_row_min_height=36, data_row_max_height=40, column_spacing=20,
        ) if run_rows else ft.Text("No runs yet.", color=ft.Colors.ON_SURFACE_VARIANT)

        return ft.Column([
            page_header(f"Welcome, {self.app.user.full_name or self.app.user.username}",
                        "Map spreadsheets, files and database tables to a target schema and serve them over REST."),
            ft.Row([
                self._stat("Sources", len(srcs), ft.Icons.TABLE_CHART, "sources"),
                self._stat(f"Mappings ({len(published)} published)", len(maps), ft.Icons.COMPARE_ARROWS, "mappings"),
                self._stat("Endpoints", len(eps), ft.Icons.API, "endpoints"),
                *([self._stat("API keys", len([k for k in keys if k.active]), ft.Icons.KEY, "keys")]
                  if self.app.can("manage_keys") else []),
            ], spacing=14),
            ft.Row([
                card(ft.Column([ft.Text("Quick start", size=16, weight=ft.FontWeight.W_600), *step_rows], spacing=2),
                     expand=1),
                card(ft.Column([ft.Text("Recent runs", size=16, weight=ft.FontWeight.W_600), runs_table],
                               spacing=8, scroll=ft.ScrollMode.AUTO), expand=1),
            ], spacing=14, vertical_alignment=ft.CrossAxisAlignment.START),
        ], spacing=18, scroll=ft.ScrollMode.AUTO, expand=True)
