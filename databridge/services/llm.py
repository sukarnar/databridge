"""Model gateway: shared endpoints, personal API keys, model resolution, policy, budgets and usage.

Every AI call in DataBridge goes through `chat()`, so keys, permissions, budgets and the usage ledger
live in one place.

Model references name both the route and the key:
    endpoint:<id>/<model>   company endpoint configured by an admin (local LLM, company gateway). Depending on
                            the endpoint's key mode the call uses the company key, the calling user's own
                            issued key (per_user), or no key.
    key:<id>/<model>        a user's personal API key for a public provider (only that user may use it)

Company endpoints also carry a model catalog (display names, descriptions, roles, approval) and admins set
default models per task (playground, AI suggest, workflows).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from databridge.ai.providers import (
    normalize_base_url,
    LOCAL_PRESETS,
    PRESETS,
    ChatRequest,
    ChatResult,
    ProviderError,
    check_public_url,
    make_provider,
)
from databridge.ai import tls
from databridge.config import settings
from databridge.core.auth import can
from databridge.core.db import session_scope
from databridge.core.models import AiSetting, LlmCall, ModelEndpoint, User, UserCredential, UserEndpointKey
from databridge.core.security import decrypt_json, encrypt_json
from databridge.services.users import audit


class AIError(Exception):
    """Shown to the user as-is."""


KEY_MODES = {"shared": "One company key", "per_user": "Each user's own issued key", "none": "No key"}
APPROVAL = {"open": "All discovered models (disable the ones you don't want)",
            "approved": "Only models an admin enabled"}
TASKS = {"playground": "Playground", "automap": "AI suggest (mapping)", "workflow": "New workflow LLM nodes"}


@dataclass
class ModelOption:
    ref: str
    model: str
    route: str  # endpoint or key name
    kind: str  # shared | personal
    network: str  # internal | external
    display: str = ""  # catalog display name
    description: str = ""

    @property
    def label(self) -> str:
        return f"{self.display or self.model}  ·  {self.route}"


def _hint(key: str | None) -> str:
    return key[-4:] if key and len(key) >= 8 else ("set" if key else "")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ shared endpoints (admin)


def list_endpoints(include_disabled: bool = True) -> list[ModelEndpoint]:
    with session_scope() as s:
        q = select(ModelEndpoint).order_by(ModelEndpoint.name)
        if not include_disabled:
            q = q.where(ModelEndpoint.enabled.is_(True))
        return list(s.scalars(q))


def save_endpoint(values: dict[str, Any], actor: str, endpoint_id: int | None = None) -> ModelEndpoint:
    name = (values.get("name") or "").strip()
    base_url = normalize_base_url(values.get("base_url") or "", values.get("api_style") or "openai")
    if not name or not base_url.startswith(("http://", "https://")):
        raise AIError("Give the endpoint a name and an http(s) base URL")
    if values.get("api_style") not in ("openai", "anthropic"):
        raise AIError("API style must be openai or anthropic")
    with session_scope() as s:
        ep = s.get(ModelEndpoint, endpoint_id) if endpoint_id else ModelEndpoint(created_by=actor)
        ep.name, ep.base_url, ep.api_style = name, base_url, values["api_style"]
        ep.auth_header = values.get("auth_header") or "Authorization"
        ep.auth_scheme = values.get("auth_scheme", "Bearer")
        ep.network = values.get("network") or "internal"
        ep.allowed_roles = values.get("allowed_roles") or ["admin", "designer"]
        ep.enabled = bool(values.get("enabled", True))
        ep.timeout_s = int(values.get("timeout_s") or settings.ai_request_timeout)
        ep.default_model = values.get("default_model") or ep.default_model or ""
        if values.get("models") is not None:
            ep.models = [m for m in values["models"] if m]
        if values.get("key_mode"):
            if values["key_mode"] not in KEY_MODES:
                raise AIError(f"Unknown key mode {values['key_mode']}")
            ep.key_mode = values["key_mode"]
        if values.get("approval"):
            if values["approval"] not in APPROVAL:
                raise AIError(f"Unknown approval mode {values['approval']}")
            ep.approval = values["approval"]
        if "managed" in values:
            ep.managed = bool(values["managed"])
        if "extra_headers" in values:
            headers = values["extra_headers"] or {}
            if not isinstance(headers, dict):
                raise AIError("Extra headers must be name: value pairs")
            ep.extra_headers = {str(k): str(v) for k, v in headers.items()} or None
        if "key_group" in values:
            ep.key_group = values["key_group"] or None
        if "source_id" in values:
            ep.source_id = values["source_id"]
        if values.get("api_key"):
            ep.secret, ep.key_hint = encrypt_json({"api_key": values["api_key"]}), _hint(values["api_key"])
        elif values.get("clear_key"):
            ep.secret, ep.key_hint = None, ""
        tls_note = _apply_tls(ep, values)
        s.add(ep)
        s.flush()
    audit(actor, "ai.endpoint.update" if endpoint_id else "ai.endpoint.create", name, f"{base_url} {tls_note}".strip())
    return ep


def _apply_tls(ep: ModelEndpoint, values: dict[str, Any]) -> str:
    """Validates and stores TLS settings. Accepts file bytes (ca_data, client_cert_data, client_key_data,
    client_p12_data + client_key_password) or PEM text (ca_pem). Returns a short note for the audit log."""
    notes = []
    try:
        if "tls_verify" in values:
            mode = values["tls_verify"] or "system"
            if mode not in tls.VERIFY_MODES:
                raise AIError(f"Unknown TLS verification mode {mode}")
            ep.tls_verify = mode
            notes.append(f"tls={mode}")
        if "tls_check_hostname" in values:
            ep.tls_check_hostname = bool(values["tls_check_hostname"])
        if values.get("clear_ca"):
            ep.ca_pem = None
        if values.get("ca_data") or values.get("ca_pem"):
            data = values.get("ca_data") or values["ca_pem"].encode()
            ep.ca_pem = tls.normalize_ca(data)
            notes.append("custom CA")
        if values.get("clear_client_cert"):
            ep.client_cert_pem, ep.client_key_secret = None, None
            notes.append("client cert removed")
        if values.get("client_p12_data") or values.get("client_cert_data"):
            cert_pem, key_pem = tls.client_identity(values.get("client_cert_data"), values.get("client_key_data"),
                                                    values.get("client_key_password") or None,
                                                    values.get("client_p12_data"))
            ep.client_cert_pem = cert_pem
            ep.client_key_secret = encrypt_json({"key_pem": key_pem})
            notes.append("client cert")
    except tls.TLSConfigError as e:
        raise AIError(str(e)) from e
    if (ep.tls_verify or "system") == "custom" and not ep.ca_pem:
        raise AIError("Upload the provider's CA certificate, or choose another verification mode")
    ep.tls_info = {"ca": [c.to_dict() for c in tls.describe(ep.ca_pem)],
                   "client": [c.to_dict() for c in tls.describe(ep.client_cert_pem)][:1]}
    tls.clear_cache()
    return " ".join(notes)


def tls_context_for(ep: ModelEndpoint):
    """True (default verification) or an SSLContext for custom CA / client certificate / verification off."""
    mode = ep.tls_verify or "system"
    key_pem = decrypt_json(ep.client_key_secret).get("key_pem") if ep.client_key_secret else None
    if mode == "system" and not key_pem and not settings.ai_ca_bundle and ep.tls_check_hostname is not False:
        return True
    try:
        return tls.build_context(mode, ep.ca_pem, ep.tls_check_hostname is not False, ep.client_cert_pem, key_pem,
                                 settings.ai_ca_bundle or None)
    except tls.TLSConfigError as e:
        raise AIError(f"{ep.name}: {e}") from e


def delete_endpoint(endpoint_id: int, actor: str) -> None:
    with session_scope() as s:
        ep = s.get(ModelEndpoint, endpoint_id)
        name = ep.name if ep else str(endpoint_id)
        if ep:
            for k in s.scalars(select(UserEndpointKey).where(UserEndpointKey.endpoint_id == endpoint_id)):
                s.delete(k)
            s.delete(ep)
    audit(actor, "ai.endpoint.delete", name)


def _provider_for_endpoint(ep: ModelEndpoint, api_key: str | None = None, use_endpoint_key: bool = True):
    """Provider for a company endpoint. api_key = a user's own key (per_user mode); otherwise the endpoint's key
    (the company key, or in per_user mode the discovery key used only for tests and health checks)."""
    key = api_key or (decrypt_json(ep.secret).get("api_key") if ep.secret and use_endpoint_key else None)
    verify = tls_context_for(ep) if ep.base_url.startswith("https://") else True
    return make_provider(ep.api_style, base_url=ep.base_url, api_key=key, auth_header=ep.auth_header,
                         auth_scheme=ep.auth_scheme, timeout=ep.timeout_s or settings.ai_request_timeout,
                         verify=verify, extra_headers=ep.extra_headers)


def set_endpoint_key(endpoint_id: int, api_key: str | None, actor: str) -> None:
    """Sets (or clears) an endpoint's company/discovery key, e.g. a key found in an uploaded config file."""
    with session_scope() as s:
        ep = s.get(ModelEndpoint, endpoint_id)
        if api_key:
            ep.secret, ep.key_hint = encrypt_json({"api_key": api_key}), _hint(api_key)
        else:
            ep.secret, ep.key_hint = None, ""
        name = ep.name
    audit(actor, "ai.endpoint.key", name, "set" if api_key else "cleared")


