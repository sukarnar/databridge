"""Mappings list: create a mapping, open it in the studio, publish."""

import flet as ft

from databridge.services import mappings as svc
from databridge.services import sources as src_svc
from databridge.services import targets as tgt_svc
from databridge.ui.common import (card, chip, mounted, close_dialog, confirm, dialog, empty_state, guarded, page_header,
                                  toast)


class MappingsView:
    def __init__(self, app):
        self.app = app
        self.page = app.page

    def build(self) -> ft.Control:
        sources = {s.id: s for s in src_svc.list_sources()}
        targets = {t.id: t for t in tgt_svc.list_targets()}
        designer = self.app.can("design")
        items = []
        for m in svc.list_mappings():
            src, tgt = sources.get(m.source_id), targets.get(m.target_id)
            mapped = len([r for r in m.rules if r.get("formula")])
            total = len(tgt.fields) if tgt else 0
            status = chip(f"published v{m.published_version}", ft.Colors.GREEN_600) if m.status == "published" \
                else chip("draft", ft.Colors.AMBER_700)
            items.append(card(ft.Row([
                ft.Icon(ft.Icons.COMPARE_ARROWS, color=ft.Colors.PRIMARY, size=30),
                ft.Column([
                    ft.Row([ft.Text(m.name, size=15, weight=ft.FontWeight.W_600), status], spacing=8),
                    ft.Text(f"{src.name if src else '?'} to {tgt.name if tgt else '?'}  ·  {mapped} of {total} fields mapped",
                            size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ], spacing=2, expand=True),
                ft.FilledButton("Open studio" if designer else "View", icon=ft.Icons.EDIT if designer else ft.Icons.VISIBILITY,
                                on_click=lambda _, mid=m.id: self.app.navigate("studio", mapping_id=mid)),
                *([ft.OutlinedButton("Publish", icon=ft.Icons.PUBLISH,
                                     on_click=guarded(self.page, lambda _, mid=m.id: self.publish(mid))),
                   ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete", on_click=lambda _, mm=m: confirm(
                       self.page, "Delete mapping", f"Delete {mm.name}? Endpoints using it stop working.",
                       lambda: self.delete(mm)))] if designer else []),
            ], spacing=10)))
        new_btn = ft.FilledButton("New mapping", icon=ft.Icons.ADD, on_click=lambda _: self.new())
        body = ft.Column(items, spacing=10) if items else empty_state(
            ft.Icons.COMPARE_ARROWS, "No mappings yet", "Pick a source and a target; DataBridge suggests matches.",
            new_btn if designer else None)
        return ft.Column([page_header("Mappings", "How each source becomes its target.",
                                      [new_btn] if designer else []), body],
                         spacing=18, scroll=ft.ScrollMode.AUTO, expand=True)

    def delete(self, m) -> None:
        self.app.require("design")
        svc.delete_mapping(m.id)
        self.app.audit("mapping.delete", m.name)
        self.app.navigate("mappings")

    def publish(self, mapping_id: int) -> None:
        self.app.require("design")
        ds = svc.publish(mapping_id)
        self.app.audit("mapping.publish", svc.get_mapping(mapping_id).name, f"v{ds.version}, {ds.row_count} rows")
        toast(self.page, f"Published v{ds.version}: {ds.row_count:,} rows, {ds.rejected_count:,} rejected")
        self.app.navigate("mappings")

    def new(self, source_id: int | None = None) -> None:
        sources, targets = src_svc.list_sources(), tgt_svc.list_targets()
        if not sources:
            toast(self.page, "Add a source first (Sources or Explorer)", error=True)
            return
        NEW = "__new__"
        first_src = str(source_id) if source_id else str(sources[0].id)
        name = ft.TextField(label="Mapping name", dense=True, autofocus=True, width=420)
        src_dd = ft.Dropdown(label="Source", dense=True, value=first_src, width=420,
                             options=[ft.DropdownOption(key=str(s.id), text=s.name) for s in sources])
        tgt_dd = ft.Dropdown(label="Target", dense=True, width=420,
                             value=str(targets[0].id) if targets and not source_id else NEW,
                             options=[ft.DropdownOption(key=NEW, text="New target from the source's columns")] +
                             [ft.DropdownOption(key=str(t.id), text=t.name) for t in targets])
        tgt_name = ft.TextField(label="New target name", dense=True, width=420,
                                helper="One field per source column; edit it later under Targets")
        auto = ft.Checkbox(label="Auto-map matching fields now", value=True)

        def src_name() -> str:
            return next(s.name for s in sources if str(s.id) == src_dd.value)

        def sync(_=None):
            tgt_name.visible = tgt_dd.value == NEW
            if tgt_name.visible and not (tgt_name.value or "").strip():
                tgt_name.value = f"{src_name()} target"[:160]
            if mounted(tgt_name):
                tgt_name.update()

        src_dd.on_select = lambda _: (setattr(tgt_name, "value", ""), sync())
        tgt_dd.on_select = sync
        sync()

        def create(_):
            self.app.require("design")
            if tgt_dd.value == NEW:
                t = tgt_svc.target_from_source(((tgt_name.value or "").strip() or f"{src_name()} target")[:160],
                                               int(src_dd.value))
                self.app.audit("target.create", t.name, f"from source {src_name()}")
                target_id, target_label = t.id, t.name
            else:
                target_id = int(tgt_dd.value)
                target_label = next(t.name for t in targets if str(t.id) == tgt_dd.value)
            nm = name.value.strip() or (f"{src_name()} mapping" if tgt_dd.value == NEW
                                        else f"{src_name()} to {target_label}")[:160]
            m = svc.create_mapping(nm, int(src_dd.value), target_id, auto=auto.value)
            self.app.audit("mapping.create", m.name)
            close_dialog(self.page)
            self.app.navigate("studio", mapping_id=m.id)

        dialog(self.page, "New mapping", ft.Column([name, src_dd, tgt_dd, tgt_name, auto], spacing=12, tight=True), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Create and open", on_click=guarded(self.page, create)),
        ], width=460)
