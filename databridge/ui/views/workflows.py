"""AI workflows in the studio: list, canvas editor with node inspector and guardrails, run results.

Canvas: nodes are cards placed freely on a fixed-size board (drag to move). Connect by clicking a node's
output port (the dot on its right edge), then clicking the node to feed. Connections are drawn as curves on
a canvas behind the cards, the same technique as the Mapping Studio's arrows.
"""

import copy
import json

import flet as ft
import flet.canvas as cv

from databridge.ai import guardrails as gr
from databridge.services import llm, mappings as map_svc, prompts as prompt_svc, sources as src_svc
from databridge.services import workflows as wf_svc
from databridge.ui.common import (background, card, chip, close_dialog, confirm, df_table, dialog, empty_state, guarded,
                                  mounted, toast)

NODE_W, NODE_H = 170, 86
BOARD_W, BOARD_H = 1600, 900
GROUP_COLOR = {"Inputs": ft.Colors.TEAL_600, "Logic": ft.Colors.INDIGO_400, "AI": ft.Colors.DEEP_PURPLE_400,
               "Outputs": ft.Colors.GREEN_700}
ICONS = {"input_rows": ft.Icons.TABLE_ROWS_OUTLINED, "input_form": ft.Icons.INPUT, "transform": ft.Icons.FUNCTIONS,
         "sql": ft.Icons.STORAGE, "router": ft.Icons.CALL_SPLIT, "llm": ft.Icons.AUTO_AWESOME,
         "output_dataset": ft.Icons.SAVE_ALT}
STATUS_COLOR = {"ok": ft.Colors.GREEN_600, "warning": ft.Colors.AMBER_700, "failed": ft.Colors.RED_500,
                "blocked": ft.Colors.RED_500, "running": ft.Colors.BLUE_400, "skipped": ft.Colors.OUTLINE}
TEMPLATES = {"classify": "Classify each row (JSON output, router, two outputs)",
             "summarize": "Summarize a dataset (SQL aggregate, then one LLM call)",
             "ask": "Answer a question from the run input",
             "blank": "Blank"}
PORT_LABEL = {"true": "yes", "false": "no"}


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def small(text: str, **kw) -> ft.Text:
    return ft.Text(text, size=11.5, color=ft.Colors.ON_SURFACE_VARIANT, **kw)


def section(title: str, *controls: ft.Control, icon=None) -> ft.Control:
    head = ft.Row([ft.Icon(icon, size=16, color=ft.Colors.PRIMARY)] if icon else [], spacing=6)
    head.controls.append(ft.Text(title, size=13, weight=ft.FontWeight.W_600))
    return ft.Container(ft.Column([head, *controls], spacing=8, tight=True), padding=10,
                        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=ft.BorderRadius.all(8))


# ================================================================== list (AI > Workflows tab)


class WorkflowsTab:
    def __init__(self, view):
        self.view, self.app, self.page = view, view.app, view.page

    def build(self) -> ft.Control:
        items = []
        runs = wf_svc.runs_for(limit=300)
        last = {}
        for r in runs:
            last.setdefault(r.workflow_id, r)
        for wf in wf_svc.list_workflows():
            r = last.get(wf.id)
            state = (chip(f"published v{wf.published_version}", ft.Colors.GREEN_600) if wf.published_version
                     else chip("draft"))
            items.append(card(ft.Row([
                ft.Icon(ft.Icons.ACCOUNT_TREE_OUTLINED, color=ft.Colors.PRIMARY),
                ft.Column([
                    ft.Row([ft.Text(wf.name, size=14, weight=ft.FontWeight.W_600), state,
                            *([chip("unpublished changes", ft.Colors.AMBER_700)]
                              if wf.published_version and wf.has_draft_changes else []),
                            *([chip(f"last run {r.status}", STATUS_COLOR.get(r.status, ft.Colors.OUTLINE))] if r else [])],
                           spacing=8, wrap=True),
                    small(f"{len(wf.spec.get('nodes', []))} nodes · owner {wf.owner} · API: POST /api/v1/workflows/"
                          f"{wf.slug}/run" + (f" · last run {_fmt(r.started_at)}" if r else ""), selectable=True),
                    *([small(wf.description)] if wf.description else []),
                ], spacing=2, expand=True),
                ft.FilledTonalButton("Open", icon=ft.Icons.EDIT_OUTLINED,
                                     on_click=lambda _, w=wf: self.app.navigate("workflow", workflow_id=w.id)),
                ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete", on_click=lambda _, w=wf: confirm(
                    self.page, "Delete workflow", f"Delete {w.name} and its run history?", lambda: self.delete(w))),
            ], spacing=10), padding=12))
        run_rows = [ft.DataRow(cells=[
            ft.DataCell(ft.Text(_fmt(r.started_at), size=12)),
            ft.DataCell(ft.Text(self._name(r.workflow_id), size=12)),
            ft.DataCell(ft.Text(r.trigger + ("" if r.version else " (draft)"), size=12)),
            ft.DataCell(chip(r.status, STATUS_COLOR.get(r.status, ft.Colors.OUTLINE))),
            ft.DataCell(ft.Text(f"{r.rows_in} / {r.rows_out}", size=12)),
            ft.DataCell(ft.Text(f"{r.rows_flagged} / {r.rows_rejected}", size=12)),
            ft.DataCell(ft.Text(f"{r.tokens:,}", size=12)),
            ft.DataCell(ft.TextButton("Details", on_click=lambda _, rid=r.id: show_run(self.page, rid))),
        ]) for r in runs[:30]]
        return ft.Column([
            ft.Row([ft.Text("Workflows", size=16, weight=ft.FontWeight.W_600, expand=True),
                    ft.FilledButton("New workflow", icon=ft.Icons.ADD, on_click=lambda _: self.new())]),
            small("Chain inputs, logic and LLM calls. Guardrails check what is sent to models and what comes back."),
            *(items or [empty_state(ft.Icons.ACCOUNT_TREE_OUTLINED, "No workflows yet",
                                    "Start from a template: classify rows, summarize a dataset or answer questions.")]),
            ft.Divider(),
            ft.Text("Recent runs", size=16, weight=ft.FontWeight.W_600),
            ft.Row([ft.DataTable(columns=[ft.DataColumn(ft.Text(h, size=12, weight=ft.FontWeight.W_600)) for h in (
                "Started", "Workflow", "Trigger", "Status", "Rows in / out", "Flagged / rejected", "Tokens", "")],
                rows=run_rows, data_row_min_height=36, data_row_max_height=40)], scroll=ft.ScrollMode.AUTO)
            if run_rows else small("No runs yet."),
        ], spacing=10)

    def _name(self, wid: int) -> str:
        if not hasattr(self, "_names"):
            self._names = {w.id: w.name for w in wf_svc.list_workflows()}
        return self._names.get(wid, str(wid))

    def new(self) -> None:
        name = ft.TextField(label="Name", dense=True, autofocus=True, hint_text="e.g. Triage support tickets")
        template = ft.RadioGroup(value="classify", content=ft.Column(
            [ft.Radio(value=k, label=v) for k, v in TEMPLATES.items()], spacing=0))
        desc = ft.TextField(label="Description (optional)", dense=True)

        def create(_):
            wf = wf_svc.create_workflow(name.value, self.app.user.username, template.value, desc.value or "")
            models = llm.available_models(self.app.user)
            if models:  # start the template's LLM nodes on the first model this user may use
                spec = wf.spec
                for n in spec["nodes"]:
                    if n["type"] == "llm" and not n["config"].get("model"):
                        n["config"]["model"] = llm.default_model(self.app.user, "workflow", models)
                wf = wf_svc.save_spec(wf.id, spec, self.app.user.username)
            close_dialog(self.page)
            self.app.navigate("workflow", workflow_id=wf.id)

        dialog(self.page, "New workflow", ft.Column([name, desc, ft.Text("Start from", size=13), template],
                                                    spacing=12, tight=True),
               [ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
                ft.FilledButton("Create", on_click=guarded(self.page, create))], width=520)

    def delete(self, wf) -> None:
        wf_svc.delete_workflow(wf.id, self.app.user.username)
        self.app.navigate("ai", tab="workflows")


