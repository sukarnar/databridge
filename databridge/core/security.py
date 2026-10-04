"""Secret encryption and API key helpers."""

import hashlib
import json
import secrets
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet

from databridge.config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = settings.secret_key.strip()
    if not key:
        path = settings.data_dir / "secret.key"
        if not path.exists():
            path.write_bytes(Fernet.generate_key())
        key = path.read_text().strip()
    return Fernet(key.encode())


def encrypt_json(data: dict[str, Any]) -> str | None:
    clean = {k: v for k, v in data.items() if v not in (None, "")}
    if not clean:
        return None
    return _fernet().encrypt(json.dumps(clean).encode()).decode()


def decrypt_json(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    return json.loads(_fernet().decrypt(token.encode()))


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_api_key() -> tuple[str, str, str]:
    """Returns (raw_key, display_prefix, sha256_hash)."""
    raw = "dbk_" + secrets.token_urlsafe(32)
    return raw, raw[:12], hash_key(raw)
