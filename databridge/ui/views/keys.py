"""API keys: issue (shown once), scope to endpoints, revoke."""

import flet as ft

from databridge.services import endpoints as svc
from databridge.ui.common import chip, close_dialog, confirm, dialog, empty_state, guarded, page_header, toast


class KeysView:
    def __init__(self, app):
        self.app = app
        self.page = app.page

    def build(self) -> ft.Control:
        self.app.require("manage_keys")
        keys = svc.list_api_keys()
        rows = [ft.DataRow(cells=[
            ft.DataCell(ft.Text(k.name, size=13, weight=ft.FontWeight.W_500)),
            ft.DataCell(ft.Text(k.prefix + "…", size=12, font_family="monospace")),
            ft.DataCell(ft.Text("all endpoints (admin)" if "*" in k.endpoints else ", ".join(k.endpoints), size=12)),
            ft.DataCell(ft.Text(k.created_at.strftime("%Y-%m-%d") if k.created_at else "", size=12)),
            ft.DataCell(ft.Text(k.last_used_at.strftime("%Y-%m-%d %H:%M") if k.last_used_at else "never", size=12)),
            ft.DataCell(chip("active", ft.Colors.GREEN_600) if k.active else chip("revoked", ft.Colors.RED_500)),
            ft.DataCell(ft.TextButton("Revoke", disabled=not k.active, on_click=lambda _, kk=k: confirm(
                self.page, "Revoke key", f"Revoke {kk.name}? Calls using it fail immediately.",
                lambda: self.revoke(kk)))),
        ]) for k in keys]
        new_btn = ft.FilledButton("New API key", icon=ft.Icons.ADD, on_click=lambda _: self.new())
        body = ft.DataTable(columns=[ft.DataColumn(ft.Text(h, size=12, weight=ft.FontWeight.W_600)) for h in
                                     ("Name", "Key", "Scope", "Created", "Last used", "Status", "")],
                            rows=rows, column_spacing=22) if rows else empty_state(
            ft.Icons.KEY, "No API keys yet", "Consumers send a key in the X-API-Key header.", new_btn)
        return ft.Column([
            page_header("API keys", "Keys are stored hashed; the full key is shown only once. "
                        "Keys scoped to all endpoints can also ingest files and trigger refreshes.", [new_btn]),
            body,
        ], spacing=18, scroll=ft.ScrollMode.AUTO, expand=True)

    def revoke(self, key) -> None:
        self.app.require("manage_keys")
        svc.revoke_api_key(key.id)
        self.app.audit("api_key.revoke", key.name, key.prefix)
        self.app.navigate("keys")

    def new(self) -> None:
        eps = svc.list_endpoints()
        name = ft.TextField(label="Who is this key for?", hint_text="e.g. Finance dashboard", autofocus=True)
        all_eps = ft.Checkbox(label="All endpoints, plus ingest and refresh (admin)", value=not eps)
        boxes = {e.slug: ft.Checkbox(label=e.slug, value=False) for e in eps}
        from databridge.services import workflows as wf_svc

        wf_boxes = {f"workflow:{w.slug}": ft.Checkbox(label=f"Run workflow: {w.name}", value=False)
                    for w in wf_svc.list_workflows() if w.published_version}
        boxes |= wf_boxes

        def create(_):
            scope = ["*"] if all_eps.value else [s for s, cb in boxes.items() if cb.value]
            if not scope:
                raise ValueError("Choose at least one endpoint or workflow, or all endpoints")
            self.app.require("manage_keys")
            key, raw = svc.create_api_key(name.value.strip() or "unnamed", scope)
            self.app.audit("api_key.create", key.name, f"{key.prefix} scope={','.join(scope)}")
            close_dialog(self.page)

            async def copy(_):
                await ft.Clipboard().set(raw)
                toast(self.page, "Key copied")

            dialog(self.page, "Copy your key now", ft.Column([
                ft.Text("This is the only time the full key is shown.", size=13),
                ft.Container(ft.Row([ft.Text(raw, selectable=True, font_family="monospace", size=13, expand=True),
                                     ft.IconButton(ft.Icons.CONTENT_COPY, on_click=copy)]),
                             bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, padding=10, border_radius=ft.BorderRadius.all(8)),
            ], tight=True, spacing=10), [ft.FilledButton("Done", on_click=lambda _: (close_dialog(self.page),
                                                                                      self.app.navigate("keys")))],
                width=560)

        dialog(self.page, "New API key", ft.Column([name, all_eps, *[cb for k, cb in boxes.items() if k not in wf_boxes],
                                                    *([ft.Text("AI workflows (POST /api/v1/workflows/{slug}/run)",
                                                               size=12.5, weight=ft.FontWeight.W_600)] if wf_boxes else []),
                                                    *wf_boxes.values()], tight=True, spacing=8,
                                                   scroll=ft.ScrollMode.AUTO), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Create key", on_click=guarded(self.page, create)),
        ], width=480)
