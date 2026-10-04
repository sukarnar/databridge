"""Company LLM configs in the Continue `config.yaml` format, mapped to DataBridge's standard configuration.

Many companies hand out a Continue (continue.dev) assistant file for their internal models:

    name: Local Assistant
    version: 1.0.0
    schema: v1
    models:
      - name: Llama 4 Scout
        provider: lmstudio
        model: /genai/Llama-4-Scout-17B
        roles: [chat, edit, apply]
        apiBase: https://llm.corp.example/llama4/v1/
        requestOptions:
          verifySsl: true
          caBundlePath: C:/Users/me/Downloads/Corp_Root_CA.pem
          headers: {"apikey": "putapikeyhere"}

Mapping (one DataBridge endpoint per distinct apiBase + auth + TLS; models become catalog entries):

    models[].apiBase                  -> endpoint base_url (/chat/completions etc. stripped)
    models[].provider                 -> api_style: anthropic for "anthropic", openai-compatible for the rest
    models[].model / name             -> catalog model id / display name
    models[].roles                    -> capabilities; models that can't chat (embed, rerank, autocomplete only)
                                         are kept but disabled
    requestOptions.verifySsl: false   -> TLS verification off (warned)
    requestOptions.caBundlePath       -> the CA file, uploaded by the admin (the path is on someone's laptop)
    requestOptions.headers            -> the key header (apikey, x-api-key, api-key, Authorization, ...) becomes
                                         the auth header; other headers are sent as extra headers
    apiKey / key header value         -> a placeholder ("putapikeyhere") means each user adds their own issued
                                         key; a real value can become the company key (admin's choice) and is
                                         never stored in the kept copy of the file
    requestOptions.timeout            -> endpoint timeout

Copied files are often slightly broken (mixed quotes, uneven indentation, chat apps turning paths into links);
`load` repairs those and reports every repair.
"""

import re
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from typing import Any
from urllib.parse import urlparse

import yaml

from databridge.ai.providers import normalize_base_url

KEY_HEADERS = ["authorization", "apikey", "api-key", "x-api-key", "api_key", "x-apikey",
               "ocp-apim-subscription-key", "x-functions-key", "x-goog-api-key"]
REDACTED = "***redacted***"
PLACEHOLDER = re.compile(r"put.*key|your|here|change.?me|replace|xxx|todo|example|dummy|<[^>]*>|\$\{|\{\{|^\s*$",
                         re.I)
CHAT_ROLES = {"chat", "edit", "apply", "summarize"}
ANTHROPIC = {"anthropic"}
KNOWN_PROVIDERS = {"openai", "lmstudio", "ollama", "vllm", "llama.cpp", "llamacpp", "llamafile", "openrouter",
                   "azure", "mistral", "groq", "deepseek", "together", "fireworks", "gemini", "anthropic",
                   "nvidia", "cerebras", "sambanova", "huggingface-tgi", "tgi", "text-gen-webui", "kobold",
                   "openai-compatible", "localai", "msty", "jan", "llamastack", "scaleway", "nebius", "novita",
                   "ovhcloud", "venice", "xai", "deepinfra", "moonshot"}
UNSUPPORTED_MODEL_KEYS = {"uses", "embedOptions", "chatOptions", "autocompleteOptions", "promptTemplates",
                          "capabilities", "defaultCompletionOptions", "env", "apiVersion", "deployment",
                          "apiType", "region", "profile"}
PORTED_OPTIONS = {"verifySsl", "caBundlePath", "headers", "timeout"}


class ContinueConfigError(ValueError):
    pass


# ------------------------------------------------------------------ tolerant loading


def _fix_flow_quotes(line: str) -> str:
    """{"apikey': 'x'} -> {"apikey": "x"}: normalise quotes inside a one-line {...} mapping."""
    m = re.search(r"\{.*\}", line)
    if not m or ("'" not in m.group(0) or '"' not in m.group(0)):
        return line
    inner = m.group(0)[1:-1]
    pairs = []
    for part in inner.split(","):
        if ":" not in part:
            return line
        k, v = part.split(":", 1)
        k, v = k.strip().strip("'\""), v.strip().strip("'\"")
        pairs.append(f'"{k}": "{v}"')
    return line[:m.start()] + "{" + ", ".join(pairs) + "}" + line[m.end():]


