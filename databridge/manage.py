"""Admin command line for accounts (run on the server).

    python -m databridge.manage create-admin --username admin
    python -m databridge.manage create-user --username jane.doe --role designer
    python -m databridge.manage reset-password --username jane.doe
    python -m databridge.manage unlock --username jane.doe
    python -m databridge.manage list-users

Passwords are prompted for (never passed on the command line); leave blank to generate one.
"""

import argparse
import getpass
import sys

from databridge.core.auth import ROLES, password_problems
from databridge.core.db import init_db
from databridge.services import users


def _ask_password(username: str) -> str | None:
    if not sys.stdin.isatty():
        return None
    while True:
        pw = getpass.getpass("Password (blank = generate a temporary one): ")
        if not pw:
            return None
        problems = password_problems(pw, username)
        if problems:
            print("Not accepted: " + "; ".join(problems))
            continue
        if getpass.getpass("Repeat password: ") != pw:
            print("Passwords do not match")
            continue
        return pw


def _find(username: str):
    for u in users.list_users():
        if u.username == username.strip().lower():
            return u
    sys.exit(f"No user named {username}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m databridge.manage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("create-admin", "create-user"):
        p = sub.add_parser(name)
        p.add_argument("--username", required=True)
        p.add_argument("--name", default="")
        p.add_argument("--email", default="")
        if name == "create-user":
            p.add_argument("--role", choices=list(ROLES), default="designer")
    for name in ("reset-password", "unlock", "disable", "enable"):
        sub.add_parser(name).add_argument("--username", required=True)
    sub.add_parser("list-users")
    args = ap.parse_args(argv)

    init_db()
    try:
        if args.cmd in ("create-admin", "create-user"):
            role = "admin" if args.cmd == "create-admin" else args.role
            chosen = _ask_password(args.username)
            user, pw = users.create_user(args.username, role, created_by="cli", full_name=args.name,
                                         email=args.email, password=chosen, must_change=chosen is None)
            print(f"Created {role} {user.username}.")
            if chosen is None:
                print(f"Temporary password (shown once, must be changed at first sign-in): {pw}")
        elif args.cmd == "reset-password":
            pw = users.reset_password(_find(args.username).id, actor="cli")
            print(f"Temporary password for {args.username} (shown once): {pw}")
        elif args.cmd == "unlock":
            users.unlock(_find(args.username).id, actor="cli")
            print("Unlocked.")
        elif args.cmd in ("disable", "enable"):
            users.update_user(_find(args.username).id, actor="cli", active=args.cmd == "enable")
            print(f"{args.cmd.title()}d {args.username}.")
        elif args.cmd == "list-users":
            for u in users.list_users():
                state = "active" if u.active else "disabled"
                print(f"{u.username:24} {u.role:9} {state:9} last login {u.last_login_at or 'never'}")
    except users.AuthError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
