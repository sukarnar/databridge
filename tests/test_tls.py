"""TLS to local LLM endpoints: custom CA, mutual TLS (client certificate), PKCS#12, friendly errors.

Spins up the fake LLM over real HTTPS (uvicorn + ssl) with certificates generated on the fly.
"""

import glob
import os
import socket
import ssl
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from databridge.ai import providers, tls
from databridge.ai.fake_llm import asgi_app
from databridge.core.db import session_scope
from databridge.core.models import ModelEndpoint
from databridge.services import llm, users


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject, issuer_name, issuer_key, public_key, ca=False, sans=None, days=30, client=False):
    now = datetime.now(timezone.utc)
    b = (x509.CertificateBuilder().subject_name(_name(subject)).issuer_name(issuer_name)
         .public_key(public_key).serial_number(x509.random_serial_number())
         .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    # Key identifiers: Python 3.13+ verifies strictly (VERIFY_X509_STRICT) and rejects certificates without them
    b = b.add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
    b = b.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
    if ca:
        b = b.add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                          content_commitment=False, key_encipherment=False, data_encipherment=False,
                                          key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
    if sans:
        b = b.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    if client:
        b = b.add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
    return b.sign(issuer_key, hashes.SHA256())


def _pem(obj) -> bytes:
    if isinstance(obj, x509.Certificate):
        return obj.public_bytes(serialization.Encoding.PEM)
    return obj.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert("Local LLM Provider CA", _name("Local LLM Provider CA"), ca_key, ca_key.public_key(), ca=True)
    srv_key = ec.generate_private_key(ec.SECP256R1())
    srv = _cert("llm.local", ca.subject, ca_key, srv_key.public_key(),
                sans=[x509.DNSName("localhost")])
    cli_key = ec.generate_private_key(ec.SECP256R1())
    cli = _cert("databridge-client", ca.subject, ca_key, cli_key.public_key(), client=True)
    other_key = ec.generate_private_key(ec.SECP256R1())
    files = {"ca": ca, "srv": srv, "srv_key": srv_key, "cli": cli, "cli_key": cli_key}
    paths = {}
    for k, v in files.items():
        p = d / f"{k}.pem"
        p.write_bytes(_pem(v))
        paths[k] = str(p)
    p12 = pkcs12.serialize_key_and_certificates(b"databridge", cli_key, cli, [ca],
                                                serialization.BestAvailableEncryption(b"p12-pass"))
    return {"paths": paths, "ca_pem": _pem(ca), "ca_der": ca.public_bytes(serialization.Encoding.DER),
            "cli_pem": _pem(cli), "cli_key_pem": _pem(cli_key), "p12": p12, "other_key_pem": _pem(other_key),
            "cli_key_encrypted": cli_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.BestAvailableEncryption(b"key-pass"))}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(pki, require_client_cert: bool) -> tuple[str, uvicorn.Server]:
    port = _free_port()
    cfg = uvicorn.Config(asgi_app(), host="127.0.0.1", port=port, log_level="error",
                         ssl_certfile=pki["paths"]["srv"], ssl_keyfile=pki["paths"]["srv_key"],
                         ssl_ca_certs=pki["paths"]["ca"] if require_client_cert else None,
                         ssl_cert_reqs=ssl.CERT_REQUIRED if require_client_cert else ssl.CERT_NONE)
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return f"https://localhost:{port}/v1", server


@pytest.fixture(scope="module")
def tls_server(pki):
    url, server = _serve(pki, require_client_cert=False)
    yield url
    server.should_exit = True


@pytest.fixture(scope="module")
def mtls_server(pki):
    url, server = _serve(pki, require_client_cert=True)
    yield url
    server.should_exit = True


@pytest.fixture(autouse=True)
def real_network(monkeypatch):
    monkeypatch.setattr(providers, "TRANSPORT", None)
    tls.clear_cache()


@pytest.fixture
def admin_user():
    name = f"tls.admin{len(users.list_users())}"
    u, _ = users.create_user(name, "admin", "test", password="Tls-Admin-2026!", must_change=False)
    return u


def _endpoint(url, **tls_values):
    return llm.save_endpoint({"name": f"Local LLM {time.time_ns()}", "api_style": "openai", "base_url": url,
                              "network": "internal", **tls_values}, "admin")


def test_untrusted_server_gives_advice(tls_server):
    ep = _endpoint(tls_server)
    ok, msg = llm.test_endpoint(ep.id, "admin")
    assert not ok and "not trusted" in msg and "CA certificate" in msg


def test_custom_ca_pem_and_der(tls_server, pki, admin_user):
    ep = _endpoint(tls_server, tls_verify="custom", ca_data=pki["ca_pem"])
    ok, msg = llm.test_endpoint(ep.id, "admin")
    assert ok, msg
    assert llm.chat(admin_user, f"endpoint:{ep.id}/fake-small", "over tls").text == "Echo: over tls"
    ep2 = _endpoint(tls_server, tls_verify="custom", ca_data=pki["ca_der"])  # .cer (DER) works too
    assert llm.test_endpoint(ep2.id, "admin")[0]
    with session_scope() as s:
        info = s.get(ModelEndpoint, ep2.id).tls_info
    assert info["ca"][0]["subject"] == "CN=Local LLM Provider CA" and info["ca"][0]["is_ca"]


