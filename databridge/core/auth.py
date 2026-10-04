"""Password hashing, password policy and role permissions."""

import base64
import hashlib
import hmac
import re
import secrets

from databridge.config import settings

# ------------------------------------------------------------------ roles

ROLES = {
    "admin": "Admin: everything, including users, connections, API keys, shared AI models and the audit log",
    "designer": "Designer: sources, targets, mappings, endpoints, AI prompts; can use existing connections",
    "viewer": "Viewer: read-only; can view data, mappings and test endpoints",
}

PERMISSIONS: dict[str, set[str]] = {
    "view": {"admin", "designer", "viewer"},
    "design": {"admin", "designer"},  # create/edit sources, targets, mappings, endpoints; publish; add from explorer
    "use_connections": {"admin", "designer"},  # browse connections in the explorer
    "manage_connections": {"admin"},  # create/edit/delete connections (they hold credentials)
    "manage_keys": {"admin"},
    "manage_users": {"admin"},
    "view_audit": {"admin"},
    "use_ai": {"admin", "designer"},  # AI section: playground, prompts, personal API keys, AI suggestions
    "manage_models": {"admin"},  # shared model endpoints (local LLMs, company gateway), all usage
}


def can(role: str | None, permission: str) -> bool:
    return bool(role) and role in PERMISSIONS.get(permission, set())


# ------------------------------------------------------------------ passwords (scrypt, stdlib only)

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


# Used to spend the same time on unknown usernames as on real ones (no user enumeration by timing).
DUMMY_HASH = hash_password(secrets.token_hex(8))

_COMMON = {"password", "password1", "welcome1", "letmein", "qwerty123", "admin12345", "changeme", "databridge"}


def password_problems(password: str, username: str = "") -> list[str]:
    """Empty list = acceptable."""
    problems = []
    if len(password) < settings.min_password_length:
        problems.append(f"at least {settings.min_password_length} characters")
    kinds = sum(bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if kinds < 3:
        problems.append("at least three of: lower case, upper case, digit, symbol")
    if username and username.lower() in password.lower():
        problems.append("must not contain the user name")
    if password.lower() in _COMMON:
        problems.append("too common")
    return problems


def generate_password(length: int = 16) -> str:
    """Temporary password that satisfies the policy, without look-alike characters."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    while True:
        core = "".join(secrets.choice(alphabet) for _ in range(length - 2))
        pw = core + secrets.choice("23456789") + secrets.choice("!@#%*-+")
        if not password_problems(pw):
            return pw


def new_session_token() -> tuple[str, str]:
    """Returns (raw_token, sha256_hash)."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
