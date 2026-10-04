"""Admin UI for uploaded company LLM configuration files (Continue config.yaml or DataBridge format)."""

import flet as ft

from databridge.core.auth import ROLES
from databridge.services import config_sources as svc
from databridge.ui.common import card, chip, close_dialog, confirm, dialog, guarded, mounted, toast

FORMAT_LABEL = {"continue": "Continue config.yaml", "databridge": "DataBridge config"}
KEY_CHOICES = {
    "per_user": "Each user adds the key issued to them (recommended)",
    "shared": "Use the key in the file as the company key for everyone",
    "none": "No key",
}


def small(text: str, **kw) -> ft.Text:
    return ft.Text(text, size=11.5, color=ft.Colors.ON_SURFACE_VARIANT, **kw)


async def _pick(page: ft.Page, extensions: list[str]) -> tuple[str, bytes] | None:
    from databridge.ui.uploads import pick_and_upload

    try:
        return await pick_and_upload(page, extensions, 2 * 1024 * 1024)
    except ValueError as e:
        toast(page, str(e), error=True)
        return None


class UploadDialog:
    """Pick a config file, upload the CA files it references, choose options, preview the mapping, apply."""

    def __init__(self, page: ft.Page, app, source_id: int | None = None):
        self.page, self.app, self.source_id = page, app, source_id
        src = svc.get_source(source_id) if source_id else None
        self.text, self.file_name = "", ""
        self.ca_files: dict[str, bytes] = {}
        self.options = dict(src.options) if src else dict(svc.DEFAULT_OPTIONS)
        self.body = ft.Column(spacing=10, scroll=ft.ScrollMode.AUTO)
        self.apply_btn = ft.FilledButton("Apply", icon=ft.Icons.CHECK, disabled=True,
                                         on_click=guarded(page, self.apply))
        self.title = f"New version of {src.name}" if src else "Upload company LLM config"

    def open(self) -> None:
        self.render()
        dialog(self.page, self.title, self.body, [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)), self.apply_btn], width=1000, height=640)

    async def pick_file(self, _=None) -> None:
        got = await _pick(self.page, ["yaml", "yml"])
        if got:
            self.file_name, raw = got
            self.text = raw.decode("utf-8", "replace")
            self.render()

    async def pick_ca(self, _=None, expected: str | None = None) -> None:
        got = await _pick(self.page, ["pem", "crt", "cer", "der"])
        if got:
            name, data = got
            if expected and svc.cc.ca_key(name) != expected:
                name = expected  # the admin picked the right certificate under another file name
            self.ca_files[name] = data
            self.render()

    def set_option(self, key, value) -> None:
        self.options[key] = value
        self.render()

    def render(self) -> None:
        c: list[ft.Control] = [
            small("Upload the configuration file the company provides (Continue config.yaml, or DataBridge's own "
                  "format). DataBridge keeps it as the source, maps each model to a company endpoint, and shows the "
                  "result before anything changes."),
            ft.Row([ft.OutlinedButton("Choose config.yaml", icon=ft.Icons.FOLDER_OPEN, on_click=self.pick_file),
                    small(self.file_name or "no file chosen")], spacing=10),
        ]
        self.apply_btn.disabled = True
        if self.text:
            try:
                p = svc.plan(self.text, self.file_name, self.options, self.ca_files, self.source_id)
            except svc.SourceError as e:
                c.append(ft.Container(ft.Text(str(e), size=12.5, color=ft.Colors.RED_700, selectable=True),
                                      bgcolor=ft.Colors.with_opacity(0.08, ft.Colors.RED), padding=10,
                                      border_radius=ft.BorderRadius.all(8)))
                p = None
            if p:
                c += self.preview(p)
                self.apply_btn.disabled = False
        self.body.controls = c
        if mounted(self.body):
            self.body.update()
        if mounted(self.apply_btn):
            self.apply_btn.update()

    def preview(self, p: dict) -> list[ft.Control]:
        out: list[ft.Control] = [ft.Row([
            chip(FORMAT_LABEL.get(p["format"], p["format"]), ft.Colors.INDIGO_400),
            ft.Text(p["name"], size=15, weight=ft.FontWeight.W_600),
            *([small(f"version {p['version']}")] if p["version"] else []),
            *([chip(f"updates the existing source (revision {p['revision']})", ft.Colors.TEAL_600)]
              if p.get("source_id") and not self.source_id else [])], spacing=8, wrap=True)]
        if p["repairs"]:
            out.append(ft.Container(ft.Column(
                [ft.Text("The file had copy/paste damage, repaired automatically:", size=12.5,
                         weight=ft.FontWeight.W_500)] + [small("- " + r) for r in p["repairs"]], spacing=2),
                bgcolor=ft.Colors.with_opacity(0.08, ft.Colors.BLUE), padding=10, border_radius=ft.BorderRadius.all(8)))
        if p["format"] == "continue":
            out.append(ft.Text("Mapping", size=14, weight=ft.FontWeight.W_600))
            out.append(ft.Row([ft.DataTable(
                columns=[ft.DataColumn(ft.Text(h, size=12, weight=ft.FontWeight.W_600)) for h in
                         ("Model", "Model id", "DataBridge endpoint", "Auth header", "TLS", "Roles", "Use")],
                rows=[ft.DataRow(cells=[
                    ft.DataCell(ft.Text(r["model"], size=12)), ft.DataCell(ft.Text(r["id"], size=12)),
                    ft.DataCell(ft.Text(r["endpoint"], size=12, tooltip=r["base_url"])),
                    ft.DataCell(ft.Text(r["auth"], size=12)), ft.DataCell(ft.Text(r["tls"], size=12)),
                    ft.DataCell(ft.Text(r["roles"], size=12)),
                    ft.DataCell(chip("chat", ft.Colors.GREEN_600) if r["enabled"] else
                                chip("off", ft.Colors.OUTLINE))]) for r in p["rows"]],
                data_row_min_height=34, data_row_max_height=40, column_spacing=18)], scroll=ft.ScrollMode.AUTO))
            if p["ca_needed"]:
                rows = []
                for need in p["ca_needed"]:
                    rows.append(ft.Row([
                        ft.Icon(ft.Icons.CHECK_CIRCLE if need["uploaded"] else ft.Icons.UPLOAD_FILE, size=18,
                                color=ft.Colors.GREEN_600 if need["uploaded"] else ft.Colors.AMBER_700),
                        ft.Text(need["file"], size=12.5, weight=ft.FontWeight.W_500),
                        small("uploaded" if need["uploaded"] else f"referenced as {need['path']}"),
                        ft.TextButton("Replace" if need["uploaded"] else "Upload this CA file",
                                      on_click=lambda e, k=need["file"]: self.page.run_task(self.pick_ca, e, k)),
                    ], spacing=8, wrap=True))
                out.append(ft.Column([ft.Text("Certificates", size=14, weight=ft.FontWeight.W_600),
                                      small("caBundlePath points at a file on someone's computer. Upload that file "
                                            "once; every endpoint that uses it will trust it."), *rows], spacing=4))
            for n in p["ca_notes"]:
                out.append(small(n))
            roles = [r for r in ROLES if r != "viewer"]
            out.append(ft.Column([
                ft.Text("Options", size=14, weight=ft.FontWeight.W_600),
                ft.RadioGroup(value=self.options["key_mode"], on_change=lambda e: self.set_option("key_mode", e.control.value),
                              content=ft.Column([ft.Radio(value=k, label=v) for k, v in KEY_CHOICES.items()], spacing=0)),
                ft.Row([ft.Text("Network:", size=13),
                        ft.RadioGroup(value=self.options["network"],
                                      on_change=lambda e: self.set_option("network", e.control.value),
                                      content=ft.Row([ft.Radio(value="internal", label="Internal (company network)"),
                                                      ft.Radio(value="external", label="External")]))], spacing=8),
                ft.Row([ft.Text("Roles that may use these models:", size=13),
                        *[ft.Checkbox(label=r, value=r in self.options["allowed_roles"],
                                      on_change=lambda e, rr=r: self.set_option(
                                          "allowed_roles", sorted(set(self.options["allowed_roles"]) ^ {rr})))
                          for r in roles]], spacing=8),
                ft.Checkbox(label="Make the first chat model the default (only if no defaults are set yet)",
                            value=bool(self.options.get("set_defaults", True)),
                            on_change=lambda e: self.set_option("set_defaults", bool(e.control.value))),
            ], spacing=6))
        if p["warnings"]:
            out.append(ft.Container(ft.Column([ft.Text(w, size=12, color=ft.Colors.AMBER_900) for w in p["warnings"]],
                                              spacing=3),
                                    bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.AMBER), padding=10,
                                    border_radius=ft.BorderRadius.all(8)))
        out.append(ft.Column([ft.Text("What Apply will do", size=14, weight=ft.FontWeight.W_600),
                              *[small("- " + line) for line in p["report"]]], spacing=2))
        out.append(ft.ExpansionTile(
            title=ft.Text("DataBridge standard configuration (generated)", size=13),
            subtitle=small("The mapping in DataBridge's own format; keys are never included"),
            controls=[ft.Container(ft.Text(p["standard"], font_family="monospace", size=11, selectable=True),
                                   padding=10, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                                   border_radius=ft.BorderRadius.all(8))]))
        return out

    def apply(self, _=None) -> None:
        src = svc.save(self.text, self.app.user.username, self.file_name, self.options, self.ca_files, self.source_id)
        close_dialog(self.page)
        toast(self.page, f"{src.name}: {len(src.endpoint_ids)} endpoint(s) configured (revision {src.revision}). "
                         "Users can now add their keys under Company models.")
        self.app.navigate("ai", tab="models")