def probe_endpoint(ep: ModelEndpoint, api_key: str | None = None) -> tuple[str, str, list[str] | None]:
    """Lists models. Returns (status, message, models): status ok | reachable | failed.

    "reachable" = the server answered but wants a key we don't have (per-user endpoints without a discovery key):
    the network, TLS and certificates work; users add their own keys.
    """
    try:
        models = _provider_for_endpoint(ep, api_key).list_models()
        return "ok", f"Connected. {len(models)} model(s) available.", models
    except ProviderError as e:
        if e.status in (401, 403) and (ep.key_mode or "shared") == "per_user" and not api_key and not ep.secret:
            return "reachable", "Reachable (TLS and network OK). The server needs a key to list models: " \
                                "each user adds their own, or add a discovery key.", None
        return "failed", str(e), None
    except AIError as e:
        return "failed", str(e), None


def test_endpoint(endpoint_id: int, actor: str) -> tuple[bool, str]:
    """Discovers the endpoint's models and stores them. Returns (ok, message)."""
    with session_scope() as s:
        ep = s.get(ModelEndpoint, endpoint_id)
    status, msg, models = probe_endpoint(ep)
    ok = status != "failed"
    with session_scope() as s:
        ep = s.get(ModelEndpoint, endpoint_id)
        if models is not None:
            ep.models = models or ep.models
            _merge_catalog(ep, ep.models)
            if not ep.default_model and ep.models:
                ep.default_model = ep.models[0]
        ep.last_test_ok, ep.last_test_message = ok, msg
    audit(actor, "ai.endpoint.test", str(endpoint_id), msg)
    return ok, msg


