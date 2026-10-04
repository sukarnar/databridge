"""LLM provider adapters.

Two API styles cover nearly every model:
  * "openai"    - OpenAI Chat Completions, also spoken by Ollama (/v1), vLLM, LM Studio, Gemini's
                  OpenAI endpoint, Groq, Mistral, OpenRouter, Azure OpenAI v1 and most company gateways.
  * "anthropic" - Anthropic Messages API.

Both are thin httpx clients behind the same `complete()` / `list_models()` interface.
"""

import ipaddress
import json
import socket
import ssl
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

# Tests inject an httpx.MockTransport here.
TRANSPORT: httpx.BaseTransport | None = None

PRESETS: dict[str, dict[str, str]] = {
    "openai": {"label": "OpenAI", "api_style": "openai", "base_url": "https://api.openai.com/v1"},
    "anthropic": {"label": "Anthropic", "api_style": "anthropic", "base_url": "https://api.anthropic.com"},
    "gemini": {"label": "Google Gemini (OpenAI-compatible)", "api_style": "openai",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "groq": {"label": "Groq", "api_style": "openai", "base_url": "https://api.groq.com/openai/v1"},
    "mistral": {"label": "Mistral", "api_style": "openai", "base_url": "https://api.mistral.ai/v1"},
    "openrouter": {"label": "OpenRouter", "api_style": "openai", "base_url": "https://openrouter.ai/api/v1"},
    "azure": {"label": "Azure OpenAI (v1 API, edit the resource name)", "api_style": "openai",
              "base_url": "https://YOUR-RESOURCE.openai.azure.com/openai/v1", "auth_header": "api-key",
              "auth_scheme": ""},
    "custom": {"label": "Other OpenAI-compatible", "api_style": "openai", "base_url": "https://"},
}

# Shared (admin) endpoint presets for local / on-prem servers.
LOCAL_PRESETS: dict[str, dict[str, str]] = {
    "ollama": {"label": "Ollama", "api_style": "openai", "base_url": "http://localhost:11434/v1"},
    "vllm": {"label": "vLLM", "api_style": "openai", "base_url": "http://localhost:8000/v1"},
    "lmstudio": {"label": "LM Studio", "api_style": "openai", "base_url": "http://localhost:1234/v1"},
    "gateway": {"label": "Company AI gateway (OpenAI-compatible)", "api_style": "openai", "base_url": "https://"},
    "anthropic": PRESETS["anthropic"],
    "openai": PRESETS["openai"],
}


# Paths people often paste from provider docs; the adapters add these themselves.
_OPENAI_SUFFIXES = ("/chat/completions", "/completions", "/embeddings", "/responses", "/models")
_ANTHROPIC_SUFFIXES = ("/v1/messages", "/messages", "/v1/models", "/models", "/v1")


def normalize_base_url(url: str, api_style: str = "openai") -> str:
    """Strips operation paths from a pasted URL.

    For example, "https://host/v1/chat/completions" becomes "https://host/v1" (OpenAI style), and
    "https://host/v1/messages" becomes "https://host" (Anthropic style).
    """
    url = (url or "").strip().split("?", 1)[0].rstrip("/")
    suffixes = _ANTHROPIC_SUFFIXES if api_style == "anthropic" else _OPENAI_SUFFIXES
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if url.lower().endswith(suffix):
                url = url[: -len(suffix)].rstrip("/")
                changed = True
                break
    return url


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class ChatRequest:
    model: str
    user: str
    system: str = ""
    history: list[dict[str, str]] = field(default_factory=list)  # earlier {"role", "content"} turns
    temperature: float | None = None
    max_tokens: int = 1024
    json_mode: bool = False


@dataclass
class ChatResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str = ""

    def json(self) -> Any:
        """Parses the reply as JSON, tolerating code fences or prose around one JSON object/array."""
        return extract_json(self.text)


def extract_json(text: str) -> Any:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = t.find(open_c), t.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("The model's reply is not valid JSON")


def check_public_url(url: str) -> None:
    """Personal keys may only call public HTTPS endpoints; blocks requests into the server's own network."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ProviderError("Personal API endpoints must use https://")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ProviderError(f"Cannot resolve {parsed.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ProviderError(f"{parsed.hostname} resolves to a private address; ask an admin to add it as a "
                                "shared endpoint instead")


class Provider(ABC):
    api_style: str

    def __init__(self, base_url: str, api_key: str | None = None, auth_header: str = "Authorization",
                 auth_scheme: str = "Bearer", timeout: float = 120, retries: int = 2,
                 verify: "ssl.SSLContext | bool" = True, extra_headers: dict[str, str] | None = None):
        self.base_url = normalize_base_url(base_url, self.api_style)
        self.api_key = api_key
        self.auth_header = auth_header or "Authorization"
        self.auth_scheme = auth_scheme
        self.timeout = timeout
        self.retries = retries
        self.verify = verify  # True, or an SSLContext with a custom CA and/or client certificate (mTLS)
        self.extra_headers = dict(extra_headers or {})  # static headers some gateways require

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(self.timeout, connect=10), transport=TRANSPORT, verify=self.verify)

    def _auth(self) -> dict[str, str]:
        headers = dict(self.extra_headers)
        if self.api_key:
            headers[self.auth_header] = f"{self.auth_scheme} {self.api_key}".strip()
        return headers

    def _request(self, method: str, path: str, **kw) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with self._client() as client:
                    resp = client.request(method, url, **kw)
            except httpx.TimeoutException as e:
                last = ProviderError(f"Timed out after {self.timeout:.0f}s calling {url}")
                if attempt < self.retries:
                    continue
                raise last from e
            except httpx.HTTPError as e:
                from databridge.ai.tls import friendly_error

                msg = friendly_error(e) or f"Cannot reach {url}: {type(e).__name__}: {e}"
                if url.startswith("https") and isinstance(e, (httpx.RemoteProtocolError, httpx.ReadError)):
                    # TLS 1.3 servers that demand a client certificate drop the connection after the handshake
                    msg += (" (TLS: if the server requires a client certificate, upload it in the endpoint's "
                            "certificate settings)")
                raise ProviderError(msg) from e
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                time.sleep(min(2 ** attempt, 8))
                last = ProviderError(f"HTTP {resp.status_code}", resp.status_code)
                continue
            if resp.status_code >= 400:
                raise ProviderError(self._error_text(resp), resp.status_code)
            try:
                return resp.json()
            except ValueError as e:
                raise ProviderError(f"Non-JSON response from {url}") from e
        raise last or ProviderError("Request failed")

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        if resp.status_code in (401, 403):
            return f"HTTP {resp.status_code}: the API key was rejected or lacks access"
        try:
            body = resp.json()
            msg = body.get("error", body)
            if isinstance(msg, dict):
                msg = msg.get("message") or json.dumps(msg)[:300]
        except ValueError:
            msg = resp.text[:300]
        text = f"HTTP {resp.status_code}: {msg}"
        if resp.status_code == 404:
            text += (" - check the Base URL: it should end at the API root (e.g. https://host/v1), "
                     "not include /chat/completions")
        return text

    @abstractmethod
    def list_models(self) -> list[str]: ...

    @abstractmethod
    def complete(self, req: ChatRequest) -> ChatResult: ...


class OpenAICompatible(Provider):
    api_style = "openai"

    def list_models(self) -> list[str]:
        data = self._request("GET", "/models", headers=self._auth())
        items = data.get("data") or data.get("models") or []
        ids = [m.get("id") or m.get("name") for m in items if isinstance(m, dict)]
        return sorted({i for i in ids if i})

    def complete(self, req: ChatRequest) -> ChatResult:
        messages = ([{"role": "system", "content": req.system}] if req.system else []) + req.history + [
            {"role": "user", "content": req.user}]
        body: dict[str, Any] = {"model": req.model, "messages": messages, "max_tokens": req.max_tokens}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.json_mode:
            body["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            data = self._request("POST", "/chat/completions", headers=self._auth(), json=body)
        except ProviderError as e:
            if req.json_mode and e.status == 400:  # some local servers don't support response_format
                body.pop("response_format", None)
                data = self._request("POST", "/chat/completions", headers=self._auth(), json=body)
            else:
                raise
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        return ChatResult(
            text=(choice.get("message") or {}).get("content") or "",
            model=data.get("model") or req.model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=choice.get("finish_reason") or "",
        )


class AnthropicMessages(Provider):
    api_style = "anthropic"
    VERSION = "2023-06-01"

    def _auth(self) -> dict[str, str]:
        headers = {**self.extra_headers, "anthropic-version": self.VERSION}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def list_models(self) -> list[str]:
        data = self._request("GET", "/v1/models", headers=self._auth(), params={"limit": 100})
        return sorted({m["id"] for m in data.get("data", []) if m.get("id")})

    def complete(self, req: ChatRequest) -> ChatResult:
        system = req.system
        if req.json_mode:
            system = (system + "\n\n" if system else "") + "Respond with a single valid JSON value and nothing else."
        body: dict[str, Any] = {"model": req.model, "max_tokens": req.max_tokens,
                                "messages": req.history + [{"role": "user", "content": req.user}]}
        if system:
            body["system"] = system
        if req.temperature is not None:
            body["temperature"] = req.temperature
        started = time.perf_counter()
        data = self._request("POST", "/v1/messages", headers=self._auth(), json=body)
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            model=data.get("model") or req.model,
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=data.get("stop_reason") or "",
        )


def make_provider(api_style: str, **kw) -> Provider:
    if api_style == "anthropic":
        return AnthropicMessages(**kw)
    if api_style == "openai":
        return OpenAICompatible(**kw)
    raise ProviderError(f"Unknown API style {api_style!r}")
