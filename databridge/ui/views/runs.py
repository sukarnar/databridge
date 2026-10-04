"""Run history: ingests, extracts and publishes, with reject reports."""

import flet as ft
import polars as pl
from sqlalchemy import select

from databridge.core.db import session_scope
from databridge.core.models import Dataset
from databridge.services import runs as svc
from databridge.ui.common import RUN_COLORS, chip, close_dialog, df_table, dialog, empty_state, guarded, page_header


class RunsView:
    def __init__(self, app):
        self.app = app
        self.page = app.page

    def show_rejects(self, run_id: int) -> None:
        with session_scope() as s:
            ds = s.scalars(select(Dataset).where(Dataset.run_id == run_id)).first()
        if not ds or not ds.reject_path:
            raise LookupError("No reject report for this run")
        df = pl.read_excel(ds.reject_path)
        dialog(self.page, f"Rejected rows · run {run_id}", ft.Column([
            ft.Text(f"{df.height:,} rows. File on the server: {ds.reject_path}. "
                    f"Consumers with an admin key can download it from GET /api/v1/runs/{run_id}/rejects.",
                    size=12, selectable=True),
            df_table(df, max_rows=200),
        ], scroll=ft.ScrollMode.AUTO, spacing=10), [ft.TextButton("Close", on_click=lambda _: close_dialog(self.page))],
            width=1000, height=560)

    def build(self) -> ft.Control:
        runs = svc.recent_runs(200)
        rows = []
        for r in runs:
            dur = (r.finished_at - r.started_at).total_seconds() if r.finished_at and r.started_at else None
            rejects = (ft.TextButton("View rejects", icon=ft.Icons.VISIBILITY,
                                     on_click=guarded(self.page, lambda _, rid=r.id: self.show_rejects(rid)))
                       if r.kind == "publish" and r.rows_rejected else ft.Container())
            rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(str(r.id), size=12)),
                ft.DataCell(ft.Text(r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "", size=12)),
                ft.DataCell(ft.Text(r.kind, size=12)),
                ft.DataCell(ft.Text(r.subject, size=12, width=280, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS,
                                    tooltip=r.subject)),
                ft.DataCell(chip(r.status, RUN_COLORS.get(r.status, ft.Colors.OUTLINE))),
                ft.DataCell(ft.Text(f"{r.rows_in:,} / {r.rows_out:,}", size=12)),
                ft.DataCell(ft.Text(f"{r.rows_rejected:,}", size=12,
                                    color=ft.Colors.RED_600 if r.rows_rejected else None)),
                ft.DataCell(ft.Text(f"{dur:.1f}s" if dur is not None else "", size=12)),
                ft.DataCell(ft.Text(r.message, size=11.5, width=260, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS,
                                    tooltip=r.message or None)),
                ft.DataCell(rejects),
            ]))
        body = ft.Row([ft.DataTable(
            columns=[ft.DataColumn(ft.Text(h, size=12, weight=ft.FontWeight.W_600)) for h in
                     ("#", "Started", "Kind", "Subject", "Status", "Rows in / out", "Rejected", "Time", "Message", "")],
            rows=rows, column_spacing=16, data_row_min_height=36, data_row_max_height=40,
        )], scroll=ft.ScrollMode.AUTO) if rows else empty_state(ft.Icons.HISTORY, "No runs yet",
                                                                 "Imports, refreshes and publishes appear here.")
        return ft.Column([
            page_header("Runs", "Every import, refresh and publish, newest first.",
                        [ft.OutlinedButton("Refresh", icon=ft.Icons.REFRESH, on_click=lambda _: self.app.navigate("runs"))]),
            body,
        ], spacing=18, scroll=ft.ScrollMode.AUTO, expand=True)
