"""Company LLM provisioning: per-user issued keys, model catalog and defaults, config file, health checks."""

import itertools

import pytest
import yaml

from databridge.ai import fake_llm, providers
from databridge.ai.fake_llm import mock_transport
from databridge.config import settings
from databridge.core.db import session_scope
from databridge.core.models import ModelEndpoint, UserEndpointKey
from databridge.services import ai_config, ai_health, llm, users
from databridge.services import workflows as wf_svc

_n = itertools.count(1)


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setattr(providers, "TRANSPORT", mock_transport())
    monkeypatch.setattr(settings, "ai_allow_private_urls", True)
    fake_llm.KEYS_SEEN.clear()


def make_user(role="designer"):
    u, _ = users.create_user(f"co.{role}{next(_n)}", role, "test", password="Company-LLM-2026!", must_change=False)
    return u


def gateway(**extra):
    values = {"name": f"Company gateway {next(_n)}", "api_style": "openai", "base_url": "http://llm.test/secure/v1",
              "network": "internal", "key_mode": "per_user", "allowed_roles": ["admin", "designer"]}
    values.update(extra)
    return llm.save_endpoint(values, "admin")


# ------------------------------------------------------------------ per-user keys


def test_per_user_keys_are_used_per_caller():
    ep = gateway()
    ok, msg = llm.test_endpoint(ep.id, "admin")
    assert ok and "Reachable" in msg  # no discovery key: TLS/network fine, models need a key
    ann, bob = make_user(), make_user()
    assert not [o for o in llm.available_models(ann) if o.route == ep.name]  # no key yet: not offered
    with pytest.raises(llm.AIError, match="didn't work"):
        llm.save_my_endpoint_key(ann, ep.id, "wrong-key")
    llm.save_my_endpoint_key(ann, ep.id, "user-ann-123456")
    llm.save_my_endpoint_key(bob, ep.id, "user-bob-654321")
    with session_scope() as s:
        row = s.query(UserEndpointKey).filter_by(user_id=ann.id).one()
        assert "user-ann" not in row.secret and row.key_hint == "3456"  # stored encrypted
    llm.save_catalog(ep.id, [{"id": "fake-small", "enabled": True}], None, "admin")
    ref = f"endpoint:{ep.id}/fake-small"
    fake_llm.KEYS_SEEN.clear()
    llm.chat(ann, ref, "hi")
    llm.chat(bob, ref, "hi")
    assert fake_llm.KEYS_SEEN == ["user-ann-123456", "user-bob-654321"]  # each call used its caller's key
    carl = make_user()
    with pytest.raises(llm.AIError, match="add yours"):
        llm.chat(carl, ref, "hi")
    viewer = make_user("viewer")
    with pytest.raises(llm.AIError):
        llm.save_my_endpoint_key(viewer, ep.id, "user-v-1")
    llm.delete_my_endpoint_key(ann, ep.id)
    with pytest.raises(llm.AIError, match="add yours"):
        llm.chat(ann, ref, "hi")


def test_workflow_runs_with_owner_key_and_discovery_key_only_for_admin_checks():
    ep = gateway(api_key="good-key")  # discovery key: model listing and health checks only
    assert llm.test_endpoint(ep.id, "admin")[0]
    with session_scope() as s:
        assert s.get(ModelEndpoint, ep.id).models == ["fake-large", "fake-small"]
    owner = make_user()
    with pytest.raises(llm.AIError, match="add yours"):  # the discovery key is never lent to users
        llm.chat(owner, f"endpoint:{ep.id}/fake-small", "hi")
    llm.save_my_endpoint_key(owner, ep.id, "user-owner-777777")
    wf = wf_svc.create_workflow(f"Ask {next(_n)}", owner.username, template="ask")
    spec = wf.spec
    nodes = {n["id"]: n for n in spec["nodes"]}
    nodes["answer"]["config"]["model"] = f"endpoint:{ep.id}/fake-small"
    nodes["out"]["config"]["name"] = f"Company answers {wf.id}"
    wf_svc.save_spec(wf.id, spec, owner.username)
    wf_svc.publish(wf.id, owner)
    fake_llm.KEYS_SEEN.clear()
    run = wf_svc.run(wf.id, None, {"question": "hi"}, trigger="api")  # API runs act as the owner
    assert run.status == "ok" and fake_llm.KEYS_SEEN == ["user-owner-777777"]


def test_shared_and_no_key_modes():
    shared = gateway(key_mode="shared", api_key="good-key", base_url="http://llm.test/secure/v1")
    none = gateway(key_mode="none", base_url="http://llm.test/v1")
    u = make_user()
    for ep in (shared, none):
        llm.test_endpoint(ep.id, "admin")
        assert llm.chat(u, f"endpoint:{ep.id}/fake-small", "hi").text == "Echo: hi"
    with pytest.raises(llm.AIError, match="company key"):
        llm.save_my_endpoint_key(u, shared.id, "user-x-1")


# ------------------------------------------------------------------ catalog, approval, defaults