def repair(text: str) -> tuple[str, list[str]]:
    """Fixes common copy/paste damage. Returns (text, notes)."""
    notes = []
    if "\t" in text:
        text = text.replace("\t", "    ")
        notes.append("replaced tabs with spaces")
    linked = re.sub(r"\[([^\]\n]+)\]\((?:https?|mailto)://[^)\n]*\)", r"\1", text)
    if linked != text:
        notes.append("removed web links that a chat or email app added inside values (e.g. file paths)")
        text = linked
    lines, fixed_quotes = [], 0
    for line in text.splitlines():
        new = _fix_flow_quotes(line)
        fixed_quotes += new != line
        lines.append(new.rstrip())
    if fixed_quotes:
        notes.append(f"fixed mismatched quotes in {fixed_quotes} header line(s)")
    # re-indent by structure: each deeper indentation opens one level, shallower closes levels
    out, stack, changed = [], [], False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            out.append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        while stack and indent < stack[-1]:
            stack.pop()
        if not stack or indent > stack[-1]:
            stack.append(indent)
        level = len(stack) - 1
        new = "  " * level + line.lstrip(" ")
        changed |= new != line
        out.append(new)
    if changed:
        notes.append("normalised uneven indentation")
    return "\n".join(out) + "\n", notes


def load(text: str) -> tuple[dict[str, Any], list[str]]:
    """Parses the file strictly, else after repairs. Returns (data, repair notes)."""
    try:
        data, notes = yaml.safe_load(text), []
    except yaml.YAMLError:
        fixed, notes = repair(text)
        try:
            data = yaml.safe_load(fixed)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f" near line {mark.line + 1}" if mark else ""
            raise ContinueConfigError(f"The file is not valid YAML{where}, even after automatic repairs "
                                      f"({', '.join(notes) or 'none applied'}): {getattr(e, 'problem', e)}") from e
    if not isinstance(data, dict):
        raise ContinueConfigError("Expected a YAML mapping with a 'models' list")
    return data, notes


def detect_format(data: dict[str, Any]) -> str:
    """"continue" (models with apiBase/provider), "databridge" (endpoints), or "unknown"."""
    if isinstance(data.get("endpoints"), list):
        return "databridge"
    models = data.get("models")
    if isinstance(models, list) and any(isinstance(m, dict) and ("provider" in m or "apiBase" in m) for m in models):
        return "continue"
    return "unknown"


# ------------------------------------------------------------------ mapping


def ca_key(path: str) -> str:
    """Matches caBundlePath values to uploaded files by file name (paths are from someone's laptop)."""
    cleaned = re.sub(r"[\\/]+", "/", str(path or "").strip())
    return PureWindowsPath(cleaned).name.lower() or cleaned.lower()


def is_placeholder(value: Any) -> bool:
    return value is None or bool(PLACEHOLDER.search(str(value)))


@dataclass
class MappedModel:
    name: str
    model: str
    provider: str
    roles: list[str]
    enabled: bool
    note: str = ""


@dataclass
class MappedEndpoint:
    name: str
    base_url: str
    api_style: str
    auth_header: str
    auth_scheme: str
    key_in_file: str | None  # a real key found in the file (never stored in the kept copy)
    key_placeholder: bool
    key_redacted: bool  # the kept copy of an earlier upload: the real key is already stored on the endpoint
    extra_headers: dict[str, str]
    verify: str  # system | custom | off
    ca_path: str | None
    timeout_s: int | None
    models: list[MappedModel] = field(default_factory=list)


@dataclass
class Mapping:
    name: str
    version: str
    schema: str
    endpoints: list[MappedEndpoint]
    warnings: list[str]
    ca_paths: list[str]  # caBundlePath values that need an uploaded file

    def rows(self) -> list[dict[str, Any]]:
        """One row per model, for the preview table."""
        return [{"model": m.name, "id": m.model, "endpoint": e.name, "base_url": e.base_url, "style": e.api_style,
                 "auth": f"{e.auth_header} ({'placeholder' if e.key_placeholder else 'key in file' if e.key_in_file else 'stored key' if e.key_redacted else 'none'})"
                 if e.auth_header else "none",
                 "tls": e.verify + (f" ({ca_key(e.ca_path)})" if e.ca_path else ""),
                 "roles": ", ".join(m.roles), "enabled": m.enabled, "note": m.note}
                for e in self.endpoints for m in e.models]