def sources_section(page: ft.Page, app) -> list[ft.Control]:
    """Config sources list for admins."""
    items = []
    for src in svc.list_sources():
        notes = len(src.notes or [])
        items.append(card(ft.Row([
            ft.Icon(ft.Icons.DESCRIPTION_OUTLINED, color=ft.Colors.PRIMARY),
            ft.Column([
                ft.Row([ft.Text(src.name, size=14, weight=ft.FontWeight.W_600),
                        chip(FORMAT_LABEL.get(src.format, src.format), ft.Colors.INDIGO_400),
                        chip(f"revision {src.revision}"),
                        *([chip(f"{notes} note(s)", ft.Colors.AMBER_700)] if notes else [])], spacing=8, wrap=True),
                small(f"{src.file_name or 'file'} · version {src.version or '-'} · {len(src.endpoint_ids)} endpoint(s) · "
                      f"uploaded by {src.uploaded_by} {src.uploaded_at:%Y-%m-%d %H:%M} · keys: "
                      f"{KEY_CHOICES.get(src.options.get('key_mode'), '-').split(' (')[0].lower()}"),
            ], spacing=2, expand=True),
            ft.TextButton("View file", on_click=lambda _, s=src: view_source(page, s)),
            ft.OutlinedButton("New version", icon=ft.Icons.UPLOAD_FILE,
                              on_click=lambda _, s=src: UploadDialog(page, app, s.id).open()),
            ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete source", on_click=lambda _, s=src: delete_source(page, app, s)),
        ], spacing=8), padding=12))
    if not items:
        return []
    return [ft.Text("Config sources", size=14, weight=ft.FontWeight.W_600),
            small("Company configuration files uploaded here. Endpoints they created show 'from <source>' and are "
                  "updated when you upload a new version."), ft.Column(items, spacing=8)]