def test_catalog_approval_roles_and_labels():
    ep = gateway(key_mode="none", base_url="http://llm.test/v1", approval="approved")
    llm.test_endpoint(ep.id, "admin")
    designer, admin = make_user(), make_user("admin")
    assert not [o for o in llm.available_models(designer) if o.route == ep.name]  # discovered, not approved yet
    with pytest.raises(llm.AIError, match="not approved"):
        llm.chat(designer, f"endpoint:{ep.id}/fake-small", "hi")
    llm.save_catalog(ep.id, [
        {"id": "fake-small", "label": "Small (fast)", "description": "classification", "enabled": True},
        {"id": "fake-large", "label": "Large", "enabled": True, "roles": ["admin"]},
    ], "approved", "admin")
    d_opts = {o.model: o for o in llm.available_models(designer) if o.route == ep.name}
    a_opts = {o.model for o in llm.available_models(admin) if o.route == ep.name}
    assert set(d_opts) == {"fake-small"} and a_opts == {"fake-small", "fake-large"}
    assert d_opts["fake-small"].label.startswith("Small (fast)")
    with pytest.raises(llm.AIError, match="not approved for your role"):
        llm.chat(designer, f"endpoint:{ep.id}/fake-large", "hi")
    open_ep = gateway(key_mode="none", base_url="http://llm.test/v1")
    llm.test_endpoint(open_ep.id, "admin")
    assert {o.model for o in llm.available_models(designer) if o.route == open_ep.name} == {"fake-small",
                                                                                              "fake-large"}


def test_default_models_per_task():
    ep = gateway(key_mode="none", base_url="http://llm.test/v1")
    llm.test_endpoint(ep.id, "admin")
    u = make_user()
    llm.set_defaults({"workflow": f"endpoint:{ep.id}/fake-large"}, "admin")
    assert llm.default_model(u, "workflow") == f"endpoint:{ep.id}/fake-large"
    assert llm.default_model(u, "playground") == llm.available_models(u)[0].ref  # no default: first usable
    llm.set_defaults({"workflow": "endpoint:99999/nope"}, "admin")
    assert llm.default_model(u, "workflow") == llm.available_models(u)[0].ref  # unusable default: fallback


# ------------------------------------------------------------------ config file


def test_config_file_apply_export_roundtrip(tmp_path, monkeypatch):
    ca = tmp_path / "ca.pem"
    from tests.test_tls import _cert, _name, _pem  # noqa: PLC0415 - reuse the certificate helpers
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    ca.write_bytes(_pem(_cert("Corp Root CA", _name("Corp Root CA"), key, key.public_key(), ca=True, days=400)))
    monkeypatch.setenv("CORP_DISCOVERY_KEY", "good-key")
    name = f"Corp gateway {next(_n)}"
    text = f"""
version: 1
endpoints:
  - name: {name}
    base_url: https://llm.corp.test/v1/chat/completions
    key_mode: per_user
    api_key_env: CORP_DISCOVERY_KEY
    allowed_roles: [admin, designer]
    tls:
      ca_file: {ca}
    approval: approved
    models:
      - id: fake-small
        label: Small
        description: fast model
      - fake-large
defaults:
  workflow: {name} / fake-small
"""
    preview = ai_config.apply(text, "admin", dry_run=True)
    assert preview[0].startswith("create") and not [e for e in llm.list_endpoints() if e.name == name]
    report = ai_config.apply(text, "admin")
    assert any(line.startswith("defaults") for line in report)
    ep = next(e for e in llm.list_endpoints() if e.name == name)
    assert ep.managed and ep.key_mode == "per_user" and ep.tls_verify == "custom" and ep.base_url.endswith("/v1")
    assert ep.secret and "good-key" not in ep.secret
    assert [c["label"] for c in llm.catalog_of(ep)] == ["Small", ""]
    assert llm.get_defaults()["workflow"] == f"endpoint:{ep.id}/fake-small"
    exported = ai_config.export()
    assert "good-key" not in exported and "SET_ME_" in exported and "BEGIN CERTIFICATE" in exported
    doc = yaml.safe_load(exported)
    mine = next(e for e in doc["endpoints"] if e["name"] == name)
    assert mine["key_mode"] == "per_user" and mine["models"][0]["id"] == "fake-small"
    assert doc["defaults"]["workflow"] == f"{name} / fake-small"
    ai_config.apply(text.replace("approval: approved", "approval: open"), "admin")  # re-apply updates in place
    assert len([e for e in llm.list_endpoints() if e.name == name]) == 1


@pytest.mark.parametrize("bad,message", [
    ("endpoints:\n  - name: X\n    base_url: http://a/v1\n    api_key: abc", "don't put keys"),
    ("endpoints:\n  - name: X\n    base_url: http://a/v1\n    colour: red", "unknown keys"),
    ("endpoints:\n  - name: X\n    base_url: http://a/v1\n    api_key_env: NOT_SET_ANYWHERE", "not set"),
    ("endpoints:\n  - name: X\n    base_url: http://a/v1\n    tls: {ca_file: /nope.pem}", "file not found"),
    ("endpoints:\n  - name: X\n    base_url: http://a/v1\n    key_mode: sometimes", "key_mode"),
    ("endpoints:\n  - name: X\n    base_url: http://a/v1\n    allowed_roles: [boss]", "unknown roles"),
    ("endpoints: []\ndefaults:\n  workflow: Nowhere / m", "no endpoint named"),
    ("- just a list", "must be a mapping"),
])
def test_config_file_errors_change_nothing(bad, message):
    before = len(llm.list_endpoints())
    with pytest.raises(ai_config.ConfigError, match=message):
        ai_config.apply(bad, "admin")
    assert len(llm.list_endpoints()) == before


