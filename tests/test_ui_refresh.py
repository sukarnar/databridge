"""Event handlers that change what's on screen must redraw it.

In Flet, changing a control's controls/content/value/visible after it is displayed does nothing until .update() is
called (or the page is rebuilt). Missing it shows up as "the dialog only refreshes when I switch windows". This test
flags any UI function that changes such properties without calling update(), navigating, or opening a dialog.
Builders that run before anything is displayed are listed in ALLOWED after review.
"""

import ast
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "databridge" / "ui"
ATTRS = {"controls", "content", "value", "visible", "options", "disabled", "label", "helper", "color", "bgcolor",
         "selected_index", "title", "error_text"}
REDRAWS = {"update", "navigate", "dialog", "open", "show_dialog", "toast", "render_all", "redraw", "render",
           "render_board", "render_inspector", "refresh"}
# Reviewed: builders called before display, or whose caller redraws (e.g. studio render_* via render_all).
ALLOWED = {
    "app.py:main", "explorer.py:show_placeholder", "studio.py:render_header", "studio.py:render_lists",
    "studio.py:render_inspector", "studio.py:render_preview", "targets.py:field_row", "workflows.py:section",
    "endpoints.py:param_row",
}


def _own_nodes(fn):
    stack, out = list(fn.body), []
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        out.append(n)
        stack.extend(ast.iter_child_nodes(n))
    return out


def test_handlers_redraw_what_they_change():
    problems = []
    for path in sorted(UI.rglob("*.py")):
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name in ("build", "__init__") or fn.name.startswith(("build", "_build", "make", "view", "preview")):
                continue
            nodes = _own_nodes(fn)
            changes = any(isinstance(t, ast.Attribute) and t.attr in ATTRS
                          for n in nodes if isinstance(n, (ast.Assign, ast.AugAssign))
                          for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))
            changes |= any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                           and n.func.attr in ("append", "extend", "insert", "clear", "remove", "pop")
                           and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "controls"
                           for n in nodes)
            if not changes:
                continue
            calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                     for n in nodes if isinstance(n, ast.Call)}
            if calls & REDRAWS or f"{path.name}:{fn.name}" in ALLOWED:
                continue
            problems.append(f"{path.name}:{fn.lineno} {fn.name}")
    assert not problems, "Handlers that change the UI without update():\n" + "\n".join(problems)
