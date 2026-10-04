"""Endpoints: define REST routes over published mappings, with a built-in test console."""

import json

import flet as ft

from databridge.config import settings
from databridge.engine.mapper import slugify
from databridge.services import endpoints as svc
from databridge.services import mappings as map_svc
from databridge.services import targets as tgt_svc
from databridge.ui.common import (mounted, card, chip, close_dialog, confirm, df_table, dialog, empty_state, guarded,
                                  page_header, toast)

OP_LABELS = {"eq": "equals", "ne": "not equal", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
             "contains": "contains", "in": "in list"}


def endpoint_url(slug: str) -> str:
    return f"{settings.public_base_url}/api/v1/data/{slug}"


class EndpointsView:
    def __init__(self, app):
        self.app = app
        self.page = app.page

    def build(self) -> ft.Control:
        maps = {m.id: m for m in map_svc.list_mappings()}
        designer = self.app.can("design")
        items = []
        for ep in svc.list_endpoints():
            m = maps.get(ep.mapping_id)
            ds = map_svc.dataset_for(ep.mapping_id, ep.pinned_version) if m else None
            served = f"serving v{ds.version} · {ds.row_count:,} rows" if ds else "nothing published yet"
            params = ", ".join(f"{p['name']} ({OP_LABELS[p['op']]} {p['column']})" for p in ep.params) or "no filters"
            items.append(card(ft.Row([
                ft.Icon(ft.Icons.API, color=ft.Colors.PRIMARY, size=30),
                ft.Column([
                    ft.Row([ft.Text(ep.name, size=15, weight=ft.FontWeight.W_600),
                            chip("public", ft.Colors.AMBER_700) if ep.public else chip("API key", ft.Colors.GREEN_600),
                            chip("inactive", ft.Colors.RED_500) if not ep.active else ft.Container()], spacing=8),
                    ft.Text(f"GET {endpoint_url(ep.slug)}", size=12, selectable=True, font_family="monospace"),
                    ft.Text(f"{m.name if m else '?'} · {served} · filters: {params}", size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                ], spacing=2, expand=True),
                ft.FilledTonalButton("Test", icon=ft.Icons.PLAY_ARROW, on_click=guarded(self.page, lambda _, e=ep: self.test(e))),
                *([ft.IconButton(ft.Icons.EDIT_OUTLINED, tooltip="Edit", on_click=lambda _, e=ep: self.edit(e)),
                   ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete", on_click=lambda _, e=ep: confirm(
                       self.page, "Delete endpoint", f"Delete {e.slug}? Consumers calling it will get 404.",
                       lambda: self.delete(e)))] if designer else []),
            ], spacing=10)))
        new_btn = ft.FilledButton("New endpoint", icon=ft.Icons.ADD, on_click=lambda _: self.edit(None))
        body = ft.Column(items, spacing=10) if items else empty_state(
            ft.Icons.API, "No endpoints yet", "Publish a mapping, then expose it here as a REST endpoint.",
            new_btn if designer else None)
        return ft.Column([
            page_header("Endpoints", f"REST routes consumers call. API docs: {settings.public_base_url}/api/docs",
                        [new_btn] if designer else []),
            body,
        ], spacing=18, scroll=ft.ScrollMode.AUTO, expand=True)

    def delete(self, ep) -> None:
        self.app.require("design")
        svc.delete_endpoint(ep.id)
        self.app.audit("endpoint.delete", ep.slug)
        self.app.navigate("endpoints")

    def edit(self, ep) -> None:
        self.app.require("design")
        maps = map_svc.list_mappings()
        if not maps:
            toast(self.page, "Create and publish a mapping first", error=True)
            return
        name = ft.TextField(label="Name", value=ep.name if ep else "", dense=True, expand=True)
        slug = ft.TextField(label="URL slug", value=ep.slug if ep else "", dense=True, width=240,
                            prefix=ft.Text("/data/", size=12))
        name.on_change = lambda _: (setattr(slug, "value", slugify(name.value)), slug.update()) if not ep else None
        mapping_dd = ft.Dropdown(label="Serves mapping", dense=True, value=str(ep.mapping_id if ep else maps[0].id),
                                 options=[ft.DropdownOption(key=str(m.id), text=m.name) for m in maps])
        desc = ft.TextField(label="Description", value=ep.description if ep else "", dense=True, multiline=True)
        page_size = ft.TextField(label="Page size", value=str(ep.page_size if ep else settings.default_page_size),
                                 dense=True, width=120, keyboard_type=ft.KeyboardType.NUMBER)
        public = ft.Switch(label="Public (no API key needed)", value=bool(ep.public) if ep else False)
        active = ft.Switch(label="Active", value=bool(ep.active) if ep else True)
        fmt = {f: ft.Checkbox(label=f.upper(), value=f in (ep.formats if ep else ["json", "csv", "xlsx"]))
               for f in ("json", "csv", "xlsx")}
        params_col = ft.Column(spacing=6)

        def target_columns() -> list[str]:
            m = next(m for m in maps if str(m.id) == mapping_dd.value)
            return [f["name"] for f in tgt_svc.get_target(m.target_id).fields]

        def param_row(p: dict) -> ft.Control:
            pname = ft.TextField(value=p.get("name", ""), dense=True, width=150, hint_text="query param")
            pcol = ft.Dropdown(value=p.get("column"), dense=True, width=200,
                               options=[ft.DropdownOption(key=c, text=c) for c in target_columns()])
            pop = ft.Dropdown(value=p.get("op", "eq"), dense=True, width=140,
                              options=[ft.DropdownOption(key=k, text=v) for k, v in OP_LABELS.items()])
            row = ft.Row([pname, pcol, pop], spacing=8)
            row.controls.append(ft.IconButton(ft.Icons.DELETE_OUTLINE,
                                              on_click=lambda _: (params_col.controls.remove(row), params_col.update())))
            row.data = (pname, pcol, pop)

            def auto_name(e):
                if not pname.value:
                    pname.value = slugify(e.control.value).replace("-", "_")
                    pname.update()
            pcol.on_select = auto_name
            return row

        for p in (ep.params if ep else []):
            params_col.controls.append(param_row(p))

        def mapping_changed(_=None):
            """Filter parameters pick from the chosen mapping's target columns: refresh their lists."""
            cols = target_columns()
            for r in params_col.controls:
                pcol = r.data[1]
                pcol.options = [ft.DropdownOption(key=c, text=c) for c in cols]
                if pcol.value not in cols:
                    pcol.value = None
            if mounted(params_col):
                params_col.update()

        mapping_dd.on_select = mapping_changed

        def add_param(_):
            params_col.controls.append(param_row({}))
            params_col.update()

        def save(_):
            values = {
                "name": name.value.strip(), "slug": slug.value.strip(), "mapping_id": int(mapping_dd.value),
                "description": desc.value or "", "page_size": int(page_size.value or settings.default_page_size),
                "public": public.value, "active": active.value,
                "formats": [k for k, cb in fmt.items() if cb.value],
                "params": [{"name": r.data[0].value, "column": r.data[1].value, "op": r.data[2].value}
                           for r in params_col.controls],
            }
            if not values["name"]:
                raise ValueError("Give the endpoint a name")
            self.app.require("design")
            saved = svc.save_endpoint(values, ep.id if ep else None)
            self.app.audit("endpoint.update" if ep else "endpoint.create", saved.slug,
                           "public" if saved.public else "API key")
            close_dialog(self.page)
            toast(self.page, f"Saved. GET {endpoint_url(saved.slug)}")
            self.app.navigate("endpoints")

        dialog(self.page, "Edit endpoint" if ep else "New endpoint", ft.Column([
            ft.Row([name, slug], spacing=10),
            mapping_dd, desc,
            ft.Row([page_size, *fmt.values()], spacing=14),
            ft.Row([public, active], spacing=20),
            ft.Text("Filters consumers can pass as query parameters", size=14, weight=ft.FontWeight.W_600),
            params_col,
            ft.TextButton("Add filter", icon=ft.Icons.ADD, on_click=add_param),
        ], spacing=12, scroll=ft.ScrollMode.AUTO), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Save", on_click=guarded(self.page, save)),
        ], width=720, height=560)

    def test(self, ep) -> None:
        inputs = {p["name"]: ft.TextField(label=f"{p['name']} ({OP_LABELS[p['op']]} {p['column']})", dense=True,
                                          width=220) for p in ep.params}
        page_tf = ft.TextField(label="page", value="1", dense=True, width=80)
        curl = ft.Text("", selectable=True, font_family="monospace", size=12)
        out = ft.Column(spacing=8)

        def send(_=None):
            args = {k: v.value for k, v in inputs.items() if v.value}
            res = svc.query(ep, args, page=int(page_tf.value or 1))
            qs = "&".join(f"{k}={v}" for k, v in {**args, "page": page_tf.value}.items())
            key_hdr = "" if ep.public else ' -H "X-API-Key: <your key>"'
            curl.value = f'curl{key_hdr} "{endpoint_url(ep.slug)}?{qs}"'
            sample = {"endpoint": ep.slug, "version": res["version"], "page": res["page"], "total": res["total"],
                      "data": svc.to_jsonable(res["df"].head(2))}
            out.controls = [
                ft.Text(f"{res['total']:,} matching rows · dataset v{res['version']}", size=13, weight=ft.FontWeight.W_600),
                df_table(res["df"], max_rows=20),
                ft.Text("JSON response (first 2 rows)", size=12, weight=ft.FontWeight.W_600),
                ft.Container(ft.Text(json.dumps(sample, indent=2, default=str), font_family="monospace", size=11.5,
                                     selectable=True), bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, padding=10,
                             border_radius=ft.BorderRadius.all(8)),
            ]
            out.update()
            curl.update()

        async def copy(_):
            await ft.Clipboard().set(curl.value)
            toast(self.page, "Copied")

        dialog(self.page, f"Test · {ep.slug}", ft.Column([
            ft.Row([*inputs.values(), page_tf, ft.FilledButton("Send", icon=ft.Icons.PLAY_ARROW,
                                                               on_click=guarded(self.page, send))], wrap=True, spacing=8),
            ft.Row([curl, ft.IconButton(ft.Icons.CONTENT_COPY, tooltip="Copy", on_click=copy)]),
            out,
        ], spacing=10, scroll=ft.ScrollMode.AUTO), [ft.TextButton("Close", on_click=lambda _: close_dialog(self.page))],
            width=900, height=580)
        guarded(self.page, send)()
