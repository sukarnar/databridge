"""Mapping Studio: drag source fields onto target fields, edit formulas, preview, publish.

Layout
    toolbar     back · name · progress · Auto-map · Row steps · Save · Publish
    workspace   [source list | arrow canvas | target list]  [inspector]
    preview     stats + transformed sample rows, rejected cells in red

Ways to create an arrow
    - drag a source field onto a target field
    - click a source field, then a target field (keyboard/touch friendly)
    - Auto-map, then accept the dashed suggestions
"""

import flet as ft

from databridge.core.types import base_type, compatibility
from databridge.engine.formula import FUNCTIONS, FormulaError, compile_formula, referenced_columns
from databridge.engine.mapper import MappingSpec, MapResult, source_columns_after_steps
from databridge.services import mappings as svc
from databridge.services import sources as src_svc
from databridge.services import targets as tgt_svc
from databridge.ui.common import (background, mounted, STATUS_COLORS, card, chip, close_dialog, df_table, dialog, guarded, toast,
                                  type_badge)
from databridge.ui.components.mapping_canvas import ROW_H, Link, link_canvas

SRC_W, CANVAS_W, TGT_W = 290, 150, 320
VALIDATION_KINDS = {"allowed": "Allowed values (comma separated)", "regex": "Must match pattern (regex)",
                    "min": "Minimum", "max": "Maximum", "unique": "Must be unique"}


