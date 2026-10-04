"""Uploaded company configs (Continue config.yaml) kept as sources and mapped to DataBridge endpoints."""

import itertools
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from databridge.ai import fake_llm, providers
from databridge.ai.fake_llm import mock_transport
from databridge.config import settings
from databridge.core.db import session_scope
from databridge.core.models import ModelEndpoint
from databridge.services import config_sources, llm, users
from databridge.services import continue_config as cc
from tests.test_tls import _cert, _name, _pem

EXAMPLE = (Path(__file__).parent / "fixtures" / "continue_config_example.yaml").read_text()
_n = itertools.count(1)


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setattr(providers, "TRANSPORT", mock_transport())
    monkeypatch.setattr(settings, "ai_allow_private_urls", True)
    monkeypatch.setattr(fake_llm, "MODELS", fake_llm.MODELS + ["/genai/Llama-4-Scout-17B", "/genai/gpt-oss-120b"])
    fake_llm.KEYS_SEEN.clear()


@pytest.fixture(scope="module")
def ca_pem() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    return _pem(_cert("Corp Root CA 03", _name("Corp Root CA 03"), key, key.public_key(), ca=True, days=300))


def unique(text: str) -> str:
    """A copy of the example with its own name and hosts, so tests don't share endpoints."""
    n = next(_n)
    return text.replace("Local Assistant", f"Local Assistant {n}").replace("llm-gateway.corp.example",
                                                                            f"gw{n}.corp.example")


def user(role="designer"):
    u, _ = users.create_user(f"cfg.{role}{next(_n)}", role, "t", password="Config-Src-2026!", must_change=False)
    return u


def test_the_company_example_is_repaired_and_mapped():
    data, repairs = cc.load(EXAMPLE)
    assert len(repairs) == 3 and any("quotes" in r for r in repairs) and any("indentation" in r for r in repairs)
    assert any("links" in r for r in repairs)
    m = cc.map_continue(data)
    assert m.name == "Local Assistant" and len(m.endpoints) == 2 and not m.warnings
    llama, gpt = m.endpoints
    assert llama.base_url == "https://llm-gateway.corp.example/bae-api-llama4/v1"  # trailing slash removed
    assert llama.models[0].model == "/genai/Llama-4-Scout-17B" and llama.models[0].name == "Llama 4 Scout"
    assert (llama.auth_header, llama.auth_scheme, llama.key_placeholder) == ("apikey", "", True)
    assert llama.verify == "custom" and cc.ca_key(llama.ca_path) == "ca03_base64_fullrootchain.pem"
    assert cc.ca_key(gpt.ca_path) == cc.ca_key(llama.ca_path)  # the pasted link was cleaned: same file
    assert m.ca_paths == [llama.ca_path]  # one CA upload serves both


def test_upload_creates_endpoints_users_add_their_key_once(ca_pem):
    text = unique(EXAMPLE)
    preview = config_sources.plan(text, "config.yaml")
    assert preview["format"] == "continue" and len(preview["rows"]) == 2
    assert preview["ca_needed"][0]["uploaded"] is False and any("Upload ca03" in w for w in preview["warnings"])
    src = config_sources.save(text, "admin", "config.yaml", ca_files={"CA03_Base64_FullRootChain.pem": ca_pem})
    assert src.format == "continue" and len(src.endpoint_ids) == 2 and src.revision == 1
    assert "putapikeyhere" in src.original  # kept as the custom source (placeholders aren't secrets)
    with session_scope() as s:
        eps = [s.get(ModelEndpoint, i) for i in src.endpoint_ids]
    for ep in eps:
        assert ep.managed and ep.source_id == src.id and ep.key_mode == "per_user" and ep.approval == "approved"
        assert ep.auth_header == "apikey" and ep.auth_scheme == "" and ep.tls_verify == "custom" and ep.ca_pem
    assert eps[0].key_group == eps[1].key_group
    u = user()
    assert not [o for o in llm.available_models(u) if o.route in (eps[0].name, eps[1].name)]
    llm.save_my_endpoint_key(u, eps[0].id, "user-issued-key-42")  # added once...
    opts = {o.model: o for o in llm.available_models(u) if o.route in (eps[0].name, eps[1].name)}
    assert set(opts) == {"/genai/Llama-4-Scout-17B", "/genai/gpt-oss-120b"}  # ...works for both
    assert opts["/genai/gpt-oss-120b"].display == "GPT OSS 120B 19"
    fake_llm.KEYS_SEEN.clear()
    llm.chat(u, opts["/genai/gpt-oss-120b"].ref, "hello")
    assert fake_llm.KEYS_SEEN == ["user-issued-key-42"]  # sent in the apikey header


