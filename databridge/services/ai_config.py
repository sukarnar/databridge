"""Company LLM configuration as a YAML file: endpoints, TLS, key mode, model catalog and default models.

Apply it at startup with DATABRIDGE_AI_CONFIG_FILE=/path/llm.yaml (endpoints become "managed"), or import it in
AI > Models. Secrets never live in the file: keys come from environment variables (api_key_env) and TLS material
from files on the server (ca_file, client_cert_file, client_key_file, client_p12_file).

    version: 1
    endpoints:
      - name: Company LLM gateway
        base_url: https://llm.corp.local/v1
        api_style: openai             # openai | anthropic
        network: internal             # internal | external
        key_mode: per_user            # shared | per_user | none
        api_key_env: LLM_DISCOVERY_KEY   # shared key, or discovery key for per_user (optional)
        allowed_roles: [admin, designer]
        tls:
          verify: custom              # system | custom | off
          ca_file: /certs/corp-root-ca.pem
          client_cert_file: /certs/databridge.crt
          client_key_file: /certs/databridge.key
          client_key_password_env: DATABRIDGE_CLIENT_KEY_PASSWORD
        approval: approved            # open | approved
        models:
          - id: llama3.1:70b
            label: Llama 3.1 70B (general)
            description: Default for classification and summaries
            roles: [admin, designer]
    defaults:
      playground: Company LLM gateway / llama3.1:70b
      workflow: Company LLM gateway / llama3.1:70b
"""

import os
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from databridge.core.auth import ROLES
from databridge.core.db import session_scope
from databridge.core.models import ModelEndpoint
from databridge.services import llm
from databridge.services.users import audit

ENDPOINT_KEYS = {"name", "base_url", "api_style", "network", "key_mode", "api_key_env", "auth_header", "auth_scheme",
                 "allowed_roles", "enabled", "timeout_s", "tls", "approval", "models", "extra_headers", "key_group",
                 "keep_key"}
TLS_KEYS = {"verify", "check_hostname", "ca_file", "ca_pem", "client_cert_file", "client_key_file",
            "client_key_password_env", "client_p12_file"}
MODEL_KEYS = {"id", "label", "description", "roles", "enabled"}


class ConfigError(ValueError):
    pass


def _read_file(path: str, what: str) -> bytes:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(f"{what}: file not found: {path}")
    return p.read_bytes()


