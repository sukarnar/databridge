"""AI > Models > Company models: endpoints with key mode, health, per-user keys, model catalog, defaults and the
configuration file (import / export)."""

from datetime import datetime

import flet as ft

from databridge.core.auth import ROLES
from databridge.services import ai_config, ai_health, config_sources, llm
from databridge.ui.views.config_sources_ui import UploadDialog, sources_section
from databridge.ui.common import background, card, chip, close_dialog, confirm, dialog, guarded, toast

HEALTH_COLOR = {"ok": ft.Colors.GREEN_600, "warning": ft.Colors.AMBER_700, "failed": ft.Colors.RED_500}
KEY_MODE_CHIP = {"shared": "company key", "per_user": "your own key", "none": "no key"}


def small(text: str, **kw) -> ft.Text:
    return ft.Text(text, size=11.5, color=ft.Colors.ON_SURFACE_VARIANT, **kw)


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    mins = int((datetime.now(dt.tzinfo) - dt).total_seconds() // 60)
    return "just now" if mins < 1 else (f"{mins} min ago" if mins < 90 else f"{mins // 60} h ago")


def health_dots(history: list[dict] | None) -> ft.Control:
    dots = [ft.Container(width=7, height=14, border_radius=ft.BorderRadius.all(2),
                         bgcolor=HEALTH_COLOR.get(h.get("status"), ft.Colors.OUTLINE),
                         tooltip=f"{h.get('at', '')}: {h.get('status')} ({h.get('ms', 0)} ms)")
            for h in (history or [])[-24:]]
    return ft.Row(dots, spacing=2) if dots else small("no checks yet")


class CompanyModels:
    """Builds the company endpoints section; `tab` is the ModelsTab (edit_ep, test_ep, delete_ep live there)."""

    def __init__(self, tab):
        self.tab, self.app, self.page = tab, tab.app, tab.page
        self.admin = self.app.can("manage_models")

    def build(self, tls_chips) -> list[ft.Control]:
        user = self.app.user
        endpoints = [e for e in llm.list_endpoints()
                     if self.admin or (e.enabled and user.role in (e.allowed_roles or []))]
        my_keys = llm.my_endpoint_keys(user.id)
        source_names = {src.id: src.name for src in config_sources.list_sources()}
        counts = llm.endpoint_key_counts() if self.admin else {}
        cards = []
        for e in endpoints:
            h = e.health or {}
            if h.get("status"):
                status = chip(f"{h['status']} · {_ago(h.get('checked_at'))}", HEALTH_COLOR.get(h["status"]))
            else:
                status = chip("connected", ft.Colors.GREEN_600) if e.last_test_ok else (
                    chip("failed", ft.Colors.RED_500) if e.last_test_ok is False else chip("not tested"))
            mode = e.key_mode or "shared"
            usable = llm.usable_models(e, user.role)
            names = [c.get("label") or c["id"] for c in usable]
            catalog = llm.catalog_of(e)
            chips = [status, chip(e.network, ft.Colors.TEAL_600 if e.network == "internal" else ft.Colors.AMBER_700),
                     chip(KEY_MODE_CHIP[mode], ft.Colors.INDIGO_400)]
            if (e.approval or "open") == "approved":
                chips.append(chip("approved models only", ft.Colors.INDIGO_400))
            if e.source_id and e.source_id in source_names:
                chips.append(chip(f"from {source_names[e.source_id]}", ft.Colors.TEAL_600))
            elif e.managed:
                chips.append(chip("from config file"))
            if not e.enabled:
                chips.append(chip("disabled"))
            chips += tls_chips(e)
            pending = [c["id"] for c in catalog if not c.get("enabled", True)] if self.admin else []
            lines = [
                ft.Row([ft.Text(e.name, size=14, weight=ft.FontWeight.W_600), *chips], spacing=8, wrap=True),
                small(f"{e.api_style} · {e.base_url} · roles: {', '.join(e.allowed_roles)}", selectable=True),
                small((f"{len(usable)} model(s) for you: " if mode != "per_user" or e.id in my_keys else
                       f"{len(usable)} model(s) once you add your key: ")
                      + (", ".join(names[:8]) + (" ..." if len(names) > 8 else "") if names else "none"), max_lines=2),
            ]
            if self.admin and pending:
                lines.append(ft.Text(f"{len(pending)} model(s) waiting for approval or disabled: "
                                     + ", ".join(pending[:6]), size=11.5, color=ft.Colors.AMBER_800))
            if h.get("message") and (h.get("status") == "failed" or (self.admin and h.get("status") == "warning")):
                lines.append(ft.Text(h["message"], size=11.5, color=HEALTH_COLOR.get(h.get("status")), max_lines=2,
                                     overflow=ft.TextOverflow.ELLIPSIS, tooltip=h["message"]))
            lines.append(ft.Row([small("Health:"), health_dots(e.health_history),
                                 *([small(f"{h['latency_ms']} ms")] if h.get("latency_ms") else []),
                                 *([small(f"cert expires in {h['cert_days']} d")] if h.get("cert_days") is not None
                                   else [])], spacing=6))
            if mode == "per_user":
                k = my_keys.get(e.id)
                key_row = ft.Row([
                    ft.Icon(ft.Icons.KEY, size=16, color=ft.Colors.GREEN_600 if k else ft.Colors.AMBER_700),
                    ft.Text(f"Your key: ...{k.key_hint}" + ("" if k.last_test_ok else " (last test failed)") if k
                            else "You haven't added your key yet: this endpoint uses individually issued keys.",
                            size=12.5, weight=ft.FontWeight.W_500),
                    ft.TextButton("Replace key" if k else "Add my key", icon=ft.Icons.ADD_MODERATOR,
                                  on_click=lambda _, ee=e: self.my_key(ee)),
                    *([ft.TextButton("Test", on_click=background(self.page, lambda _, ee=e: self.test_my_key(ee))),
                       ft.TextButton("Remove", on_click=lambda _, ee=e: confirm(
                           self.page, "Remove your key", f"Remove your key for {ee.name}?",
                           lambda: self.remove_my_key(ee)))] if k else []),
                    *([small(f"{counts.get(e.id, 0)} user(s) have added keys")] if self.admin else []),
                ], spacing=6, wrap=True)
                lines.append(key_row)
            actions = []
            if self.admin:
                actions = [
                    ft.OutlinedButton("Test", icon=ft.Icons.BOLT,
                                      on_click=background(self.page, lambda _, ee=e: self.check(ee))),
                    ft.IconButton(ft.Icons.CHECKLIST, tooltip="Model catalog: approve, rename, restrict",
                                  on_click=lambda _, ee=e: self.catalog(ee)),
                    ft.IconButton(ft.Icons.EDIT_OUTLINED, tooltip="Edit", on_click=lambda _, ee=e: self.tab.edit_ep(ee)),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete", on_click=lambda _, ee=e: confirm(
                        self.page, "Delete endpoint", f"Delete {ee.name}? Users' keys for it are deleted too.",
                        lambda: self.tab.delete_ep(ee))),
                ]
            cards.append(card(ft.Row([ft.Icon(ft.Icons.DNS, color=ft.Colors.PRIMARY),
                                      ft.Column(lines, spacing=3, expand=True), *actions],
                                     spacing=10, vertical_alignment=ft.CrossAxisAlignment.START), padding=12))

        header_actions = []
        if self.admin:
            header_actions = [
                ft.FilledTonalButton("Upload config.yaml", icon=ft.Icons.UPLOAD_FILE,
                                     tooltip="The company's LLM configuration (Continue config.yaml or DataBridge format)",
                                     on_click=lambda _: UploadDialog(self.page, self.app).open()),
                ft.OutlinedButton("Default models", icon=ft.Icons.STAR_OUTLINE, on_click=lambda _: self.defaults()),
                ft.OutlinedButton("Paste config", icon=ft.Icons.CONTENT_PASTE, on_click=lambda _: self.import_config()),
                ft.OutlinedButton("Export config", icon=ft.Icons.DOWNLOAD, on_click=guarded(
                    self.page, lambda _: self.export_config())),
                ft.OutlinedButton("Check health now", icon=ft.Icons.MONITOR_HEART_OUTLINED,
                                  on_click=background(self.page, lambda _: self.check_all())),
                ft.FilledButton("Add endpoint", icon=ft.Icons.ADD, on_click=lambda _: self.tab.edit_ep(None)),
            ]
        banner = []
        if self.admin:
            issues = ai_health.problems()
            if issues:
                banner = [ft.Container(ft.Row([ft.Icon(ft.Icons.WARNING_AMBER, color=ft.Colors.RED_500),
                                               ft.Column([ft.Text(i, size=12.5) for i in issues], spacing=2,
                                                         expand=True)], spacing=10),
                                       bgcolor=ft.Colors.with_opacity(0.08, ft.Colors.RED), padding=10,
                                       border_radius=ft.BorderRadius.all(8))]
        return [
            ft.Text("Company models", size=16, weight=ft.FontWeight.W_600),
            *([ft.Row(header_actions, spacing=8, wrap=True)] if header_actions else []),
            small("LLMs hosted in or for the company: local servers (Ollama, vLLM, LM Studio) or the company AI "
                  "gateway. Some use one company key; others use keys issued to each person, which you add here."
                  + ("" if self.admin else " Ask an admin to add an endpoint.")),
            *banner,
            *(sources_section(self.page, self.app) if self.admin else []),
            *([ft.Text("Endpoints", size=14, weight=ft.FontWeight.W_600)] if self.admin and source_names else []),
            ft.Column(cards, spacing=8) if cards else small("None available to you."),
        ]

    # ---------------------------------------------------------------- per-user keys
    def my_key(self, ep) -> None:
        key = ft.TextField(label=f"Your key for {ep.name}", password=True, can_reveal_password=True, dense=True,
                           autofocus=True)

        def save(_):
            ok, msg = llm.save_my_endpoint_key(self.app.user, ep.id, key.value)
            close_dialog(self.page)
            toast(self.page, f"Key saved and tested. {msg}")
            self.app.navigate("ai", tab="models")

        dialog(self.page, "Add your key", ft.Column([
            small("Paste the key the company issued to you for this LLM. It is tested, stored encrypted, used only "
                  "for your calls and your workflows, and never shown again (admins can't read it either)."),
            key], spacing=12, tight=True),
            [ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
             ft.FilledButton("Save and test", on_click=background(self.page, save))], width=520)

    def test_my_key(self, ep) -> None:
        ok, msg = llm.test_my_endpoint_key(self.app.user, ep.id)
        toast(self.page, f"{ep.name}: {msg}", error=not ok)
        self.app.navigate("ai", tab="models")

    def remove_my_key(self, ep) -> None:
        llm.delete_my_endpoint_key(self.app.user, ep.id)
        self.app.navigate("ai", tab="models")

    # ---------------------------------------------------------------- health
    def check(self, ep) -> None:
        self.app.require("manage_models")
        llm.test_endpoint(ep.id, self.app.user.username)  # discovery (fills the catalog)
        name, result = (ai_health.run_checks(ep.id) or [(ep.name, {"status": "failed", "message": "disabled"})])[0]
        toast(self.page, f"{name}: {result['status']}. {result['message']}", error=result["status"] == "failed")
        self.app.navigate("ai", tab="models")

    def check_all(self) -> None:
        self.app.require("manage_models")
        results = ai_health.run_checks()
        bad = [n for n, r in results if r["status"] != "ok"]
        toast(self.page, f"Checked {len(results)} endpoint(s)" + (f"; attention: {', '.join(bad)}" if bad else "; all ok"),
              error=bool(bad))
        self.app.navigate("ai", tab="models")

    # ---------------------------------------------------------------- catalog
    def catalog(self, ep) -> None:
        self.app.require("manage_models")
        entries = [dict(c) for c in llm.catalog_of(ep)]
        approval = ft.Dropdown(label="Which models may be used", value=ep.approval or "open", dense=True, width=460,
                               options=[ft.DropdownOption(key=k, text=v) for k, v in llm.APPROVAL.items()])
        rows = ft.Column(spacing=6)
        role_opts = [r for r in ROLES if r != "viewer"]

        def redraw():
            rows.controls = []
            for c in entries:
                def upd(key, it=c):
                    return lambda e: it.__setitem__(key, e.control.value)

                def role_toggle(role, it=c):
                    def f(e):
                        roles = set(it.get("roles") or [])
                        (roles.add if e.control.value else roles.discard)(role)
                        it["roles"] = sorted(roles)
                    return f

                rows.controls.append(ft.Container(ft.Column([
                    ft.Row([ft.Checkbox(value=c.get("enabled", True), on_change=upd("enabled"), tooltip="Enabled"),
                            ft.Text(c["id"], size=13, weight=ft.FontWeight.W_600, width=180,
                                    overflow=ft.TextOverflow.ELLIPSIS),
                            ft.TextField(value=c.get("label", ""), label="Display name", dense=True, width=200,
                                         on_change=upd("label")),
                            ft.TextField(value=c.get("description", ""), label="Description (shown to users)",
                                         dense=True, expand=True, on_change=upd("description"))], spacing=8),
                    ft.Row([small("Only for roles (none ticked = everyone allowed on the endpoint):"),
                            *[ft.Checkbox(label=r, value=r in (c.get("roles") or []), on_change=role_toggle(r))
                              for r in role_opts]], spacing=6),
                ], spacing=2), padding=ft.Padding.only(bottom=6),
                    border=ft.Border.only(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT))))
            if ft_mounted(rows):
                rows.update()

        new_id = ft.TextField(label="Add a model by id", hint_text="if the server does not list it", dense=True, width=360)

        def add(_):
            if new_id.value and new_id.value not in {c["id"] for c in entries}:
                entries.append({"id": new_id.value.strip(), "label": "", "description": "", "roles": [],
                                "enabled": True})
                new_id.value = ""
                new_id.update()
                redraw()

        def save(_):
            llm.save_catalog(ep.id, entries, approval.value, self.app.user.username)
            close_dialog(self.page)
            toast(self.page, "Catalog saved")
            self.app.navigate("ai", tab="models")

        redraw()
        dialog(self.page, f"Models on {ep.name}", ft.Column([
            small("Approve models, give them friendly names and descriptions, and limit some to roles. With "
                  "'Only models an admin enabled', newly discovered models stay off until you enable them."),
            approval, rows, ft.Row([new_id, ft.TextButton("Add", icon=ft.Icons.ADD, on_click=add)]),
        ], spacing=10, scroll=ft.ScrollMode.AUTO), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Save", on_click=guarded(self.page, save))], width=900, height=560)

    # ---------------------------------------------------------------- defaults
    def defaults(self) -> None:
        self.app.require("manage_models")
        current = llm.get_defaults()
        # every enabled catalog model of every enabled company endpoint (not just the ones this admin can call)
        options = [llm.ModelOption(f"endpoint:{ep.id}/{c['id']}", c["id"], ep.name, "shared", ep.network,
                                   c.get("label") or "")
                   for ep in llm.list_endpoints(include_disabled=False) for c in llm.catalog_of(ep)
                   if c.get("enabled", True)]
        drops = {t: ft.Dropdown(label=label, value=current.get(t) or "", dense=True, width=520, options=[
            ft.DropdownOption(key="", text="(none: first model the user can use)"),
            *[ft.DropdownOption(key=o.ref, text=o.label) for o in options]]) for t, label in llm.TASKS.items()}

        def save(_):
            llm.set_defaults({t: d.value for t, d in drops.items()}, self.app.user.username)
            close_dialog(self.page)
            toast(self.page, "Default models saved")

        dialog(self.page, "Default models", ft.Column([
            small("Pre-selected for everyone. Users who may not use a default (role, missing key, not approved) get "
                  "the first model available to them."), *drops.values()], spacing=12, tight=True),
            [ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
             ft.FilledButton("Save", on_click=guarded(self.page, save))], width=580)

    # ---------------------------------------------------------------- config file
    def import_config(self) -> None:
        self.app.require("manage_models")
        text = ft.TextField(label="Configuration (YAML)", multiline=True, min_lines=12, max_lines=18, dense=True,
                            text_style=ft.TextStyle(font_family="monospace", size=12))
        report = ft.Column(spacing=4)

        async def pick(_):
            from databridge.ui.uploads import pick_and_upload

            try:
                picked = await pick_and_upload(self.page, ["yaml", "yml"], 1024 * 1024)
            except ValueError as e:
                toast(self.page, str(e), error=True)
                return
            if picked:
                text.value = picked[1].decode("utf-8", "replace")
                text.update()

        def show(lines: list[str], error: bool = False) -> None:
            report.controls = [ft.Text(("Error: " if error else "- ") + line, size=12.5,
                                       color=ft.Colors.RED_600 if error else None) for line in lines]
            report.update()

        def preview(_):
            try:
                show(["Preview (nothing changed yet):"] + ai_config.apply(text.value or "", self.app.user.username,
                                                                         dry_run=True))
            except ai_config.ConfigError as e:
                show([str(e)], error=True)

        def apply(_):
            try:
                lines = ai_config.apply(text.value or "", self.app.user.username)
            except ai_config.ConfigError as e:
                show([str(e)], error=True)
                return
            close_dialog(self.page)
            toast(self.page, "Applied: " + "; ".join(lines)[:300])
            self.app.navigate("ai", tab="models")

        dialog(self.page, "Import company LLM configuration", ft.Column([
            small("Endpoints are created or updated by name and marked as managed by the file. Keys are never in "
                  "the file: it names environment variables (api_key_env) and certificate files on the server. To "
                  "apply a file at every start, set DATABRIDGE_AI_CONFIG_FILE."),
            ft.OutlinedButton("Choose a .yaml file", icon=ft.Icons.FOLDER_OPEN, on_click=pick),
            text, report], spacing=10, scroll=ft.ScrollMode.AUTO), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.OutlinedButton("Preview", on_click=guarded(self.page, preview)),
            ft.FilledButton("Apply", on_click=guarded(self.page, apply))], width=820, height=600)

    def export_config(self) -> None:
        self.app.require("manage_models")
        content = ai_config.export()

        async def copy(_):
            await ft.Clipboard().set(content)
            toast(self.page, "Copied")

        async def download(_):
            await ft.FilePicker().save_file(file_name="databridge-llm.yaml", src_bytes=content.encode())

        dialog(self.page, "Company LLM configuration", ft.Column([
            small("No secrets are included. Keep it in your deployment repository and apply it with "
                  "DATABRIDGE_AI_CONFIG_FILE, or import it on another server."),
            ft.Container(ft.Text(content, font_family="monospace", size=11.5, selectable=True), padding=10,
                         bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, border_radius=ft.BorderRadius.all(8)),
        ], spacing=10, scroll=ft.ScrollMode.AUTO), [
            ft.TextButton("Copy", icon=ft.Icons.CONTENT_COPY, on_click=copy),
            ft.OutlinedButton("Download", icon=ft.Icons.DOWNLOAD, on_click=download),
            ft.FilledButton("Close", on_click=lambda _: close_dialog(self.page))], width=820, height=600)


def ft_mounted(control) -> bool:
    from databridge.ui.common import mounted

    return mounted(control)