# ------------------------------------------------------------------ model catalog (company endpoints)


def _merge_catalog(ep: ModelEndpoint, discovered: list[str]) -> None:
    """Adds newly discovered models to the catalog: enabled in "open" mode, waiting for approval otherwise."""
    catalog = [dict(c) for c in (ep.catalog or [])]
    known = {c["id"] for c in catalog}
    for m in discovered:
        if m not in known:
            catalog.append({"id": m, "label": "", "description": "", "roles": [],
                            "enabled": (ep.approval or "open") == "open"})
    ep.catalog = catalog


def catalog_of(ep: ModelEndpoint) -> list[dict[str, Any]]:
    """The catalog, including discovered models not yet in it (older endpoints)."""
    catalog = [dict(c) for c in (ep.catalog or [])]
    known = {c["id"] for c in catalog}
    for m in ep.models or ([ep.default_model] if ep.default_model else []):
        if m not in known:
            catalog.append({"id": m, "label": "", "description": "", "roles": [],
                            "enabled": (ep.approval or "open") == "open"})
    return catalog


def usable_models(ep: ModelEndpoint, role: str) -> list[dict[str, Any]]:
    """Catalog entries a role may use on this endpoint (enabled, role allowed, and approved when required)."""
    return [c for c in catalog_of(ep) if c.get("enabled", True) and (not c.get("roles") or role in c["roles"])]


