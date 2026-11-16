"""Manage sign-in accounts (there is no sign-up; accounts are created here or on the admin page).

Run inside the API container so it reaches the database (the Makefile targets do this):

    python scripts/manage_users.py seed                  # one account per role, passwords printed once
    python scripts/manage_users.py list
    python scripts/manage_users.py create jane --name "Jane Doe" --role finance --department FP&A
    python scripts/manage_users.py set-password analyst  # prompts; --generate prints a new random one
    python scripts/manage_users.py sign-out analyst      # end every browser and MCP session
    python scripts/manage_users.py deactivate analyst | activate analyst | unlock analyst

Changing a password, signing out and deactivating end the account's sessions; access tokens already
issued stop working when they expire (ACCESS_TOKEN_MINUTES, 60 by default).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg_pool import AsyncConnectionPool  # noqa: E402

from src.config.rbac_config import ROLE_PERMISSIONS  # noqa: E402
from src.config.settings import settings  # noqa: E402
from src.services import passwords, user_store  # noqa: E402

SEED_ACCOUNTS = [
    ("admin", "Administrator", "admin", "IT"),
    ("analyst", "Research Analyst", "analyst", "Research"),
    ("finance", "Finance Lead", "finance", "FP&A"),
    ("hr", "HR Partner", "hr", "People"),
    ("executive", "Executive", "c_level", "Leadership"),
]
ACTOR = "cli"


def _ask_password() -> str:
    if not sys.stdin.isatty():
        raise SystemExit("No terminal to type a password into; use --generate.")
    while True:
        first = getpass.getpass("New password: ")
        problem = passwords.password_problem(first)
        if problem:
            print(problem)
            continue
        if getpass.getpass("Repeat it: ") != first:
            print("The passwords do not match.")
            continue
        return first


def _print_credentials(rows: list[tuple[str, str, str]]) -> None:
    width = max(len(r[0]) for r in rows)
    print()
    print("Save these now; the passwords are not stored anywhere in readable form.")
    print()
    for username, role, password in rows:
        print(f"  {username.ljust(width)}  {password}   ({role})")
    print()


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="create one account per role (skips ones that exist)")
    sub.add_parser("list", help="list accounts")
    create = sub.add_parser("create", help="create an account")
    create.add_argument("username")
    create.add_argument("--name", required=True)
    create.add_argument("--role", required=True, choices=list(ROLE_PERMISSIONS))
    create.add_argument("--department", default="")
    create.add_argument("--generate", action="store_true", help="generate the password and print it")
    setpw = sub.add_parser("set-password", help="set a new password and end the account's sessions")
    setpw.add_argument("username")
    setpw.add_argument("--generate", action="store_true", help="generate the password and print it")
    for name in ("sign-out", "deactivate", "activate", "unlock"):
        sub.add_parser(name).add_argument("username")
    args = parser.parse_args(argv)

    pool = AsyncConnectionPool(
        conninfo=settings.postgres_url, min_size=1, max_size=2, open=False, kwargs={"autocommit": True}
    )
    await pool.open()
    try:
        await user_store.ensure_schema(pool)
        return await run(pool, args)
    except user_store.AccountError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        await pool.close()


async def run(pool, args) -> int:
    if args.command == "seed":
        created = []
        for username, name, role, department in SEED_ACCOUNTS:
            if await user_store.get_account(pool, username):
                print(f"{username}: exists, unchanged")
                continue
            password = passwords.generate_password()
            await user_store.create_account(pool, username, name, role, password, department, actor=ACTOR)
            created.append((username, role, password))
        if created:
            _print_credentials(created)
        return 0

    if args.command == "list":
        rows = await user_store.list_accounts(pool)
        if not rows:
            print("No accounts. Run: make users")
            return 0
        print(f"{'USERNAME':<14}{'ROLE':<10}{'STATUS':<13}{'SESSIONS':<10}LAST SIGN-IN")
        for r in rows:
            status = "active" if r["active"] else "deactivated"
            account = await user_store.get_account(pool, r["username"])
            if account and account.locked():
                status = "locked"
            last = r["last_login_at"].strftime("%Y-%m-%d %H:%M") if r["last_login_at"] else "never"
            sessions = f"{r['web_sessions']}w {r['mcp_sessions']}m"
            print(f"{r['username']:<14}{r['role']:<10}{status:<13}{sessions:<10}{last}")
        return 0

    if args.command == "create":
        password = passwords.generate_password() if args.generate else _ask_password()
        await user_store.create_account(
            pool, args.username, args.name, args.role, password, args.department, actor=ACTOR
        )
        print(f"Created {user_store.normalize_username(args.username)}.")
        if args.generate:
            _print_credentials([(user_store.normalize_username(args.username), args.role, password)])
        return 0

    account = await user_store.get_account(pool, args.username)
    if account is None:
        print(f"Error: no account named {args.username!r}.", file=sys.stderr)
        return 1

    if args.command == "set-password":
        password = passwords.generate_password() if args.generate else _ask_password()
        await user_store.set_password(pool, account.username, password, actor=ACTOR)
        print(f"Password changed for {account.username}; their sessions have been ended.")
        if args.generate:
            _print_credentials([(account.username, account.role, password)])
    elif args.command == "sign-out":
        ended = await user_store.revoke_sessions(pool, account.username, "signed_out_by_admin")
        await user_store.record_event(pool, "signed_out_everywhere", username=account.username, actor=ACTOR)
        print(f"Ended {ended} session(s) for {account.username}.")
    elif args.command == "deactivate":
        await user_store.set_active(pool, account.username, False, actor=ACTOR)
        print(f"Deactivated {account.username}; their sessions have been ended.")
    elif args.command == "activate":
        await user_store.set_active(pool, account.username, True, actor=ACTOR)
        print(f"Activated {account.username}.")
    elif args.command == "unlock":
        await user_store.unlock(pool, account.username, actor=ACTOR)
        print(f"Unlocked {account.username}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
