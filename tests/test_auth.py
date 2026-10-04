from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from databridge.config import settings
from databridge.core.auth import can, generate_password, hash_password, password_problems, verify_password
from databridge.core.db import session_scope
from databridge.core.models import User, UserSession
from databridge.services import users


def test_password_hashing():
    h = hash_password("Correct-Horse-9")
    assert h.startswith("scrypt$") and "Correct" not in h
    assert verify_password("Correct-Horse-9", h)
    assert not verify_password("correct-horse-9", h)
    assert not verify_password("x", "garbage")
    assert hash_password("same") != hash_password("same")  # salted


def test_password_policy():
    assert password_problems("short")
    assert password_problems("alllowercaseletters")
    assert "must not contain the user name" in password_problems("Jane.Doe-2026!", "jane.doe")
    assert password_problems("Str0ng-Enough!") == []
    assert password_problems(generate_password()) == []


def test_permissions():
    assert can("admin", "manage_users") and not can("designer", "manage_users")
    assert can("designer", "design") and not can("viewer", "design")
    assert can("viewer", "view") and not can(None, "view")
    assert not can("designer", "manage_connections") and can("designer", "use_connections")


def test_create_sign_in_and_force_change():
    admin, _ = users.create_user("root.admin", "admin", "test", password="Adm1n-Secret!", must_change=False)
    user, temp = users.create_user("Jane.Doe", "designer", "root.admin", full_name="Jane Doe")
    assert user.username == "jane.doe" and user.must_change_password
    with pytest.raises(users.AuthError):
        users.create_user("jane.doe", "viewer", "root.admin")  # duplicate

    s = users.authenticate("JANE.DOE", temp, ip="1.2.3.4")
    assert s.user.id == user.id and users.validate_session(s.token).id == user.id

    with pytest.raises(users.AuthError):
        users.change_own_password(user.id, "wrong", "New-Passw0rd!")
    users.change_own_password(user.id, temp, "New-Passw0rd!")
    assert not users.get_user(user.id).must_change_password
    users.authenticate("jane.doe", "New-Passw0rd!")

    users.sign_out(s.token, "jane.doe")
    assert users.validate_session(s.token) is None


def test_lockout_and_unlock():
    u, pw = users.create_user("locky", "viewer", "test")
    for _ in range(settings.max_failed_logins):
        with pytest.raises(users.AuthError, match="Incorrect"):
            users.authenticate("locky", "nope")
    with pytest.raises(users.AuthError, match="Too many failed attempts"):
        users.authenticate("locky", pw)  # right password, but locked
    users.unlock(u.id, "test")
    assert users.authenticate("locky", pw).user.id == u.id


def test_unknown_user_same_message():
    with pytest.raises(users.AuthError) as e:
        users.authenticate("nobody-here", "whatever")
    assert "Incorrect user name or password" in str(e.value)


def test_disable_revokes_sessions_and_blocks_login():
    u, pw = users.create_user("temp.worker", "viewer", "test")
    s = users.authenticate("temp.worker", pw)
    users.update_user(u.id, "test", active=False)
    assert users.validate_session(s.token) is None
    with pytest.raises(users.AuthError, match="disabled"):
        users.authenticate("temp.worker", pw)


def test_reset_password_revokes_sessions():
    u, pw = users.create_user("forgetful", "viewer", "test")
    s = users.authenticate("forgetful", pw)
    new = users.reset_password(u.id, "test")
    assert users.validate_session(s.token) is None
    assert users.authenticate("forgetful", new).user.must_change_password


def test_idle_and_expiry():
    u, pw = users.create_user("idler", "viewer", "test")
    s = users.authenticate("idler", pw)
    with session_scope() as db:
        db.execute(update(UserSession).where(UserSession.user_id == u.id).values(
            last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=settings.idle_minutes + 1)))
    assert users.validate_session(s.token) is None

    r = users.authenticate("idler", pw, remember=True)
    with session_scope() as db:
        db.execute(update(UserSession).where(UserSession.user_id == u.id, UserSession.remember.is_(True)).values(
            last_seen_at=datetime.now(timezone.utc) - timedelta(days=2)))
    assert users.validate_session(r.token) is not None  # remembered sessions survive idling...
    with session_scope() as db:
        db.execute(update(UserSession).where(UserSession.user_id == u.id).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert users.validate_session(r.token) is None  # ...but not expiry


def test_last_admin_protected():
    only_admins = [x for x in users.list_users() if x.role == "admin" and x.active]
    for a in only_admins[1:]:
        users.update_user(a.id, "test", active=False)
    last = only_admins[0]
    with pytest.raises(users.AuthError, match="at least one active admin"):
        users.update_user(last.id, "test", role="viewer")
    other, _ = users.create_user("helper.admin", "admin", "test")
    with pytest.raises(users.AuthError, match="own account"):
        users.delete_user(other.id, other.id, "helper.admin")
    users.update_user(last.id, "test", role="designer")  # fine now: helper.admin is still an admin
    users.update_user(last.id, "test", role="admin")


def test_tokens_and_hashes_not_stored_in_clear():
    u, pw = users.create_user("auditee", "viewer", "test")
    s = users.authenticate("auditee", pw)
    with session_scope() as db:
        row = db.query(UserSession).filter(UserSession.user_id == u.id).first()
        assert row.token_hash != s.token and len(row.token_hash) == 64
        assert pw not in db.get(User, u.id).password_hash
    actions = [e.action for e in users.recent_audit(50)]
    assert "auth.login" in actions and "user.create" in actions