def save_catalog(endpoint_id: int, entries: list[dict[str, Any]], approval: str | None, actor: str) -> None:
    clean = []
    for c in entries:
        if not (c.get("id") or "").strip():
            continue
        clean.append({"id": c["id"].strip(), "label": (c.get("label") or "").strip(),
                      "description": (c.get("description") or "").strip(),
                      "roles": [r for r in c.get("roles") or [] if r], "enabled": bool(c.get("enabled", True))})
    with session_scope() as s:
        ep = s.get(ModelEndpoint, endpoint_id)
        if not ep:
            raise AIError("Endpoint not found")
        ep.catalog = clean
        if approval:
            if approval not in APPROVAL:
                raise AIError(f"Unknown approval mode {approval}")
            ep.approval = approval
        for c in clean:  # models added by hand become callable
            if c["id"] not in (ep.models or []):
                ep.models = [*(ep.models or []), c["id"]]
        name = ep.name
    enabled = sum(1 for c in clean if c["enabled"])
    audit(actor, "ai.endpoint.catalog", name, f"{enabled} of {len(clean)} models enabled, approval={approval}")


# ------------------------------------------------------------------ per-user keys for company endpoints


def _group_members(s, ep: ModelEndpoint) -> list[int]:
    if not ep.key_group:
        return [ep.id]
    return list(s.scalars(select(ModelEndpoint.id).where(ModelEndpoint.key_group == ep.key_group)))


def _user_key(s, user_id: int, ep: ModelEndpoint) -> UserEndpointKey | None:
    """The user's key for this endpoint, or one they added for another endpoint in the same key group."""
    ids = _group_members(s, ep)
    keys = {k.endpoint_id: k for k in s.scalars(select(UserEndpointKey).where(
        UserEndpointKey.user_id == user_id, UserEndpointKey.endpoint_id.in_(ids)))}
    return keys.get(ep.id) or next(iter(keys.values()), None)


def my_endpoint_keys(user_id: int) -> dict[int, UserEndpointKey]:
    """{endpoint id: key}, including endpoints covered by a key shared through their key group."""
    with session_scope() as s:
        own = {k.endpoint_id: k for k in s.scalars(select(UserEndpointKey).where(UserEndpointKey.user_id == user_id))}
        if own:
            groups = {ep.key_group: k for ep_id, k in own.items()
                      if (ep := s.get(ModelEndpoint, ep_id)) is not None and ep.key_group}
            if groups:
                for ep in s.scalars(select(ModelEndpoint).where(ModelEndpoint.key_group.in_(list(groups)))):
                    own.setdefault(ep.id, groups[ep.key_group])
        return own


def endpoint_key_counts() -> dict[int, int]:
    """How many users have added a key, per endpoint (admins see counts, never keys)."""
    with session_scope() as s:
        return {eid: n for eid, n in s.execute(select(UserEndpointKey.endpoint_id, func.count(UserEndpointKey.id))
                                               .group_by(UserEndpointKey.endpoint_id))}


def _check_endpoint_access(ep: ModelEndpoint | None, user: User) -> ModelEndpoint:
    if not ep or not ep.enabled:
        raise AIError("That company model endpoint is not available")
    if user.role not in (ep.allowed_roles or []):
        raise AIError("Your role may not use that model endpoint")
    return ep


