import pytest

from databridge.ai import providers
from databridge.ai.fake_llm import GOOD_KEY, mock_transport
from databridge.ai.providers import ChatRequest, ProviderError, check_public_url, extract_json, make_provider
from databridge.ai.templating import PromptTemplateError, render, variables_in
from databridge.config import settings
from databridge.core.db import session_scope
from databridge.core.models import LlmCall, UserCredential
from databridge.services import ai_assist, llm, prompts, users


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    monkeypatch.setattr(providers, "TRANSPORT", mock_transport())
    monkeypatch.setattr(settings, "ai_allow_private_urls", True)  # mock hosts don't resolve
    yield


@pytest.fixture
def designer():
    u, _ = users.create_user(f"ai.designer{len(users.list_users())}", "designer", "test", password="Des1gner-AI!x",
                             must_change=False)
    return u


# ------------------------------------------------------------------ providers


@pytest.mark.parametrize("style,base", [("openai", "http://llm.test/v1"), ("anthropic", "https://api.anthropic.test")])
def test_providers_complete_and_list(style, base):
    p = make_provider(style, base_url=base, api_key=GOOD_KEY)
    assert p.list_models() == ["fake-large", "fake-small"]
    r = p.complete(ChatRequest(model="fake-small", user="hello there", system="be brief"))
    assert r.text == "Echo: hello there" and r.prompt_tokens > 0 and r.completion_tokens > 0


def test_provider_errors_are_friendly():
    with pytest.raises(ProviderError, match="rejected"):
        make_provider("openai", base_url="http://llm.test/v1", api_key="bad").list_models()
    with pytest.raises(ProviderError, match="not found"):
        make_provider("openai", base_url="http://llm.test/v1").complete(ChatRequest(model="nope", user="x"))


def test_extract_json():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": [1,2]} hope that helps') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_public_url_guard():
    with pytest.raises(ProviderError, match="https"):
        check_public_url("http://example.com/v1")
    with pytest.raises(ProviderError, match="private"):
        check_public_url("https://localhost/v1")
    with pytest.raises(ProviderError, match="private"):
        check_public_url("https://169.254.169.254/latest")


# ------------------------------------------------------------------ templating


def test_templates():
    assert variables_in("Hi {{ name }}", "{{ text }} and {{ name }}") == ["name", "text"]
    assert render("Hi {{ name|upper }}", {"name": "jo"}) == "Hi JO"
    with pytest.raises(PromptTemplateError):
        render("Hi {{ missing }}", {})
    with pytest.raises(PromptTemplateError):  # sandbox blocks internals
        render("{{ ''.__class__.__mro__[1].__subclasses__() }}", {})


# ------------------------------------------------------------------ gateway


def test_shared_endpoint_and_personal_key(designer):
    ep = llm.save_endpoint({"name": "Local Ollama", "api_style": "openai", "base_url": "http://ollama.test/v1",
                            "network": "internal", "allowed_roles": ["admin", "designer"]}, "admin")
    ok, msg = llm.test_endpoint(ep.id, "admin")
    assert ok and "2 model" in msg

    cred = llm.save_credential(designer.id, designer.username,
                               {"provider": "anthropic", "base_url": "https://api.anthropic.test", "api_key": GOOD_KEY})
    assert llm.test_credential(designer.id, cred.id, designer.username)[0]
    with session_scope() as s:
        stored = s.get(UserCredential, cred.id)
        assert GOOD_KEY not in stored.secret and stored.key_hint in ("-key", "set")

    refs = {o.ref for o in llm.available_models(designer)}
    assert f"endpoint:{ep.id}/fake-small" in refs and f"key:{cred.id}/fake-large" in refs

    r = llm.chat(designer, f"key:{cred.id}/fake-small", "ping", purpose="test")
    assert r.text == "Echo: ping"
    with session_scope() as s:
        row = s.query(LlmCall).filter(LlmCall.user_id == designer.id).order_by(LlmCall.id.desc()).first()
        assert row.status == "ok" and row.prompt_tokens > 0 and row.request_text == ""  # content not logged


def test_keys_are_private_and_roles_enforced(designer):
    other, _ = users.create_user("ai.other", "designer", "test", password="Oth3r-Designer!", must_change=False)
    cred = llm.save_credential(designer.id, designer.username,
                               {"provider": "openai", "base_url": "https://api.openai.test/v1", "api_key": GOOD_KEY})
    with pytest.raises(llm.AIError, match="someone else"):
        llm.chat(other, f"key:{cred.id}/fake-small", "steal")
    viewer, _ = users.create_user("ai.viewer", "viewer", "test", password="Vi3wer-Only!ai", must_change=False)
    assert llm.available_models(viewer) == []
    with pytest.raises(llm.AIError, match="role"):
        llm.chat(viewer, f"key:{cred.id}/fake-small", "hi")
    ep = llm.save_endpoint({"name": "Admins only", "api_style": "openai", "base_url": "http://a.test/v1",
                            "allowed_roles": ["admin"], "models": ["fake-small"]}, "admin")
    with pytest.raises(llm.AIError, match="role may not"):
        llm.chat(designer, f"endpoint:{ep.id}/fake-small", "hi")