def test_new_version_updates_in_place_and_disables_removed(ca_pem):
    text = unique(EXAMPLE)
    src = config_sources.save(text, "admin", "config.yaml", ca_files={"ca03_base64_fullrootchain.pem": ca_pem})
    first_ids = list(src.endpoint_ids)
    only_llama = text[: text.index("   - name: GPT OSS")]
    src2 = config_sources.save(only_llama, "admin", "config.yaml", source_id=src.id)
    assert src2.revision == 2 and src2.endpoint_ids == first_ids[:1]
    with session_scope() as s:
        kept, removed = s.get(ModelEndpoint, first_ids[0]), s.get(ModelEndpoint, first_ids[1])
        assert kept.enabled and kept.ca_pem  # CA upload remembered by the source
        assert not removed.enabled


def test_real_key_in_file_redacted_and_optionally_used(ca_pem):
    text = unique(EXAMPLE).replace("putapikeyhere", "user-company-secret-9f3a")
    per_user = config_sources.plan(text, "config.yaml")
    assert any("real key" in w for w in per_user["warnings"]) and "user-company-secret" not in per_user["redacted"]
    src = config_sources.save(text, "admin", "config.yaml", options={"key_mode": "shared"},
                              ca_files={"ca03_base64_fullrootchain.pem": ca_pem})
    assert "user-company-secret" not in src.original and cc.REDACTED in src.original
    u = user()
    ep_id = src.endpoint_ids[0]
    ref = next(o.ref for o in llm.available_models(u) if o.ref.startswith(f"endpoint:{ep_id}/"))
    fake_llm.KEYS_SEEN.clear()
    llm.chat(u, ref, "hi")
    assert fake_llm.KEYS_SEEN == ["user-company-secret-9f3a"]
    config_sources.reapply(src.id, "admin")  # the kept copy is redacted: the stored key must survive
    fake_llm.KEYS_SEEN.clear()
    llm.chat(u, ref, "hi")
    assert fake_llm.KEYS_SEEN == ["user-company-secret-9f3a"]


def test_mapping_edge_cases():
    text = """
name: Mixed
schema: v1
models:
  - name: Chat
    provider: ollama
    model: llama3.1
    apiBase: http://gpu01.corp.example:11434/v1/chat/completions
    requestOptions:
      verifySsl: false
      timeout: 90
      proxy: http://proxy:8080
      headers:
        Authorization: Bearer putapikeyhere
        X-Team: data
  - name: Embedder
    provider: ollama
    model: nomic-embed-text
    roles: [embed]
    apiBase: http://gpu01.corp.example:11434/v1
    requestOptions:
      verifySsl: false
      timeout: 90
      headers:
        Authorization: Bearer putapikeyhere
        X-Team: data
  - name: Claude via gateway
    provider: anthropic
    model: claude-sonnet
    apiBase: https://gw.corp.example/anthropic/v1/messages
    apiKey: ${{ secrets.ANTHROPIC_KEY }}
  - name: Hub model
    uses: acme/some-model
  - name: No base
    provider: lmstudio
    model: x
"""
    m = cc.map_continue(cc.load(text)[0])
    chat = m.endpoints[0]
    assert chat.base_url == "http://gpu01.corp.example:11434/v1" and chat.verify == "off" and chat.timeout_s == 90
    assert (chat.auth_header, chat.auth_scheme, chat.extra_headers) == ("Authorization", "Bearer", {"X-Team": "data"})
    assert [x.model for x in chat.models] == ["llama3.1", "nomic-embed-text"]  # same server: one endpoint
    assert chat.models[1].enabled is False and "not a chat model" in chat.models[1].note
    claude = m.endpoints[1]
    assert claude.api_style == "anthropic" and claude.base_url == "https://gw.corp.example/anthropic"
    assert claude.key_placeholder  # ${{ secrets.X }} is a reference, not a key
    joined = " ".join(m.warnings)
    for expected in ("verifySsl is false", "proxy", "Continue Hub", "no apiBase"):
        assert expected in joined