# ================================================================== editor


class WorkflowEditor:
    def __init__(self, app, workflow_id: int):
        self.app, self.page = app, app.page
        self.wf = wf_svc.get_workflow(workflow_id)
        self.spec = copy.deepcopy(self.wf.spec) or {"nodes": [], "edges": []}
        self.spec.setdefault("limits", dict(wf_svc.DEFAULT_LIMITS))
        self.selected: str | None = None
        self.connect_from: tuple[str, str] | None = None
        self.dirty = False
        self.models = llm.available_models(app.user)
        self.board = ft.Stack(width=BOARD_W, height=BOARD_H)
        self.edges_layer = ft.Container(left=0, top=0)
        self.node_controls: dict[str, ft.Control] = {}
        self.inspector = ft.Column(spacing=10, scroll=ft.ScrollMode.AUTO, expand=True)
        self.banner = ft.Container(visible=False)
        self.title = ft.Text(size=20, weight=ft.FontWeight.W_600)
        self.status_row = ft.Row(spacing=8)

    # ---------------------------------------------------------------- layout
    def build(self) -> ft.Control:
        self.app.require("use_ai")
        header = ft.Row([
            ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Back to workflows", on_click=lambda _: self.leave()),
            ft.Column([self.title, self.status_row], spacing=2, expand=True),
            ft.OutlinedButton("Settings", icon=ft.Icons.TUNE, on_click=lambda _: self.settings()),
            ft.OutlinedButton("Save", icon=ft.Icons.SAVE_OUTLINED, on_click=guarded(self.page, lambda _: self.save())),
            ft.OutlinedButton("Check & estimate", icon=ft.Icons.FACT_CHECK_OUTLINED,
                              on_click=guarded(self.page, lambda _: self.check())),
            ft.OutlinedButton("Test (3 rows)", icon=ft.Icons.SCIENCE_OUTLINED,
                              on_click=background(self.page, lambda _: self.run(test=True))),
            ft.FilledTonalButton("Run", icon=ft.Icons.PLAY_ARROW, on_click=background(self.page, lambda _: self.run())),
            ft.FilledButton("Publish", icon=ft.Icons.PUBLISH, on_click=guarded(self.page, lambda _: self.publish())),
        ], spacing=8)
        palette = ft.Column([ft.Text("Add a node", size=13, weight=ft.FontWeight.W_600)], spacing=6, width=165)
        for group in ("Inputs", "Logic", "AI", "Outputs"):
            palette.controls.append(small(group))
            for t, meta in wf_svc.NODE_TYPES.items():
                if meta["group"] == group:
                    palette.controls.append(ft.Container(
                        ft.Row([ft.Icon(ICONS[t], size=16, color=GROUP_COLOR[group]),
                                ft.Text(meta["label"], size=12.5)], spacing=8),
                        padding=ft.Padding.symmetric(horizontal=10, vertical=8), border_radius=ft.BorderRadius.all(8),
                        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), tooltip=meta["help"],
                        on_click=lambda _, tt=t: self.add_node(tt), ink=True))
        palette.controls += [ft.Container(height=8), small("Connect: click the dot on a node's right edge, then "
                                                           "click the node to feed. Drag nodes to move them.")]
        self.board.controls = [self.edges_layer]
        board_view = ft.Container(
            ft.Column([ft.Row([self.board], scroll=ft.ScrollMode.AUTO)], scroll=ft.ScrollMode.AUTO),
            expand=True, bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.ON_SURFACE),
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=ft.BorderRadius.all(10))
        body = ft.Row([
            ft.Container(palette, padding=ft.Padding.only(right=6)),
            ft.Column([self.banner, board_view], expand=True, spacing=6),
            ft.Container(self.inspector, width=370, padding=ft.Padding.only(left=6)),
        ], expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH)
        self.render_header()
        self.render_board()
        self.render_inspector()
        return ft.Column([header, body], expand=True, spacing=10)

    def render_header(self) -> None:
        self.title.value = self.wf.name
        state = [chip(f"published v{self.wf.published_version}", ft.Colors.GREEN_600) if self.wf.published_version
                 else chip("draft")]
        if self.dirty:
            state.append(chip("unsaved changes", ft.Colors.AMBER_700))
        elif self.wf.published_version and self.wf.has_draft_changes:
            state.append(chip("draft differs from published", ft.Colors.AMBER_700))
        lim = self.spec.get("limits") or {}
        state.append(small(f"limits: {int(lim.get('max_rows') or 0):,} rows, {int(lim.get('max_tokens_per_run') or 0):,}"
                           f" tokens/run, confirm over {int(lim.get('confirm_over_tokens') or 0):,}"))
        self.status_row.controls = state
        if mounted(self.status_row):
            self.status_row.update()

    def changed(self, redraw_node: str | None = None) -> None:
        if not self.dirty:
            self.dirty = True
            self.render_header()
        if redraw_node:
            self.render_board()

    # ---------------------------------------------------------------- board
    def node(self, nid: str) -> dict:
        return next(n for n in self.spec["nodes"] if n["id"] == nid)

    def summary(self, n: dict) -> str:
        c = n.get("config", {})
        t = n["type"]
        if t == "input_rows":
            if c.get("mapping_id"):
                return f"dataset of mapping {c['mapping_id']}"
            return (f"source {self._source_name(c.get('source_id'))}" if c.get("source_id") else "choose a source")
        if t == "input_form":
            return ", ".join(f["name"] for f in c.get("fields") or []) or "no fields"
        if t == "transform":
            return f"{len(c.get('columns') or [])} columns" + (" · filter" if c.get("filter") else "")
        if t == "sql":
            return (c.get("query") or "")[:60]
        if t == "router":
            return c.get("condition") or "set a condition"
        if t == "llm":
            m = (c.get("model") or "choose a model").split("/", 1)[-1]
            out = c.get("output") or {}
            kind = f"JSON {len(out.get('fields') or [])} fields" if out.get("kind") == "json" else "text"
            return f"{m} · {'batch' if c.get('mode') == 'batch' else 'per row'} · {kind}"
        if t == "output_dataset":
            return c.get("name") or "name the dataset"
        return ""

    def _source_name(self, sid) -> str:
        try:
            return src_svc.get_source(int(sid)).name
        except (LookupError, TypeError, ValueError):
            return "?"

    def port_y(self, n: dict, port: str) -> float:
        ports = wf_svc.NODE_TYPES[n["type"]]["ports"]
        if len(ports) <= 1:
            return NODE_H / 2
        return NODE_H * (ports.index(port) + 1) / (len(ports) + 1)

    def render_board(self) -> None:
        self.board.controls = [self.edges_layer] + [self.node_card(n) for n in self.spec["nodes"]]
        self.draw_edges()
        if mounted(self.board):
            self.board.update()

    def draw_edges(self) -> None:
        nodes = {n["id"]: n for n in self.spec["nodes"]}
        shapes = []
        for e in self.spec.get("edges", []):
            a, b = nodes.get(e["from"]), nodes.get(e["to"])
            if not a or not b:
                continue
            port = e.get("port") or "out"
            x1, y1 = a["x"] + NODE_W, a["y"] + self.port_y(a, port)
            x2, y2 = b["x"], b["y"] + NODE_H / 2
            color = {"true": ft.Colors.GREEN_600, "false": ft.Colors.ORANGE_700}.get(port, ft.Colors.PRIMARY)
            dx = max(40, abs(x2 - x1) / 2)
            stroke = ft.Paint(color=color, stroke_width=2, style=ft.PaintingStyle.STROKE, anti_alias=True)
            fill = ft.Paint(color=color, style=ft.PaintingStyle.FILL, anti_alias=True)
            shapes.append(cv.Path([cv.Path.MoveTo(x1, y1), cv.Path.CubicTo(x1 + dx, y1, x2 - dx, y2, x2 - 8, y2)],
                                  paint=stroke))
            shapes.append(cv.Path([cv.Path.MoveTo(x2 - 10, y2 - 5), cv.Path.LineTo(x2, y2),
                                   cv.Path.LineTo(x2 - 10, y2 + 5), cv.Path.Close()], paint=fill))
            if port in PORT_LABEL:
                shapes.append(cv.Text(x1 + 12, y1 + (-18 if port == "true" else 4), PORT_LABEL[port],
                                      style=ft.TextStyle(size=11, color=color, weight=ft.FontWeight.W_600)))
        self.edges_layer.content = cv.Canvas(shapes=shapes, width=BOARD_W, height=BOARD_H)
        if mounted(self.edges_layer):
            self.edges_layer.update()

    def node_card(self, n: dict) -> ft.Control:
        meta = wf_svc.NODE_TYPES[n["type"]]
        color = GROUP_COLOR[meta["group"]]
        selected = n["id"] == self.selected
        connecting = self.connect_from is not None
        body = ft.Container(
            ft.Column([
                ft.Row([ft.Icon(ICONS[n["type"]], size=18, color=color),
                        ft.Text(n.get("label") or meta["label"], size=13.5, weight=ft.FontWeight.W_600,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True)], spacing=6),
                ft.Text(meta["label"], size=10.5, color=color),
                ft.Text(self.summary(n), size=11, color=ft.Colors.ON_SURFACE_VARIANT, max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS),
            ], spacing=1, tight=True),
            left=8, top=0, width=NODE_W - 16, height=NODE_H, padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            bgcolor=ft.Colors.SURFACE, border_radius=ft.BorderRadius.all(10),
            border=ft.Border.all(2.5 if selected else 1,
                                 ft.Colors.PRIMARY if selected else (ft.Colors.AMBER_600 if connecting and
                                                                     meta["inputs"] else ft.Colors.OUTLINE_VARIANT)),
            shadow=ft.BoxShadow(blur_radius=6, color=ft.Colors.with_opacity(0.12, ft.Colors.BLACK)),
        )
        parts: list[ft.Control] = [body]
        if meta["inputs"]:
            parts.append(ft.Container(left=2, top=NODE_H / 2 - 6, width=12, height=12, bgcolor=ft.Colors.SURFACE,
                                      border=ft.Border.all(2, ft.Colors.OUTLINE), border_radius=ft.BorderRadius.all(6)))
        for port in meta["ports"]:
            active = self.connect_from == (n["id"], port)
            pcolor = {"true": ft.Colors.GREEN_600, "false": ft.Colors.ORANGE_700}.get(port, color)
            parts.append(ft.Container(
                left=NODE_W - 16, top=self.port_y(n, port) - 8, width=16, height=16,
                bgcolor=pcolor if active else ft.Colors.SURFACE, border=ft.Border.all(2.5, pcolor),
                border_radius=ft.BorderRadius.all(8),
                tooltip=f"Connect from {PORT_LABEL.get(port, 'output')}",
                on_click=lambda _, nid=n["id"], p=port: self.start_connect(nid, p)))
        stack = ft.Stack(parts, width=NODE_W, height=NODE_H)
        gd = ft.GestureDetector(content=stack, left=n["x"], top=n["y"], drag_interval=30,
                                mouse_cursor=ft.MouseCursor.MOVE,
                                on_tap=lambda _, nid=n["id"]: self.tap_node(nid),
                                on_pan_update=lambda e, nid=n["id"]: self.drag(nid, e),
                                on_pan_end=lambda _: self.changed())
        self.node_controls[n["id"]] = gd
        return gd

    def drag(self, nid: str, e) -> None:
        n = self.node(nid)
        d = e.local_delta
        if d is None:
            return
        n["x"] = max(0, min(BOARD_W - NODE_W, n["x"] + d.x))
        n["y"] = max(0, min(BOARD_H - NODE_H, n["y"] + d.y))
        gd = self.node_controls[nid]
        gd.left, gd.top = n["x"], n["y"]
        gd.update()
        self.draw_edges()

    def free_spot(self) -> tuple[float, float]:
        x, y = 40, 40
        taken = [(n["x"], n["y"]) for n in self.spec["nodes"]]
        while any(abs(x - a) < NODE_W and abs(y - b) < NODE_H + 10 for a, b in taken):
            y += NODE_H + 30
            if y > BOARD_H - NODE_H:
                y, x = 40, x + NODE_W + 60
        return x, y

    def add_node(self, node_type: str) -> None:
        x, y = self.free_spot()
        n = wf_svc.new_node(node_type, x, y)
        base, i = n["id"], 1
        while any(m["id"] == n["id"] for m in self.spec["nodes"]):
            n["id"] = f"{base}_{i}"
            i += 1
        if node_type == "llm" and self.models:
            n["config"]["model"] = llm.default_model(self.app.user, "workflow", self.models)
        self.spec["nodes"].append(n)
        if self.selected and wf_svc.NODE_TYPES[node_type]["inputs"]:
            src = self.node(self.selected)
            ports = wf_svc.NODE_TYPES[src["type"]]["ports"]
            if len(ports) == 1:  # auto-connect from the selected node
                self.spec["edges"].append({"from": src["id"], "to": n["id"], "port": ports[0]})
        self.selected = n["id"]
        self.changed()
        self.render_board()
        self.render_inspector()

    def start_connect(self, nid: str, port: str) -> None:
        self.connect_from = None if self.connect_from == (nid, port) else (nid, port)
        self.show_banner()
        self.render_board()

    def show_banner(self) -> None:
        if self.connect_from:
            n = self.node(self.connect_from[0])
            label = n.get("label") + (f" ({PORT_LABEL[self.connect_from[1]]})" if self.connect_from[1] in PORT_LABEL
                                      else "")
            self.banner.content = ft.Row([
                ft.Icon(ft.Icons.CABLE, color=ft.Colors.AMBER_800, size=18),
                ft.Text(f"Connecting from {label}: click the node to feed.", size=12.5, expand=True),
                ft.TextButton("Cancel", on_click=lambda _: self.start_connect(*self.connect_from))], spacing=8)
            self.banner.bgcolor = ft.Colors.with_opacity(0.12, ft.Colors.AMBER)
            self.banner.padding = ft.Padding.symmetric(horizontal=12, vertical=4)
            self.banner.border_radius = ft.BorderRadius.all(8)
            self.banner.visible = True
        else:
            self.banner.visible = False
        if mounted(self.banner):
            self.banner.update()

    def tap_node(self, nid: str) -> None:
        if self.connect_from:
            src, port = self.connect_from
            target = self.node(nid)
            if nid == src or not wf_svc.NODE_TYPES[target["type"]]["inputs"]:
                toast(self.page, "That node can't take an input", error=True)
                return
            edge = {"from": src, "to": nid, "port": port}
            if edge in self.spec["edges"]:
                toast(self.page, "Already connected")
            else:
                self.spec["edges"].append(edge)
                try:
                    wf_svc.topo_order(self.spec)
                except wf_svc.WorkflowError as ex:
                    self.spec["edges"].remove(edge)
                    toast(self.page, str(ex), error=True)
                    return
                self.changed()
            self.connect_from = None
            self.show_banner()
        self.selected = nid
        self.render_board()
        self.render_inspector()

    # ---------------------------------------------------------------- inspector
    def render_inspector(self) -> None:
        ins = self.inspector
        if not self.selected or not any(n["id"] == self.selected for n in self.spec["nodes"]):
            ins.controls = [section("Workflow", small("Select a node to edit it, or add one from the left."),
                                    small("Every LLM node has guardrails: PII masking by model network, prompt-"
                                          "injection checks, output validation and limits."), icon=ft.Icons.INFO_OUTLINE)]
            if mounted(ins):
                ins.update()
            return
        n = self.node(self.selected)
        cfg = n["config"]
        meta = wf_svc.NODE_TYPES[n["type"]]

        def set_label(e):
            n["label"] = e.control.value
            self.changed(redraw_node=n["id"])

        head = ft.Row([ft.Icon(ICONS[n["type"]], color=GROUP_COLOR[meta["group"]]),
                       ft.Text(meta["label"], size=15, weight=ft.FontWeight.W_600, expand=True),
                       ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete node",
                                     on_click=lambda _: self.delete_node(n["id"]))])
        controls: list[ft.Control] = [head, small(meta["help"]),
                                      ft.TextField(label="Label", value=n.get("label", ""), dense=True, on_blur=set_label,
                                                   on_submit=set_label)]
        builder = getattr(self, f"inspect_{n['type']}")
        controls += builder(n, cfg)
        controls.append(self.connections_section(n))
        ins.controls = controls
        if mounted(ins):
            ins.update()

    def connections_section(self, n: dict) -> ft.Control:
        rows = []
        for e in list(self.spec["edges"]):
            if n["id"] in (e["from"], e["to"]):
                other = self.node(e["to"] if e["from"] == n["id"] else e["from"])
                arrow = "to" if e["from"] == n["id"] else "from"
                port = f" ({PORT_LABEL[e['port']]})" if e.get("port") in PORT_LABEL else ""
                rows.append(ft.Row([ft.Text(f"{arrow} {other.get('label')}{port}", size=12, expand=True),
                                    ft.IconButton(ft.Icons.LINK_OFF, icon_size=16, tooltip="Disconnect",
                                                  on_click=lambda _, ed=e: self.disconnect(ed))], spacing=4))
        return section("Connections", *(rows or [small("None yet.")]), icon=ft.Icons.CABLE)

    def disconnect(self, edge: dict) -> None:
        self.spec["edges"].remove(edge)
        self.changed()
        self.render_board()
        self.render_inspector()

    def delete_node(self, nid: str) -> None:
        self.spec["nodes"] = [n for n in self.spec["nodes"] if n["id"] != nid]
        self.spec["edges"] = [e for e in self.spec["edges"] if nid not in (e["from"], e["to"])]
        self.selected = None
        self.changed()
        self.render_board()
        self.render_inspector()

    # small field helpers bound to config keys
    def tf(self, cfg: dict, key: str, label: str, multiline: bool = False, number: bool = False, hint: str = "",
           width: int | None = None, redraw: str | None = None) -> ft.TextField:
        def update(e):
            v = e.control.value
            if number:
                try:
                    v = float(v) if "." in (v or "") else int(v or 0)
                except ValueError:
                    toast(self.page, f"{label}: enter a number", error=True)
                    return
            cfg[key] = v
            self.changed(redraw_node=redraw)

        value = cfg.get(key)
        return ft.TextField(label=label, value="" if value is None else str(value), dense=True, multiline=multiline,
                            min_lines=3 if multiline else None, max_lines=12 if multiline else None, hint_text=hint,
                            width=width, on_blur=update, on_submit=None if multiline else update,
                            keyboard_type=ft.KeyboardType.NUMBER if number else None,
                            text_style=ft.TextStyle(font_family="monospace", size=12.5) if multiline else None)

    def dd(self, cfg: dict, key: str, label: str, options: dict[str, str], redraw: str | None = None,
           width: int | None = None, after=None) -> ft.Dropdown:
        def update(e):
            cfg[key] = e.control.value
            self.changed(redraw_node=redraw)
            if after:
                after()

        return ft.Dropdown(label=label, value=cfg.get(key), dense=True, width=width, on_select=update,
                           options=[ft.DropdownOption(key=k, text=v) for k, v in options.items()])

    def cb(self, cfg: dict, key: str, label: str, default: bool = True) -> ft.Checkbox:
        def update(e):
            cfg[key] = bool(e.control.value)
            self.changed()

        return ft.Checkbox(label=label, value=cfg.get(key, default), on_change=update)

    def list_editor(self, items: list[dict], columns: list[tuple[str, str, int]], add_label: str,
                    new_item: dict, type_options: dict[str, dict[str, str]] | None = None) -> ft.Control:
        """Editable rows of small dicts, e.g. transform columns or JSON output fields."""
        col = ft.Column(spacing=6)

        def redraw():
            col.controls = []
            for item in items:
                row = ft.Row(spacing=6, wrap=True)
                for key, label, width in columns:
                    if type_options and key in type_options:
                        def pick(e, it=item, k=key):
                            it[k] = e.control.value
                            self.changed()
                            redraw()
                        row.controls.append(ft.Dropdown(value=item.get(key), label=label, dense=True, width=width,
                                                        on_select=pick, options=[ft.DropdownOption(key=k2, text=v2)
                                                                                 for k2, v2 in type_options[key].items()]))
                    elif isinstance(new_item.get(key), bool):
                        def tick(e, it=item, k=key):
                            it[k] = bool(e.control.value)
                            self.changed()
                        row.controls.append(ft.Checkbox(label=label, value=item.get(key, True), on_change=tick))
                    elif key == "enum" and item.get("type") != "enum":
                        continue
                    else:
                        def edit(e, it=item, k=key):
                            v = e.control.value
                            it[k] = [x.strip() for x in v.split(",") if x.strip()] if k in ("enum", "values") else v
                            self.changed()
                        v = item.get(key)
                        row.controls.append(ft.TextField(value=", ".join(v) if isinstance(v, list) else (v or ""),
                                                         label=label, dense=True, width=width, on_blur=edit,
                                                         on_submit=edit))
                row.controls.append(ft.IconButton(ft.Icons.CLOSE, icon_size=16, tooltip="Remove",
                                                  on_click=lambda _, it=item: (items.remove(it), self.changed(),
                                                                               redraw())))
                col.controls.append(ft.Container(row, padding=ft.Padding.only(bottom=4),
                                                 border=ft.Border.only(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT))))
            col.controls.append(ft.TextButton(add_label, icon=ft.Icons.ADD,
                                              on_click=lambda _: (items.append(copy.deepcopy(new_item)),
                                                                  self.changed(), redraw())))
            if mounted(col):
                col.update()

        redraw()
        return col

    # ---- per node type
    def inspect_input_rows(self, n, cfg) -> list[ft.Control]:
        options = {f"src:{s.id}": f"Source · {s.name}" for s in src_svc.list_sources()}
        options |= {f"map:{m.id}": f"Mapping dataset · {m.name}" for m in map_svc.list_mappings() if m.published_version}
        value = f"map:{cfg['mapping_id']}" if cfg.get("mapping_id") else (
            f"src:{cfg['source_id']}" if cfg.get("source_id") else None)
        info = ft.Column(spacing=6)

        def pick(e):
            kind, _, rid = (e.control.value or "").partition(":")
            cfg["source_id"] = int(rid) if kind == "src" else None
            cfg["mapping_id"] = int(rid) if kind == "map" else None
            self.changed(redraw_node=n["id"])
            show_info()

        def show_info():
            info.controls = []
            sid = cfg.get("source_id")
            if cfg.get("mapping_id"):
                sid = map_svc.get_mapping(cfg["mapping_id"]).source_id
            if sid:
                src = src_svc.get_source(sid)
                cols = [f["name"] for f in src.fields or []]
                cls = src.classification or {}
                tags = [chip(f"{c}: {gr.LEVEL_LABELS[lvl]}", ft.Colors.RED_400 if lvl == "confidential"
                             else ft.Colors.AMBER_700) for c, lvl in cls.items()]
                info.controls = [small("Columns: " + ", ".join(cols[:40])),
                                 ft.Row(tags or [small("No columns classified: all count as internal.")], wrap=True,
                                        spacing=6),
                                 ft.OutlinedButton("Classify columns (PII / confidential)", icon=ft.Icons.SHIELD_OUTLINED,
                                                   on_click=lambda _: classify_dialog(self.page, self.app, sid,
                                                                                      on_done=show_info))]
            if mounted(info):
                info.update()

        show_info()
        cols_text = ft.TextField(label="Columns (comma; blank = all)", dense=True,
                                 value=", ".join(cfg.get("columns") or []),
                                 on_blur=lambda e: (cfg.__setitem__("columns", [c.strip() for c in e.control.value.split(",")
                                                                               if c.strip()]), self.changed()))
        return [ft.Dropdown(label="Rows from", value=value, dense=True, on_select=pick, enable_filter=True,
                            options=[ft.DropdownOption(key=k, text=v) for k, v in options.items()]),
                info, cols_text,
                self.tf(cfg, "filter", "Filter (formula, optional)", hint='e.g. [Status] = "Open"'),
                self.tf(cfg, "limit", "Max rows (0 = all)", number=True, width=160)]

    def inspect_input_form(self, n, cfg) -> list[ft.Control]:
        cfg.setdefault("fields", [])
        return [small("The run form asks for these values; API callers send them as \"input\"."),
                self.list_editor(cfg["fields"], [("name", "Field", 150), ("default", "Default", 170)], "Add field",
                                 {"name": "", "default": ""})]

    def inspect_transform(self, n, cfg) -> list[ft.Control]:
        cfg.setdefault("columns", [])
        return [section("New columns", self.list_editor(cfg["columns"], [("name", "Column", 130),
                                                                         ("formula", "Formula", 210)],
                                                        "Add column", {"name": "", "formula": ""}),
                        small('Same formulas as mappings, e.g. UPPER([Region]) or IF([Amount] > 1000, "big", "small")')),
                self.tf(cfg, "filter", "Keep rows where (formula, optional)", hint="e.g. AND([urgency] >= 3, "
                                                                                     "NOT(ISBLANK([note])))")]

    def inspect_sql(self, n, cfg) -> list[ft.Control]:
        return [self.tf(cfg, "query", "Query", multiline=True, redraw=n["id"]),
                small("Read-only DuckDB SELECT over the incoming rows as table rows. Aggregate before calling a "
                      "model: fewer tokens, and raw rows never leave. Example: SELECT region, SUM(amount) AS total "
                      "FROM rows GROUP BY region")]

    def inspect_router(self, n, cfg) -> list[ft.Control]:
        return [self.tf(cfg, "condition", "Condition (formula)", hint="e.g. [urgency] >= 4", redraw=n["id"]),
                small("Rows where the condition is true leave by the green (yes) port, the others by the orange "
                      "(no) port. Functions: AND, OR, NOT, IN, CONTAINS, ISBLANK, IF...")]

    def inspect_output_dataset(self, n, cfg) -> list[ft.Control]:
        return [self.tf(cfg, "name", "Dataset name", redraw=n["id"], hint="Becomes a source you can map and serve"),
                self.cb(cfg, "include_flagged", "Include rows flagged by guardrails (with an ai_review column)"),
                ft.TextField(label="Columns (comma; blank = all)", dense=True,
                             value=", ".join(cfg.get("columns") or []),
                             on_blur=lambda e: (cfg.__setitem__("columns", [c.strip() for c in e.control.value.split(",")
                                                                           if c.strip()]), self.changed())),
                small("Each run writes a new snapshot of this source. Its column classification (PII, confidential) "
                      "is carried over.")]

    def inspect_llm(self, n, cfg) -> list[ft.Control]:
        cfg.setdefault("prompt", {"source": "inline", "system": "", "user": "{{ data }}"})
        cfg.setdefault("output", {"kind": "text", "column": "ai_answer", "fields": []})
        g = cfg.setdefault("guardrails", dict(wf_svc.DEFAULT_GUARDRAILS))
        for k, v in wf_svc.DEFAULT_GUARDRAILS.items():
            g.setdefault(k, copy.deepcopy(v))
        p, out = cfg["prompt"], cfg["output"]
        refs = {o.ref: o for o in self.models}

        model_info = small("")

        def show_network():
            o = refs.get(cfg.get("model"))
            model_info.value = (f"{o.route} · {o.network} network" + (": PII is masked, confidential columns are "
                                                                        "blocked" if o.network == "external" else
                                                                        ": data stays in your network")
                                if o else "Model not available to you")
            if mounted(model_info):
                model_info.update()

        def pick_model(e):
            cfg["model"] = e.control.value
            self.changed(redraw_node=n["id"])
            show_network()

        model = ft.Dropdown(label="Model", value=cfg.get("model"), dense=True, enable_filter=True,
                            on_select=pick_model,
                            options=[ft.DropdownOption(key=o.ref, text=o.label) for o in self.models])
        show_network()

        prompt_box = ft.Column(spacing=8)

        def render_prompt_box():
            if p.get("source") == "library":
                names = [pt.name for pt in prompt_svc.list_prompts() if pt.published_version]
                prompt_box.controls = [
                    ft.Dropdown(label="Published prompt", value=p.get("name"), dense=True,
                                options=[ft.DropdownOption(key=x, text=x) for x in names],
                                on_select=lambda e: (p.__setitem__("name", e.control.value), self.changed())),
                    self.tf(p, "version", "Pin version (blank = latest published)", number=True, width=260),
                    small("System prompts from the library are reviewed and versioned; the run records the "
                          "version and hash.")]
            else:
                prompt_box.controls = [self.tf(p, "system", "System prompt", multiline=True),
                                       self.tf(p, "user", "User prompt", multiline=True)]
            if mounted(prompt_box):
                prompt_box.update()

        render_prompt_box()
        out_box = ft.Column(spacing=8)

        def render_out():
            if out.get("kind") == "json":
                out.setdefault("fields", [])
                out_box.controls = [
                    small("Each field becomes a column. Replies are checked against these types and retried if "
                          "invalid."),
                    self.list_editor(out["fields"], [("name", "Field", 110), ("type", "Type", 110),
                                                     ("enum", "Options (comma)", 150), ("required", "Required", 0),
                                                     ("description", "Hint for the model", 200)],
                                     "Add field", {"name": "", "type": "string", "required": True, "description": "",
                                                   "enum": []},
                                     type_options={"type": {t: t for t in gr.FIELD_TYPES}})]
            else:
                out_box.controls = [self.tf(out, "column", "Answer column", width=220)]
            if mounted(out_box):
                out_box.update()

        render_out()
        batch_size = self.tf(cfg, "batch_size", "Rows per call", number=True, width=140)

        guard = [
            self.dd(g, "pii_mode", "Personal data (PII)", gr.PII_MODES),
            self.dd(g, "injection", "Prompt injection in data", {"flag": "Flag the row for review",
                                                                 "reject": "Reject the row (don't send it)",
                                                                 "fail": "Stop the run", "off": "Don't check"}),
            self.dd(g, "on_invalid", "When the reply fails a check", gr.ON_FAIL),
            ft.Text("Rules on the reply (formulas)", size=12.5, weight=ft.FontWeight.W_500),
            self.list_editor(g["rules"], [("formula", "Must be true", 200), ("message", "Message", 140)], "Add rule",
                             {"formula": "", "message": ""}),
            ft.Text("No invented values", size=12.5, weight=ft.FontWeight.W_500),
            self.list_editor(g["grounding"], [("field", "Reply field", 110), ("source", "Must match", 130),
                                              ("column", "Input column", 120), ("values", "Allowed (list)", 150)],
                             "Add check", {"field": "", "source": "column", "column": "", "values": []},
                             type_options={"source": {"column": "a value of column", "row": "this row's column",
                                                      "list": "a fixed list"}}),
            ft.TextField(label="Banned words or patterns (comma separated)", dense=True,
                         value=", ".join(g.get("banned") or []),
                         on_blur=lambda e: (g.__setitem__("banned", [x.strip() for x in e.control.value.split(",")
                                                                     if x.strip()]), self.changed())),
            ft.Row([self.tf(g, "max_output_chars", "Max reply chars (0 = off)", number=True, width=180),
                    self.tf(g, "max_prompt_tokens", "Max prompt tokens", number=True, width=170)], spacing=8),
            self.cb(g, "pii_leak", "Flag replies containing personal data that wasn't in the input"),
            small("Data is always sent inside <data> tags with a notice that it is not instructions."),
        ]
        return [
            model, model_info,
            section("Prompt", ft.RadioGroup(value=p.get("source", "inline"), content=ft.Row([
                ft.Radio(value="inline", label="Write here"), ft.Radio(value="library", label="From the library")]),
                on_change=lambda e: (p.__setitem__("source", e.control.value), self.changed(), render_prompt_box())),
                prompt_box,
                small("Variables: {{ data }} = the row (or batch table) inside safe delimiters, {{ row.Column }}, "
                      "{{ rows_table }}, {{ input.name }}. If the prompt uses none, {{ data }} is added."),
                icon=ft.Icons.TEXT_SNIPPET_OUTLINED),
            section("Calls", ft.Row([self.dd(cfg, "mode", "Mode", {"per_row": "One call per row",
                                                                  "batch": "One call per batch"}, redraw=n["id"],
                                             width=190), batch_size], spacing=8, wrap=True),
                    ft.TextField(label="Columns sent (comma; blank = all)", dense=True,
                                 value=", ".join(cfg.get("columns") or []),
                                 on_blur=lambda e: (cfg.__setitem__("columns", [c.strip() for c in
                                                                                e.control.value.split(",") if c.strip()]),
                                                    self.changed())),
                    ft.Row([self.tf(cfg, "temperature", "Temperature", number=True, width=130),
                            self.tf(cfg, "max_tokens", "Max reply tokens", number=True, width=140),
                            self.tf(cfg, "concurrency", "Parallel", number=True, width=90),
                            self.tf(cfg, "retries", "Retries", number=True, width=90)], spacing=8, wrap=True),
                    icon=ft.Icons.SPEED),
            section("Output", ft.RadioGroup(value=out.get("kind", "text"), content=ft.Row([
                ft.Radio(value="text", label="Text"), ft.Radio(value="json", label="JSON fields")]),
                on_change=lambda e: (out.__setitem__("kind", e.control.value), self.changed(redraw_node=n["id"]),
                                     render_out())), out_box, icon=ft.Icons.OUTPUT),
            section("Guardrails", *guard, icon=ft.Icons.SHIELD_OUTLINED),
        ]

    # ---------------------------------------------------------------- actions
    def save(self, quiet: bool = False) -> None:
        self.wf = wf_svc.save_spec(self.wf.id, self.spec, self.app.user.username)
        self.dirty = False
        self.render_header()
        if not quiet:
            toast(self.page, "Saved")

    def check(self) -> None:
        self.save(quiet=True)
        problems = wf_svc.validate(self.spec, self.app.user)
        if problems:
            dialog(self.page, "Fix before running", ft.Column([ft.Text("- " + p, size=13) for p in problems],
                                                              tight=True, spacing=6),
                   [ft.FilledButton("OK", on_click=lambda _: close_dialog(self.page))], width=560)
            return
        try:
            est = wf_svc.estimate(self.wf.id, self.app.user)
        except gr.GuardrailError as e:
            dialog(self.page, "Blocked by a guardrail", ft.Text(str(e), size=13),
                   [ft.FilledButton("OK", on_click=lambda _: close_dialog(self.page))], width=560)
            return
        estimate_dialog(self.page, est)

    def ask_input(self, then) -> None:
        forms = [n for n in self.spec["nodes"] if n["type"] == "input_form"]
        fields = [f for n in forms for f in n["config"].get("fields") or [] if f.get("name")]
        if not fields:
            then({})
            return
        boxes = {f["name"]: ft.TextField(label=f["name"], value=str(f.get("default") or ""), dense=True, multiline=True,
                                         min_lines=1, max_lines=6) for f in fields}

        def go(_):
            close_dialog(self.page)
            then({k: v.value for k, v in boxes.items()})

        dialog(self.page, "Run input", ft.Column(list(boxes.values()), spacing=10, tight=True),
               [ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
                ft.FilledButton("Run", on_click=background(self.page, go))], width=560)

    def run(self, test: bool = False) -> None:
        self.save(quiet=True)
        problems = wf_svc.validate(self.spec, self.app.user)
        if problems:
            self.check()
            return

        def execute(run_input: dict, confirmed: bool = False) -> None:
            dialog(self.page, "Running" + (" a test on 3 rows" if test else ""),
                              ft.Row([ft.ProgressRing(width=22, height=22), ft.Text("Calling the model(s)...")],
                                     spacing=12), [], width=360)
            try:
                r = wf_svc.run(self.wf.id, self.app.user, run_input, trigger="test" if test else "manual",
                               use_draft=True, row_limit=3 if test else 0, confirm=confirmed or test)
            except wf_svc.NeedsConfirmation as nc:
                close_dialog(self.page)
                estimate_dialog(self.page, nc.estimate, on_run=lambda: execute(run_input, True))
                return
            except Exception:
                close_dialog(self.page)
                raise
            close_dialog(self.page)
            show_run(self.page, r.id)

        self.ask_input(lambda values: guarded(self.page, execute)(values))

    def publish(self) -> None:
        self.save(quiet=True)
        self.wf = wf_svc.publish(self.wf.id, self.app.user)
        self.render_header()
        toast(self.page, f"Published v{self.wf.published_version}. API: POST /api/v1/workflows/{self.wf.slug}/run")

    def settings(self) -> None:
        lim = self.spec.setdefault("limits", dict(wf_svc.DEFAULT_LIMITS))
        name = ft.TextField(label="Name", value=self.wf.name, dense=True)
        desc = ft.TextField(label="Description", value=self.wf.description, dense=True)
        fields = {k: ft.TextField(label=label, value=str(lim.get(k, wf_svc.DEFAULT_LIMITS[k])), dense=True, width=260,
                                  keyboard_type=ft.KeyboardType.NUMBER)
                  for k, label in (("max_rows", "Max input rows per run"),
                                   ("max_tokens_per_run", "Max tokens per run"),
                                   ("confirm_over_tokens", "Ask to confirm above (tokens)"),
                                   ("monthly_tokens", "Monthly token budget (0 = none)"))}

        def save(_):
            for k, f in fields.items():
                lim[k] = int(f.value or 0)
            self.wf = wf_svc.save_spec(self.wf.id, self.spec, self.app.user.username, name=name.value,
                                       description=desc.value)
            self.dirty = False
            close_dialog(self.page)
            self.render_header()

        dialog(self.page, "Workflow settings", ft.Column([
            name, desc, ft.Text("Limits", size=14, weight=ft.FontWeight.W_600),
            ft.Row(list(fields.values())[:2], spacing=10), ft.Row(list(fields.values())[2:], spacing=10),
            small("Runs over the row limit are blocked. Runs whose estimate exceeds the confirmation threshold ask "
                  "first (API callers must send \"confirm\": true). The monthly budget blocks runs once used up."),
            small(f"API: POST /api/v1/workflows/{self.wf.slug}/run with an API key allowed for \"*\" or "
                  f"\"workflow:{self.wf.slug}\"."),
        ], spacing=12, tight=True), [ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
                                     ft.FilledButton("Save", on_click=guarded(self.page, save))], width=600)

    def leave(self) -> None:
        if not self.dirty:
            self.app.navigate("ai", tab="workflows")
            return

        def save_leave(_):
            close_dialog(self.page)
            self.save(quiet=True)
            self.app.navigate("ai", tab="workflows")

        def discard(_):
            close_dialog(self.page)
            self.app.navigate("ai", tab="workflows")

        dialog(self.page, "Unsaved changes", ft.Text("Save your changes before leaving?"),
               [ft.TextButton("Discard", on_click=discard), ft.FilledButton("Save", on_click=guarded(self.page, save_leave))])


