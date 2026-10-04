"""Every ft.<Control>(...) call in the studio uses keyword arguments that Flet actually accepts.

A wrong keyword (e.g. helper_text on a TextField) only fails when that screen opens, so check them statically.
"""

import ast
import inspect
from pathlib import Path

import flet as ft
import flet.canvas as cv

UI = Path(__file__).resolve().parents[1] / "databridge" / "ui"


def _calls():
    for path in sorted(UI.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            base = node.func.value
            if isinstance(base, ast.Name) and base.id in ("ft", "cv"):
                module = ft if base.id == "ft" else cv
                yield path.name, node.lineno, getattr(module, node.func.attr, None), node


def test_flet_keyword_arguments_exist():
    problems = []
    for fname, line, cls, node in _calls():
        if cls is None:
            problems.append(f"{fname}:{line} {ast.unparse(node.func)} does not exist in this Flet version")
            continue
        if not inspect.isclass(cls):
            continue
        try:
            params = inspect.signature(cls.__init__).parameters
        except (TypeError, ValueError):
            continue
        if any(p.kind == p.VAR_KEYWORD for p in params.values()):
            continue
        for kw in node.keywords:
            if kw.arg and kw.arg not in params:
                problems.append(f"{fname}:{line} {cls.__name__}({kw.arg}=...)")
    assert not problems, "Unknown Flet keyword arguments:\n" + "\n".join(problems)
