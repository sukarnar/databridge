"""Prompt templates: Jinja2 in a sandbox with strict undefined variables.

    Summarise the complaint from {{ customer }}:
    <data>{{ text }}</data>

Templates cannot touch Python internals (sandbox), and a missing variable is an error rather than
silently rendering empty text.
"""

import re
from typing import Any

from jinja2 import StrictUndefined, TemplateError, meta
from jinja2.sandbox import SandboxedEnvironment

_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True,
                            trim_blocks=True, lstrip_blocks=True)


class PromptTemplateError(ValueError):
    pass


def variables_in(*texts: str) -> list[str]:
    """Top-level variable names used by the templates, in first-seen order."""
    names: list[str] = []
    for text in texts:
        if not text:
            continue
        try:
            found = meta.find_undeclared_variables(_env.parse(text))
        except TemplateError as e:
            raise PromptTemplateError(f"Template syntax error: {e}") from e
        # preserve the order in which the placeholders first appear
        def pos(n: str) -> int:
            m = re.search(r"\{[{%][^}]*\b" + re.escape(n) + r"\b", text)
            return m.start() if m else len(text)

        for name in sorted(found, key=pos):
            if name not in names:
                names.append(name)
    return names


def render(text: str, values: dict[str, Any]) -> str:
    if not text:
        return ""
    try:
        return _env.from_string(text).render(**values)
    except TemplateError as e:
        raise PromptTemplateError(str(e).replace("'", "")) from e