def test_hostname_mismatch_and_override(tls_server, pki):
    bad_host = tls_server.replace("localhost", "127.0.0.1")  # the certificate only names "localhost"
    ok, msg = llm.test_endpoint(_endpoint(bad_host, tls_verify="custom", ca_data=pki["ca_pem"]).id, "admin")
    assert not ok and "host name" in msg
    ok, msg = llm.test_endpoint(_endpoint(bad_host, tls_verify="custom", ca_data=pki["ca_pem"],
                                          tls_check_hostname=False).id, "admin")
    assert ok, msg


def test_verify_off(tls_server):
    assert llm.test_endpoint(_endpoint(tls_server, tls_verify="off").id, "admin")[0]


def test_mtls_requires_client_cert(mtls_server, pki):
    ok, msg = llm.test_endpoint(_endpoint(mtls_server, tls_verify="custom", ca_data=pki["ca_pem"]).id, "admin")
    assert not ok and "TLS" in msg


def test_mtls_with_pem_files(mtls_server, pki, admin_user):
    ep = _endpoint(mtls_server, tls_verify="custom", ca_data=pki["ca_pem"], client_cert_data=pki["cli_pem"],
                   client_key_data=pki["cli_key_pem"])
    ok, msg = llm.test_endpoint(ep.id, "admin")
    assert ok, msg
    assert llm.chat(admin_user, f"endpoint:{ep.id}/fake-large", "mutual").text == "Echo: mutual"
    with session_scope() as s:
        row = s.get(ModelEndpoint, ep.id)
        assert "PRIVATE KEY" not in (row.client_key_secret or "")  # stored encrypted
        assert row.tls_info["client"][0]["subject"] == "CN=databridge-client"
    assert not glob.glob(os.path.join(tempfile.gettempdir(), "databridge-tls-*"))  # no key files left behind


def test_mtls_with_encrypted_key_and_p12(mtls_server, pki):
    ep = _endpoint(mtls_server, tls_verify="custom", ca_data=pki["ca_pem"], client_cert_data=pki["cli_pem"],
                   client_key_data=pki["cli_key_encrypted"], client_key_password="key-pass")
    assert llm.test_endpoint(ep.id, "admin")[0]
    ep = _endpoint(mtls_server, tls_verify="custom", ca_data=pki["ca_pem"], client_p12_data=pki["p12"],
                   client_key_password="p12-pass")
    assert llm.test_endpoint(ep.id, "admin")[0]


def test_bad_uploads_rejected(pki):
    with pytest.raises(llm.AIError, match="does not belong"):
        _endpoint("https://x.test/v1", client_cert_data=pki["cli_pem"], client_key_data=pki["other_key_pem"])
    with pytest.raises(llm.AIError, match="password"):
        _endpoint("https://x.test/v1", client_cert_data=pki["cli_pem"], client_key_data=pki["cli_key_encrypted"])
    with pytest.raises(llm.AIError, match="wrong password"):
        _endpoint("https://x.test/v1", client_p12_data=pki["p12"], client_key_password="nope")
    with pytest.raises(llm.AIError, match="Not a valid certificate"):
        _endpoint("https://x.test/v1", tls_verify="custom", ca_data=b"hello")
    with pytest.raises(llm.AIError, match="Upload the provider's CA"):
        _endpoint("https://x.test/v1", tls_verify="custom")


def test_remove_client_cert(mtls_server, pki):
    ep = _endpoint(mtls_server, tls_verify="custom", ca_data=pki["ca_pem"], client_p12_data=pki["p12"],
                   client_key_password="p12-pass")
    llm.save_endpoint({"name": ep.name, "api_style": "openai", "base_url": mtls_server, "clear_client_cert": True},
                      "admin", ep.id)
    with session_scope() as s:
        assert s.get(ModelEndpoint, ep.id).client_cert_pem is None
    assert not llm.test_endpoint(ep.id, "admin")[0]


def test_der_certificate_ending_in_whitespace_byte():
    """Binary DER must not be stripped: regression for a ~1-in-40 random failure on DER uploads."""
    from cryptography.hazmat.primitives.serialization import Encoding

    key = ec.generate_private_key(ec.SECP256R1())
    for _ in range(400):  # signatures are random: find a certificate whose DER ends in a whitespace byte
        cert = _cert("edge", _name("edge"), key, key.public_key(), ca=True)
        der = cert.public_bytes(Encoding.DER)
        if der[-1:] in (b" ", b"\n", b"\r", b"\t", b"\x0b", b"\x0c"):
            break
    else:
        pytest.skip("no such certificate generated")
    assert tls.load_certs(der)[0] == cert
