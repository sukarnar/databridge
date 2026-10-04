"""User accounts, sign-in with lockout, sessions and the audit log."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update

from databridge.config import settings
from databridge.core.auth import (
    DUMMY_HASH,
    ROLES,
    generate_password,
    hash_password,
    new_session_token,
    password_problems,
    token_hash,
    verify_password,
)
from databridge.core.db import session_scope
from databridge.core.models import AuditLog, User, UserSession

log = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._@-]{1,78}$")


class AuthError(Exception):
    """Shown to the user as-is."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; treat stored values as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ------------------------------------------------------------------ audit


def audit(username: str, action: str, target: str = "", detail: str = "", ip: str = "") -> None:
    try:
        with session_scope() as s:
            # Column limits (PostgreSQL enforces them): never let a long value lose the audit entry
            s.add(AuditLog(username=(username or "")[:80], action=action[:60], target=(target or "")[:200],
                           detail=detail, ip=(ip or "")[:64]))
    except Exception:  # noqa: BLE001 - auditing must never break the action itself
        log.exception("audit write failed")


def recent_audit(limit: int = 300, username: str | None = None) -> list[AuditLog]:
    with session_scope() as s:
        q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        if username:
            q = q.where(AuditLog.username == username)
        return list(s.scalars(q))


# ------------------------------------------------------------------ accounts


def list_users() -> list[User]:
    with session_scope() as s:
        return list(s.scalars(select(User).order_by(User.username)))


def get_user(user_id: int) -> User:
    with session_scope() as s:
        u = s.get(User, user_id)
        if not u:
            raise LookupError("User not found")
        return u


def count_users() -> int:
    with session_scope() as s:
        return s.scalar(select(func.count(User.id))) or 0


def _active_admins(s, exclude_id: int | None = None) -> int:
    q = select(func.count(User.id)).where(User.role == "admin", User.active.is_(True))
    if exclude_id:
        q = q.where(User.id != exclude_id)
    return s.scalar(q) or 0


def _check_username(username: str) -> str:
    u = username.strip().lower()
    if not USERNAME_RE.match(u):
        raise AuthError("User name: 2-79 characters, letters, digits, and . _ @ - (e.g. jane.doe)")
    return u


def create_user(username: str, role: str, created_by: str, full_name: str = "", email: str = "",
                password: str | None = None, must_change: bool = True) -> tuple[User, str]:
    """Returns (user, password). When no password is given a temporary one is generated."""
    username = _check_username(username)
    if role not in ROLES:
        raise AuthError(f"Unknown role {role}")
    pw = password or generate_password()
    problems = password_problems(pw, username)
    if problems:
        raise AuthError("Password: " + "; ".join(problems))
    with session_scope() as s:
        if s.scalar(select(User.id).where(User.username == username)):
            raise AuthError(f"User {username} already exists")
        user = User(username=username, role=role, full_name=full_name.strip(), email=email.strip(),
                    password_hash=hash_password(pw), must_change_password=must_change, created_by=created_by)
        s.add(user)
        s.flush()
    audit(created_by, "user.create", username, f"role={role}")
    return user, pw


def update_user(user_id: int, actor: str, *, role: str | None = None, full_name: str | None = None,
                email: str | None = None, active: bool | None = None) -> User:
    with session_scope() as s:
        u = s.get(User, user_id)
        if not u:
            raise LookupError("User not found")
        demoting = (role is not None and role != "admin") or active is False
        if u.role == "admin" and demoting and _active_admins(s, exclude_id=u.id) == 0:
            raise AuthError("Keep at least one active admin")
        changes = []
        if role is not None and role != u.role:
            if role not in ROLES:
                raise AuthError(f"Unknown role {role}")
            changes.append(f"role {u.role}->{role}")
            u.role = role
        if full_name is not None:
            u.full_name = full_name.strip()
        if email is not None:
            u.email = email.strip()
        if active is not None and active != u.active:
            changes.append("enabled" if active else "disabled")
            u.active = active
            if not active:
                s.execute(update(UserSession).where(UserSession.user_id == u.id).values(revoked=True))
        username = u.username
    audit(actor, "user.update", username, ", ".join(changes))
    return get_user(user_id)


def reset_password(user_id: int, actor: str) -> str:
    """Sets a new temporary password (returned once), forces a change and signs the user out everywhere."""
    pw = generate_password()
    with session_scope() as s:
        u = s.get(User, user_id)
        u.password_hash = hash_password(pw)
        u.must_change_password = True
        u.failed_attempts, u.locked_until = 0, None
        u.password_changed_at = _now()
        s.execute(update(UserSession).where(UserSession.user_id == u.id).values(revoked=True))
        username = u.username
    audit(actor, "user.reset_password", username)
    return pw


def unlock(user_id: int, actor: str) -> None:
    with session_scope() as s:
        u = s.get(User, user_id)
        u.failed_attempts, u.locked_until = 0, None
        username = u.username
    audit(actor, "user.unlock", username)


def delete_user(user_id: int, actor_id: int, actor: str) -> None:
    if user_id == actor_id:
        raise AuthError("You cannot delete your own account")
    with session_scope() as s:
        u = s.get(User, user_id)
        if not u:
            return
        if u.role == "admin" and u.active and _active_admins(s, exclude_id=u.id) == 0:
            raise AuthError("Keep at least one active admin")
        username = u.username
        s.execute(delete(UserSession).where(UserSession.user_id == u.id))
        from databridge.core.models import UserCredential, UserEndpointKey  # their API keys go with them

        s.execute(delete(UserCredential).where(UserCredential.user_id == u.id))
        s.execute(delete(UserEndpointKey).where(UserEndpointKey.user_id == u.id))
        s.delete(u)
    audit(actor, "user.delete", username)


