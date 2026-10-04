"""Connection management: create, update, test, browse."""

from typing import Any

from sqlalchemy import select

from databridge.connectors.base import Connector, Node, TestResult, split_config
from databridge.connectors.registry import build_connector, connector_class
from databridge.core.db import session_scope
from databridge.core.models import Connection
from databridge.core.security import decrypt_json, encrypt_json


def list_connections() -> list[Connection]:
    with session_scope() as s:
        return list(s.scalars(select(Connection).order_by(Connection.name)))


def get_connection(conn_id: int) -> Connection:
    with session_scope() as s:
        conn = s.get(Connection, conn_id)
        if not conn:
            raise LookupError(f"Connection {conn_id} not found")
        return conn


def readable_validation(e, model) -> str:
    """'Root: field required' instead of pydantic's dump."""
    parts = []
    for err in e.errors():
        field = str(err["loc"][0]) if err.get("loc") else ""
        label = field.replace("_", " ").capitalize() if field else "Settings"
        msg = err.get("msg", "invalid").removeprefix("Value error, ")
        msg = {"Field required": "this field is required"}.get(msg, msg)
        parts.append(f"{label}: {msg}")
    return "; ".join(parts) or "Check the settings"


def save_connection(name: str, type_name: str, values: dict[str, Any], conn_id: int | None = None) -> Connection:
    """Creates or updates a connection. Blank secret fields keep the stored secret."""
    from pydantic import ValidationError

    cls = connector_class(type_name)
    try:
        model = cls.config_model(**values)  # validate before saving
    except ValidationError as e:
        raise ValueError(readable_validation(e, cls.config_model)) from None
    cleaned = model.model_dump()
    values = {k: cleaned.get(k, v) for k, v in values.items()}  # e.g. a quoted Windows path, cleaned
    if type_name == "filesystem" and cleaned.get("file_pattern") not in (None, "*", values.get("file_pattern")):
        values["file_pattern"] = cleaned["file_pattern"]  # Root was a file: its folder + that file name
    if type_name == "filesystem" and cleaned.get("protocol") == "file":
        from pathlib import Path

        if not Path(cleaned["root"]).is_dir():
            raise ValueError(f"Root: \u201c{cleaned['root']}\u201d was not found on the DataBridge "
                             "server (the path is read on the machine running DataBridge, not in your browser).")
    plain, secret = split_config(cls, values)
    with session_scope() as s:
        conn = s.get(Connection, conn_id) if conn_id else Connection(name=name, type=type_name)
        if conn_id and not conn:
            raise LookupError(f"Connection {conn_id} not found")
        existing = decrypt_json(conn.secret) if conn_id else {}
        merged = {**existing, **{k: v for k, v in secret.items() if v not in (None, "")}}
        if conn_id:  # fields the form doesn't show (e.g. uploaded certificates) are kept
            kept = {k: v for k, v in (conn.config or {}).items() if k in cls.form_exclude and k not in plain}
            plain = {**kept, **plain}
        conn.name, conn.type, conn.config, conn.secret = name, type_name, plain, encrypt_json(merged)
        s.add(conn)
        s.flush()
        return conn


def delete_connection(conn_id: int) -> None:
    with session_scope() as s:
        conn = s.get(Connection, conn_id)
        if conn:
            s.delete(conn)


def connector_for(conn_id: int) -> Connector:
    conn = get_connection(conn_id)
    return build_connector(conn.type, conn.config, conn.secret)


def test_connection(conn_id: int) -> TestResult:
    connector = connector_for(conn_id)
    try:
        result = connector.test()
    finally:
        connector.close()
    with session_scope() as s:
        conn = s.get(Connection, conn_id)
        conn.last_test_ok, conn.last_test_message = result.ok, result.message
    return result


def browse(conn_id: int, ref: dict[str, Any] | None = None) -> list[Node]:
    connector = connector_for(conn_id)
    try:
        return connector.browse(ref)
    finally:
        connector.close()


def describe(conn_id: int, ref: dict[str, Any]) -> list[dict[str, Any]]:
    connector = connector_for(conn_id)
    try:
        return connector.describe(ref)
    finally:
        connector.close()


def preview(conn_id: int, ref: dict[str, Any], limit: int = 100):
    connector = connector_for(conn_id)
    try:
        return connector.preview(ref, limit)
    finally:
        connector.close()


def set_rest_certificates(conn_id: int, *, ca_data: bytes | None = None, cert_data: bytes | None = None,
                          key_data: bytes | None = None, key_password: str | None = None,
                          p12_data: bytes | None = None, clear_ca: bool = False,
                          clear_client: bool = False) -> dict[str, Any]:
    """Company CA and client certificate (mutual TLS) for a REST connection, validated before saving.

    The CA and client certificate are public and kept in the config; the private key is stored encrypted.
    Returns what is now configured ({"ca": [CertInfo...], "client": [CertInfo...]}).
    """
    from databridge.ai import tls

    with session_scope() as s:
        conn = s.get(Connection, conn_id)
        if not conn or conn.type != "rest":
            raise LookupError("REST connection not found")
        config, secret = dict(conn.config or {}), decrypt_json(conn.secret)
        if clear_ca:
            config.pop("ca_pem", None)
            if config.get("tls_verify") == "custom":
                config["tls_verify"] = "system"
        if ca_data:
            config["ca_pem"] = tls.normalize_ca(ca_data)
            config["tls_verify"] = "custom"
        if clear_client:
            config.pop("client_cert_pem", None)
            secret.pop("client_key_pem", None)
        if p12_data or cert_data or key_data:
            cert_pem, key_pem = tls.client_identity(cert_data, key_data, key_password, p12_data)
            config["client_cert_pem"], secret["client_key_pem"] = cert_pem, key_pem
        conn.config, conn.secret = config, encrypt_json(secret)
        tls.clear_cache()
        return {"ca": [c.to_dict() for c in tls.describe(config.get("ca_pem"))],
                "client": [c.to_dict() for c in tls.describe(config.get("client_cert_pem"))]}


def rest_certificates(conn_id: int) -> dict[str, Any]:
    from databridge.ai import tls

    conn = get_connection(conn_id)
    return {"ca": [c.to_dict() for c in tls.describe((conn.config or {}).get("ca_pem"))],
            "client": [c.to_dict() for c in tls.describe((conn.config or {}).get("client_cert_pem"))]}