def save_my_endpoint_key(user: User, endpoint_id: int, api_key: str) -> tuple[bool, str]:
    """Stores (encrypted) and tests the user's own key for a per-user company endpoint."""
    api_key = (api_key or "").strip()
    if not api_key:
        raise AIError("Paste the key issued to you")
    with session_scope() as s:
        ep = _check_endpoint_access(s.get(ModelEndpoint, endpoint_id), user)
        if (ep.key_mode or "shared") != "per_user":
            raise AIError(f"{ep.name} uses a company key; you don't need your own")
    status, msg, _ = probe_endpoint(ep, api_key)
    ok = status == "ok"
    if not ok:
        raise AIError(f"The key didn't work: {msg}")
    with session_scope() as s:
        ep_row = s.get(ModelEndpoint, endpoint_id)
        k = _user_key(s, user.id, ep_row)  # one key per key group
        if not k:
            k = UserEndpointKey(user_id=user.id, endpoint_id=endpoint_id, secret="")
            s.add(k)
        k.secret, k.key_hint = encrypt_json({"api_key": api_key}), _hint(api_key)
        k.last_test_ok, k.last_test_message = ok, msg
    audit(user.username, "ai.user_key.save", ep.name, f"...{_hint(api_key)}")
    return ok, msg


def test_my_endpoint_key(user: User, endpoint_id: int) -> tuple[bool, str]:
    with session_scope() as s:
        ep = _check_endpoint_access(s.get(ModelEndpoint, endpoint_id), user)
        k = _user_key(s, user.id, ep)
        if not k:
            raise AIError("You haven't added a key for this endpoint")
        key, key_id = decrypt_json(k.secret).get("api_key"), k.id
    status, msg, _ = probe_endpoint(ep, key)
    with session_scope() as s:
        k = s.get(UserEndpointKey, key_id)
        k.last_test_ok, k.last_test_message = status == "ok", msg
    return status == "ok", msg


def delete_my_endpoint_key(user: User, endpoint_id: int) -> None:
    with session_scope() as s:
        ep = s.get(ModelEndpoint, endpoint_id)
        ids = _group_members(s, ep) if ep else [endpoint_id]
        for k in s.scalars(select(UserEndpointKey).where(UserEndpointKey.user_id == user.id,
                                                         UserEndpointKey.endpoint_id.in_(ids))):
            s.delete(k)
    audit(user.username, "ai.user_key.delete", ep.name if ep else str(endpoint_id))


# ------------------------------------------------------------------ default models per task


def get_defaults() -> dict[str, str]:
    with session_scope() as s:
        row = s.get(AiSetting, "defaults")
        return dict(row.value) if row and isinstance(row.value, dict) else {}


def set_defaults(values: dict[str, str], actor: str) -> None:
    clean = {t: v for t, v in values.items() if t in TASKS and v}
    for ref in clean.values():
        parse_ref(ref)
    with session_scope() as s:
        row = s.get(AiSetting, "defaults") or AiSetting(key="defaults", value={})
        row.value, row.updated_by = clean, actor
        s.add(row)
    audit(actor, "ai.defaults", "", ", ".join(f"{k}={v}" for k, v in clean.items()))


def default_model(user: User, task: str, options: list[ModelOption] | None = None) -> str | None:
    """The admin's default for the task if this user may use it, else the first model they can use."""
    options = options if options is not None else available_models(user)
    refs = [o.ref for o in options]
    wanted = get_defaults().get(task)
    if wanted in refs:
        return wanted
    return refs[0] if refs else None


# ------------------------------------------------------------------ personal keys


def list_credentials(user_id: int) -> list[UserCredential]:
    with session_scope() as s:
        return list(s.scalars(select(UserCredential).where(UserCredential.user_id == user_id)
                              .order_by(UserCredential.name)))


def save_credential(user_id: int, username: str, values: dict[str, Any],
                    credential_id: int | None = None) -> UserCredential:
    provider = values.get("provider") or "custom"
    preset = PRESETS.get(provider, PRESETS["custom"])
    base_url = normalize_base_url(values.get("base_url") or preset["base_url"], preset.get("api_style", "openai"))
    if not settings.ai_allow_private_urls:
        check_public_url(base_url)
    name = (values.get("name") or preset["label"]).strip()
    with session_scope() as s:
        cred = s.get(UserCredential, credential_id) if credential_id else UserCredential(user_id=user_id)
        if credential_id and (not cred or cred.user_id != user_id):
            raise AIError("Key not found")
        if not credential_id and not values.get("api_key"):
            raise AIError("Paste the API key")
        cred.name, cred.provider, cred.base_url = name, provider, base_url
        cred.api_style = preset.get("api_style", "openai")
        cred.auth_header = values.get("auth_header") or preset.get("auth_header", "Authorization")
        cred.auth_scheme = values.get("auth_scheme", preset.get("auth_scheme", "Bearer"))
        if values.get("api_key"):
            cred.secret, cred.key_hint = encrypt_json({"api_key": values["api_key"].strip()}), _hint(values["api_key"])
        cred.default_model = values.get("default_model", cred.default_model or "") or ""
        if values.get("models") is not None:
            cred.models = [m for m in values["models"] if m]
        s.add(cred)
        s.flush()
    audit(username, "ai.key.update" if credential_id else "ai.key.create", name, provider)
    return cred


