"""TLS for model endpoints: custom CA certificates and client certificates (mutual TLS).

Local LLM providers often use a self-signed or company-CA server certificate, and some require the
caller to present a client certificate. This module:
  * accepts PEM, DER (.cer/.crt) and PKCS#12 (.p12/.pfx) files,
  * validates them (parseable, key matches certificate, expiry),
  * builds an ssl.SSLContext for httpx.

The client private key is stored Fernet-encrypted in the database. When a context is built, the key is
written to a private temp file *encrypted with a random one-time password*, loaded, and deleted at
once, so an unencrypted key never touches the disk.
"""

import hashlib
import os
import secrets
import ssl
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

VERIFY_MODES = {
    "system": "Trusted public CAs (default)",
    "custom": "Trust the provider's CA certificate",
    "off": "Don't verify (not recommended)",
}


class TLSConfigError(ValueError):
    pass


@dataclass
class CertInfo:
    subject: str
    issuer: str
    not_after: str
    days_left: int
    is_ca: bool

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ parsing and validation


def load_certs(data: bytes) -> list[x509.Certificate]:
    """PEM (one or many) or a single DER certificate."""
    if not data.strip():
        raise TLSConfigError("The certificate file is empty")
    try:
        if b"-----BEGIN" in data:
            certs = x509.load_pem_x509_certificates(data.strip())
        else:  # binary DER: never strip, its last byte may happen to be a "whitespace" value
            certs = [x509.load_der_x509_certificate(data)]
    except ValueError as e:
        raise TLSConfigError("Not a valid certificate (expected PEM, CRT or CER)") from e
    if not certs:
        raise TLSConfigError("No certificate found in the file")
    return certs


def to_pem(certs: list[x509.Certificate]) -> str:
    return "".join(c.public_bytes(serialization.Encoding.PEM).decode() for c in certs)


def describe(pem: str | None) -> list[CertInfo]:
    if not pem:
        return []
    out = []
    now = datetime.now(timezone.utc)
    for c in load_certs(pem.encode()):
        try:
            is_ca = c.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        except x509.ExtensionNotFound:
            is_ca = False
        not_after = c.not_valid_after_utc
        out.append(CertInfo(subject=c.subject.rfc4514_string(), issuer=c.issuer.rfc4514_string(),
                            not_after=not_after.strftime("%Y-%m-%d"), days_left=(not_after - now).days, is_ca=is_ca))
    return out


def normalize_ca(data: bytes) -> str:
    """Returns PEM text for a CA bundle file (PEM or DER)."""
    return to_pem(load_certs(data))


def _load_key(data: bytes, password: str | None):
    pw = password.encode() if password else None
    try:
        if b"-----BEGIN" in data:
            return serialization.load_pem_private_key(data, password=pw)
        return serialization.load_der_private_key(data, password=pw)
    except TypeError as e:
        raise TLSConfigError("The private key is password-protected: enter its password") from e
    except ValueError as e:
        raise TLSConfigError("Could not read the private key (wrong password or not a key file)") from e


def _key_pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _same_key(cert: x509.Certificate, key) -> bool:
    fmt = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return cert.public_key().public_bytes(*fmt) == key.public_key().public_bytes(*fmt)


def client_identity(cert_data: bytes | None = None, key_data: bytes | None = None, password: str | None = None,
                    p12_data: bytes | None = None) -> tuple[str, str]:
    """Validates a client certificate + key (separate files, or one PKCS#12 bundle).

    Returns (certificate chain PEM, unencrypted key PEM) - the caller must encrypt the key before storing.
    """
    if p12_data:
        try:
            key, cert, extra = pkcs12.load_key_and_certificates(p12_data, password.encode() if password else None)
        except ValueError as e:
            raise TLSConfigError("Could not open the .p12/.pfx file (wrong password?)") from e
        if not key or not cert:
            raise TLSConfigError("The .p12/.pfx file must contain a certificate and its private key")
        chain = [cert] + list(extra or [])
    else:
        if not cert_data or not key_data:
            raise TLSConfigError("Upload both the client certificate and its private key, or one .p12/.pfx file")
        chain = load_certs(cert_data)
        key = _load_key(key_data.strip() if b"-----BEGIN" in key_data else key_data, password)
        cert = chain[0]
    if not _same_key(cert, key):
        raise TLSConfigError("The private key does not belong to the client certificate")
    if cert.not_valid_after_utc < datetime.now(timezone.utc):
        raise TLSConfigError(f"The client certificate expired on {cert.not_valid_after_utc:%Y-%m-%d}")
    return to_pem(chain), _key_pem(key)