def test_budget_and_errors_logged(designer, monkeypatch):
    cred = llm.save_credential(designer.id, designer.username,
                               {"provider": "openai", "base_url": "https://api.openai.test/v1", "api_key": GOOD_KEY})
    with pytest.raises(llm.AIError):
        llm.chat(designer, f"key:{cred.id}/missing-model", "x")
    llm.chat(designer, f"key:{cred.id}/fake-small", "one two three four five")
    monkeypatch.setattr(settings, "ai_monthly_token_limit", 1)
    with pytest.raises(llm.AIError, match="limit"):
        llm.chat(designer, f"key:{cred.id}/fake-small", "more")
    statuses = [c.status for c in llm.usage(designer)]
    assert {"ok", "error", "blocked"} <= set(statuses)


# ------------------------------------------------------------------ prompts


def test_prompt_library_versions():
    p = prompts.save_prompt({"name": "Summarise complaint", "system": "You are concise.",
                             "user": "Summarise for {{ team }}:\n<data>{{ text }}</data>",
                             "variables": [{"name": "team", "default": "support"}]}, "tester")
    assert [v["name"] for v in p.variables] == ["team", "text"] and p.variables[0]["default"] == "support"
    v1 = prompts.publish(p.id, "tester", "first")
    prompts.save_prompt({"name": "Summarise complaint", "system": "You are very concise.", "user": p.user,
                         "variables": p.variables}, "tester", p.id)
    prompts.publish(p.id, "tester")
    assert prompts.published("Summarise complaint")["system"] == "You are very concise."
    assert prompts.published("Summarise complaint", 1)["content_hash"] == v1.content_hash
    prompts.restore(p.id, 1, "tester")
    assert prompts.get_prompt(p.id).system == "You are concise."
    sys_text, user_text = prompts.render_prompt(p.system, p.user, {"team": "billing", "text": "late"})
    assert "billing" in user_text and "<data>late</data>" in user_text
    with pytest.raises(prompts.PromptError):
        prompts.save_prompt({"name": "Bad", "user": "{{ oops"}, "tester")


# ------------------------------------------------------------------ AI auto-map


def test_ai_automap(designer):
    ep = llm.save_endpoint({"name": "Mapper", "api_style": "openai", "base_url": "http://map.test/v1",
                            "network": "internal", "models": ["fake-large"]}, "admin")
    src = [{"name": "Cust No", "type": "string", "samples": ["00123"]}, {"name": "Order Dt", "type": "date"},
           {"name": "Amt", "type": "float"}, {"name": "Notes", "type": "string"}]
    tgt = [{"name": "customer_id", "type": "string"}, {"name": "order_date", "type": "date"},
           {"name": "amount_usd", "type": "decimal(12,2)"}, {"name": "region", "type": "string"}]
    out = ai_assist.suggest_mappings(designer, f"endpoint:{ep.id}/fake-large", src, tgt)
    pairs = {(s.source, s.target) for s in out}
    assert pairs == {("Cust No", "customer_id"), ("Order Dt", "order_date"), ("Amt", "amount_usd")}
    out = ai_assist.suggest_mappings(designer, f"endpoint:{ep.id}/fake-large", src, tgt, already_mapped={"customer_id"})
    assert "customer_id" not in {s.target for s in out}


def test_pasted_full_url_is_normalized():
    from databridge.ai.providers import normalize_base_url

    assert normalize_base_url("https://llm.corp/v1/chat/completions") == "https://llm.corp/v1"
    assert normalize_base_url("https://llm.corp/v1/chat/completions/") == "https://llm.corp/v1"
    assert normalize_base_url("http://localhost:11434/v1/models") == "http://localhost:11434/v1"
    assert normalize_base_url("https://llm.corp/openai/v1") == "https://llm.corp/openai/v1"
    assert normalize_base_url("https://api.anthropic.com/v1/messages", "anthropic") == "https://api.anthropic.com"
    assert normalize_base_url("https://gw.corp/anthropic/v1", "anthropic") == "https://gw.corp/anthropic"


def test_endpoint_saved_with_full_url_works():
    ep = llm.save_endpoint({"name": "Pasted URL", "api_style": "openai",
                            "base_url": "http://127.0.0.1:8799/v1/chat/completions", "network": "internal"}, "admin")
    assert ep.base_url == "http://127.0.0.1:8799/v1"
    ok, msg = llm.test_endpoint(ep.id, "admin")
    assert ok, msg
