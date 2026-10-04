"""AI section: Playground, Prompts, Models (shared endpoints + personal API keys), Usage."""

import json
from datetime import date

import flet as ft

from databridge.ai import tls
from databridge.ai.providers import LOCAL_PRESETS, PRESETS
from databridge.ai.templating import PromptTemplateError, variables_in
from databridge.core.auth import ROLES
from databridge.services import llm, prompts
from databridge.ui.views.company_models import CompanyModels
from databridge.ui.common import (background, card, chip, close_dialog, confirm, dialog, empty_state, guarded, mounted,
                                  page_header, toast)

TABS = [("playground", "Playground", ft.Icons.SCIENCE), ("prompts", "Prompts", ft.Icons.TEXT_SNIPPET),
        ("workflows", "Workflows", ft.Icons.ACCOUNT_TREE), ("models", "Models", ft.Icons.MEMORY),
        ("usage", "Usage", ft.Icons.INSIGHTS)]


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "never"


def model_dropdown(options: list[llm.ModelOption], value: str | None, label: str = "Model", width: int = 420,
                   allow_none: bool = False) -> ft.Dropdown:
    opts = [ft.DropdownOption(key="", text="(choose when running)")] if allow_none else []
    opts += [ft.DropdownOption(key=o.ref, text=o.label) for o in options]
    refs = {o.ref for o in options}
    if value and value not in refs:
        opts.append(ft.DropdownOption(key=value, text=f"{value} (not available to you)"))
    return ft.Dropdown(label=label, value=value if value else ("" if allow_none else (options[0].ref if options else None)),
                       options=opts, dense=True, width=width, enable_filter=True, editable=True)


class AIView:
    def __init__(self, app, tab: str = "playground", prompt_id: int | None = None):
        self.app = app
        self.page = app.page
        self.tab = tab
        self.prompt_id = prompt_id

    def build(self) -> ft.Control:
        self.app.require("use_ai")
        seg = ft.SegmentedButton(
            segments=[ft.Segment(value=k, label=ft.Text(label), icon=ft.Icon(icon)) for k, label, icon in TABS],
            selected=[self.tab],
            on_change=lambda e: self.app.navigate("ai", tab=e.control.selected[0]),
        )
        from databridge.ui.views.workflows import WorkflowsTab

        body = {"playground": lambda: Playground(self).build(), "prompts": lambda: PromptsTab(self).build(),
                "workflows": lambda: WorkflowsTab(self).build(),
                "models": lambda: ModelsTab(self).build(), "usage": lambda: UsageTab(self).build()}[self.tab]()
        return ft.Column([
            page_header("AI", "Try models, keep reusable prompts, build workflows with guardrails, and manage LLMs."),
            seg, body,
        ], spacing=14, expand=True, scroll=ft.ScrollMode.AUTO if self.tab != "playground" else None)


# ================================================================== Playground