def delete_credential(user_id: int, credential_id: int, username: str) -> None:
    with session_scope() as s:
        cred = s.get(UserCredential, credential_id)
        if not cred or cred.user_id != user_id:
            raise AIError("Key not found")
        name = cred.name
        s.delete(cred)
    audit(username, "ai.key.delete", name)


def _provider_for_credential(cred: UserCredential):
    if not settings.ai_allow_private_urls:
        check_public_url(cred.base_url)
    verify = tls.build_context(extra_ca_file=settings.ai_ca_bundle) if settings.ai_ca_bundle else True
    return make_provider(cred.api_style, base_url=cred.base_url, api_key=decrypt_json(cred.secret).get("api_key"),
                         auth_header=cred.auth_header, auth_scheme=cred.auth_scheme,
                         timeout=settings.ai_request_timeout, verify=verify)


def test_credential(user_id: int, credential_id: int, username: str) -> tuple[bool, str]:
    with session_scope() as s:
        cred = s.get(UserCredential, credential_id)
        if not cred or cred.user_id != user_id:
            raise AIError("Key not found")
    try:
        models = _provider_for_credential(cred).list_models()
        ok, msg = True, f"Key works. {len(models)} model(s) available."
    except ProviderError as e:
        models, ok, msg = None, False, str(e)
    with session_scope() as s:
        c = s.get(UserCredential, credential_id)
        if models:
            c.models = models
            if not c.default_model:
                c.default_model = models[0]
        c.last_test_ok, c.last_test_message = ok, msg
    audit(username, "ai.key.test", cred.name, "ok" if ok else msg)
    return ok, msg


# ------------------------------------------------------------------ model catalogue and resolution


def available_models(user: User) -> list[ModelOption]:
    """Models this user may call: company endpoints allowed for their role (approved models only; per-user
    endpoints once the user has added their key), plus their own personal keys."""
    if not can(user.role, "use_ai"):
        return []
    options: list[ModelOption] = []
    my_keys = my_endpoint_keys(user.id)
    for ep in list_endpoints(include_disabled=False):
        if user.role not in (ep.allowed_roles or []):
            continue
        if (ep.key_mode or "shared") == "per_user" and ep.id not in my_keys:
            continue
        for c in usable_models(ep, user.role):
            options.append(ModelOption(f"endpoint:{ep.id}/{c['id']}", c["id"], ep.name, "shared", ep.network,
                                       c.get("label") or "", c.get("description") or ""))
    for cred in list_credentials(user.id):
        for m in cred.models or ([cred.default_model] if cred.default_model else []):
            options.append(ModelOption(f"key:{cred.id}/{m}", m, cred.name, "personal", "external"))
    return options


def parse_ref(ref: str) -> tuple[str, int, str]:
    try:
        route, model = ref.split("/", 1)
        kind, rid = route.split(":", 1)
        if kind not in ("endpoint", "key") or not model:
            raise ValueError
        return kind, int(rid), model
    except ValueError as e:
        raise AIError(f"Invalid model reference {ref!r}") from e