def _env(name: str, what: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ConfigError(f"{what}: environment variable {name} is not set")
    return value


def parse(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("The file must be a mapping with 'endpoints' (and optionally 'defaults')")
    unknown = set(data) - {"version", "endpoints", "defaults", "prune"}
    if unknown:
        raise ConfigError(f"Unknown top-level keys: {', '.join(sorted(unknown))}")
    if not isinstance(data.get("endpoints") or [], list):
        raise ConfigError("'endpoints' must be a list")
    return data


def _endpoint_values(e: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    """Validates one endpoint entry and turns it into save_endpoint values (+ catalog)."""
    name = (e.get("name") or "").strip() if isinstance(e, dict) else ""
    if not name:
        raise ConfigError("Every endpoint needs a name")
    where = f"endpoint {name!r}"
    unknown = set(e) - ENDPOINT_KEYS
    if "api_key" in e or "key" in e:
        raise ConfigError(f"{where}: don't put keys in the file; use api_key_env: <ENV VAR NAME>")
    if unknown:
        raise ConfigError(f"{where}: unknown keys {', '.join(sorted(unknown))}")
    roles = e.get("allowed_roles") or ["admin", "designer"]
    if not set(roles) <= set(ROLES):
        raise ConfigError(f"{where}: unknown roles {', '.join(sorted(set(roles) - set(ROLES)))}")
    values: dict[str, Any] = {
        "name": name, "base_url": e.get("base_url") or "", "api_style": e.get("api_style") or "openai",
        "network": e.get("network") or "internal", "key_mode": e.get("key_mode") or "shared",
        "auth_header": e.get("auth_header") or "Authorization", "auth_scheme": e.get("auth_scheme", "Bearer"),
        "allowed_roles": roles, "enabled": bool(e.get("enabled", True)),
        "timeout_s": e.get("timeout_s"), "approval": e.get("approval") or "open", "managed": True,
        "extra_headers": e.get("extra_headers") or {}, "key_group": e.get("key_group") or None,
    }
    if not isinstance(values["extra_headers"], dict):
        raise ConfigError(f"{where}: extra_headers must be a mapping of header: value")
    lowered = {h.lower() for h in values["extra_headers"]}
    if lowered & {"authorization", "apikey", "api-key", "x-api-key"}:
        raise ConfigError(f"{where}: put the key header in auth_header and the key in api_key_env, not extra_headers")
    if values["network"] not in ("internal", "external"):
        raise ConfigError(f"{where}: network must be internal or external")
    if values["key_mode"] not in llm.KEY_MODES:
        raise ConfigError(f"{where}: key_mode must be one of {', '.join(llm.KEY_MODES)}")
    if values["approval"] not in llm.APPROVAL:
        raise ConfigError(f"{where}: approval must be open or approved")
    if e.get("api_key_env"):
        values["api_key"] = _env(e["api_key_env"], where)
    elif not e.get("keep_key"):  # keep_key: true = leave the key already stored for this endpoint
        values["clear_key"] = True
    t = e.get("tls") or {}
    if not isinstance(t, dict) or set(t) - TLS_KEYS:
        raise ConfigError(f"{where}: unknown tls keys {', '.join(sorted(set(t) - TLS_KEYS))}")
    values["tls_verify"] = t.get("verify") or ("custom" if (t.get("ca_file") or t.get("ca_pem")) else "system")
    values["tls_check_hostname"] = bool(t.get("check_hostname", True))
    if t.get("ca_file"):
        values["ca_data"] = _read_file(t["ca_file"], f"{where} ca_file")
    elif t.get("ca_pem"):
        values["ca_pem"] = t["ca_pem"]
    else:
        values["clear_ca"] = True
    if t.get("client_p12_file"):
        values["client_p12_data"] = _read_file(t["client_p12_file"], f"{where} client_p12_file")
    elif t.get("client_cert_file") or t.get("client_key_file"):
        if not (t.get("client_cert_file") and t.get("client_key_file")):
            raise ConfigError(f"{where}: give both client_cert_file and client_key_file")
        values["client_cert_data"] = _read_file(t["client_cert_file"], f"{where} client_cert_file")
        values["client_key_data"] = _read_file(t["client_key_file"], f"{where} client_key_file")
    else:
        values["clear_client_cert"] = True
    if t.get("client_key_password_env"):
        values["client_key_password"] = _env(t["client_key_password_env"], where)
    catalog = None
    if e.get("models") is not None:
        catalog = []
        for m in e["models"]:
            m = {"id": m} if isinstance(m, str) else m
            if not isinstance(m, dict) or not m.get("id") or set(m) - MODEL_KEYS:
                raise ConfigError(f"{where}: each model needs an id (allowed keys: {', '.join(sorted(MODEL_KEYS))})")
            catalog.append({"id": str(m["id"]), "label": m.get("label") or "", "description": m.get("description") or "",
                            "roles": m.get("roles") or [], "enabled": bool(m.get("enabled", True))})
        values["models"] = [c["id"] for c in catalog]
    return values, catalog


def apply(text: str, actor: str, dry_run: bool = False) -> list[str]:
    """Creates or updates endpoints by name. Returns a human-readable report. Everything is validated first,
    so a bad file changes nothing."""
    data = parse(text)
    planned = [_endpoint_values(e) for e in data.get("endpoints") or []]
    names = [v["name"] for v, _ in planned]
    if len(set(names)) != len(names):
        raise ConfigError("Endpoint names must be unique in the file")
    existing = {ep.name: ep for ep in llm.list_endpoints()}
    defaults = data.get("defaults") or {}
    if set(defaults) - set(llm.TASKS):
        raise ConfigError(f"Unknown defaults: {', '.join(sorted(set(defaults) - set(llm.TASKS)))}")
    for task, ref in defaults.items():
        if " / " not in str(ref) and "/" not in str(ref):
            raise ConfigError(f"defaults.{task}: write it as '<endpoint name> / <model>'")
        ep_name = str(ref).split(" / ", 1)[0] if " / " in str(ref) else str(ref).split("/", 1)[0]
        if ep_name.strip() not in names and ep_name.strip() not in existing:
            raise ConfigError(f"defaults.{task}: no endpoint named {ep_name.strip()!r}")
    report = []
    for values, catalog in planned:
        verb = "update" if values["name"] in existing else "create"
        report.append(f"{verb} {values['name']} ({values['base_url']}, key {values['key_mode']}"
                      + (f", {len(catalog)} catalog models, approval {values['approval']}" if catalog is not None
                         else "") + ")")
    prune = [n for n, ep in existing.items() if ep.managed and n not in names] if data.get("prune") else []
    report += [f"disable {n} (managed, not in the file)" for n in prune]
    if dry_run:
        return report
    try:
        for values, catalog in planned:
            ep = existing.get(values["name"])
            saved = llm.save_endpoint(values, actor, ep.id if ep else None)
            if catalog is not None:
                llm.save_catalog(saved.id, catalog, values["approval"], actor)
    except llm.AIError as e:
        raise ConfigError(str(e)) from e
    for n in prune:
        with session_scope() as s:
            ep = s.scalars(select(ModelEndpoint).where(ModelEndpoint.name == n)).first()
            ep.enabled = False
    if defaults:
        eps = {ep.name: ep for ep in llm.list_endpoints()}
        refs = {}
        for task, ref in defaults.items():
            ep_name, model = (str(ref).split(" / ", 1) if " / " in str(ref) else str(ref).split("/", 1))
            refs[task] = f"endpoint:{eps[ep_name.strip()].id}/{model.strip()}"
        llm.set_defaults(refs, actor)
        report.append("defaults: " + ", ".join(f"{t} = {r}" for t, r in defaults.items()))
    audit(actor, "ai.config.apply", "", "; ".join(report)[:1000])
    return report


def apply_file(path: str, actor: str = "config-file") -> list[str]:
    return apply(_read_file(path, "DATABRIDGE_AI_CONFIG_FILE").decode(), actor)


def export() -> str:
    """Current company endpoints as YAML. No secrets: keys become api_key_env placeholders, private keys become
    file paths to fill in; public CA certificates are included inline."""
    eps = llm.list_endpoints()
    by_id = {ep.id: ep for ep in eps}
    out: dict[str, Any] = {"version": 1, "endpoints": []}
    for ep in eps:
        e: dict[str, Any] = {"name": ep.name, "base_url": ep.base_url, "api_style": ep.api_style,
                             "network": ep.network, "key_mode": ep.key_mode or "shared",
                             "allowed_roles": list(ep.allowed_roles or []), "enabled": ep.enabled,
                             "timeout_s": ep.timeout_s}
        if ep.auth_header != "Authorization" or ep.auth_scheme != "Bearer":
            e.update(auth_header=ep.auth_header, auth_scheme=ep.auth_scheme)
        if ep.extra_headers:
            e["extra_headers"] = dict(ep.extra_headers)
        if ep.key_group:
            e["key_group"] = ep.key_group
        if ep.secret:
            e["api_key_env"] = "SET_ME_" + "".join(ch if ch.isalnum() else "_" for ch in ep.name.upper())[:40] + "_KEY"
        t: dict[str, Any] = {}
        if (ep.tls_verify or "system") != "system":
            t["verify"] = ep.tls_verify
        if ep.tls_check_hostname is False:
            t["check_hostname"] = False
        if ep.ca_pem:
            t["ca_pem"] = ep.ca_pem
        if ep.client_cert_pem:
            t["client_cert_file"] = "/path/on/server/client.crt"
            t["client_key_file"] = "/path/on/server/client.key"
        if t:
            e["tls"] = t
        e["approval"] = ep.approval or "open"
        e["models"] = [{k: v for k, v in c.items() if v not in ("", [], None) or k == "enabled"}
                       for c in llm.catalog_of(ep)]
        out["endpoints"].append(e)
    defaults = {}
    for task, ref in llm.get_defaults().items():
        try:
            kind, rid, model = llm.parse_ref(ref)
        except llm.AIError:
            continue
        if kind == "endpoint" and rid in by_id:
            defaults[task] = f"{by_id[rid].name} / {model}"
    if defaults:
        out["defaults"] = defaults

    class _Literal(str):
        pass

    def literal(dumper, data):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")

    yaml.SafeDumper.add_representer(_Literal, literal)
    for e in out["endpoints"]:
        if e.get("tls", {}).get("ca_pem"):
            e["tls"]["ca_pem"] = _Literal(e["tls"]["ca_pem"])
    header = ("# DataBridge company LLM configuration. Keys are never exported: set the api_key_env variables and\n"
              "# client certificate paths on the server. Apply with DATABRIDGE_AI_CONFIG_FILE or AI > Models > Import.\n")
    return header + yaml.safe_dump(out, sort_keys=False, allow_unicode=True, width=120)