class MappingStudio:
    def __init__(self, app, mapping_id: int):
        self.app = app
        self.page = app.page
        self.m = svc.get_mapping(mapping_id)
        self.src = src_svc.get_source(self.m.source_id)
        self.tgt = tgt_svc.get_target(self.m.target_id)
        self.rules: dict[str, str] = {r["target"]: r.get("formula", "") for r in self.m.rules if r.get("target")}
        self.row_steps: list[dict] = list(self.m.row_steps or [])
        self.validations: list[dict] = list(self.m.validations or [])
        self.suggestions: dict[str, tuple[str, float]] = {}  # target -> (source, score)
        self.sel_src: str | None = None
        self.sel_tgt: str | None = None
        self.q_src = ""
        self.q_tgt = ""
        self.dirty = False
        self.result: MapResult | None = None
        self.readonly = not app.can("design")  # viewers see the mapping but cannot change it

        # containers updated in place
        self.src_col = ft.Column(spacing=0, width=SRC_W)
        self.tgt_col = ft.Column(spacing=0, width=TGT_W)
        self.canvas_holder = ft.Container(width=CANVAS_W)
        self.inspector = ft.Column(spacing=10, scroll=ft.ScrollMode.AUTO, expand=True)
        self.preview_box = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, expand=True)
        self.progress = ft.Text(size=12)
        self.dirty_chip = ft.Container()
        self.banner = ft.Container(visible=False)
        self.steps_btn = ft.OutlinedButton(icon=ft.Icons.FILTER_ALT_OUTLINED, on_click=lambda _: self.edit_steps())

    # ================================================================== data helpers

    @property
    def src_fields(self) -> list[dict]:
        cols = source_columns_after_steps([f["name"] for f in self.src.fields], self.row_steps)
        by = {f["name"]: f for f in self.src.fields}
        return [by.get(c, {"name": c, "type": "string"}) for c in cols]

    def visible_src(self) -> list[dict]:
        q = self.q_src.lower()
        return [f for f in self.src_fields if q in f["name"].lower()]

    def visible_tgt(self) -> list[dict]:
        q = self.q_tgt.lower()
        return [f for f in self.tgt.fields if q in f["name"].lower()]

    def spec(self) -> MappingSpec:
        rules = [{"target": t["name"], "formula": self.rules[t["name"]]}
                 for t in self.tgt.fields if self.rules.get(t["name"], "").strip()]
        return MappingSpec(rules=rules, row_steps=self.row_steps, validations=self.validations)

    def field_status(self, tgt: dict) -> str:
        name = tgt["name"]
        formula = self.rules.get(name, "").strip()
        if not formula:
            return "suggested" if name in self.suggestions else "unmapped"
        if self.result and name in self.result.rule_errors:
            return "error"
        refs = referenced_columns(formula)
        types = {f["name"]: f["type"] for f in self.src_fields}
        simple = bool(refs) and formula == f"[{refs[0]}]"
        if simple:
            status = compatibility(types.get(refs[0], "string"), tgt.get("type", "string"))
        else:
            status = "ok"
        if status == "ok" and self.result and any(name in errs for errs in self.result.cell_errors.values()):
            status = "lossy"
        return status

    # ================================================================== build

    def build(self) -> ft.Control:
        self.run_preview()
        self.render_all(initial=True)
        header = ft.Row([
            ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Back to mappings", on_click=lambda _: self.leave()),
            ft.Column([
                ft.Row([ft.Text(self.m.name, size=20, weight=ft.FontWeight.W_600, max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS, tooltip=self.m.name, expand=True),
                        self.dirty_chip], spacing=10),
                ft.Text(f"{self.src.name} to {self.tgt.name}", size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ], spacing=0, expand=True),
            self.progress,
            *([chip("read-only", ft.Colors.OUTLINE)] if self.readonly else [
                ft.OutlinedButton("Auto-map", icon=ft.Icons.AUTO_FIX_HIGH, on_click=guarded(self.page, self.auto_map)),
                *([ft.OutlinedButton("AI suggest", icon=ft.Icons.AUTO_AWESOME,
                                     on_click=background(self.page, self.ai_suggest))] if self.app.can("use_ai") else []),
                self.steps_btn,
                ft.OutlinedButton("Save draft", icon=ft.Icons.SAVE_OUTLINED, on_click=guarded(self.page, self.save)),
                ft.FilledButton("Publish", icon=ft.Icons.PUBLISH, on_click=guarded(self.page, self.publish)),
            ]),
        ], spacing=8)

        src_search = ft.TextField(hint_text="Search source fields", prefix_icon=ft.Icons.SEARCH, dense=True,
                                  width=SRC_W, on_change=self.on_search_src)
        tgt_search = ft.TextField(hint_text="Search target fields", prefix_icon=ft.Icons.SEARCH, dense=True,
                                  width=TGT_W, on_change=self.on_search_tgt)
        lists = ft.Column([
            ft.Row([
                ft.Column([ft.Text(f"SOURCE · {self.src.name}", size=11, weight=ft.FontWeight.W_700,
                                   color=ft.Colors.ON_SURFACE_VARIANT), src_search], width=SRC_W, spacing=4),
                ft.Container(width=CANVAS_W),
                ft.Column([ft.Text(f"TARGET · {self.tgt.name}", size=11, weight=ft.FontWeight.W_700,
                                   color=ft.Colors.ON_SURFACE_VARIANT), tgt_search], width=TGT_W, spacing=4),
            ], spacing=0),
            ft.Column([
                ft.Row([self.src_col, self.canvas_holder, self.tgt_col], spacing=0,
                       vertical_alignment=ft.CrossAxisAlignment.START),
            ], scroll=ft.ScrollMode.AUTO, expand=True),
        ], spacing=10, expand=True)

        workspace = ft.Row([
            ft.Container(card(lists, padding=12), width=SRC_W + CANVAS_W + TGT_W + 26),
            ft.Container(card(self.inspector, padding=14), expand=True),
        ], spacing=12, expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH)

        return ft.Column([
            header,
            self.banner,
            ft.Container(workspace, expand=3),
            ft.Container(card(self.preview_box, padding=12), expand=2),
        ], spacing=10, expand=True)

    # ================================================================== rendering

    def render_all(self, initial: bool = False) -> None:
        self.render_lists()
        self.render_inspector()
        self.render_preview()
        self.render_header()
        if not initial:
            self.page.update()

    def render_header(self) -> None:
        required = [t for t in self.tgt.fields if t.get("required")]
        mapped_req = [t for t in required if self.rules.get(t["name"], "").strip()]
        mapped = [t for t in self.tgt.fields if self.rules.get(t["name"], "").strip()]
        self.progress.value = (f"{len(mapped)}/{len(self.tgt.fields)} mapped · "
                               f"{len(mapped_req)}/{len(required)} required")
        self.progress.color = ft.Colors.GREEN_700 if len(mapped_req) == len(required) else ft.Colors.AMBER_800
        self.dirty_chip.content = chip("unsaved changes", ft.Colors.AMBER_700) if self.dirty else None
        self.steps_btn.content = f"Row steps ({len(self.row_steps)})" if self.row_steps else "Row steps"
        if self.suggestions:
            self.banner.visible = True
            self.banner.content = ft.Container(ft.Row([
                ft.Icon(ft.Icons.AUTO_FIX_HIGH, color=ft.Colors.PRIMARY),
                ft.Text(f"{len(self.suggestions)} suggested matches are shown as dashed arrows. "
                        "Accept them all, or click the check mark on a target field to accept one.", expand=True, size=13),
                ft.TextButton("Dismiss", on_click=lambda _: self.dismiss_suggestions()),
                ft.FilledTonalButton("Accept all", icon=ft.Icons.DONE_ALL, on_click=lambda _: self.accept_all()),
            ]), bgcolor=ft.Colors.with_opacity(0.08, ft.Colors.PRIMARY), padding=10, border_radius=ft.BorderRadius.all(8))
        else:
            self.banner.visible = False

    def render_lists(self) -> None:
        srcs, tgts = self.visible_src(), self.visible_tgt()
        used = {c for f in self.rules.values() for c in referenced_columns(f)}
        self.src_col.controls = [self.src_row(f, i, f["name"] in used) for i, f in enumerate(srcs)]
        self.tgt_col.controls = [self.tgt_row(f) for f in tgts]

        s_idx = {f["name"]: i for i, f in enumerate(srcs)}
        links: list[Link] = []
        for ti, t in enumerate(tgts):
            name = t["name"]
            formula = self.rules.get(name, "").strip()
            if formula:
                color = STATUS_COLORS[self.field_status(t)]
                width = 3.0 if name == self.sel_tgt else 1.8
                for ref in referenced_columns(formula):
                    if ref in s_idx:
                        links.append(Link(s_idx[ref], ti, color, width))
            elif name in self.suggestions and self.suggestions[name][0] in s_idx:
                links.append(Link(s_idx[self.suggestions[name][0]], ti, STATUS_COLORS["suggested"], 1.6, dashed=True))
        self.canvas_holder.content = link_canvas(links, CANVAS_W, max(len(srcs), len(tgts)))

    def src_row(self, f: dict, idx: int, used: bool) -> ft.Control:
        selected = f["name"] == self.sel_src
        body = ft.Container(
            ft.Row([
                ft.Text(f.get("column_letter", str(idx + 1)), size=11, width=22, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text(f["name"], size=13, expand=True, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS,
                        weight=ft.FontWeight.W_600 if selected else ft.FontWeight.W_500,
                        tooltip=self._src_tooltip(f)),
                type_badge(f["type"]),
                ft.Container(width=8, height=8, border_radius=ft.BorderRadius.all(4),
                             bgcolor=ft.Colors.PRIMARY if used else ft.Colors.TRANSPARENT),
            ], spacing=6),
            height=ROW_H - 4, margin=ft.Margin.symmetric(vertical=2),
            padding=ft.Padding.symmetric(horizontal=8),
            border=ft.Border.all(2 if selected else 1, ft.Colors.PRIMARY if selected else ft.Colors.OUTLINE_VARIANT),
            border_radius=ft.BorderRadius.all(8),
            bgcolor=ft.Colors.PRIMARY_CONTAINER if selected else ft.Colors.SURFACE,
            on_click=None if self.readonly else (lambda _, n=f["name"]: self.select_source(n)), ink=not self.readonly,
        )
        feedback = ft.Container(ft.Text(f["name"], size=13, color=ft.Colors.ON_PRIMARY),
                                bgcolor=ft.Colors.PRIMARY, padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                                border_radius=ft.BorderRadius.all(8))
        if self.readonly:
            return body
        return ft.Draggable(group="fields", data=f["name"], content=body, content_feedback=feedback)

    def _src_tooltip(self, f: dict) -> str:
        parts = [f"{f['name']} ({f['type']})"]
        if f.get("samples"):
            parts.append("e.g. " + ", ".join(str(s) for s in f["samples"][:3]))
        if "null_pct" in f:
            parts.append(f"{f['null_pct']}% blank")
        if f.get("note"):
            parts.append(f["note"])
        return "\n".join(parts)

    def tgt_row(self, t: dict) -> ft.Control:
        name = t["name"]
        status = self.field_status(t)
        selected = name == self.sel_tgt
        formula = self.rules.get(name, "").strip()
        refs = referenced_columns(formula)
        is_simple = bool(refs) and formula == f"[{refs[0]}]"
        trailing: list[ft.Control] = [type_badge(t["type"])]
        if name in self.suggestions and not formula and not self.readonly:
            trailing.insert(0, ft.IconButton(ft.Icons.CHECK, icon_size=16, tooltip=f"Accept {self.suggestions[name][0]}",
                                             on_click=lambda _, n=name: self.accept(n), width=28, height=28,
                                             style=ft.ButtonStyle(padding=0)))
        body = ft.Container(
            ft.Row([
                ft.Container(width=10, height=10, border_radius=ft.BorderRadius.all(5), bgcolor=STATUS_COLORS[status],
                             tooltip=status),
                ft.Column([
                    ft.Text(name + (" *" if t.get("required") else ""), size=13, no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            weight=ft.FontWeight.W_600 if selected else ft.FontWeight.W_500),
                    ft.Text(("ƒ " + formula) if formula and not is_simple else "", size=10.5, no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS, color=ft.Colors.ON_SURFACE_VARIANT,
                            visible=bool(formula) and not is_simple),
                ], spacing=0, expand=True, alignment=ft.MainAxisAlignment.CENTER),
                *trailing,
            ], spacing=8),
            height=ROW_H - 4, margin=ft.Margin.symmetric(vertical=2),
            padding=ft.Padding.symmetric(horizontal=8),
            border=ft.Border.all(2 if selected else 1, ft.Colors.PRIMARY if selected else ft.Colors.OUTLINE_VARIANT),
            border_radius=ft.BorderRadius.all(8),
            bgcolor=ft.Colors.PRIMARY_CONTAINER if selected else ft.Colors.SURFACE,
            on_click=lambda _, n=name: self.select_target(n), ink=True,
        )

        def will_accept(e):
            body.border = ft.Border.all(2, ft.Colors.PRIMARY)
            body.update()

        def leave(e):
            body.border = ft.Border.all(2 if selected else 1,
                                        ft.Colors.PRIMARY if selected else ft.Colors.OUTLINE_VARIANT)
            body.update()

        if self.readonly:
            return body
        return ft.DragTarget(group="fields", content=body, on_will_accept=will_accept, on_leave=leave,
                             on_accept=guarded(self.page, lambda e, n=name: self.link(e.src.data, n)))

    # ------------------------------------------------------------------ inspector

    def render_inspector(self) -> None:
        if not self.sel_tgt:
            self.inspector.controls = self.help_panel()
            return
        t = next((f for f in self.tgt.fields if f["name"] == self.sel_tgt), None)
        if not t:
            self.sel_tgt = None
            self.inspector.controls = self.help_panel()
            return
        name = t["name"]
        formula = ft.TextField(value=self.rules.get(name, ""), multiline=True, min_lines=2, max_lines=5,
                               read_only=self.readonly,
                               label="Formula", text_style=ft.TextStyle(font_family="monospace", size=13),
                               hint_text='[Source column]   or   TRIM([First Name]) & " " & [Last Name]')
        err = self.result.rule_errors.get(name) if self.result else None
        formula_msg = ft.Text(err or "", size=12, color=ft.Colors.RED_600, visible=bool(err))

        def apply(_=None):
            text = (formula.value or "").strip()
            if text:
                try:
                    compile_formula(text, {f["name"] for f in self.src_fields})
                except FormulaError as ex:
                    formula_msg.value, formula_msg.visible = str(ex), True
                    formula_msg.update()
                    return
            self.set_rule(name, text)

        def insert(snippet: str):
            formula.value = (formula.value or "") + snippet
            formula.update()
            formula.focus()

        col_dd = ft.Dropdown(label="Insert column", dense=True, width=190, enable_filter=True,
                             options=[ft.DropdownOption(key=f["name"], text=f["name"]) for f in self.src_fields],
                             on_select=lambda e: insert(f"[{e.control.value}]"))
        fn_help = ft.Text("", size=11.5, color=ft.Colors.ON_SURFACE_VARIANT)

        def pick_fn(e):
            fn_help.value = f"{e.control.value}: {FUNCTIONS[e.control.value].doc}"
            fn_help.update()
            insert(f"{e.control.value}(")

        fn_dd = ft.Dropdown(label="Insert function", dense=True, width=170, enable_filter=True,
                            options=[ft.DropdownOption(key=k, text=k) for k in sorted(FUNCTIONS)], on_select=pick_fn)

        # Sample values for this field from the preview
        samples: list[ft.Control] = []
        if self.result is not None and name in self.result.all_rows.columns:
            errs = {i: e[name] for i, e in self.result.cell_errors.items() if name in e}
            for i, v in enumerate(self.result.all_rows[name].head(6).to_list()):
                samples.append(ft.Row([
                    ft.Text(f"row {i + 1}", size=11, width=48, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text("(blank)" if v is None else str(v), size=12, expand=True, no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS, color=ft.Colors.RED_600 if i in errs else None),
                    ft.Text(errs.get(i, ""), size=11, color=ft.Colors.RED_600),
                ], spacing=6))
            bad = len(errs)
            samples.insert(0, ft.Text(f"{bad} of {self.result.all_rows.height} sample rows fail for this field"
                                      if bad else "All sample rows pass for this field",
                                      size=12, color=ft.Colors.RED_600 if bad else ft.Colors.GREEN_700))

        # Validation rules
        rules = [v for v in self.validations if v.get("field") == name]
        v_rows = [ft.Row([
            ft.Text(VALIDATION_KINDS.get(v["kind"], v["kind"]), size=12, expand=True),
            ft.Text(str(v.get("value", "")), size=12, weight=ft.FontWeight.W_500),
            ft.IconButton(ft.Icons.CLOSE, icon_size=16, visible=not self.readonly,
                          on_click=lambda _, vv=v: self.remove_validation(vv)),
        ]) for v in rules]
        kind_dd = ft.Dropdown(dense=True, width=210, value="allowed",
                              options=[ft.DropdownOption(key=k, text=t_) for k, t_ in VALIDATION_KINDS.items()])
        val_tf = ft.TextField(dense=True, expand=True, hint_text="value")

        def add_validation(_):
            if kind_dd.value != "unique" and not (val_tf.value or "").strip():
                raise ValueError("Enter a value for this rule")
            self.validations.append({"field": name, "kind": kind_dd.value, "value": (val_tf.value or "").strip()})
            self.changed()

        def toggle_required(e):
            self.app.require("design")
            t["required"] = e.control.value
            tgt_svc.save_target(self.tgt.name, self.tgt.fields, target_id=self.tgt.id)
            self.changed(dirty=False)

        self.inspector.controls = [
            ft.Row([ft.Text(name, size=17, weight=ft.FontWeight.W_600, expand=True), type_badge(t["type"])]),
            ft.Text(t.get("description") or "", size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                    visible=bool(t.get("description"))),
            ft.Checkbox(label="Required (blank values reject the row)", value=bool(t.get("required")),
                        on_change=guarded(self.page, toggle_required), disabled=self.readonly),
            formula, formula_msg,
            *([] if self.readonly else [
                ft.Row([col_dd, fn_dd], spacing=8, wrap=True),
                fn_help,
                ft.Row([
                    ft.FilledButton("Apply", icon=ft.Icons.CHECK, on_click=guarded(self.page, apply)),
                    ft.TextButton("Remove mapping", icon=ft.Icons.LINK_OFF,
                                  on_click=guarded(self.page, lambda _: self.set_rule(name, ""))),
                ]),
            ]),
            ft.Divider(),
            ft.Text("Sample output", size=13, weight=ft.FontWeight.W_600),
            *samples,
            ft.Divider(),
            ft.Text("Validation rules", size=13, weight=ft.FontWeight.W_600),
            *v_rows,
            *([] if self.readonly else [
                ft.Row([kind_dd, val_tf, ft.IconButton(ft.Icons.ADD, on_click=guarded(self.page, add_validation))],
                       spacing=6)]),
        ]

    def help_panel(self) -> list[ft.Control]:
        legend = ft.Column([
            ft.Row([ft.Container(width=10, height=10, bgcolor=STATUS_COLORS[k], border_radius=ft.BorderRadius.all(5)),
                    ft.Text(t, size=12)], spacing=8)
            for k, t in [("ok", "Mapped, types compatible"), ("lossy", "Mapped, some values may not convert"),
                         ("error", "Formula error or incompatible types"), ("suggested", "Suggested (dashed)"),
                         ("unmapped", "Not mapped")]
        ], spacing=4)
        return [
            ft.Text("How to map", size=16, weight=ft.FontWeight.W_600),
            ft.Text("• Drag a source field onto a target field, or click a source field and then a target field.\n"
                    "• Click a target field to edit its formula, e.g. combine first and last name.\n"
                    "• Auto-map suggests matches by name and type.\n"
                    "• Row steps filter, de-duplicate, sort or unpivot rows before mapping.\n"
                    "• The preview below updates as you work; red cells would be rejected.", size=13),
            ft.Divider(),
            ft.Text("Legend", size=13, weight=ft.FontWeight.W_600),
            legend,
        ]

    # ------------------------------------------------------------------ preview

    def run_preview(self) -> None:
        try:
            self.result = svc.preview(self.m.id, self.spec())
        except Exception as e:  # noqa: BLE001 - shown in the preview panel
            self.result = None
            self.preview_error = f"{type(e).__name__}: {e}"
        else:
            self.preview_error = ""

    def render_preview(self) -> None:
        if not self.result:
            self.preview_box.controls = [ft.Text("Preview unavailable: " + getattr(self, "preview_error", ""),
                                                 color=ft.Colors.RED_600)]
            return
        s = self.result.stats
        self.preview_box.controls = [
            ft.Row([
                ft.Text("Preview", size=14, weight=ft.FontWeight.W_600),
                chip(f"{s['rows_in']:,} rows in (sample)"),
                chip(f"{s['rows_out']:,} valid", ft.Colors.GREEN_600),
                chip(f"{s['rows_rejected']:,} rejected", ft.Colors.RED_500 if s["rows_rejected"] else ft.Colors.OUTLINE),
                ft.Text("Hover a red cell to see why.", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ], spacing=8),
            df_table(self.result.all_rows, self.result.cell_errors, max_rows=25),
        ]

    # ================================================================== actions

    def changed(self, dirty: bool = True) -> None:
        self.dirty = self.dirty or dirty
        self.run_preview()
        self.render_all()

    def select_source(self, name: str) -> None:
        self.sel_src = None if self.sel_src == name else name
        self.render_lists()
        self.page.update()

    def select_target(self, name: str) -> None:
        if self.sel_src:
            self.link(self.sel_src, name)
            return
        self.sel_tgt = name
        self.render_lists()
        self.render_inspector()
        self.page.update()

    def link(self, source: str, target: str) -> None:
        self.app.require("design")
        self.suggestions.pop(target, None)
        self.rules[target] = f"[{source}]"
        self.sel_src = None
        self.sel_tgt = target
        self.changed()
        t = next(f for f in self.tgt.fields if f["name"] == target)
        s = next((f for f in self.src_fields if f["name"] == source), {"type": "string"})
        compat = compatibility(s["type"], t["type"])
        if compat != "ok":
            toast(self.page, f"{source} ({s['type']}) to {target} ({t['type']}): {compat} conversion. "
                  "Check the preview or adjust the formula.")

    def set_rule(self, target: str, formula: str) -> None:
        self.app.require("design")
        if formula:
            self.rules[target] = formula
        else:
            self.rules.pop(target, None)
        self.changed()

    def remove_validation(self, v: dict) -> None:
        self.app.require("design")
        self.validations.remove(v)
        self.changed()

    def auto_map(self, _=None) -> None:
        self.app.require("design")
        svc.save_spec(self.m.id, self.spec())  # suggestions consider current rules and row steps
        found = svc.auto_map_suggestions(self.m.id)
        self.suggestions = {s.target: (s.source, s.score) for s in found}
        if not found:
            toast(self.page, "No further confident matches. Map the remaining fields by hand.")
        self.render_all()

    def ai_suggest(self, _=None) -> None:
        """Asks an LLM for matches by meaning; results show as dashed suggestions like Auto-map."""
        self.app.require("design")
        self.app.require("use_ai")
        from databridge.services import ai_assist, llm

        options = llm.available_models(self.app.user)
        if not options:
            toast(self.page, "No AI models available. Add an API key under AI > Models.", error=True)
            return
        model = ft.Dropdown(label="Model", dense=True, width=460, value=llm.default_model(self.app.user, "automap", options),
                            options=[ft.DropdownOption(key=o.ref, text=f"{o.label} ({o.network})") for o in options])
        note = ft.Text("Column names and types are sent to the model. Sample values are sent only to internal "
                       "(shared, on-network) models.", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        spinner = ft.ProgressRing(visible=False, width=20, height=20)

        def go(_):
            spinner.visible = True
            spinner.update()
            try:
                mapped = {t for t, f in self.rules.items() if f.strip()}
                found = ai_assist.suggest_mappings(self.app.user, model.value, self.src_fields, self.tgt.fields,
                                                   already_mapped=mapped)
            finally:
                spinner.visible = False
            close_dialog(self.page)
            for sug in found:
                self.suggestions[sug.target] = (sug.source, round(sug.confidence * 100, 1))
            self.app.audit("mapping.ai_suggest", self.m.name, f"{len(found)} suggestions")
            toast(self.page, f"AI suggested {len(found)} match(es)" if found else
                  "The model found no confident matches for the unmapped fields")
            self.render_all()

        dialog(self.page, "AI suggest mappings", ft.Column([model, note, spinner], tight=True, spacing=10), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Suggest", icon=ft.Icons.AUTO_AWESOME, on_click=background(self.page, go))], width=520)

    def accept(self, target: str) -> None:
        self.app.require("design")
        source, _ = self.suggestions.pop(target)
        self.rules[target] = f"[{source}]"
        self.changed()

    def accept_all(self) -> None:
        self.app.require("design")
        for target, (source, _) in self.suggestions.items():
            self.rules[target] = f"[{source}]"
        self.suggestions = {}
        self.changed()

    def dismiss_suggestions(self) -> None:
        self.suggestions = {}
        self.render_all()

    def on_search_src(self, e) -> None:
        self.q_src = e.control.value or ""
        self.render_lists()
        self.page.update()

    def on_search_tgt(self, e) -> None:
        self.q_tgt = e.control.value or ""
        self.render_lists()
        self.page.update()

    def save(self, _=None) -> None:
        self.app.require("design")
        svc.save_spec(self.m.id, self.spec())
        self.dirty = False
        self.app.audit("mapping.save", self.m.name, f"{len(self.spec().rules)} rules")
        toast(self.page, "Draft saved")
        self.render_all()

    def publish(self, _=None) -> None:
        self.app.require("design")
        missing = [t["name"] for t in self.tgt.fields if t.get("required") and not self.rules.get(t["name"], "").strip()]
        if missing:
            raise ValueError(f"Map required fields first: {', '.join(missing)}")
        svc.save_spec(self.m.id, self.spec())
        self.dirty = False
        ds = svc.publish(self.m.id)
        self.app.audit("mapping.publish", self.m.name, f"v{ds.version}, {ds.row_count} rows")
        self.m = svc.get_mapping(self.m.id)
        toast(self.page, f"Published v{ds.version}: {ds.row_count:,} rows served, {ds.rejected_count:,} rejected"
              + (" (download the reject report from Runs)" if ds.rejected_count else ""))
        self.render_all()

    def leave(self) -> None:
        if not self.dirty or self.readonly:
            self.app.navigate("mappings")
            return

        def discard(_):
            close_dialog(self.page)
            self.app.navigate("mappings")

        def save_and_leave(_):
            close_dialog(self.page)
            svc.save_spec(self.m.id, self.spec())
            self.app.navigate("mappings")

        dialog(self.page, "Unsaved changes", ft.Text("Save your changes to this mapping before leaving?"), [
            ft.TextButton("Discard", on_click=discard),
            ft.FilledButton("Save and leave", on_click=guarded(self.page, save_and_leave)),
        ], width=400)

    # ------------------------------------------------------------------ row steps

    def edit_steps(self) -> None:
        self.app.require("design")
        steps = [dict(s) for s in self.row_steps]
        listing = ft.Column(spacing=6)
        cols = [f["name"] for f in self.src.fields]

        def describe(s: dict) -> str:
            k = s["kind"]
            if k == "filter":
                return f"Keep rows where {s.get('formula')}"
            if k == "dedupe":
                return f"Remove duplicates on {', '.join(s.get('columns') or ['all columns'])}"
            if k == "sort":
                return f"Sort by {s.get('column')} {'descending' if s.get('descending') else 'ascending'}"
            if k == "unpivot":
                return (f"Unpivot {', '.join(s.get('value_columns', []))} into "
                        f"{s.get('variable_name') or 'attribute'} / {s.get('value_name') or 'value'}")
            return k

        def redraw():
            listing.controls = [ft.Row([
                ft.Text(f"{i + 1}.", size=12, width=20),
                ft.Text(describe(s), size=13, expand=True),
                ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=18, on_click=lambda _, i=i: (steps.pop(i), redraw())),
            ]) for i, s in enumerate(steps)] or [ft.Text("No row steps. Rows are mapped as they are.", size=12,
                                                         color=ft.Colors.ON_SURFACE_VARIANT)]
            if mounted(listing):
                listing.update()

        kind = ft.Dropdown(label="Add step", dense=True, width=200, value="filter", options=[
            ft.DropdownOption(key="filter", text="Filter rows"), ft.DropdownOption(key="dedupe", text="Remove duplicates"),
            ft.DropdownOption(key="sort", text="Sort"), ft.DropdownOption(key="unpivot", text="Unpivot columns to rows")])
        a = ft.TextField(dense=True, expand=True, label="Condition", hint_text='[Status] <> "Cancelled"')
        b = ft.TextField(dense=True, width=160, label="", visible=False)
        c = ft.TextField(dense=True, width=160, label="", visible=False)
        col_dd = ft.Dropdown(dense=True, width=220, label="Column", visible=False,
                             options=[ft.DropdownOption(key=x, text=x) for x in cols])
        desc = ft.Checkbox(label="Descending", visible=False)

        def on_kind(_=None):
            k = kind.value
            a.visible = k in {"filter", "dedupe", "unpivot"}
            a.label = {"filter": "Condition", "dedupe": "Columns (comma separated, blank = all)",
                       "unpivot": "Columns to turn into rows (comma separated)"}.get(k, "")
            a.hint_text = {"filter": '[Status] <> "Cancelled"', "unpivot": "Jan, Feb, Mar"}.get(k, "")
            b.visible = c.visible = k == "unpivot"
            b.label, c.label = "Name column", "Value column"
            col_dd.visible = desc.visible = k == "sort"
            for ctl in (a, b, c, col_dd, desc):
                if mounted(ctl):
                    ctl.update()

        kind.on_select = on_kind
        on_kind()

        def add(_):
            k = kind.value
            split = [x.strip() for x in (a.value or "").split(",") if x.strip()]
            if k == "filter":
                compile_formula(a.value or "", set(cols))
                steps.append({"kind": "filter", "formula": a.value})
            elif k == "dedupe":
                steps.append({"kind": "dedupe", "columns": split})
            elif k == "sort":
                if not col_dd.value:
                    raise ValueError("Choose a column to sort by")
                steps.append({"kind": "sort", "column": col_dd.value, "descending": desc.value})
            elif k == "unpivot":
                unknown = [x for x in split if x not in cols]
                if not split or unknown:
                    raise ValueError(f"Unknown columns: {unknown}" if unknown else "List the columns to unpivot")
                steps.append({"kind": "unpivot", "value_columns": split, "variable_name": b.value or "attribute",
                              "value_name": c.value or "value"})
            a.value = ""
            redraw()

        def done(_):
            self.row_steps = steps
            close_dialog(self.page)
            self.changed()

        redraw()
        dialog(self.page, "Row steps (run before column mapping)", ft.Column([
            listing, ft.Divider(),
            ft.Row([kind, a], spacing=8),
            ft.Row([col_dd, desc, b, c], spacing=8),
            ft.Row([ft.OutlinedButton("Add step", icon=ft.Icons.ADD, on_click=guarded(self.page, add))]),
        ], spacing=10, tight=True, scroll=ft.ScrollMode.AUTO), [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Apply", on_click=guarded(self.page, done)),
        ], width=720)


__all__ = ["MappingStudio", "base_type"]