# ================================================================== dialogs shared by the list and the editor


def estimate_dialog(page: ft.Page, est: dict, on_run=None) -> None:
    rows = [ft.DataRow(cells=[ft.DataCell(ft.Text(name, size=12)), ft.DataCell(ft.Text(str(v["calls"]), size=12)),
                              ft.DataCell(ft.Text(f"{v['prompt_tokens']:,}", size=12)),
                              ft.DataCell(ft.Text(f"{v['max_output_tokens']:,}", size=12)),
                              ft.DataCell(chip(v["network"], ft.Colors.TEAL_600 if v["network"] == "internal"
                                               else ft.Colors.AMBER_700))])
            for name, v in est.get("nodes", {}).items()]
    egress = []
    for name, e in est.get("egress", {}).items():
        egress.append(ft.Text(f"{name} ({e['network']}): sends {', '.join(e['send']) or 'nothing'}"
                              + (f"; masks {', '.join(e['mask'])}" if e["mask"] else ""), size=12))
    events = est.get("guardrail_events") or {}
    content = ft.Column([
        ft.Row([chip(f"{est['calls']} model calls"), chip(f"~{est['tokens_expected']:,} tokens expected"),
                chip(f"max {est['tokens_max']:,}")], spacing=8, wrap=True),
        ft.DataTable(columns=[ft.DataColumn(ft.Text(h, size=12, weight=ft.FontWeight.W_600)) for h in
                              ("LLM node", "Calls", "Prompt tokens", "Max reply tokens", "Model network")],
                     rows=rows) if rows else small("No LLM nodes."),
        ft.Text("What is sent", size=13, weight=ft.FontWeight.W_600), *egress,
        ft.Text("Guardrails found (before any call)", size=13, weight=ft.FontWeight.W_600),
        ft.Row([chip(f"{k}: {v}", ft.Colors.AMBER_700) for k, v in events.items()] or [small("Nothing flagged.")],
               wrap=True, spacing=6),
        *([small(f"{est['rows_rejected_before_calls']} row(s) would be rejected before calling the model.")]
          if est.get("rows_rejected_before_calls") else []),
        *[ft.Text("Warning: " + w, size=12.5, color=ft.Colors.AMBER_800) for w in est.get("warnings", [])],
        small("Rows reaching each output depend on the model's answers; the run shows them."
              if est.get("nodes") else
              "Output rows: " + ", ".join(f"{k}: {v}" for k, v in (est.get("outputs") or {}).items())),
    ], spacing=10, tight=True)
    actions = [ft.TextButton("Close", on_click=lambda _: close_dialog(page))]
    if on_run:
        def go(_):
            close_dialog(page)
            on_run()
        actions.append(ft.FilledButton(f"Run (~{est['tokens_expected']:,} tokens)", on_click=background(page, go)))
    dialog(page, "Estimate" if not on_run else "Confirm run", content, actions, width=720)