class Playground:
    def __init__(self, view: AIView):
        self.view, self.app, self.page = view, view.app, view.page
        self.options = llm.available_models(self.app.user)
        self.prompt = prompts.get_prompt(view.prompt_id) if view.prompt_id else None

    def build(self) -> ft.Control:
        if not self.options:
            return empty_state(ft.Icons.MEMORY, "No models available yet",
                               "Add the key the company issued you for its LLM, or your own API key (OpenAI, Anthropic, "
                               "Gemini, ...), in the Models tab. Or ask an admin to add a company model endpoint.",
                               ft.FilledButton("Add an API key", icon=ft.Icons.KEY,
                                               on_click=lambda _: self.app.navigate("ai", tab="models")))
        p = self.prompt
        self.model = model_dropdown(self.options, p.model_ref if p and p.model_ref else
                                    llm.default_model(self.app.user, "playground", self.options))
        self.library = ft.Dropdown(label="Load a saved prompt", dense=True, width=300,
                                   value=str(p.id) if p else None,
                                   options=[ft.DropdownOption(key=str(x.id), text=x.name) for x in prompts.list_prompts()],
                                   on_select=lambda e: self.app.navigate("ai", tab="playground",
                                                                         prompt_id=int(e.control.value)))
        self.system = ft.TextField(label="System prompt", multiline=True, min_lines=3, max_lines=8,
                                   value=p.system if p else "", on_blur=lambda _: self.refresh_vars())
        self.user = ft.TextField(label="User prompt  (use {{ variable }} for inputs)", multiline=True, min_lines=5,
                                 max_lines=12, value=p.user if p else "", on_blur=lambda _: self.refresh_vars())
        self.temperature = ft.TextField(label="Temperature", width=140, dense=True,
                                        value="" if not p or p.temperature is None else str(p.temperature))
        self.max_tokens = ft.TextField(label="Max tokens", width=140, dense=True, value=str(p.max_tokens if p else 1024))
        self.json_mode = ft.Checkbox(label="JSON output", value=bool(p.json_mode) if p else False)
        self.var_box = ft.Column(spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.var_fields: dict[str, ft.TextField] = {}
        self.defaults = {v["name"]: v.get("default", "") for v in (p.variables if p else [])}
        self.refresh_vars(initial=True)

        self.status = ft.Row([], spacing=8, wrap=True)
        self.output = ft.Column([ft.Text("Run a prompt to see the model's answer here.",
                                         color=ft.Colors.ON_SURFACE_VARIANT)], spacing=8, scroll=ft.ScrollMode.AUTO,
                                expand=True)
        self.spinner = ft.ProgressRing(visible=False, width=22, height=22)
        run = ft.FilledButton("Run", icon=ft.Icons.PLAY_ARROW, on_click=background(self.page, self.run))
        actions = [run, self.spinner]
        if self.app.can("design"):
            actions.append(ft.OutlinedButton("Save as prompt" if not p else "Update prompt draft",
                                             icon=ft.Icons.SAVE_OUTLINED, on_click=guarded(self.page, self.save)))
        left = ft.Column([
            ft.Row([self.model, self.library], wrap=True, spacing=10),
            self.system, self.user,
            ft.Text("Variables", size=13, weight=ft.FontWeight.W_600), self.var_box,
            ft.Row([self.temperature, self.max_tokens, self.json_mode], spacing=10),
            ft.Row(actions, spacing=10),
        ], spacing=12, scroll=ft.ScrollMode.AUTO, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        right = ft.Column([ft.Row([ft.Text("Response", size=15, weight=ft.FontWeight.W_600)]), self.status,
                           self.output], spacing=8, expand=True)
        return ft.Row([ft.Container(card(left), expand=3), ft.Container(card(right), expand=2)],
                      expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH, spacing=14)

    def refresh_vars(self, initial: bool = False) -> None:
        try:
            names = variables_in(self.system.value or "", self.user.value or "")
        except PromptTemplateError as e:
            self.var_box.controls = [ft.Text(str(e), color=ft.Colors.ERROR, size=12)]
            if not initial and mounted(self.var_box):
                self.var_box.update()
            return
        current = {n: f.value for n, f in self.var_fields.items()}
        self.var_fields = {n: ft.TextField(label=n, dense=True, multiline=True, max_lines=4,
                                           value=current.get(n, self.defaults.get(n, ""))) for n in names}
        self.var_box.controls = list(self.var_fields.values()) or [
            ft.Text("None. Add {{ name }} placeholders to the prompts to create inputs.", size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT)]
        if not initial and mounted(self.var_box):
            self.var_box.update()

    def _params(self) -> dict:
        t = (self.temperature.value or "").strip()
        return {"temperature": float(t) if t else None, "max_tokens": int(self.max_tokens.value or 1024),
                "json_mode": bool(self.json_mode.value)}

    def run(self, _=None) -> None:
        self.refresh_vars()
        values = {n: f.value or "" for n, f in self.var_fields.items()}
        system, user = prompts.render_prompt(self.system.value or "", self.user.value or "", values)
        if not user.strip():
            raise ValueError("Write a user prompt first")
        if not self.model.value:
            raise ValueError("Choose a model")
        self.spinner.visible = True
        self.output.controls = [ft.Text("Waiting for the model...", color=ft.Colors.ON_SURFACE_VARIANT)]
        self.page.update()
        try:
            result = llm.chat(self.app.user, self.model.value, user, system, purpose="playground",
                              prompt_template=self.prompt.name if self.prompt else "", **self._params())
        except llm.AIError as e:
            self.output.controls = [ft.Text(str(e), color=ft.Colors.ERROR, selectable=True)]
            self.status.controls = [chip("error", ft.Colors.RED_500)]
            return
        finally:
            self.spinner.visible = False
            self.page.update()
        self.status.controls = [chip(result.model), chip(f"{result.prompt_tokens:,} in / {result.completion_tokens:,} out"),
                                chip(f"{result.latency_ms / 1000:.1f} s")]
        body: ft.Control
        if self._params()["json_mode"]:
            try:
                body = ft.Markdown("```json\n" + json.dumps(result.json(), indent=2) + "\n```", selectable=True)
            except ValueError:
                body = ft.Text(result.text, selectable=True)
        else:
            body = ft.Markdown(result.text, selectable=True, extension_set=ft.MarkdownExtensionSet.GITHUB_WEB)
        self.output.controls = [
            body,
            ft.ExpansionTile(title=ft.Text("Prompt sent", size=12), controls=[
                ft.Text(f"[system]\n{system}\n\n[user]\n{user}", selectable=True, size=12, font_family="monospace")]),
        ]
        self.page.update()

    def save(self, _=None) -> None:
        self.app.require("design")
        values = {"system": self.system.value or "", "user": self.user.value or "", "model_ref": self.model.value or "",
                  **self._params(),
                  "variables": [{"name": n, "default": f.value or ""} for n, f in self.var_fields.items()]}
        if self.prompt:
            p = self.prompt
            prompts.save_prompt({**values, "name": p.name, "description": p.description}, self.app.user.username, p.id)
            toast(self.page, f"Saved draft of {p.name}. Publish it from the Prompts tab.")
            return
        name = ft.TextField(label="Prompt name", autofocus=True)
        desc = ft.TextField(label="Description (optional)")

        def create(_):
            p = prompts.save_prompt({**values, "name": name.value, "description": desc.value or ""},
                                    self.app.user.username)
            close_dialog(self.page)
            toast(self.page, f"Saved {p.name}")
            self.app.navigate("ai", tab="playground", prompt_id=p.id)

        dialog(self.page, "Save as prompt", ft.Column([name, desc], tight=True, spacing=10), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Save", on_click=guarded(self.page, create))], width=420)


# ================================================================== Prompts


class PromptsTab:
    def __init__(self, view: AIView):
        self.view, self.app, self.page = view, view.app, view.page

    def build(self) -> ft.Control:
        designer = self.app.can("design")
        items = []
        for p in prompts.list_prompts():
            state = (chip(f"published v{p.published_version}", ft.Colors.GREEN_600) if p.published_version else
                     chip("draft", ft.Colors.AMBER_700))
            badges = [state]
            if p.published_version and p.has_draft_changes:
                badges.append(chip("unpublished changes", ft.Colors.AMBER_700))
            vars_txt = ", ".join(v["name"] for v in p.variables) or "no variables"
            actions: list[ft.Control] = [
                ft.FilledTonalButton("Open in playground", icon=ft.Icons.SCIENCE,
                                     on_click=lambda _, pid=p.id: self.app.navigate("ai", tab="playground", prompt_id=pid)),
                ft.IconButton(ft.Icons.HISTORY, tooltip="Versions", on_click=guarded(self.page, lambda _, pp=p: self.versions(pp))),
            ]
            if designer:
                actions += [
                    ft.IconButton(ft.Icons.EDIT_OUTLINED, tooltip="Edit", on_click=lambda _, pp=p: self.edit(pp)),
                    ft.IconButton(ft.Icons.PUBLISH, tooltip="Publish", on_click=lambda _, pp=p: self.publish(pp)),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete", on_click=lambda _, pp=p: confirm(
                        self.page, "Delete prompt", f"Delete {pp.name} and all its versions?",
                        lambda: self.delete(pp))),
                ]
            items.append(card(ft.Row([
                ft.Icon(ft.Icons.TEXT_SNIPPET, color=ft.Colors.PRIMARY, size=28),
                ft.Column([
                    ft.Row([ft.Text(p.name, size=15, weight=ft.FontWeight.W_600), *badges], spacing=8),
                    ft.Text(p.description or (p.user[:120] + ("..." if len(p.user) > 120 else "")), size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(f"Variables: {vars_txt}  ·  updated {_fmt(p.updated_at)} by {p.updated_by}", size=11.5,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                ], spacing=2, expand=True),
                *actions,
            ], spacing=8)))
        new_btn = ft.FilledButton("New prompt", icon=ft.Icons.ADD, on_click=lambda _: self.edit(None))
        body = ft.Column(items, spacing=10) if items else empty_state(
            ft.Icons.TEXT_SNIPPET, "No saved prompts yet",
            "Write one in the Playground and save it, or create it here. Published versions are what workflows use.",
            new_btn if designer else None)
        return ft.Column([ft.Row([ft.Container(expand=True), new_btn] if designer else []), body], spacing=10)

    def edit(self, p) -> None:
        self.app.require("design")
        options = llm.available_models(self.app.user)
        name = ft.TextField(label="Name", value=p.name if p else "", dense=True)
        desc = ft.TextField(label="Description", value=p.description if p else "", dense=True)
        system = ft.TextField(label="System prompt", value=p.system if p else "", multiline=True, min_lines=3, max_lines=8)
        user = ft.TextField(label="User prompt", value=p.user if p else "", multiline=True, min_lines=4, max_lines=10)
        model = model_dropdown(options, p.model_ref if p else "", label="Default model", allow_none=True)
        temp = ft.TextField(label="Temperature", width=140, dense=True,
                            value="" if not p or p.temperature is None else str(p.temperature))
        max_tokens = ft.TextField(label="Max tokens", width=140, dense=True, value=str(p.max_tokens if p else 1024))
        json_mode = ft.Checkbox(label="JSON output", value=bool(p.json_mode) if p else False)
        var_box = ft.Column(spacing=6)
        fields: dict[str, tuple[ft.TextField, ft.TextField]] = {}
        known = {v["name"]: v for v in (p.variables if p else [])}

        def refresh(_=None):
            try:
                names = variables_in(system.value or "", user.value or "")
            except PromptTemplateError as e:
                var_box.controls = [ft.Text(str(e), color=ft.Colors.ERROR, size=12)]
                if mounted(var_box):
                    var_box.update()
                return
            old = {n: (d.value, ds.value) for n, (d, ds) in fields.items()}
            fields.clear()
            rows = []
            for n in names:
                d0, ds0 = old.get(n, (known.get(n, {}).get("default", ""), known.get(n, {}).get("description", "")))
                d = ft.TextField(label=f"{n}: default", value=d0, dense=True, width=260)
                ds = ft.TextField(label="description", value=ds0, dense=True, expand=True)
                fields[n] = (d, ds)
                rows.append(ft.Row([d, ds], spacing=8))
            var_box.controls = rows or [ft.Text("No {{ variables }} used yet.", size=12, color=ft.Colors.ON_SURFACE_VARIANT)]
            if mounted(var_box):
                var_box.update()

        system.on_blur = refresh
        user.on_blur = refresh
        refresh()

        def save(_):
            t = (temp.value or "").strip()
            saved = prompts.save_prompt({
                "name": name.value, "description": desc.value or "", "system": system.value or "",
                "user": user.value or "", "model_ref": model.value or "", "temperature": float(t) if t else None,
                "max_tokens": int(max_tokens.value or 1024), "json_mode": bool(json_mode.value),
                "variables": [{"name": n, "default": d.value or "", "description": ds.value or ""}
                              for n, (d, ds) in fields.items()],
            }, self.app.user.username, p.id if p else None)
            close_dialog(self.page)
            toast(self.page, f"Saved draft of {saved.name}")
            self.app.navigate("ai", tab="prompts")

        dialog(self.page, f"Edit {p.name}" if p else "New prompt", ft.Column([
            ft.Row([name, desc], spacing=10), system, user,
            ft.Text("Variables (detected from {{ ... }})", size=13, weight=ft.FontWeight.W_600), var_box,
            ft.Row([model], wrap=True), ft.Row([temp, max_tokens, json_mode], spacing=10),
            ft.Text("Tip: put untrusted or external text inside the user prompt, e.g. <data>{{ text }}</data>, "
                    "never in the system prompt.", size=11.5, color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=12, scroll=ft.ScrollMode.AUTO), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Save draft", on_click=guarded(self.page, save)),
        ], width=820, height=620)

    def publish(self, p) -> None:
        note = ft.TextField(label="What changed? (optional)", autofocus=True)

        def go(_):
            self.app.require("design")
            v = prompts.publish(p.id, self.app.user.username, note.value or "")
            close_dialog(self.page)
            toast(self.page, f"Published {p.name} v{v.version}")
            self.app.navigate("ai", tab="prompts")

        dialog(self.page, f"Publish {p.name}", ft.Column([
            ft.Text("Publishing freezes the current draft as a new version. Workflows use the latest published "
                    "version unless they pin one.", size=13), note], tight=True, spacing=10), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Publish", on_click=guarded(self.page, go))], width=480)

    def versions(self, p) -> None:
        rows = []
        for v in prompts.versions(p.id):
            rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(f"v{v.version}", size=12, weight=ft.FontWeight.W_600)),
                ft.DataCell(ft.Text(_fmt(v.created_at), size=12)),
                ft.DataCell(ft.Text(v.created_by, size=12)),
                ft.DataCell(ft.Text(v.note or "", size=12, width=220, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
                ft.DataCell(ft.Text(v.content_hash[:10], size=11.5, font_family="monospace")),
                ft.DataCell(ft.TextButton("Restore to draft", visible=self.app.can("design"),
                                          on_click=guarded(self.page, lambda _, vv=v: self.restore(p, vv.version)))),
            ]))
        content = ft.DataTable(columns=[ft.DataColumn(ft.Text(h, size=12, weight=ft.FontWeight.W_600)) for h in
                                        ("Version", "Published", "By", "Note", "Hash", "")], rows=rows) if rows else \
            ft.Text("Not published yet.")
        dialog(self.page, f"Versions of {p.name}", ft.Column([content], scroll=ft.ScrollMode.AUTO),
               [ft.TextButton("Close", on_click=lambda _: close_dialog(self.page))], width=820)

    def restore(self, p, version: int) -> None:
        self.app.require("design")
        prompts.restore(p.id, version, self.app.user.username)
        close_dialog(self.page)
        toast(self.page, f"Draft of {p.name} restored from v{version}")
        self.app.navigate("ai", tab="prompts")

    def delete(self, p) -> None:
        self.app.require("design")
        prompts.delete_prompt(p.id, self.app.user.username)
        self.app.navigate("ai", tab="prompts")


# ================================================================== Models


class ModelsTab:
    def __init__(self, view: AIView):
        self.view, self.app, self.page = view, view.app, view.page

    def build(self) -> ft.Control:
        admin = self.app.can("manage_models")
        keys = llm.list_credentials(self.app.user.id)
        key_cards = []
        for c in keys:
            status = chip("works", ft.Colors.GREEN_600) if c.last_test_ok else (
                chip("failed", ft.Colors.RED_500) if c.last_test_ok is False else chip("not tested"))
            key_cards.append(card(ft.Row([
                ft.Icon(ft.Icons.KEY, color=ft.Colors.PRIMARY),
                ft.Column([
                    ft.Row([ft.Text(c.name, size=14, weight=ft.FontWeight.W_600), status], spacing=8),
                    ft.Text(f"{PRESETS.get(c.provider, {}).get('label', c.provider)} · key ••••{c.key_hint} · "
                            f"{len(c.models)} models · default {c.default_model or '-'} · last used {_fmt(c.last_used_at)}",
                            size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text(c.last_test_message or "", size=11.5, color=ft.Colors.ON_SURFACE_VARIANT, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS),
                ], spacing=2, expand=True),
                ft.OutlinedButton("Test", icon=ft.Icons.BOLT, on_click=background(self.page, lambda _, cc=c: self.test_key(cc))),
                ft.IconButton(ft.Icons.EDIT_OUTLINED, tooltip="Edit", on_click=lambda _, cc=c: self.edit_key(cc)),
                ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete", on_click=lambda _, cc=c: confirm(
                    self.page, "Delete API key", f"Delete {cc.name}? Prompts and workflows using it will stop working.",
                    lambda: self.delete_key(cc))),
            ], spacing=10), padding=12))
        return ft.Column([
            ft.Row([ft.Text("My API keys", size=16, weight=ft.FontWeight.W_600, expand=True),
                    ft.FilledButton("Add API key", icon=ft.Icons.ADD, on_click=lambda _: self.edit_key(None))]),
            ft.Text("Your own keys for public cloud providers. Keys the company issued you for its own LLMs go under "
                    "Company models below. Keys are encrypted, only you can use them, and nobody (including "
                    "admins) can read them back.", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Column(key_cards, spacing=8) if key_cards else ft.Text("No keys yet.", size=12),
            ft.Divider(),
            *CompanyModels(self).build(_tls_chips),
        ], spacing=10)

    # --- personal keys
    def edit_key(self, c) -> None:
        provider = ft.Dropdown(label="Provider", dense=True, value=c.provider if c else "openai", width=360,
                               options=[ft.DropdownOption(key=k, text=v["label"]) for k, v in PRESETS.items()])
        name = ft.TextField(label="Name", dense=True, value=c.name if c else "", hint_text="e.g. My OpenAI key",
                            width=360)
        base = ft.TextField(label="Base URL", dense=True, value=c.base_url if c else PRESETS["openai"]["base_url"],
                            width=360)
        key = ft.TextField(label="API key" + (" (leave blank to keep)" if c else ""), password=True,
                           can_reveal_password=True, dense=True, width=360)
        default = ft.Dropdown(label="Default model", dense=True, value=c.default_model if c else None, width=360,
                              options=[ft.DropdownOption(key=m, text=m) for m in (c.models if c else [])],
                              visible=bool(c and c.models))

        def preset_changed(e):
            base.value = PRESETS[provider.value]["base_url"]
            if not name.value:
                name.value = PRESETS[provider.value]["label"]
            base.update()
            name.update()

        provider.on_select = preset_changed

        def save(_):
            saved = llm.save_credential(self.app.user.id, self.app.user.username, {
                "provider": provider.value, "name": name.value, "base_url": base.value, "api_key": key.value,
                "default_model": default.value or ""}, c.id if c else None)
            ok, msg = llm.test_credential(self.app.user.id, saved.id, self.app.user.username)
            close_dialog(self.page)
            toast(self.page, msg, error=not ok)
            self.app.navigate("ai", tab="models")

        dialog(self.page, "Edit API key" if c else "Add API key", ft.Column([
            provider, name, base, key, default,
            ft.Text("The key is tested and the provider's model list is loaded when you save.", size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT)], spacing=12, tight=True), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Save and test", on_click=background(self.page, save))], width=480)

    def test_key(self, c) -> None:
        ok, msg = llm.test_credential(self.app.user.id, c.id, self.app.user.username)
        toast(self.page, msg, error=not ok)
        self.app.navigate("ai", tab="models")

    def delete_key(self, c) -> None:
        llm.delete_credential(self.app.user.id, c.id, self.app.user.username)
        self.app.navigate("ai", tab="models")

    # --- shared endpoints (admin)
    def edit_ep(self, e) -> None:
        self.app.require("manage_models")
        preset = ft.Dropdown(label="Type", dense=True, width=360, value=None,
                             options=[ft.DropdownOption(key=k, text=v["label"]) for k, v in LOCAL_PRESETS.items()])
        name = ft.TextField(label="Name", dense=True, width=560, value=e.name if e else "",
                            hint_text="e.g. Company AI gateway")
        base = ft.TextField(label="Base URL", dense=True, width=560, value=e.base_url if e else "http://localhost:11434/v1",
                            helper="API root, e.g. https://host/v1 (without /chat/completions)")
        style = ft.Dropdown(label="API style", dense=True, width=200, value=e.api_style if e else "openai",
                            options=[ft.DropdownOption(key="openai", text="OpenAI-compatible"),
                                     ft.DropdownOption(key="anthropic", text="Anthropic Messages")])
        key = ft.TextField(label="API key (optional" + (", blank keeps current)" if e else ")"), password=True,
                           can_reveal_password=True, dense=True, width=560)
        header = ft.TextField(label="Auth header", dense=True, width=200, value=e.auth_header if e else "Authorization")
        scheme = ft.TextField(label="Scheme", dense=True, width=120, value=e.auth_scheme if e else "Bearer")
        network = ft.RadioGroup(value=e.network if e else "internal", content=ft.Row([
            ft.Radio(value="internal", label="Internal (data stays in your network)"),
            ft.Radio(value="external", label="External")]))
        roles = {r: ft.Checkbox(label=r, value=r in (e.allowed_roles if e else ["admin", "designer"])) for r in ROLES
                 if r != "viewer"}
        enabled = ft.Switch(label="Enabled", value=e.enabled if e else True)
        key_mode = ft.RadioGroup(value=(e.key_mode if e else None) or "shared", content=ft.Column([
            ft.Radio(value="shared", label="One company key for everyone (enter it below)"),
            ft.Radio(value="per_user", label="Each user adds the key issued to them"),
            ft.Radio(value="none", label="No key")], spacing=0))
        approval = ft.Dropdown(label="Model access", dense=True, width=460, value=(e.approval if e else None) or "open",
                               options=[ft.DropdownOption(key=k, text=v) for k, v in llm.APPROVAL.items()])

        def key_mode_changed(_=None):
            key.label = {"shared": "Company API key" + (" (blank keeps current)" if e else ""),
                         "per_user": "Discovery key (optional)", "none": "API key (not used)"}[key_mode.value]
            key.helper = ("Used only to list models and for health checks, never for users' calls"
                          if key_mode.value == "per_user" else None)
            key.disabled = key_mode.value == "none"
            if mounted(key):
                key.update()

        key_mode.on_change = key_mode_changed
        key_mode_changed()
        managed_note = ([ft.Container(ft.Text("This endpoint is managed by the configuration file: edits here are "
                                              "replaced the next time the file is applied.", size=12,
                                              color=ft.Colors.AMBER_800),
                                      bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.AMBER), padding=8,
                                      border_radius=ft.BorderRadius.all(8))] if e and e.managed else [])
        models = ft.TextField(label="Models (comma separated; filled automatically by Test)", dense=True, width=560,
                              value=", ".join(e.models) if e else "")
        tls_section, tls_values = self._tls_section(e)

        def preset_changed(_):
            pr = LOCAL_PRESETS[preset.value]
            base.value, style.value = pr["base_url"], pr["api_style"]
            if not name.value:
                name.value = pr["label"]
            if preset.value in ("anthropic", "openai"):
                network.value = "external"
            for c in (base, style, name, network):
                c.update()

        preset.on_select = preset_changed

        def save(_):
            saved = llm.save_endpoint({
                "name": name.value, "base_url": base.value, "api_style": style.value, "api_key": key.value,
                "auth_header": header.value, "auth_scheme": scheme.value, "network": network.value,
                "allowed_roles": [r for r, cb in roles.items() if cb.value], "enabled": enabled.value,
                "models": [m.strip() for m in (models.value or "").split(",") if m.strip()],
                "key_mode": key_mode.value, "approval": approval.value,
                "clear_key": key_mode.value == "none",
                **tls_values(),
            }, self.app.user.username, e.id if e else None)
            ok, msg = llm.test_endpoint(saved.id, self.app.user.username)
            close_dialog(self.page)
            toast(self.page, f"{saved.name}: {msg}", error=not ok)
            self.app.navigate("ai", tab="models")

        dialog(self.page, f"Edit {e.name}" if e else "Add model endpoint", ft.Column([
            *managed_note, preset, name, base, ft.Row([style, header, scheme], spacing=8),
            ft.Text("Keys", size=13, weight=ft.FontWeight.W_600), key_mode, key, network, approval,
            ft.Row([ft.Text("Roles that may use it:", size=13), *roles.values()], spacing=8), models, enabled,
            tls_section,
            ft.Text("On a VPS, a local Ollama is usually http://127.0.0.1:11434/v1 (native install) or "
                    "http://host.docker.internal:11434/v1 (Docker).", size=11.5, color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=12, tight=True, scroll=ft.ScrollMode.AUTO), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Save and test", on_click=background(self.page, save))], width=620)

    def _tls_section(self, e):
        """Certificate settings for HTTPS endpoints: provider CA, client certificate (mutual TLS)."""
        pending: dict = {}  # uploaded bytes and clear flags, sent on save
        info = (e.tls_info if e else None) or {}
        mode = ft.Dropdown(label="Verify the server certificate", dense=True, width=340,
                           value=(e.tls_verify if e else None) or "system",
                           options=[ft.DropdownOption(key=k, text=v) for k, v in tls.VERIFY_MODES.items()])
        check_host = ft.Checkbox(label="Check host name", value=e.tls_check_hostname is not False if e else True,
                                 tooltip="Turn off only if the provider's certificate doesn't list the host name you use")
        key_pw = ft.TextField(label="Key or .p12 password", password=True, can_reveal_password=True,
                              dense=True, width=300, hint_text="only if the file is protected")
        ca_text = ft.Text(size=12)
        client_text = ft.Text(size=12)
        warn = ft.Text("Verification is off: traffic is encrypted but the server is not authenticated.",
                       size=11.5, color=ft.Colors.RED_500)

        def describe(certs: list, empty: str) -> str:
            if not certs:
                return empty
            c = certs[0]
            more = f" (+{len(certs) - 1} more)" if len(certs) > 1 else ""
            days = (date.fromisoformat(c["not_after"]) - date.today()).days
            return f"{c['subject']} · expires {c['not_after']} ({days} days){more}"

        def refresh():
            if "ca_data" in pending:
                ca_text.value = "New: " + describe([x.to_dict() for x in tls.describe(pending["ca_pem_preview"])], "")
            elif pending.get("clear_ca"):
                ca_text.value = "Will be removed"
            else:
                ca_text.value = describe(info.get("ca") or [], "No CA certificate uploaded")
            if pending.get("client_p12_data"):
                client_text.value = f"New: {pending['p12_name']} (checked on save)"
            elif pending.get("client_cert_data") or pending.get("client_key_data"):
                parts = [n for n, k in (("certificate", "client_cert_data"), ("key", "client_key_data")) if pending.get(k)]
                client_text.value = "New: " + " + ".join(parts) + (" (upload the key too)" if len(parts) == 1 else "")
            elif pending.get("clear_client_cert"):
                client_text.value = "Will be removed"
            else:
                client_text.value = describe(info.get("client") or [], "No client certificate (not needed unless the provider requires mutual TLS)")
            warn.visible = mode.value == "off"
            for c in (ca_text, client_text, warn):
                if mounted(c):
                    c.update()

        async def pick(kind: str):
            exts = {"ca": ["pem", "crt", "cer", "der"], "cert": ["pem", "crt", "cer"], "key": ["pem", "key"],
                    "p12": ["p12", "pfx"]}[kind]
            try:
                from databridge.ui.uploads import pick_and_upload

                try:
                    picked = await pick_and_upload(self.page, exts, 1024 * 1024)
                except ValueError as e:
                    raise tls.TLSConfigError(f"{e}. Certificate files are small (under 1 MB)") from e
                if not picked:
                    return
                f_name, data = picked
                if kind == "ca":
                    pending["ca_pem_preview"] = tls.normalize_ca(data)  # validates now
                    pending["ca_data"] = data
                    pending.pop("clear_ca", None)
                    if mode.value == "system":
                        mode.value = "custom"
                        mode.update()
                elif kind == "p12":
                    pending.update(client_p12_data=data, p12_name=f_name)
                    pending.pop("client_cert_data", None), pending.pop("client_key_data", None)
                    pending.pop("clear_client_cert", None)
                else:
                    if kind == "cert":
                        tls.load_certs(data)
                    pending["client_cert_data" if kind == "cert" else "client_key_data"] = data
                    pending.pop("client_p12_data", None), pending.pop("clear_client_cert", None)
                refresh()
            except Exception as ex:  # noqa: BLE001 - user-facing boundary
                toast(self.page, str(ex), error=True)

        def remove(which: str):
            if which == "ca":
                for k in ("ca_data", "ca_pem_preview"):
                    pending.pop(k, None)
                pending["clear_ca"] = True
                if mode.value == "custom":
                    mode.value = "system"
                    mode.update()
            else:
                for k in ("client_cert_data", "client_key_data", "client_p12_data"):
                    pending.pop(k, None)
                pending["clear_client_cert"] = True
            refresh()

        mode.on_select = lambda _: refresh()

        def values() -> dict:
            out = {"tls_verify": mode.value, "tls_check_hostname": bool(check_host.value)}
            for k in ("ca_data", "clear_ca", "client_cert_data", "client_key_data", "client_p12_data",
                      "clear_client_cert"):
                if k in pending:
                    out[k] = pending[k]
            if key_pw.value:
                out["client_key_password"] = key_pw.value
            return out

        async def pick_ca(_):
            await pick("ca")

        async def pick_cert(_):
            await pick("cert")

        async def pick_key(_):
            await pick("key")

        async def pick_p12(_):
            await pick("p12")

        refresh()
        small = {"style": ft.ButtonStyle(padding=ft.Padding.symmetric(horizontal=10))}
        section = ft.Container(ft.Column([
            ft.Row([ft.Icon(ft.Icons.VERIFIED_USER_OUTLINED, size=18, color=ft.Colors.PRIMARY),
                    ft.Text("Certificates (HTTPS)", size=14, weight=ft.FontWeight.W_600)], spacing=6),
            ft.Text("Use these when the provider gives you a CA certificate (self-signed or company CA) and/or a "
                    "client certificate for mutual TLS.", size=11.5, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Row([mode, check_host], spacing=12, wrap=True), warn,
            ft.Text("Provider CA certificate", size=12.5, weight=ft.FontWeight.W_500),
            ca_text,
            ft.Row([ft.OutlinedButton("Upload CA (.pem/.crt/.cer)", icon=ft.Icons.UPLOAD_FILE, on_click=pick_ca, **small),
                    ft.TextButton("Remove", on_click=lambda _: remove("ca"))], spacing=6),
            ft.Text("Client certificate (mutual TLS)", size=12.5, weight=ft.FontWeight.W_500),
            client_text,
            ft.Row([ft.OutlinedButton("Certificate", icon=ft.Icons.UPLOAD_FILE, on_click=pick_cert, **small),
                    ft.OutlinedButton("Private key", icon=ft.Icons.KEY, on_click=pick_key, **small),
                    ft.Text("or", size=12),
                    ft.OutlinedButton(".p12 / .pfx", icon=ft.Icons.INVENTORY_2_OUTLINED, on_click=pick_p12, **small),
                    ft.TextButton("Remove", on_click=lambda _: remove("client"))], spacing=6, wrap=True),
            key_pw,
            ft.Text("The private key is stored encrypted and is never shown again.", size=11,
                    color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=8, tight=True), padding=12, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=ft.BorderRadius.all(8))
        return section, values

    def test_ep(self, e) -> None:
        self.app.require("manage_models")
        ok, msg = llm.test_endpoint(e.id, self.app.user.username)
        toast(self.page, f"{e.name}: {msg}", error=not ok)
        self.app.navigate("ai", tab="models")

    def delete_ep(self, e) -> None:
        self.app.require("manage_models")
        llm.delete_endpoint(e.id, self.app.user.username)
        self.app.navigate("ai", tab="models")


# ================================================================== Usage


class UsageTab:
    def __init__(self, view: AIView):
        self.view, self.app, self.page = view, view.app, view.page

    def build(self) -> ft.Control:
        everyone = self.app.can("manage_models")
        scope = None if everyone else self.app.user
        summary = llm.usage_summary(scope)
        calls = llm.usage(scope, 200)
        mine = llm.tokens_used_this_month(self.app.user.id)
        from databridge.config import settings

        limit = settings.ai_monthly_token_limit
        header = ft.Row([
            chip(f"You: {mine:,} tokens this month" + (f" of {limit:,}" if limit else "")),
            chip("showing everyone" if everyone else "showing your usage"),
        ], spacing=8)
        sum_table = ft.DataTable(columns=[ft.DataColumn(ft.Text(h, size=12, weight=ft.FontWeight.W_600)) for h in
                                          ("User", "Model", "Calls", "Tokens in", "Tokens out")],
                                 rows=[ft.DataRow(cells=[ft.DataCell(ft.Text(str(r[k]) if not isinstance(r[k], int) else f"{r[k]:,}", size=12))
                                                         for k in ("user", "model", "calls", "prompt_tokens", "completion_tokens")])
                                       for r in summary]) if summary else ft.Text("No calls this month.", size=12)
        colors = {"ok": ft.Colors.GREEN_600, "error": ft.Colors.RED_500, "blocked": ft.Colors.AMBER_700}
        call_rows = [ft.DataRow(cells=[
            ft.DataCell(ft.Text(_fmt(c.at), size=12)),
            ft.DataCell(ft.Text(c.username, size=12)),
            ft.DataCell(ft.Text(c.purpose, size=12)),
            ft.DataCell(ft.Text(f"{c.model} · {c.route}", size=12, width=240, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
            ft.DataCell(ft.Text(f"{c.prompt_tokens:,} / {c.completion_tokens:,}", size=12)),
            ft.DataCell(ft.Text(f"{c.latency_ms / 1000:.1f} s", size=12)),
            ft.DataCell(chip(c.status, colors.get(c.status, ft.Colors.OUTLINE))),
            ft.DataCell(ft.Text(c.error, size=11.5, width=220, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS,
                                tooltip=c.error or None)),
        ]) for c in calls]
        calls_table = ft.Row([ft.DataTable(columns=[ft.DataColumn(ft.Text(h, size=12, weight=ft.FontWeight.W_600)) for h in
                                                    ("When", "User", "Purpose", "Model", "Tokens in / out", "Time", "Status", "Error")],
                                           rows=call_rows, data_row_min_height=34, data_row_max_height=38)],
                             scroll=ft.ScrollMode.AUTO) if call_rows else ft.Text("No calls yet.", size=12)
        return ft.Column([header, ft.Text("This month by model", size=15, weight=ft.FontWeight.W_600), sum_table,
                          ft.Text("Recent calls", size=15, weight=ft.FontWeight.W_600), calls_table], spacing=10)


def _tls_chips(e) -> list[ft.Control]:
    """Certificate status on an endpoint card: custom CA, mTLS, verification off, expiry warnings."""
    if not e.base_url.startswith("https"):
        return []
    info = e.tls_info or {}
    out: list[ft.Control] = []
    if e.tls_verify == "off":
        out.append(chip("TLS verify off", ft.Colors.RED_500))
    elif e.tls_verify == "custom":
        out.append(chip("custom CA", ft.Colors.INDIGO_400))
    if e.client_cert_pem:
        out.append(chip("mTLS", ft.Colors.INDIGO_400))
    for label, certs in (("CA", info.get("ca") or []), ("client cert", info.get("client") or [])):
        if not certs:
            continue
        days = (date.fromisoformat(certs[0]["not_after"]) - date.today()).days  # stored info ages; recompute
        if days < 0:
            out.append(chip(f"{label} expired", ft.Colors.RED_500))
        elif days < 30:
            out.append(chip(f"{label} expires in {days} d", ft.Colors.AMBER_700))
    return out