def change_own_password(user_id: int, current: str, new: str, ip: str = "") -> None:
    with session_scope() as s:
        u = s.get(User, user_id)
        if not u or not verify_password(current, u.password_hash):
            raise AuthError("Current password is incorrect")
        if current == new:
            raise AuthError("Choose a password different from the current one")
        problems = password_problems(new, u.username)
        if problems:
            raise AuthError("Password: " + "; ".join(problems))
        u.password_hash = hash_password(new)
        u.must_change_password = False
        u.password_changed_at = _now()
        username = u.username
    audit(username, "user.change_password", username, ip=ip)


def revoke_sessions(user_id: int, actor: str) -> int:
    with session_scope() as s:
        res = s.execute(update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked.is_(False))
                        .values(revoked=True))
        u = s.get(User, user_id)
        username = u.username if u else str(user_id)
    audit(actor, "user.sign_out_everywhere", username)
    return res.rowcount or 0


# ------------------------------------------------------------------ sign-in and sessions


@dataclass
class SignIn:
    user: User
    token: str  # raw session token (keep in memory; store in the browser only when remember=True)


def authenticate(username: str, password: str, remember: bool = False, ip: str = "", user_agent: str = "") -> SignIn:
    uname = (username or "").strip().lower()
    outcome, detail, error, result = "", "", None, None
    with session_scope() as s:
        u = s.scalars(select(User).where(User.username == uname)).first()
        locked = _aware(u.locked_until) if u else None
        if not u:
            verify_password(password or "", DUMMY_HASH)  # same cost as a real check: no user enumeration by timing
            outcome, detail = "auth.login_failed", "unknown user"
        elif locked and locked > _now():
            mins = max(1, int((locked - _now()).total_seconds() // 60) + 1)
            outcome, detail = "auth.login_blocked", "account locked"
            error = AuthError(f"Too many failed attempts. Try again in {mins} minute(s) or ask an admin to unlock.")
        elif not verify_password(password or "", u.password_hash):
            u.failed_attempts += 1
            outcome, detail = "auth.login_failed", f"attempt {u.failed_attempts}"
            if u.failed_attempts >= settings.max_failed_logins:
                u.locked_until = _now() + timedelta(minutes=settings.lockout_minutes)
                u.failed_attempts = 0
                detail += f"; locked for {settings.lockout_minutes} min"
        elif not u.active:
            outcome, detail = "auth.login_blocked", "account disabled"
            error = AuthError("This account is disabled. Contact an admin.")
        else:
            u.failed_attempts, u.locked_until = 0, None
            u.last_login_at = _now()
            raw, digest = new_session_token()
            life = timedelta(days=settings.remember_days) if remember else timedelta(hours=settings.session_hours)
            s.add(UserSession(user_id=u.id, token_hash=digest, remember=remember, expires_at=_now() + life,
                              ip=ip or "", user_agent=(user_agent or "")[:300]))
            outcome, detail = "auth.login", "remember me" if remember else ""
            result = SignIn(user=u, token=raw)
    # Audit after the transaction has committed (avoids nested writes on SQLite).
    audit(uname, outcome, uname, detail, ip)
    if result:
        return result
    raise error or AuthError("Incorrect user name or password")


def validate_session(raw_token: str | None, touch: bool = True) -> User | None:
    """Returns the signed-in user, or None if the token is unknown, revoked, expired, idle or the user is disabled."""
    if not raw_token:
        return None
    now = _now()
    with session_scope() as s:
        sess = s.scalars(select(UserSession).where(UserSession.token_hash == token_hash(raw_token))).first()
        if not sess or sess.revoked or _aware(sess.expires_at) < now:
            return None
        if not sess.remember and _aware(sess.last_seen_at) < now - timedelta(minutes=settings.idle_minutes):
            sess.revoked = True
            return None
        user = s.get(User, sess.user_id)
        if not user or not user.active:
            return None
        if touch:
            sess.last_seen_at = now
        return user


def sign_out(raw_token: str | None, username: str = "", ip: str = "") -> None:
    if not raw_token:
        return
    with session_scope() as s:
        s.execute(update(UserSession).where(UserSession.token_hash == token_hash(raw_token)).values(revoked=True))
    audit(username, "auth.logout", username, ip=ip)


def purge_expired_sessions() -> int:
    with session_scope() as s:
        res = s.execute(delete(UserSession).where(
            (UserSession.expires_at < _now() - timedelta(days=1)) | UserSession.revoked.is_(True)))
        return res.rowcount or 0


def bootstrap_admin() -> None:
    """Creates the first admin from DATABRIDGE_ADMIN_USERNAME / _PASSWORD when no users exist."""
    if count_users() > 0 or not (settings.admin_username and settings.admin_password):
        return
    try:
        create_user(settings.admin_username, "admin", created_by="bootstrap", full_name="Administrator",
                    password=settings.admin_password, must_change=True)
        log.warning("Created first admin %r from environment; they must change the password at first sign-in. "
                    "Remove DATABRIDGE_ADMIN_PASSWORD from the environment.", settings.admin_username)
    except AuthError as e:
        log.error("Could not create the first admin from environment: %s", e)