def show_run(page: ft.Page, run_id: int) -> None:
    run, nodes = wf_svc.get_run(run_id)
    out, review = wf_svc.run_frames(run)
    node_rows = [ft.DataRow(cells=[
        ft.DataCell(ft.Text(nr.label, size=12)), ft.DataCell(chip(nr.status, STATUS_COLOR.get(nr.status, ft.Colors.OUTLINE))),
        ft.DataCell(ft.Text(f"{nr.rows_in} in, {nr.rows_out} out", size=12)),
        ft.DataCell(ft.Text(f"{nr.flagged} / {nr.rejected}", size=12)),
        ft.DataCell(ft.Text(f"{nr.calls} / {nr.tokens:,}", size=12)),
        ft.DataCell(ft.Text(f"{nr.ms / 1000:.1f} s", size=12)),
        ft.DataCell(ft.Text(", ".join(f"{k} {v}" for k, v in (nr.events or {}).items()) or nr.error, size=11.5,
                            width=260, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                            tooltip=nr.error or None)),
    ]) for nr in nodes]
    samples = []
    for nr in nodes:
        for i, smp in enumerate(nr.samples or []):
            samples.append(ft.ExpansionTile(
                title=ft.Text(f"{nr.label}: call {i + 1} (rows {', '.join(str(r + 1) for r in smp.get('rows', []))})", size=12.5),
                subtitle=small("; ".join(smp.get("problems") or []) or "passed all checks"),
                controls=[ft.Container(ft.Column([
                    ft.Text("System", size=12, weight=ft.FontWeight.W_600), ft.Text(smp["system"], size=11.5, selectable=True),
                    ft.Text("User (as sent: masked, delimited)", size=12, weight=ft.FontWeight.W_600),
                    ft.Text(smp["user"], size=11.5, selectable=True),
                    ft.Text("Reply", size=12, weight=ft.FontWeight.W_600), ft.Text(smp["reply"], size=11.5, selectable=True),
                ], spacing=4), padding=10)]))
    events = run.guardrail_events or {}
    content = ft.Column([
        ft.Row([chip(run.status, STATUS_COLOR.get(run.status, ft.Colors.OUTLINE)),
                chip(f"{run.trigger}" + (" · draft" if not run.version else f" · v{run.version}")),
                chip(f"rows in {run.rows_in}, out {run.rows_out}"), chip(f"flagged {run.rows_flagged}", ft.Colors.AMBER_700),
                chip(f"rejected {run.rows_rejected}", ft.Colors.RED_400), chip(f"{run.calls} calls · {run.tokens:,} tokens")],
               wrap=True, spacing=6),
        *([ft.Container(ft.Text(run.error, size=12.5, color=ft.Colors.RED_600, selectable=True), padding=8,
                        bgcolor=ft.Colors.with_opacity(0.08, ft.Colors.RED), border_radius=ft.BorderRadius.all(8))]
          if run.error else []),
        ft.Row([ft.Text("Guardrails:", size=12.5, weight=ft.FontWeight.W_600),
                *([chip(f"{k}: {v}", ft.Colors.AMBER_700) for k, v in events.items()] or [small("nothing triggered")])],
               wrap=True, spacing=6),
        ft.Row([ft.DataTable(columns=[ft.DataColumn(ft.Text(h, size=12, weight=ft.FontWeight.W_600)) for h in
                                      ("Node", "Status", "Rows", "Flagged / rejected", "Calls / tokens", "Time", "Notes")],
                             rows=node_rows, data_row_min_height=34, data_row_max_height=44)], scroll=ft.ScrollMode.AUTO),
        *([ft.Text("Review: flagged and rejected rows", size=14, weight=ft.FontWeight.W_600),
           ft.Row([df_table(review, max_rows=50)], scroll=ft.ScrollMode.AUTO)] if review is not None else []),
        *([ft.Text(f"Output ({out.height} rows)", size=14, weight=ft.FontWeight.W_600),
           ft.Row([df_table(out, max_rows=30)], scroll=ft.ScrollMode.AUTO)] if out is not None else []),
        *([ft.Text("Prompts and replies (test runs)", size=14, weight=ft.FontWeight.W_600), *samples] if samples else []),
    ], spacing=10, scroll=ft.ScrollMode.AUTO)
    dialog(page, f"Run {run.id}", content, [ft.FilledButton("Close", on_click=lambda _: close_dialog(page))],
           width=1100, height=640)