# ------------------------------------------------------------------ SSL context


_cache: dict[str, ssl.SSLContext] = {}
_lock = threading.Lock()


def build_context(verify: str = "system", ca_pem: str | None = None, check_hostname: bool = True,
                  client_cert_pem: str | None = None, client_key_pem: str | None = None,
                  extra_ca_file: str | None = None) -> ssl.SSLContext:
    """Builds (and caches) an SSLContext for the given settings."""
    fingerprint = hashlib.sha256(repr((verify, ca_pem, check_hostname, client_cert_pem, client_key_pem,
                                       extra_ca_file)).encode()).hexdigest()
    with _lock:
        if fingerprint in _cache:
            return _cache[fingerprint]
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_default_certs()  # also trust the operating system's store (company CAs installed there)
    if extra_ca_file:
        ctx.load_verify_locations(cafile=extra_ca_file)
    if verify == "off":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        if verify == "custom":
            if not ca_pem:
                raise TLSConfigError("Upload the provider's CA certificate or choose another verification mode")
            ctx.load_verify_locations(cadata=ca_pem)
        ctx.check_hostname = bool(check_hostname)
    if client_cert_pem and client_key_pem:
        _load_client_cert(ctx, client_cert_pem, client_key_pem)
    with _lock:
        _cache[fingerprint] = ctx
    return ctx


def _load_client_cert(ctx: ssl.SSLContext, cert_pem: str, key_pem: str) -> None:
    """load_cert_chain needs files: write the key encrypted with a one-time password and delete immediately."""
    one_time = secrets.token_urlsafe(24)
    key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    encrypted = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.BestAvailableEncryption(one_time.encode()))
    tmpdir = tempfile.mkdtemp(prefix="databridge-tls-")
    os.chmod(tmpdir, 0o700)
    cert_path, key_path = os.path.join(tmpdir, "client.crt"), os.path.join(tmpdir, "client.key")
    try:
        for path, data in ((cert_path, cert_pem.encode()), (key_path, encrypted)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        ctx.load_cert_chain(cert_path, key_path, password=one_time)
    except ssl.SSLError as e:
        raise TLSConfigError(f"Could not load the client certificate: {e.reason or e}") from e
    finally:
        for path in (cert_path, key_path):
            if os.path.exists(path):
                os.remove(path)
        os.rmdir(tmpdir)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def friendly_error(exc: BaseException) -> str | None:
    """Turns TLS failures into advice; None if the error isn't TLS-related."""
    text = " ".join(str(e) for e in _chain(exc)).lower()
    if "certificate_verify_failed" in text or "certificate verify failed" in text:
        if "hostname" in text or "ip address mismatch" in text:
            return ("TLS: the server certificate does not match the host name in the URL. Use the host name on "
                    "the certificate, or turn off host name checking for this endpoint.")
        if "expired" in text:
            return "TLS: the server certificate has expired. Ask the provider to renew it."
        return ("TLS: the server certificate is not trusted. Upload the provider's CA certificate "
                "(choose 'Trust the provider's CA certificate').")
    if "certificate required" in text or "alert_certificate_required" in text or "peer did not return a certificate" in text:
        return "TLS: the server requires a client certificate. Upload the certificate and key from the provider."
    if "unknown ca" in text or "tlsv1_alert_unknown_ca" in text:
        return ("TLS: the server rejected the client certificate (it was not issued by a CA the server trusts). "
                "Check you uploaded the certificate the provider issued for this client.")
    if "bad certificate" in text or "sslv3_alert_bad_certificate" in text:
        return "TLS: the server rejected the client certificate. Check it hasn't expired or been revoked."
    if "wrong_version_number" in text or "wrong version number" in text:
        return "TLS: the server doesn't speak HTTPS on this port. Try http:// or check the port."
    if "handshake failure" in text:
        return "TLS handshake failed. The server may require a client certificate or a newer TLS version."
    return None


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__
