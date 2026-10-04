"""Uploaded company LLM configuration files ("config sources").

An admin uploads the company's file (Continue config.yaml, or DataBridge's own format). DataBridge keeps it as a
custom source (real keys redacted), maps it to the standard configuration, and creates or updates the endpoints.
Uploading a new version updates the same endpoints in place; models removed from the file are disabled.
"""

from typing import Any
from urllib.parse import urlparse

import yaml
from sqlalchemy import select

from databridge.ai import tls
from databridge.core.db import session_scope
from databridge.core.models import AiConfigSource, ModelEndpoint
from databridge.services import ai_config, llm
from databridge.services import continue_config as cc
from databridge.services.users import audit

DEFAULT_OPTIONS = {"key_mode": "per_user", "network": "internal", "allowed_roles": ["admin", "designer"],
                   "set_defaults": True}


class SourceError(ValueError):
    pass


def _dump(data: dict[str, Any]) -> str:
    class NoAliases(yaml.SafeDumper):
        def ignore_aliases(self, data):
            return True

    class _Literal(str):
        pass

    NoAliases.add_representer(_Literal, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|"))
    for e in data.get("endpoints", []):
        if e.get("tls", {}).get("ca_pem"):
            e["tls"]["ca_pem"] = _Literal(e["tls"]["ca_pem"])
    return yaml.dump(data, Dumper=NoAliases, sort_keys=False, allow_unicode=True, width=120)


def list_sources() -> list[AiConfigSource]:
    with session_scope() as s:
        return list(s.scalars(select(AiConfigSource).order_by(AiConfigSource.name)))


def get_source(source_id: int) -> AiConfigSource:
    with session_scope() as s:
        src = s.get(AiConfigSource, source_id)
        if not src:
            raise SourceError("Config source not found")
        return src


def _ca_pems(ca_files: dict[str, bytes] | None, previous: dict[str, str] | None) -> tuple[dict[str, str], list[str]]:
    """Validates uploaded CA files, keyed by file name; keeps earlier uploads."""
    pems = dict(previous or {})
    notes = []
    for name, data in (ca_files or {}).items():
        try:
            pem = tls.normalize_ca(data)
        except tls.TLSConfigError as e:
            raise SourceError(f"{name}: {e}") from e
        info = tls.describe(pem)[0]
        pems[cc.ca_key(name)] = pem
        notes.append(f"CA {name}: {info.subject}, expires {info.not_after}")
    return pems, notes


def _existing(source_id: int | None) -> dict[str, ModelEndpoint]:
    """Endpoints created by this source, by base URL."""
    if not source_id:
        return {}
    with session_scope() as s:
        return {ep.base_url: ep for ep in s.scalars(select(ModelEndpoint).where(ModelEndpoint.source_id == source_id))}


def match_source(text: str) -> int | None:
    """An existing source for the same company config (same format and name): uploading it again = new version."""
    try:
        data, _ = cc.load(text)
    except cc.ContinueConfigError:
        return None
    if cc.detect_format(data) != "continue" or not data.get("name"):
        return None
    name = str(data["name"]).strip()
    return next((src.id for src in list_sources() if src.format == "continue" and src.name == name), None)


def plan(text: str, file_name: str = "", options: dict[str, Any] | None = None,
         ca_files: dict[str, bytes] | None = None, source_id: int | None = None) -> dict[str, Any]:
    """Parses and maps a file without changing anything. The preview shows all of this."""
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    source_id = source_id or match_source(text)
    previous = get_source(source_id) if source_id else None
    try:
        data, repairs = cc.load(text)
    except cc.ContinueConfigError as e:
        raise SourceError(str(e)) from e
    fmt = cc.detect_format(data)
    pems, ca_notes = _ca_pems(ca_files, previous.ca_pems if previous else None)
    if fmt == "databridge":
        try:
            report = ai_config.apply(text, "preview", dry_run=True)
        except ai_config.ConfigError as e:
            raise SourceError(str(e)) from e
        return {"format": "databridge", "name": (previous.name if previous else file_name or "DataBridge config"),
                "version": str(data.get("version") or ""), "repairs": repairs, "warnings": [], "rows": [],
                "ca_needed": [], "ca_notes": ca_notes, "standard": text, "report": report, "secrets": {},
                "redacted": text, "options": opts, "pems": pems}
    if fmt != "continue":
        raise SourceError("Unknown format: expected a Continue config.yaml (models with provider/apiBase) or a "
                          "DataBridge configuration (endpoints)")
    try:
        mapping = cc.map_continue(data, file_name)
    except cc.ContinueConfigError as e:
        raise SourceError(str(e)) from e
    existing = _existing(source_id)
    names = {base: ep.name for base, ep in existing.items()}
    taken = {ep.name: ep for ep in llm.list_endpoints()}
    for e in mapping.endpoints:  # a same-named endpoint that another source or an admin created: don't hijack it
        other = taken.get(names.get(e.base_url, e.name))
        if other is not None and (source_id is None or other.source_id != source_id):
            names[e.base_url] = f"{e.name} ({urlparse(e.base_url).hostname})"
    standard = cc.to_standard(mapping, key_mode=opts["key_mode"], network=opts["network"],
                              allowed_roles=list(opts["allowed_roles"]), ca_pems=pems, existing_names=names)
    for entry in standard["endpoints"]:  # users' keys work across one gateway host
        u = urlparse(entry["base_url"])
        entry["key_group"] = f"{mapping.name}|{u.hostname}|{entry.get('auth_header', '')}"
    secrets = {}
    if opts["key_mode"] == "shared":
        for entry, e in zip(standard["endpoints"], mapping.endpoints):
            if e.key_in_file:
                secrets[entry["name"]] = e.key_in_file
    ca_needed = [{"path": p, "file": cc.ca_key(p), "uploaded": cc.ca_key(p) in pems} for p in mapping.ca_paths]
    warnings = list(mapping.warnings)
    for c in ca_needed:
        if not c["uploaded"]:
            warnings.append(f"Upload {c['file']} (caBundlePath {c['path']}): until then these endpoints use the "
                            "server's trusted CAs and will probably fail certificate checks")
    if any(e.key_in_file for e in mapping.endpoints) and opts["key_mode"] != "shared":
        warnings.append("The file contains a real key. It is not stored (users add their own); choose 'Use the key "
                        "in the file as the company key' to use it for everyone")
    return {"format": "continue", "name": previous.name if previous else mapping.name, "version": mapping.version,
            "source_id": source_id, "revision": (previous.revision + 1) if previous else 1,
            "repairs": repairs, "warnings": warnings, "rows": mapping.rows(), "ca_needed": ca_needed,
            "ca_notes": ca_notes, "standard": _dump(standard), "secrets": secrets,
            "redacted": cc.redact(text, cc.secret_values(mapping)), "options": opts, "pems": pems,
            "report": [f"{'update' if e['base_url'] in existing else 'create'} {e['name']} ({e['base_url']}, "
                       f"{len(e['models'])} model(s), key {e['key_mode']}, TLS {e['tls']['verify']})"
                       for e in standard["endpoints"]]}


def save(text: str, actor: str, file_name: str = "", options: dict[str, Any] | None = None,
         ca_files: dict[str, bytes] | None = None, source_id: int | None = None) -> AiConfigSource:
    """Applies a file: keeps it as a source, creates/updates its endpoints, disables ones it no longer lists."""
    source_id = source_id or match_source(text)
    p = plan(text, file_name, options, ca_files, source_id)
    before = _existing(source_id)
    try:
        ai_config.apply(p["standard"], actor)
    except ai_config.ConfigError as e:
        raise SourceError(str(e)) from e
    standard = yaml.safe_load(p["standard"])
    endpoints = {ep.name: ep for ep in llm.list_endpoints()}
    ids = []
    for entry in standard.get("endpoints", []):
        ep = endpoints[entry["name"]]
        ids.append(ep.id)
        with session_scope() as s:
            row = s.get(ModelEndpoint, ep.id)
            row.enabled = True if p["format"] == "continue" else row.enabled
        if p["secrets"].get(entry["name"]):
            llm.set_endpoint_key(ep.id, p["secrets"][entry["name"]], actor)
    with session_scope() as s:
        src = s.get(AiConfigSource, source_id) if source_id else None
        if not src:
            src = AiConfigSource(name=p["name"], format=p["format"], original="")
            s.add(src)
        else:
            src.revision += 1
        src.file_name, src.original, src.version = file_name or src.file_name, p["redacted"], p["version"]
        src.options, src.ca_pems, src.standard = p["options"], p["pems"], p["standard"]
        src.mapping, src.notes = p["rows"], p["repairs"] + p["warnings"] + p["ca_notes"]
        src.endpoint_ids, src.uploaded_by = ids, actor
        s.flush()
        sid = src.id
        for eid in ids:
            s.get(ModelEndpoint, eid).source_id = sid
        removed = [ep for base, ep in before.items() if ep.id not in ids]
        for ep in removed:
            row = s.get(ModelEndpoint, ep.id)
            row.enabled = False
    report = p["report"] + [f"disable {ep.name} (no longer in the file)" for ep in removed]
    if p["options"].get("set_defaults") and not llm.get_defaults():
        first = next((f"endpoint:{endpoints[e['name']].id}/{m['id']}" for e in standard["endpoints"]
                      for m in e["models"] if m.get("enabled", True)), None)
        if first:
            llm.set_defaults({t: first for t in llm.TASKS}, actor)
            report.append(f"default model for all tasks: {first}")
    audit(actor, "ai.config_source.save", p["name"], "; ".join(report)[:1000])
    return get_source(sid)


def reapply(source_id: int, actor: str, options: dict[str, Any] | None = None,
            ca_files: dict[str, bytes] | None = None) -> AiConfigSource:
    """Re-maps the kept file, e.g. after uploading the CA file or changing options. Redacted keys stay redacted:
    a company key from the file is kept as already stored."""
    src = get_source(source_id)
    opts = {**src.options, **(options or {})}
    return save(src.original, actor, src.file_name, opts, ca_files, source_id)


def delete(source_id: int, actor: str, delete_endpoints: bool = False) -> None:
    src = get_source(source_id)
    for eid in src.endpoint_ids:
        with session_scope() as s:
            ep = s.get(ModelEndpoint, eid)
            if not ep:
                continue
            if not delete_endpoints:  # keep them, now as ordinary endpoints
                ep.source_id, ep.managed = None, False
        if delete_endpoints:
            llm.delete_endpoint(eid, actor)
    with session_scope() as s:
        s.delete(s.get(AiConfigSource, source_id))
    audit(actor, "ai.config_source.delete", src.name, "with endpoints" if delete_endpoints else "endpoints kept")