def view_source(page: ft.Page, src) -> None:
    tabs = ft.Tabs(selected_index=0, length=3, content=ft.Column([
        ft.TabBar(tabs=[ft.Tab(label="Uploaded file"), ft.Tab(label="Mapping"), ft.Tab(label="Notes")]),
        ft.TabBarView(expand=True, controls=[
            ft.Column([ft.Container(ft.Text(src.original, font_family="monospace", size=11.5, selectable=True),
                                    padding=10, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                                    border_radius=ft.BorderRadius.all(8))], scroll=ft.ScrollMode.AUTO),
            ft.Column([ft.Container(ft.Text(src.standard, font_family="monospace", size=11.5, selectable=True),
                                    padding=10, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                                    border_radius=ft.BorderRadius.all(8))], scroll=ft.ScrollMode.AUTO),
            ft.Column([small("- " + n) for n in src.notes or ["none"]], scroll=ft.ScrollMode.AUTO),
        ]),
    ], expand=True), expand=True)
    dialog(page, src.name, tabs, [ft.FilledButton("Close", on_click=lambda _: close_dialog(page))],
           width=900, height=600)


def delete_source(page: ft.Page, app, src) -> None:
    also = ft.Checkbox(label=f"Also delete its {len(src.endpoint_ids)} endpoint(s) and users' keys for them",
                       value=False)

    def go(_):
        svc.delete(src.id, app.user.username, delete_endpoints=bool(also.value))
        close_dialog(page)
        app.navigate("ai", tab="models")

    dialog(page, f"Delete {src.name}", ft.Column([
        ft.Text("The uploaded file is removed. Without the option below, its endpoints stay as ordinary endpoints.",
                size=13), also], tight=True, spacing=10),
        [ft.TextButton("Cancel", on_click=lambda _: close_dialog(page)),
         ft.FilledButton("Delete", on_click=guarded(page, go))], width=520)


__all__ = ["UploadDialog", "sources_section", "confirm"]
