"""Sources: uploaded spreadsheets and connection-backed objects, with snapshots and drift."""

import asyncio

import flet as ft

from databridge.config import settings
from databridge.ingest.sheet_profile import SUPPORTED_EXT, SheetProfile
from databridge.services import sources as svc
from databridge.ui.common import (background, card, chip, close_dialog, confirm, df_table, dialog, empty_state, guarded,
                                  page_header, toast, type_badge)
from databridge.ui.components.profile_wizard import ProfileWizard

KIND_LABEL = {"upload": "Uploaded file", "file": "File on connection", "table": "Database table", "sql": "SQL query",
              "workflow": "AI workflow output", "rest": "REST API"}
KIND_ICON = {"rest": ft.Icons.HTTP, "table": ft.Icons.TABLE_ROWS, "sql": ft.Icons.CODE,
             "file": ft.Icons.INSERT_DRIVE_FILE}


def _schedule_text(s) -> str:
    """'Every 60 min · next 14:05 · last: ok' for scheduled sources, '' otherwise."""
    parts = []
    if s.refresh_minutes:
        nxt = svc.next_refresh(s)
        parts.append(f"Every {s.refresh_minutes} min" + (f" · next {nxt:%Y-%m-%d %H:%M}" if nxt else ""))
    if s.last_refresh_status:
        when = s.last_refresh_at.strftime("%Y-%m-%d %H:%M") if s.last_refresh_at else ""
        label = {"ok": "new data", "unchanged": "no changes", "failed": "failed"}.get(s.last_refresh_status, "")
        parts.append(f"last refresh {when}: {label}")
    return " · ".join(parts)


async def pick_file(page: ft.Page) -> tuple[str, bytes] | None:
    """Opens the browser file picker, uploads over HTTPS and returns (filename, bytes), or None if cancelled."""
    from databridge.ui.uploads import pick_and_upload

    try:
        return await pick_and_upload(page, sorted(SUPPORTED_EXT), settings.max_upload_mb * 1024 * 1024)
    except ValueError as e:
        toast(page, str(e), error=True)
        return None