def test_extra_headers_are_sent(monkeypatch):
    seen = []
    real = fake_llm.handle

    def spy(method, path, headers, body):
        seen.append({k.lower(): v for k, v in headers.items()})
        return real(method, path, headers, body)

    monkeypatch.setattr(fake_llm, "handle", spy)
    monkeypatch.setattr(providers, "TRANSPORT", mock_transport())
    n = next(_n)
    text = f"""
name: Headers {n}
models:
  - name: M
    provider: vllm
    model: fake-small
    apiBase: http://h{n}.corp.example/v1
    requestOptions:
      headers: {{"apikey": "putapikeyhere", "X-Tenant": "finance"}}
"""
    src = config_sources.save(text, "admin", "c.yaml", options={"key_mode": "none"})
    u = user()
    ref = next(o.ref for o in llm.available_models(u) if o.ref.startswith(f"endpoint:{src.endpoint_ids[0]}/"))
    llm.chat(u, ref, "hi")
    assert seen[-1].get("x-tenant") == "finance"


def test_bad_files_and_other_formats(tmp_path):
    with pytest.raises(config_sources.SourceError, match="not valid YAML"):
        config_sources.plan("models: [\n  - {name: x, provider: 'a\n", "bad.yaml")
    with pytest.raises(config_sources.SourceError, match="Unknown format"):
        config_sources.plan("hello: world\n", "x.yaml")
    with pytest.raises(config_sources.SourceError, match="No usable models"):
        config_sources.plan("models:\n  - name: a\n    provider: lmstudio\n    model: m\n", "x.yaml")
    n = next(_n)
    native = f"endpoints:\n  - name: Native {n}\n    base_url: http://n{n}.corp.example/v1\n    key_mode: none\n"
    src = config_sources.save(native, "admin", "llm.yaml")
    assert src.format == "databridge" and len(src.endpoint_ids) == 1


def test_name_clash_with_manual_endpoint_is_not_hijacked():
    n = next(_n)
    manual = llm.save_endpoint({"name": f"Clash {n} · M", "api_style": "openai", "base_url": "http://manual/v1",
                                "key_mode": "none"}, "admin")
    text = f"name: Clash {n}\nmodels:\n  - name: M\n    provider: vllm\n    model: m\n    apiBase: http://c{n}.corp.example/v1\n"
    src = config_sources.save(text, "admin", "c.yaml")
    with session_scope() as s:
        assert s.get(ModelEndpoint, manual.id).base_url == "http://manual/v1"
        assert s.get(ModelEndpoint, src.endpoint_ids[0]).name.endswith(f"(c{n}.corp.example)")


def test_delete_source_keeps_or_removes_endpoints():
    n = next(_n)
    text = f"name: Del {n}\nmodels:\n  - name: M\n    provider: vllm\n    model: m\n    apiBase: http://d{n}.corp.example/v1\n"
    src = config_sources.save(text, "admin", "c.yaml")
    config_sources.delete(src.id, "admin")
    with session_scope() as s:
        ep = s.get(ModelEndpoint, src.endpoint_ids[0])
        assert ep and ep.source_id is None and not ep.managed
    src = config_sources.save(text.replace("Del", "Del2"), "admin", "c.yaml")
    config_sources.delete(src.id, "admin", delete_endpoints=True)
    with session_scope() as s:
        assert s.get(ModelEndpoint, src.endpoint_ids[0]) is None


def test_uploading_the_same_company_file_again_is_a_new_version(ca_pem):
    text = unique(EXAMPLE)
    first = config_sources.save(text, "admin", "config.yaml", ca_files={"ca03_base64_fullrootchain.pem": ca_pem})
    assert config_sources.plan(text, "config.yaml")["revision"] == 2
    second = config_sources.save(text, "admin", "config (1).yaml")
    assert second.id == first.id and second.revision == 2 and second.endpoint_ids == first.endpoint_ids