def classify_dialog(page: ft.Page, app, source_id: int, on_done=None) -> None:
    """Set a source's column classification; "Suggest" scans names and sample values for personal data."""
    src = src_svc.get_source(source_id)
    current = dict(src.classification or {})
    cols = [f["name"] for f in src.fields or []]
    drops = {c: ft.Dropdown(value=current.get(c, "internal"), dense=True, width=210,
                            options=[ft.DropdownOption(key=k, text=v) for k, v in gr.LEVEL_LABELS.items()]) for c in cols}

    def suggest(_):
        try:
            df = src_svc.load_snapshot(source_id, limit=200)
        except LookupError:
            toast(page, "No data yet", error=True)
            return
        found = gr.suggest_classification(df)
        for c, lvl in found.items():
            if c in drops and drops[c].value in ("internal", "public"):
                drops[c].value = lvl
                drops[c].update()
        toast(page, f"Suggested PII for {len(found)} column(s): " + ", ".join(found) if found else
              "No personal data patterns found")

    def save(_):
        src_svc.set_classification(source_id, {c: d.value for c, d in drops.items()}, app.user.username)
        close_dialog(page)
        toast(page, "Classification saved")
        if on_done:
            on_done()

    rows = [ft.Row([ft.Text(c, size=13, expand=True), d]) for c, d in drops.items()]
    dialog(page, f"Classify columns: {src.name}", ft.Column([
        small("Used by AI guardrails. External models: PII is masked, confidential columns are blocked. Internal "
              "models receive everything. Unclassified columns count as internal."),
        ft.OutlinedButton("Suggest from data", icon=ft.Icons.AUTO_FIX_HIGH, on_click=background(page, suggest)),
        *rows], spacing=8, tight=True, scroll=ft.ScrollMode.AUTO),
        [ft.TextButton("Cancel", on_click=lambda _: close_dialog(page)),
         ft.FilledButton("Save", on_click=guarded(page, save))], width=560, height=min(560, 170 + 48 * len(rows)))


__all__ = ["WorkflowsTab", "WorkflowEditor", "show_run", "classify_dialog", "json"]