class SourcesView:
    def __init__(self, app):
        self.app = app
        self.page = app.page

    def classify(self, src) -> None:
        from databridge.ui.views.workflows import classify_dialog

        classify_dialog(self.page, self.app, src.id, on_done=lambda: self.app.navigate("sources"))

    def build(self) -> ft.Control:
        designer = self.app.can("design")
        from databridge.services import mappings as map_svc

        used_by: dict[int, list] = {}
        for m in map_svc.list_mappings():
            used_by.setdefault(m.source_id, []).append(m)
        items = []
        for s in svc.list_sources():
            snap = svc.latest_snapshot(s.id)
            drift = (snap.drift or {}) if snap else {}
            badges = [chip(KIND_LABEL.get(s.kind, s.kind))]
            if drift.get("changed"):
                badges.append(chip("schema changed", ft.Colors.AMBER_700))
            levels = list((s.classification or {}).values())
            if levels.count("pii"):
                badges.append(chip(f"PII: {levels.count('pii')} col", ft.Colors.AMBER_700))
            if levels.count("confidential"):
                badges.append(chip(f"confidential: {levels.count('confidential')} col", ft.Colors.RED_400))
            when = snap.created_at.strftime("%Y-%m-%d %H:%M") if snap else "never"
            primary = (ft.OutlinedButton("Upload new version", icon=ft.Icons.UPLOAD_FILE,
                                         on_click=lambda _, src=s: self.page.run_task(self.upload_version, src))
                       if s.kind == "upload" else
                       ft.OutlinedButton("Refresh", icon=ft.Icons.REFRESH,
                                         on_click=background(self.page, lambda _, src=s: self.refresh(src))))
            if s.last_refresh_status == "failed":
                badges.append(chip("refresh failed", ft.Colors.RED_500))
            if s.refresh_minutes:
                badges.append(chip("scheduled", ft.Colors.TEAL_600))
            sched = _schedule_text(s)
            maps = used_by.get(s.id, [])
            usage = (ft.Text("Mapped by: " + ", ".join(f"{m.name} ({'published v' + str(m.published_version) if m.published_version else 'draft'})" for m in maps),
                             size=11, color=ft.Colors.ON_SURFACE_VARIANT, selectable=True) if maps else
                     ft.Text("Not mapped yet: map it to a target to publish it and serve it through an endpoint",
                             size=11, color=ft.Colors.AMBER_800))
            extra = []
            if designer:
                extra.append(ft.IconButton(ft.Icons.COMPARE_ARROWS, tooltip="Map this source to a target",
                                           on_click=guarded(self.page, lambda _, src=s: self.map_source(src))))
            if designer and s.connection_id:
                extra.append(ft.IconButton(ft.Icons.SCHEDULE, tooltip="Refresh schedule",
                                           on_click=lambda _, src=s: self.schedule(src)))
            if designer and s.kind == "rest":
                extra.append(ft.IconButton(ft.Icons.EDIT_NOTE, tooltip="Edit request (path, parameters, paging, "
                                           "columns)", on_click=lambda _, src=s: self.edit_request(src)))
            items.append(card(ft.Row([
                ft.Icon(KIND_ICON.get(s.kind, ft.Icons.TABLE_CHART), color=ft.Colors.PRIMARY, size=30),
                ft.Column([
                    ft.Row([ft.Text(s.name, size=15, weight=ft.FontWeight.W_600), *badges], spacing=8),
                    ft.Text(f"{len(s.fields)} columns · {snap.row_count if snap else 0:,} rows · last data {when}"
                            + (f" · {snap.file_name}" if snap and snap.file_name else ""),
                            size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text(f"Source id {s.id} (use in POST /api/v1/ingest/{s.id})" if s.kind == "upload" else
                            f"Source id {s.id} · written by AI workflow runs" if s.kind == "workflow" else
                            f"Source id {s.id} (use in POST /api/v1/sources/{s.id}/refresh)",
                            size=11, color=ft.Colors.ON_SURFACE_VARIANT, selectable=True),
                    usage,
                    *([ft.Text(sched, size=11, selectable=True,
                               color=ft.Colors.RED_600 if s.last_refresh_status == "failed" else
                               ft.Colors.ON_SURFACE_VARIANT,
                               tooltip=s.last_refresh_message or None)] if sched else []),
                ], spacing=2, expand=True),
                ft.OutlinedButton("View", icon=ft.Icons.VISIBILITY, on_click=guarded(self.page, lambda _, src=s: self.view(src))),
                *([*([primary] if s.kind != "workflow" else []), *extra,
                   ft.IconButton(ft.Icons.SHIELD_OUTLINED, tooltip="Classify columns for AI (PII, confidential)",
                                 on_click=lambda _, src=s: self.classify(src)),
                   ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete", on_click=lambda _, src=s: confirm(
                       self.page, "Delete source", f"Delete {src.name}? Mappings using it will stop working.",
                       lambda: self.delete(src)))] if designer else []),
            ], spacing=10)))
        upload_btn = ft.FilledButton("Upload spreadsheet", icon=ft.Icons.UPLOAD_FILE,
                                     on_click=lambda _: self.page.run_task(self.upload_new))
        body = ft.Column(items, spacing=10) if items else empty_state(
            ft.Icons.UPLOAD_FILE, "No sources yet",
            "Upload an Excel or CSV file. DataBridge finds the header row, skips title and total rows, "
            "and keeps leading zeros. You can also add tables and files from the Explorer.",
            upload_btn if designer else None)
        return ft.Column([
            page_header("Sources", "Data coming in. Every new file or refresh becomes a versioned snapshot.", [
                ft.OutlinedButton("From a connection", icon=ft.Icons.ACCOUNT_TREE,
                                  on_click=lambda _: self.app.navigate("explorer")),
                upload_btn] if designer else []),
            body,
        ], spacing=18, scroll=ft.ScrollMode.AUTO, expand=True)

    def delete(self, src) -> None:
        self.app.require("design")
        svc.delete_source(src.id)
        self.app.audit("source.delete", src.name)
        self.app.navigate("sources")

    async def upload_new(self):
        self.app.require("design")
        picked = await pick_file(self.page)
        if not picked:
            return
        name, content = picked

        def save(src_name: str, profile: SheetProfile):
            def work():
                self.app.require("design")
                src = svc.create_upload_source(src_name, name, content, profile)
                self.app.audit("source.create", src.name, name)
                toast(self.page, f"Imported {src.name}: {len(src.fields)} columns")
                self.app.navigate("sources")
            guarded(self.page, work)()

        await asyncio.to_thread(guarded(self.page, lambda: ProfileWizard(self.page, name, content, save,
                                                                       check_name=svc.check_new_name).open()))

    async def upload_version(self, src):
        self.app.require("design")
        picked = await pick_file(self.page)
        if not picked:
            return
        name, content = picked

        def work():
            out = svc.ingest_file(src.id, name, content)
            self.app.audit("source.ingest", src.name, name)
            if out.get("skipped"):
                toast(self.page, "This exact file was already imported; nothing changed.")
            else:
                msg = f"Imported {out['rows']:,} rows."
                if out.get("drift") and out["drift"].get("changed"):
                    msg += " Schema changed: review the source."
                pubs = [p for p in out.get("published", []) if p["status"] == "published"]
                if pubs:
                    msg += " Republished: " + ", ".join(f"{p['mapping']} v{p['version']}" for p in pubs)
                toast(self.page, msg)
            self.app.navigate("sources")

        await asyncio.to_thread(guarded(self.page, work))

    def refresh(self, src) -> None:
        self.app.require("design")
        try:
            out = svc.refresh(src.id)
        finally:
            self.app.navigate("sources")  # shows the recorded outcome, also after a failure
        self.app.audit("source.refresh", src.name)
        toast(self.page, _refresh_message(src.name, out))

    def map_source(self, src) -> None:
        from databridge.ui.views.mappings import MappingsView

        MappingsView(self.app).new(source_id=src.id)

    def schedule(self, src) -> None:
        from databridge.ui.views.explorer import SCHEDULES

        options = list(SCHEDULES)
        current = str(src.refresh_minutes or "")
        if current and current not in {k for k, _ in options}:
            options.append((current, f"Every {current} minutes"))
        choice = ft.Dropdown(label="Refresh", value=current, width=440, dense=True,
                             options=[ft.DropdownOption(key=k, text=t) for k, t in options] +
                             [ft.DropdownOption(key="custom", text="Other interval...")])
        minutes = ft.TextField(label="Minutes between refreshes (5 or more)", dense=True, width=440, visible=False,
                               keyboard_type=ft.KeyboardType.NUMBER)

        def changed(_):
            minutes.visible = choice.value == "custom"
            minutes.update()

        choice.on_select = changed

        def save(_):
            self.app.require("design")
            value = minutes.value if choice.value == "custom" else choice.value
            if choice.value == "custom" and not (value or "").strip().isdigit():
                raise ValueError("Enter the number of minutes")
            svc.set_schedule(src.id, int(value) if value else None, actor=self.app.user.username)
            close_dialog(self.page)
            toast(self.page, f"{src.name}: " + (f"refreshes every {value} minutes" if value else "schedule off"))
            self.app.navigate("sources")

        dialog(self.page, f"Refresh schedule: {src.name}", ft.Column([
            choice, minutes,
            ft.Text("Each scheduled refresh stores a new snapshot only when the data changed, then republishes "
                    "the mappings that use this source. Failures show here and in Runs. External schedulers can "
                    f"call POST /api/v1/sources/{src.id}/refresh instead.", size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT, width=440),
        ], tight=True, spacing=10), [ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
                                      ft.FilledButton("Save", on_click=guarded(self.page, save))], width=480)

    def edit_request(self, src) -> None:
        from databridge.ui.views.rest_request import request_dialog

        self.app.require("design")

        def work(ref: dict) -> dict:
            return svc.update_request(src.id, ref, actor=self.app.user.username)

        def done(out: dict) -> None:
            toast(self.page, _refresh_message(src.name, out))
            self.app.navigate("sources")

        request_dialog(self.page, f"Request: {src.name}", src.object_ref, done, action="Save and refresh",
                       show_columns=True, work=work,
                       busy_message="Trying the new request, then refreshing (reading every page)...")

    def view(self, src) -> None:
        df = svc.load_snapshot(src.id, limit=100)
        snap = svc.latest_snapshot(src.id)
        drift = (snap.drift or {}) if snap else {}
        field_rows = [ft.DataRow(cells=[
            ft.DataCell(ft.Text(f.get("column_letter", ""), size=12)),
            ft.DataCell(ft.Text(f["name"], size=12, weight=ft.FontWeight.W_500)),
            ft.DataCell(type_badge(f["type"])),
            ft.DataCell(ft.Text(f"{f.get('null_pct', 0)}%", size=12)),
            ft.DataCell(ft.Text(str(f.get("distinct", "")), size=12)),
            ft.DataCell(ft.Text(", ".join(str(x) for x in f.get("samples", [])[:3]), size=12, width=260,
                                no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
            ft.DataCell(ft.Text(f.get("note", ""), size=11, color=ft.Colors.AMBER_800)),
        ]) for f in src.fields]
        drift_text = []
        if drift.get("changed"):
            for k in ("added", "removed"):
                if drift.get(k):
                    drift_text.append(f"{k.title()}: {', '.join(drift[k])}")
            for r in drift.get("renamed", []):
                drift_text.append(f"Renamed: {r['from']} -> {r['to']}")
            for r in drift.get("retyped", []):
                drift_text.append(f"Type changed: {r['name']} {r['from']} -> {r['to']}")
        content = ft.Column([
            ft.Container(ft.Text("Schema drift in the latest file — " + "; ".join(drift_text), size=12),
                         bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.AMBER), padding=10,
                         border_radius=ft.BorderRadius.all(8), visible=bool(drift_text)),
            ft.Text("Columns", size=14, weight=ft.FontWeight.W_600),
            ft.DataTable(columns=[ft.DataColumn(ft.Text(h, size=12, weight=ft.FontWeight.W_600)) for h in
                                  ("Col", "Name", "Type", "Blank", "Distinct", "Samples", "Note")],
                         rows=field_rows, data_row_min_height=30, data_row_max_height=34, column_spacing=16),
            ft.Text(f"Data (first {df.height} rows)", size=14, weight=ft.FontWeight.W_600),
            df_table(df, max_rows=100, letters={f["name"]: f.get("column_letter", "") for f in src.fields}),
        ], spacing=10, scroll=ft.ScrollMode.AUTO)
        dialog(self.page, src.name, content, [ft.TextButton("Close", on_click=lambda _: close_dialog(self.page))],
               width=1000, height=600)


def _refresh_message(name: str, out: dict) -> str:
    if out.get("skipped"):
        return f"{name}: no changes since the last snapshot; nothing republished."
    msg = f"Refreshed {name}: {out['rows']:,} rows."
    if out.get("drift") and out["drift"].get("changed"):
        msg += " Columns changed: review the source."
    pubs = [p for p in out.get("published", []) if p["status"] == "published"]
    if pubs:
        msg += " Republished: " + ", ".join(f"{p['mapping']} v{p['version']}" for p in pubs)
    return msg