def _resolve(ref: str, user: User):
    kind, rid, model = parse_ref(ref)
    with session_scope() as s:
        if kind == "endpoint":
            ep = _check_endpoint_access(s.get(ModelEndpoint, rid), user)
            if model not in {c["id"] for c in usable_models(ep, user.role)}:
                raise AIError(f"Model {model} is not approved for your role on {ep.name}")
            mode = ep.key_mode or "shared"
            if mode == "per_user":
                k = _user_key(s, user.id, ep)
                if not k:
                    raise AIError(f"{ep.name} uses individually issued keys: add yours under AI > Models "
                                  f"(user {user.username} has none)")
                k.last_used_at = _now()
                return (_provider_for_endpoint(ep, decrypt_json(k.secret).get("api_key"), use_endpoint_key=False),
                        model, ep.name, ep.network)
            return _provider_for_endpoint(ep, use_endpoint_key=mode == "shared"), model, ep.name, ep.network
        cred = s.get(UserCredential, rid)
        if not cred or cred.user_id != user.id:
            raise AIError("That API key belongs to someone else or was deleted")
        return _provider_for_credential(cred), model, cred.name, "external"


def tokens_used_this_month(user_id: int) -> int:
    start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as s:
        total = s.scalar(select(func.coalesce(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens), 0))
                         .where(LlmCall.user_id == user_id, LlmCall.at >= start))
        return int(total or 0)


def _record(user: User, ref: str, purpose: str, **fields) -> None:
    with session_scope() as s:
        s.add(LlmCall(user_id=user.id, username=user.username, model_ref=ref, purpose=purpose, **fields))


def chat(user: User, ref: str, user_prompt: str, system: str = "", *, purpose: str = "playground",
         temperature: float | None = None, max_tokens: int = 1024, json_mode: bool = False,
         prompt_template: str = "", history: list[dict[str, str]] | None = None) -> ChatResult:
    """The single entry point for model calls: permission, budget, call, usage ledger."""
    if not settings.ai_enabled:
        raise AIError("AI features are disabled on this server")
    if not can(user.role, "use_ai"):
        raise AIError("Your role does not allow AI features")
    if settings.ai_monthly_token_limit and tokens_used_this_month(user.id) >= settings.ai_monthly_token_limit:
        _record(user, ref, purpose, status="blocked", error="monthly token limit reached")
        raise AIError(f"Monthly token limit ({settings.ai_monthly_token_limit:,}) reached")
    provider, model, route, network = _resolve(ref, user)
    req = ChatRequest(model=model, user=user_prompt, system=system, temperature=temperature,
                      max_tokens=max_tokens, json_mode=json_mode, history=history or [])
    content = {"request_text": f"[system]\n{system}\n\n[user]\n{user_prompt}"[:20000]} if settings.ai_log_content else {}
    try:
        result = provider.complete(req)
    except ProviderError as e:
        _record(user, ref, purpose, model=model, route=route, network=network, prompt_template=prompt_template,
                status="error", error=str(e), **content)
        raise AIError(str(e)) from e
    if settings.ai_log_content:
        content["response_text"] = result.text[:20000]
    _record(user, ref, purpose, model=result.model or model, route=route, network=network,
            prompt_template=prompt_template, prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens, latency_ms=result.latency_ms, **content)
    kind, rid, _ = parse_ref(ref)
    if kind == "key":
        with session_scope() as s:
            c = s.get(UserCredential, rid)
            if c:
                c.last_used_at = _now()
    return result


def usage(user: User | None = None, limit: int = 200) -> list[LlmCall]:
    with session_scope() as s:
        q = select(LlmCall).order_by(LlmCall.id.desc()).limit(limit)
        if user is not None:
            q = q.where(LlmCall.user_id == user.id)
        return list(s.scalars(q))


def usage_summary(user: User | None = None) -> list[dict[str, Any]]:
    """This month's calls and tokens per model (one user, or everyone when user is None)."""
    start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as s:
        q = (select(LlmCall.username, LlmCall.model, func.count(LlmCall.id),
                    func.sum(LlmCall.prompt_tokens), func.sum(LlmCall.completion_tokens))
             .where(LlmCall.at >= start).group_by(LlmCall.username, LlmCall.model)
             .order_by(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens).desc()))
        if user is not None:
            q = q.where(LlmCall.user_id == user.id)
        return [{"user": u, "model": m, "calls": c, "prompt_tokens": int(p or 0), "completion_tokens": int(o or 0)}
                for u, m, c, p, o in s.execute(q)]


__all__ = ["PRESETS", "LOCAL_PRESETS", "AIError", "ModelOption", "chat", "available_models"]