def _auth_from(model: dict[str, Any], opts: dict[str, Any]) -> tuple[str, str, str | None, bool, dict[str, str]]:
    """(auth header, scheme, key value, placeholder?, extra headers)."""
    headers = opts.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}
    headers = {str(k): "" if v is None else str(v) for k, v in headers.items()}
    lower = {k.lower(): k for k in headers}
    auth_name = next((lower[h] for h in KEY_HEADERS if h in lower), None)
    extra = {k: v for k, v in headers.items() if k != auth_name}
    if auth_name:
        value = headers[auth_name]
        scheme = ""
        if auth_name.lower() == "authorization" and " " in value.strip():
            scheme, value = value.strip().split(" ", 1)
        return auth_name, scheme, value, is_placeholder(value), extra
    if "apiKey" in model:
        value = model.get("apiKey")
        return "Authorization", "Bearer", None if value is None else str(value), is_placeholder(value), extra
    return "", "", None, False, extra


def map_continue(data: dict[str, Any], config_name: str | None = None) -> Mapping:
    name = str(data.get("name") or config_name or "Company assistant").strip()
    warnings: list[str] = []
    schema = str(data.get("schema") or "")
    if schema and schema.lower() not in ("v1", "vl", "1"):
        warnings.append(f"schema {schema!r}: only Continue schema v1 is known; mapped anyway")
    models = data.get("models")
    if not isinstance(models, list) or not models:
        raise ContinueConfigError("The file has no 'models' list")
    groups: dict[tuple, MappedEndpoint] = {}
    ca_paths: list[str] = []
    for i, m in enumerate(models, 1):
        if not isinstance(m, dict):
            warnings.append(f"model #{i}: not a mapping, skipped")
            continue
        label = str(m.get("name") or m.get("model") or f"model {i}").strip()
        if "uses" in m:
            warnings.append(f"{label}: 'uses' refers to a Continue Hub block; export the full model definition "
                            "instead (skipped)")
            continue
        provider = str(m.get("provider") or "openai").strip().lower()
        model_id = str(m.get("model") or "").strip()
        if not model_id:
            warnings.append(f"{label}: no 'model' id, skipped")
            continue
        api_base = str(m.get("apiBase") or "").strip()
        if not api_base:
            warnings.append(f"{label}: no apiBase. DataBridge needs the server's URL (a provider default like "
                            "localhost would point at the DataBridge server itself), skipped")
            continue
        if provider not in KNOWN_PROVIDERS:
            warnings.append(f"{label}: provider {provider!r} is not known; treated as OpenAI-compatible")
        style = "anthropic" if provider in ANTHROPIC else "openai"
        base = normalize_base_url(api_base, style)
        if not base.startswith(("http://", "https://")):
            warnings.append(f"{label}: apiBase {api_base!r} is not an http(s) URL, skipped")
            continue
        opts = m.get("requestOptions") or {}
        if not isinstance(opts, dict):
            opts = {}
        ignored = sorted(set(opts) - PORTED_OPTIONS)
        if ignored:
            warnings.append(f"{label}: requestOptions {', '.join(ignored)} not used by DataBridge")
        ignored_model = sorted(set(m) & UNSUPPORTED_MODEL_KEYS)
        if ignored_model:
            warnings.append(f"{label}: {', '.join(ignored_model)} not used by DataBridge")
        verify_raw = opts.get("verifySsl", True)
        verify_ok = str(verify_raw).strip().lower() not in ("false", "0", "no", "off")
        ca_path = str(opts["caBundlePath"]).strip() if opts.get("caBundlePath") else None
        if not verify_ok:
            verify = "off"
            warnings.append(f"{label}: verifySsl is false, so certificates won't be checked (not recommended)")
        else:
            verify = "custom" if ca_path else "system"
        if ca_path and verify_ok and ca_key(ca_path) not in [ca_key(p) for p in ca_paths]:
            ca_paths.append(ca_path)
        header, scheme, key, placeholder, extra = _auth_from(m, opts)
        timeout = opts.get("timeout")
        try:
            timeout = int(float(timeout)) if timeout else None
        except (TypeError, ValueError):
            timeout = None
        roles = [str(r).strip() for r in (m.get("roles") or ["chat"])]
        can_chat = bool(set(r.lower() for r in roles) & CHAT_ROLES)
        mm = MappedModel(label, model_id, provider, roles, can_chat,
                         "" if can_chat else "not a chat model (only " + ", ".join(roles) + "): kept disabled")
        group = (base, style, header, scheme, key or "", verify, ca_key(ca_path) if ca_path else "",
                 tuple(sorted(extra.items())))
        ep = groups.get(group)
        if not ep:
            redacted = key == REDACTED
            ep = MappedEndpoint("", base, style, header, scheme, None if placeholder or redacted else key,
                                placeholder, redacted, extra, verify, ca_path, timeout)
            groups[group] = ep
        ep.models.append(mm)
    endpoints = list(groups.values())
    if not endpoints:
        raise ContinueConfigError("No usable models in the file: " + "; ".join(warnings))
    used: set[str] = set()
    for ep in endpoints:
        if len(ep.models) == 1:
            base_name = f"{name} · {ep.models[0].name}"
        else:
            u = urlparse(ep.base_url)
            tail = [p for p in u.path.split("/") if p and p.lower() not in ("v1", "api")]
            base_name = f"{name} · {tail[-1] if tail else u.hostname}"
        candidate, n = base_name[:115], 2
        while candidate in used:
            candidate, n = f"{base_name[:110]} ({n})", n + 1
        used.add(candidate)
        ep.name = candidate
    return Mapping(name, str(data.get("version") or ""), schema, endpoints, warnings, ca_paths)