# ------------------------------------------------------------------ health checks


def test_health_checks_record_and_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(settings, "ai_alert_webhook", "https://hooks.example/alert")
    monkeypatch.setattr(ai_health.httpx, "post", lambda url, json, timeout: sent.append((url, json)))
    ok_ep = gateway(key_mode="none", base_url="http://llm.test/v1")
    keyed = gateway()  # per-user, no discovery key: reachable -> warning
    results = dict(ai_health.run_checks())
    assert results[ok_ep.name]["status"] == "ok" and results[ok_ep.name]["models"] == 2
    assert results[keyed.name]["status"] == "warning" and "without a key" in results[keyed.name]["message"]
    # the endpoint goes down: state change -> alert
    llm.save_endpoint({"name": ok_ep.name, "api_style": "openai", "base_url": "http://llm.test/v1",
                       "api_key": "revoked-key", "key_mode": "shared"}, "admin", ok_ep.id)
    ai_health.run_checks(ok_ep.id)
    assert sent and "DOWN" in sent[-1][1]["text"] and ok_ep.name in sent[-1][1]["text"]
    assert any(ok_ep.name in p for p in ai_health.problems())
    llm.save_endpoint({"name": ok_ep.name, "api_style": "openai", "base_url": "http://llm.test/v1",
                       "clear_key": True, "key_mode": "none"}, "admin", ok_ep.id)
    ai_health.run_checks(ok_ep.id)
    assert "back up" in sent[-1][1]["text"]
    with session_scope() as s:
        history = s.get(ModelEndpoint, ok_ep.id).health_history
    assert [h["status"] for h in history] == ["ok", "failed", "ok"]


def test_health_check_reads_certificate_expiry(monkeypatch):
    from tests import test_tls

    pki = test_tls.pki.__wrapped__(_TmpFactory())  # generated CA + server cert valid 30 days
    url, server = test_tls._serve(pki, require_client_cert=False)
    monkeypatch.setattr(providers, "TRANSPORT", None)
    try:
        ep = gateway(key_mode="none", base_url=url, tls_verify="custom", ca_data=pki["ca_pem"])
        monkeypatch.setattr(settings, "ai_cert_warn_days", 45)
        sent = []
        monkeypatch.setattr(ai_health, "_alert", sent.append)
        result = dict(ai_health.run_checks(ep.id))[ep.name]
        assert result["cert_days"] in (29, 30) and result["status"] == "warning"
        assert sent and "expires in" in sent[0]
        ai_health.run_checks(ep.id)
        assert len(sent) == 1  # certificate warnings alert once a day
    finally:
        server.should_exit = True


class _TmpFactory:
    def mktemp(self, name):
        import pathlib
        import tempfile

        return pathlib.Path(tempfile.mkdtemp(prefix=name))


def test_exported_config_reimports(monkeypatch):
    ep = gateway(api_key="good-key", approval="approved")
    llm.test_endpoint(ep.id, "admin")
    llm.save_catalog(ep.id, [{"id": "fake-large", "label": "Big", "enabled": True}], "approved", "admin")
    doc = yaml.safe_load(ai_config.export())
    doc["endpoints"] = [e for e in doc["endpoints"] if e["name"] == ep.name]
    doc.pop("defaults", None)
    monkeypatch.setenv(doc["endpoints"][0]["api_key_env"], "good-key")
    exported = yaml.safe_dump(doc)
    report = ai_config.apply(exported, "admin")
    assert any(line.startswith(f"update {ep.name}") for line in report)
    again = next(e for e in llm.list_endpoints() if e.name == ep.name)
    assert again.approval == "approved" and llm.catalog_of(again)[0]["label"] == "Big"


def test_shipped_example_config_is_valid(tmp_path, monkeypatch):
    from pathlib import Path

    from tests.test_tls import _cert, _name, _pem
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    ca = tmp_path / "ca.pem"
    ca.write_bytes(_pem(_cert("Corp Root", _name("Corp Root"), key, key.public_key(), ca=True)))
    text = (Path(__file__).resolve().parents[1] / "deploy" / "llm-config.example.yaml").read_text()
    text = text.replace("/etc/databridge/certs/corp-root-ca.pem", str(ca))
    monkeypatch.setenv("LLM_GATEWAY_DISCOVERY_KEY", "good-key")
    monkeypatch.setenv("VLLM_API_KEY", "good-key")
    report = ai_config.apply(text, "admin", dry_run=True)
    assert len(report) == 2 and "3 catalog models" in report[0]
