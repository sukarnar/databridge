"""Target schemas: from a template workbook, copied from a source, or defined by hand."""

import asyncio
from pathlib import PurePath

import flet as ft

from databridge.services import sources as src_svc
from databridge.services import targets as svc
from databridge.ui.common import (mounted, card, chip, close_dialog, confirm, dialog, empty_state, guarded, page_header,
                                  toast, type_badge, type_dropdown)
from databridge.ui.views.sources import pick_file


class TargetsView:
    def __init__(self, app):
        self.app = app
        self.page = app.page

    def build(self) -> ft.Control:
        designer = self.app.can("design")
        items = []
        for t in svc.list_targets():
            preview = ft.Row([ft.Row([ft.Text(f["name"] + (" *" if f.get("required") else ""), size=12),
                                      type_badge(f["type"])], spacing=4) for f in t.fields[:8]],
                             wrap=True, spacing=12, run_spacing=6)
            items.append(card(ft.Row([
                ft.Icon(ft.Icons.SCHEMA, color=ft.Colors.PRIMARY, size=30),
                ft.Column([
                    ft.Row([ft.Text(t.name, size=15, weight=ft.FontWeight.W_600), chip(t.origin),
                            ft.Text(f"{len(t.fields)} fields", size=12, color=ft.Colors.ON_SURFACE_VARIANT)], spacing=8),
                    preview,
                ], spacing=6, expand=True),
                *([ft.OutlinedButton("Edit", icon=ft.Icons.EDIT_OUTLINED, on_click=lambda _, tt=t: self.edit(tt)),
                ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete", on_click=lambda _, tt=t: confirm(
                    self.page, "Delete target", f"Delete {tt.name}?",
                    lambda: self.delete(tt)))] if designer else []),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.START)))
        tmpl = ft.FilledButton("From template workbook", icon=ft.Icons.UPLOAD_FILE,
                               on_click=lambda _: self.page.run_task(self.from_template))
        body = ft.Column(items, spacing=10) if items else empty_state(
            ft.Icons.SCHEMA, "No target schemas yet",
            "The easiest way: upload the workbook your consumers expect, with just the header row filled in.",
            tmpl if designer else None)
        return ft.Column([
            page_header("Targets", "The shape consumers receive. Required fields (*) reject rows that leave them blank.", [
                ft.OutlinedButton("New target", icon=ft.Icons.ADD, on_click=lambda _: self.edit(None)), tmpl]
                if designer else []),
            body,
        ], spacing=18, scroll=ft.ScrollMode.AUTO, expand=True)

    def delete(self, target) -> None:
        self.app.require("design")
        svc.delete_target(target.id)
        self.app.audit("target.delete", target.name)
        self.app.navigate("targets")

    async def from_template(self):
        self.app.require("design")
        picked = await pick_file(self.page)
        if not picked:
            return
        name, content = picked

        def work():
            t = svc.target_from_template(PurePath(name).stem.replace("_", " ").title(), name, content)
            self.app.audit("target.create", t.name, f"from {name}")
            toast(self.page, f"Created {t.name} with {len(t.fields)} fields. Set types and required fields next.")
            self.edit(t)

        await asyncio.to_thread(guarded(self.page, work))

    def edit(self, target) -> None:
        name = ft.TextField(label="Target name", value=target.name if target else "", dense=True)
        rows = ft.Column(spacing=6)
        sources = src_svc.list_sources()

        def field_row(f: dict) -> ft.Control:
            fname = ft.TextField(value=f.get("name", ""), dense=True, width=220, hint_text="field_name")
            ftype = type_dropdown(f.get("type", "string"), label="", width=160)
            req = ft.Checkbox(label="Required", value=bool(f.get("required")))
            desc = ft.TextField(value=f.get("description", ""), dense=True, expand=True, hint_text="Description")
            row = ft.Row([fname, ftype, req, desc], spacing=8)
            row.controls.append(ft.IconButton(ft.Icons.DELETE_OUTLINE, on_click=lambda _: remove(row)))
            row.data = (fname, ftype, req, desc)
            return row

        def remove(row):
            rows.controls.remove(row)
            rows.update()

        def add(_=None, f=None):
            rows.controls.append(field_row(f or {}))
            if mounted(rows):
                rows.update()

        def copy_from(e):
            src = next(s for s in sources if str(s.id) == e.control.value)
            for f in src.fields:
                add(f={"name": f["name"], "type": f["type"]})

        for f in (target.fields if target else []):
            add(f=f)
        if not target:
            add()

        def save(_):
            fields = []
            for r in rows.controls:
                fname, ftype, req, desc = r.data
                if fname.value and fname.value.strip():
                    fields.append({"name": fname.value.strip(), "type": ftype.value, "required": req.value,
                                   "description": desc.value or ""})
            if not name.value.strip() or not fields:
                raise ValueError("Enter a name and at least one field")
            self.app.require("design")
            saved = svc.save_target(name.value.strip(), fields, target_id=target.id if target else None)
            self.app.audit("target.update" if target else "target.create", saved.name, f"{len(fields)} fields")
            close_dialog(self.page)
            toast(self.page, "Target saved")
            self.app.navigate("targets")

        copy_dd = ft.Dropdown(label="Copy fields from a source", dense=True, width=280,
                              options=[ft.DropdownOption(key=str(s.id), text=s.name) for s in sources],
                              on_select=guarded(self.page, copy_from))
        dialog(self.page, "Edit target" if target else "New target", ft.Column([
            ft.Row([name, copy_dd], spacing=10),
            ft.Text("Fields", size=14, weight=ft.FontWeight.W_600),
            rows,
            ft.TextButton("Add field", icon=ft.Icons.ADD, on_click=add),
        ], spacing=10, scroll=ft.ScrollMode.AUTO), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Save", on_click=guarded(self.page, save)),
        ], width=860, height=560)