def secret_values(mapping: Mapping) -> list[str]:
    return [e.key_in_file for e in mapping.endpoints if e.key_in_file]


def redact(text: str, secrets: list[str]) -> str:
    """The kept copy of the file: real keys replaced (placeholders are left as they are)."""
    for s in sorted(set(secrets), key=len, reverse=True):
        if s and len(s) >= 4:
            text = text.replace(s, REDACTED)
    return text


def to_standard(mapping: Mapping, *, key_mode: str, network: str, allowed_roles: list[str],
                ca_pems: dict[str, str], existing_names: dict[str, str] | None = None) -> dict[str, Any]:
    """DataBridge standard configuration (the llm-config.yaml schema) for the mapped endpoints.

    key_mode: per_user | shared (use the keys found in the file) | none. ca_pems: {ca_key: PEM text}.
    existing_names: {base_url: name} to keep names of endpoints created by an earlier upload.
    """
    endpoints = []
    for e in mapping.endpoints:
        mode = key_mode
        if mode == "shared" and not e.key_in_file and not e.key_redacted:
            mode = "per_user" if e.auth_header else "none"
        if not e.auth_header and mode == "per_user":
            mode = "none"
        entry: dict[str, Any] = {
            "name": (existing_names or {}).get(e.base_url, e.name), "base_url": e.base_url,
            "api_style": e.api_style, "network": network, "key_mode": mode, "allowed_roles": allowed_roles,
            "approval": "approved",
            "models": [{"id": m.model, "label": m.name,
                        "description": "Continue roles: " + ", ".join(m.roles), "enabled": m.enabled}
                       for m in e.models],
        }
        if e.auth_header:
            entry.update(auth_header=e.auth_header, auth_scheme=e.auth_scheme)
        if mode == "shared" and e.key_redacted:
            entry["keep_key"] = True  # re-applying the kept copy: leave the stored company key as it is
        if e.extra_headers:
            entry["extra_headers"] = dict(e.extra_headers)
        if e.timeout_s:
            entry["timeout_s"] = e.timeout_s
        tls: dict[str, Any] = {"verify": e.verify}
        if e.verify == "custom":
            pem = ca_pems.get(ca_key(e.ca_path or ""))
            if pem:
                tls["ca_pem"] = pem
            else:
                tls["verify"] = "system"  # until the CA file is uploaded
        entry["tls"] = tls
        endpoints.append(entry)
    return {"version": 1, "endpoints": endpoints}
