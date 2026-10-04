"""Prompt library: versioned templates (system + user prompt + declared variables)."""

import hashlib
import json
from typing import Any

from sqlalchemy import select

from databridge.ai.templating import PromptTemplateError, render, variables_in
from databridge.core.db import session_scope
from databridge.core.models import PromptTemplate, PromptVersion
from databridge.services.users import audit

FIELDS = ("name", "description", "system", "user", "variables", "model_ref", "temperature", "max_tokens", "json_mode")


class PromptError(Exception):
    pass


def list_prompts() -> list[PromptTemplate]:
    with session_scope() as s:
        return list(s.scalars(select(PromptTemplate).order_by(PromptTemplate.name)))


def get_prompt(prompt_id: int) -> PromptTemplate:
    with session_scope() as s:
        p = s.get(PromptTemplate, prompt_id)
        if not p:
            raise PromptError("Prompt not found")
        return p


def _merge_variables(system: str, user: str, declared: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keeps defaults/descriptions for variables still used; adds newly used ones; drops unused."""
    by_name = {v["name"]: v for v in declared or [] if v.get("name")}
    return [{"name": n, "default": by_name.get(n, {}).get("default", ""),
             "description": by_name.get(n, {}).get("description", "")} for n in variables_in(system, user)]


def save_prompt(values: dict[str, Any], actor: str, prompt_id: int | None = None) -> PromptTemplate:
    name = (values.get("name") or "").strip()
    if not name:
        raise PromptError("Give the prompt a name")
    if not (values.get("user") or "").strip():
        raise PromptError("The user prompt cannot be empty")
    try:
        variables = _merge_variables(values.get("system", ""), values.get("user", ""), values.get("variables") or [])
    except PromptTemplateError as e:
        raise PromptError(str(e)) from e
    with session_scope() as s:
        dup = s.scalars(select(PromptTemplate).where(PromptTemplate.name == name)).first()
        if dup and dup.id != prompt_id:
            raise PromptError(f"A prompt named {name!r} already exists")
        p = s.get(PromptTemplate, prompt_id) if prompt_id else PromptTemplate(owner=actor)
        p.name, p.description = name, values.get("description", "")
        p.system, p.user, p.variables = values.get("system", ""), values["user"], variables
        p.model_ref = values.get("model_ref") or ""
        p.temperature = values.get("temperature")
        p.max_tokens = int(values.get("max_tokens") or 1024)
        p.json_mode = bool(values.get("json_mode"))
        p.has_draft_changes = True
        p.updated_by = actor
        s.add(p)
        s.flush()
    audit(actor, "ai.prompt.save", name)
    return p


def _snapshot(p: PromptTemplate) -> dict[str, Any]:
    return {f: getattr(p, f) for f in FIELDS}


def publish(prompt_id: int, actor: str, note: str = "") -> PromptVersion:
    with session_scope() as s:
        p = s.get(PromptTemplate, prompt_id)
        snap = _snapshot(p)
        digest = hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()
        p.published_version += 1
        p.has_draft_changes = False
        v = PromptVersion(template_id=p.id, version=p.published_version, snapshot=snap, content_hash=digest,
                          note=note, created_by=actor)
        s.add(v)
        s.flush()
        name, version = p.name, v.version
    audit(actor, "ai.prompt.publish", name, f"v{version}")
    return v


def versions(prompt_id: int) -> list[PromptVersion]:
    with session_scope() as s:
        return list(s.scalars(select(PromptVersion).where(PromptVersion.template_id == prompt_id)
                              .order_by(PromptVersion.version.desc())))


def published(name: str, version: int | None = None) -> dict[str, Any]:
    """Published snapshot by name (latest, or a pinned version): used by workflows and the API later."""
    with session_scope() as s:
        p = s.scalars(select(PromptTemplate).where(PromptTemplate.name == name)).first()
        if not p or not p.published_version:
            raise PromptError(f"No published prompt named {name!r}")
        v = s.scalars(select(PromptVersion).where(PromptVersion.template_id == p.id,
                                                  PromptVersion.version == (version or p.published_version))).first()
        if not v:
            raise PromptError(f"{name} has no version {version}")
        return {**v.snapshot, "version": v.version, "content_hash": v.content_hash}


def restore(prompt_id: int, version: int, actor: str) -> None:
    """Copies an old published version back into the draft."""
    with session_scope() as s:
        v = s.scalars(select(PromptVersion).where(PromptVersion.template_id == prompt_id,
                                                  PromptVersion.version == version)).first()
        p = s.get(PromptTemplate, prompt_id)
        for f in FIELDS:
            if f != "name":
                setattr(p, f, v.snapshot.get(f))
        p.has_draft_changes = True
        name = p.name
    audit(actor, "ai.prompt.restore", name, f"from v{version}")


def delete_prompt(prompt_id: int, actor: str) -> None:
    with session_scope() as s:
        p = s.get(PromptTemplate, prompt_id)
        if p:
            name = p.name
            for v in s.scalars(select(PromptVersion).where(PromptVersion.template_id == p.id)):
                s.delete(v)
            s.delete(p)
    audit(actor, "ai.prompt.delete", name)


def render_prompt(system: str, user: str, values: dict[str, Any]) -> tuple[str, str]:
    try:
        return render(system, values), render(user, values)
    except PromptTemplateError as e:
        raise PromptError(f"Prompt template: {e}") from e
