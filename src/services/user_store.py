"""Accounts, sign-in sessions and refresh tokens, stored in PostgreSQL.

There is no sign-up: accounts are created by an administrator (scripts/manage_users.py). Passwords
are stored as scrypt hashes (src/services/passwords.py).

A *session* is one signed-in browser or one connected MCP client. It holds a refresh token that is
rotated on every use; only its SHA-256 hash is stored. Using an old refresh token again (a sign that
it was copied) ends the whole session. Each session records the account's password version when it
began, so changing the password ends every older session at its next renewal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.rbac_config import ROLE_PERMISSIONS
from src.config.settings import settings
from src.services import passwords

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")
REFRESH_PREFIX = "fra_rt_"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_users (
    username            text PRIMARY KEY,
    name                text NOT NULL,
    role                text NOT NULL,
    department          text NOT NULL DEFAULT '',
    password_hash       text NOT NULL,
    password_version    integer NOT NULL DEFAULT 1,
    active              boolean NOT NULL DEFAULT true,
    failed_logins       integer NOT NULL DEFAULT 0,
    locked_until        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    password_changed_at timestamptz NOT NULL DEFAULT now(),
    last_login_at       timestamptz,
    last_seen_at        timestamptz
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id               uuid PRIMARY KEY,
    username         text NOT NULL REFERENCES app_users(username) ON DELETE CASCADE,
    kind             text NOT NULL CHECK (kind IN ('web', 'mcp')),
    client_id        text,
    client_name      text,
    scopes           text[] NOT NULL DEFAULT '{}',
    resource         text,
    password_version integer NOT NULL,
    user_agent       text,
    ip               text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_used_at     timestamptz NOT NULL DEFAULT now(),
    expires_at       timestamptz NOT NULL,
    revoked_at       timestamptz,
    revoked_reason   text
);
CREATE INDEX IF NOT EXISTS auth_sessions_username ON auth_sessions (username);

CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
    token_hash text PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES auth_sessions(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);
CREATE INDEX IF NOT EXISTS auth_refresh_tokens_session ON auth_refresh_tokens (session_id);

CREATE TABLE IF NOT EXISTS auth_events (
    id       bigserial PRIMARY KEY,
    at       timestamptz NOT NULL DEFAULT now(),
    username text,
    actor    text,
    event    text NOT NULL,
    ip       text,
    detail   jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS auth_events_at ON auth_events (at DESC);

CREATE TABLE IF NOT EXISTS rate_limit_counters (
    key          text NOT NULL,
    window_start timestamptz NOT NULL,
    count        integer NOT NULL,
    PRIMARY KEY (key, window_start)
);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id  text PRIMARY KEY,
    info       jsonb NOT NULL,
    ip         text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS oauth_requests (
    id         text PRIMARY KEY,
    client_id  text NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    params     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash  text PRIMARY KEY,
    client_id  text NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    username   text NOT NULL REFERENCES app_users(username) ON DELETE CASCADE,
    data       jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);

CREATE TABLE IF NOT EXISTS auth_schema_version (version integer NOT NULL);
"""


async def _query(conn, sql, params=None):
    """Run a statement on a cursor that returns rows as dicts (leaving the pooled connection unchanged)."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(sql, params)
    return cur


class AccountError(Exception):
    """A request about an account that cannot be carried out; the message is safe to show."""


class SessionEnded(Exception):
    """A refresh token that can no longer be used. `reason` is for logs, not for the client."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Account:
    username: str
    name: str
    role: str
    department: str
    password_hash: str
    password_version: int
    active: bool
    failed_logins: int
    locked_until: datetime | None
    created_at: datetime
    password_changed_at: datetime
    last_login_at: datetime | None
    last_seen_at: datetime | None

    def locked(self, now: datetime | None = None) -> bool:
        return self.locked_until is not None and self.locked_until > (now or datetime.now(timezone.utc))


@dataclass(frozen=True)
class Session:
    id: str
    username: str
    kind: str
    client_id: str | None
    client_name: str | None
    scopes: list[str]
    resource: str | None
    password_version: int
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime


_ACCOUNT_FIELDS = (
    "username, name, role, department, password_hash, password_version, active, failed_logins, "
    "locked_until, created_at, password_changed_at, last_login_at, last_seen_at"
)
_SESSION_FIELDS = (
    "id, username, kind, client_id, client_name, scopes, resource, password_version, created_at, "
    "last_used_at, expires_at"
)


def _account(row: dict | None) -> Account | None:
    return Account(**{k: row[k] for k in _ACCOUNT_FIELDS.split(", ")}) if row else None


def _session(row: dict) -> Session:
    values = {k: row[k] for k in _SESSION_FIELDS.split(", ")}
    values["id"] = str(values["id"])
    values["scopes"] = list(values["scopes"] or [])
    return Session(**values)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def _check_username(username: str) -> str:
    username = normalize_username(username)
    if not USERNAME_PATTERN.match(username):
        raise AccountError("Usernames are 2 to 40 characters: lowercase letters, digits, '.', '_' or '-'.")
    return username


def _check_role(role: str) -> str:
    if role not in ROLE_PERMISSIONS:
        raise AccountError(f"Unknown role {role!r}. Roles: {', '.join(ROLE_PERMISSIONS)}.")
    return role


def _check_new_password(password: str) -> None:
    problem = passwords.password_problem(password)
    if problem:
        raise AccountError(problem)


# --- Schema --------------------------------------------------------------------------------------


async def ensure_schema(pool) -> None:
    """Create the tables if missing (safe to run on every start, and from several processes)."""
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('fra_auth_schema'))")
            for statement in filter(str.strip, SCHEMA.split(";")):
                await conn.execute(statement)
            cur = await conn.execute("SELECT version FROM auth_schema_version")
            if await cur.fetchone() is None:
                await conn.execute("INSERT INTO auth_schema_version (version) VALUES (%s)", (SCHEMA_VERSION,))


async def purge_expired(pool) -> None:
    """Delete rows that can never be used again (run at start and periodically)."""
    keep_days = max(settings.SESSION_MAX_DAYS, 30)
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM auth_sessions WHERE (revoked_at IS NOT NULL AND revoked_at < now() - interval '30 days')"
            " OR expires_at < now() - make_interval(days => %s)",
            (keep_days,),
        )
        await conn.execute("DELETE FROM auth_refresh_tokens WHERE expires_at < now() - interval '1 day'")
        await conn.execute("DELETE FROM oauth_requests WHERE expires_at < now()")
        await conn.execute("DELETE FROM oauth_codes WHERE expires_at < now() - interval '1 day'")
        await conn.execute("DELETE FROM rate_limit_counters WHERE window_start < now() - interval '2 days'")
        await conn.execute("DELETE FROM auth_events WHERE at < now() - interval '180 days'")
        # Registered clients that never completed a sign-in within a day.
        await conn.execute(
            "DELETE FROM oauth_clients c WHERE c.created_at < now() - interval '1 day'"
            " AND NOT EXISTS (SELECT 1 FROM auth_sessions s WHERE s.client_id = c.client_id)"
        )


# --- Accounts ------------------------------------------------------------------------------------


async def get_account(pool, username: str) -> Account | None:
    async with pool.connection() as conn:
        cur = await _query(
            conn, f"SELECT {_ACCOUNT_FIELDS} FROM app_users WHERE username = %s", (normalize_username(username),)
        )
        return _account(await cur.fetchone())


async def count_accounts(pool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM app_users")
        return (await cur.fetchone())[0]


async def list_accounts(pool) -> list[dict[str, Any]]:
    """Accounts with their open web and MCP session counts, for the admin page."""
    async with pool.connection() as conn:
        cur = await _query(
            conn,
            f"""
            SELECT {", ".join("u." + f for f in _ACCOUNT_FIELDS.split(", "))},
              count(s.id) FILTER (WHERE s.kind = 'web') AS web_sessions,
              count(s.id) FILTER (WHERE s.kind = 'mcp') AS mcp_sessions
            FROM app_users u
            LEFT JOIN auth_sessions s ON s.username = u.username AND s.revoked_at IS NULL
              AND s.expires_at > now() AND s.password_version = u.password_version
              AND EXISTS (SELECT 1 FROM auth_refresh_tokens t WHERE t.session_id = s.id
                          AND t.used_at IS NULL AND t.expires_at > now())
            GROUP BY u.username
            ORDER BY CASE u.role WHEN 'admin' THEN 0 ELSE 1 END, u.username
            """,
        )
        return await cur.fetchall()


async def create_account(
    pool, username: str, name: str, role: str, password: str, department: str = "", actor: str | None = None
) -> Account:
    username = _check_username(username)
    role = _check_role(role)
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise AccountError("Give a display name of 1 to 80 characters.")
    _check_new_password(password)
    async with pool.connection() as conn:
        cur = await _query(
            conn,
            f"""INSERT INTO app_users (username, name, role, department, password_hash)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT (username) DO NOTHING RETURNING {_ACCOUNT_FIELDS}""",
            (username, name, role, (department or "").strip()[:80], passwords.hash_password(password)),
        )
        account = _account(await cur.fetchone())
    if account is None:
        raise AccountError(f"An account named {username!r} already exists.")
    await record_event(pool, "account_created", username=username, actor=actor, role=role)
    return account


async def set_password(pool, username: str, password: str, actor: str | None = None) -> Account:
    """Set a new password, end every session of the account and clear any lockout."""
    _check_new_password(password)
    username = normalize_username(username)
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await _query(
                conn,
                f"""UPDATE app_users SET password_hash = %s, password_version = password_version + 1,
                      password_changed_at = now(), updated_at = now(), failed_logins = 0, locked_until = NULL
                    WHERE username = %s RETURNING {_ACCOUNT_FIELDS}""",
                (passwords.hash_password(password), username),
            )
            account = _account(await cur.fetchone())
            if account is None:
                raise AccountError("No such account.")
            await _query(
                conn,
                "UPDATE auth_sessions SET revoked_at = now(), revoked_reason = 'password_changed'"
                " WHERE username = %s AND revoked_at IS NULL",
                (username,),
            )
    await record_event(pool, "password_changed", username=username, actor=actor)
    return account


async def set_active(pool, username: str, active: bool, actor: str | None = None) -> Account:
    """Activate or deactivate an account; deactivating ends its sessions."""
    username = normalize_username(username)
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await _query(
                conn,
                f"UPDATE app_users SET active = %s, updated_at = now() WHERE username = %s RETURNING {_ACCOUNT_FIELDS}",
                (active, username),
            )
            account = _account(await cur.fetchone())
            if account is None:
                raise AccountError("No such account.")
            if not active:
                await _query(
                    conn,
                    "UPDATE auth_sessions SET revoked_at = now(), revoked_reason = 'deactivated'"
                    " WHERE username = %s AND revoked_at IS NULL",
                    (username,),
                )
    await record_event(pool, "account_activated" if active else "account_deactivated", username=username, actor=actor)
    return account


async def update_profile(
    pool, username: str, *, name: str | None = None, department: str | None = None, actor: str | None = None
) -> Account:
    username = normalize_username(username)
    if name is not None and (not name.strip() or len(name.strip()) > 80):
        raise AccountError("Give a display name of 1 to 80 characters.")
    async with pool.connection() as conn:
        cur = await _query(
            conn,
            f"""UPDATE app_users SET name = COALESCE(%s, name), department = COALESCE(%s, department),
                  updated_at = now() WHERE username = %s RETURNING {_ACCOUNT_FIELDS}""",
            (name.strip() if name else None, department.strip()[:80] if department is not None else None, username),
        )
        account = _account(await cur.fetchone())
    if account is None:
        raise AccountError("No such account.")
    await record_event(pool, "profile_updated", username=username, actor=actor)
    return account


async def unlock(pool, username: str, actor: str | None = None) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE app_users SET failed_logins = 0, locked_until = NULL WHERE username = %s",
            (normalize_username(username),),
        )
    await record_event(pool, "account_unlocked", username=normalize_username(username), actor=actor)


async def record_failed_login(pool, username: str) -> Account | None:
    """Count a wrong password; lock the account after LOGIN_MAX_FAILURES in a row."""
    async with pool.connection() as conn:
        cur = await _query(
            conn,
            f"""UPDATE app_users SET
                  failed_logins = CASE WHEN failed_logins + 1 >= %(max)s THEN 0 ELSE failed_logins + 1 END,
                  locked_until = CASE WHEN failed_logins + 1 >= %(max)s
                                      THEN now() + make_interval(mins => %(mins)s) ELSE locked_until END
                WHERE username = %(username)s RETURNING {_ACCOUNT_FIELDS}""",
            {
                "max": settings.LOGIN_MAX_FAILURES,
                "mins": settings.LOGIN_LOCKOUT_MINUTES,
                "username": normalize_username(username),
            },
        )
        return _account(await cur.fetchone())


async def record_login(pool, username: str, new_hash: str | None = None) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE app_users SET failed_logins = 0, locked_until = NULL, last_login_at = now(), last_seen_at = now(),"
            " password_hash = COALESCE(%s, password_hash) WHERE username = %s",
            (new_hash, normalize_username(username)),
        )


# --- Sessions and refresh tokens -----------------------------------------------------------------


def _new_refresh_token() -> str:
    return REFRESH_PREFIX + secrets.token_urlsafe(32)


async def create_session(
    pool,
    account: Account,
    kind: str,
    *,
    client_id: str | None = None,
    client_name: str | None = None,
    scopes: list[str] | None = None,
    resource: str | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[Session, str]:
    """Start a session and return it with its first refresh token."""
    session_id = uuid.uuid4()
    refresh = _new_refresh_token()
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await _query(
                conn,
                f"""INSERT INTO auth_sessions (id, username, kind, client_id, client_name, scopes, resource,
                      password_version, user_agent, ip, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(days => %s))
                    RETURNING {_SESSION_FIELDS}""",
                (
                    session_id,
                    account.username,
                    kind,
                    client_id,
                    (client_name or "")[:120] or None,
                    scopes or [],
                    resource,
                    account.password_version,
                    (user_agent or "")[:300] or None,
                    ip,
                    settings.SESSION_MAX_DAYS,
                ),
            )
            session = _session(await cur.fetchone())
            await _query(
                conn,
                "INSERT INTO auth_refresh_tokens (token_hash, session_id, expires_at)"
                " VALUES (%s, %s, LEAST(now() + make_interval(days => %s), %s))",
                (token_hash(refresh), session_id, settings.SESSION_IDLE_DAYS, session.expires_at),
            )
    return session, refresh


async def _load_for_refresh(conn, token: str, lock: bool) -> dict | None:
    cur = await _query(
        conn,
        f"""SELECT t.token_hash, t.expires_at AS token_expires_at, t.used_at, t.session_id,
                   s.revoked_at, s.revoked_reason, {", ".join("s." + f for f in _SESSION_FIELDS.split(", "))},
                   u.active, u.password_version AS current_password_version
            FROM auth_refresh_tokens t
            JOIN auth_sessions s ON s.id = t.session_id
            JOIN app_users u ON u.username = s.username
            WHERE t.token_hash = %s {"FOR UPDATE OF t, s" if lock else ""}""",
        (token_hash(token),),
    )
    return await cur.fetchone()


def _problem(row: dict | None, kind: str, client_id: str | None, now: datetime) -> str | None:
    if row is None:
        return "unknown_token"
    if row["kind"] != kind or (client_id is not None and row["client_id"] != client_id):
        return "wrong_client"
    if row["revoked_at"] is not None:
        return f"session_ended:{row['revoked_reason']}"
    if row["expires_at"] <= now or row["token_expires_at"] <= now:
        return "expired"
    if not row["active"]:
        return "account_deactivated"
    if row["password_version"] != row["current_password_version"]:
        return "password_changed"
    return None


async def check_refresh_token(pool, token: str, kind: str, client_id: str | None = None) -> Session:
    """Validate a refresh token without using it (for the OAuth token endpoint's first step)."""
    if not token.startswith(REFRESH_PREFIX):
        raise SessionEnded("unknown_token")
    async with pool.connection() as conn:
        row = await _load_for_refresh(conn, token, lock=False)
        now = datetime.now(timezone.utc)
        problem = _problem(row, kind, client_id, now)
        if problem is None and row["used_at"] is not None:
            problem = "already_used"
    if problem:
        if problem == "already_used" and now - row["used_at"] > timedelta(seconds=settings.REFRESH_REUSE_GRACE_SECONDS):
            await revoke_session(pool, str(row["session_id"]), "refresh_token_reused")
            await record_event(pool, "refresh_token_reused", username=row["username"], session=str(row["session_id"]))
        raise SessionEnded(problem)
    return _session(row)


async def rotate_refresh_token(
    pool, token: str, kind: str, client_id: str | None = None
) -> tuple[Session, Account, str]:
    """Use a refresh token: mark it used and return the session, the account and a new token.

    A token used a second time ends the session (unless both uses fall within the reuse grace,
    such as two tabs renewing at once, which is refused without ending the session).
    """
    if not token.startswith(REFRESH_PREFIX):
        raise SessionEnded("unknown_token")
    reused = None
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _load_for_refresh(conn, token, lock=True)
            now = datetime.now(timezone.utc)
            problem = _problem(row, kind, client_id, now)
            if problem:
                raise SessionEnded(problem)
            if row["used_at"] is not None:
                if now - row["used_at"] <= timedelta(seconds=settings.REFRESH_REUSE_GRACE_SECONDS):
                    raise SessionEnded("concurrent_refresh")
                await _query(
                    conn,
                    "UPDATE auth_sessions SET revoked_at = now(), revoked_reason = 'refresh_token_reused'"
                    " WHERE id = %s",
                    (row["session_id"],),
                )
                reused = row
            else:
                new_token = _new_refresh_token()
                await _query(
                    conn, "UPDATE auth_refresh_tokens SET used_at = now() WHERE token_hash = %s", (row["token_hash"],)
                )
                await _query(
                    conn,
                    "INSERT INTO auth_refresh_tokens (token_hash, session_id, expires_at)"
                    " VALUES (%s, %s, LEAST(now() + make_interval(days => %s), %s))",
                    (token_hash(new_token), row["session_id"], settings.SESSION_IDLE_DAYS, row["expires_at"]),
                )
                await _query(conn, "UPDATE auth_sessions SET last_used_at = now() WHERE id = %s", (row["session_id"],))
                cur = await _query(
                    conn,
                    f"UPDATE app_users SET last_seen_at = now() WHERE username = %s RETURNING {_ACCOUNT_FIELDS}",
                    (row["username"],),
                )
                account = _account(await cur.fetchone())
    if reused is not None:
        logger.warning("Refresh token reused; ended session %s of %s", reused["session_id"], reused["username"])
        await record_event(pool, "refresh_token_reused", username=reused["username"], session=str(reused["session_id"]))
        raise SessionEnded("refresh_token_reused")
    return _session(row), account, new_token


async def get_session(pool, session_id: str) -> Session | None:
    try:
        uuid.UUID(str(session_id))
    except ValueError:
        return None
    async with pool.connection() as conn:
        cur = await _query(
            conn, f"SELECT {_SESSION_FIELDS} FROM auth_sessions WHERE id = %s AND revoked_at IS NULL", (session_id,)
        )
        row = await cur.fetchone()
        return _session(row) if row else None


async def revoke_session(pool, session_id: str, reason: str, username: str | None = None) -> bool:
    """End one session (optionally only if it belongs to `username`)."""
    try:
        uuid.UUID(str(session_id))
    except ValueError:
        return False
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE auth_sessions SET revoked_at = now(), revoked_reason = %s"
            " WHERE id = %s AND revoked_at IS NULL AND (%s::text IS NULL OR username = %s)",
            (reason, session_id, username, username),
        )
        return cur.rowcount > 0


async def revoke_by_refresh_token(pool, token: str, kind: str, reason: str) -> str | None:
    """End the session a refresh token (current or already used) belongs to; return its username."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE auth_sessions SET revoked_at = now(), revoked_reason = %s"
            " WHERE revoked_at IS NULL AND kind = %s"
            " AND id = (SELECT session_id FROM auth_refresh_tokens WHERE token_hash = %s) RETURNING username",
            (reason, kind, token_hash(token)),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def revoke_sessions(pool, username: str, reason: str, *, except_session: str | None = None) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE auth_sessions SET revoked_at = now(), revoked_reason = %s"
            " WHERE username = %s AND revoked_at IS NULL AND (%s::uuid IS NULL OR id <> %s::uuid)",
            (reason, normalize_username(username), except_session, except_session),
        )
        return cur.rowcount


async def list_sessions(pool, username: str) -> list[dict[str, Any]]:
    """Open sessions of an account, newest activity first."""
    async with pool.connection() as conn:
        cur = await _query(
            conn,
            f"""SELECT {", ".join("s." + f for f in _SESSION_FIELDS.split(", "))}, s.user_agent, s.ip
                FROM auth_sessions s JOIN app_users u ON u.username = s.username
                WHERE s.username = %s AND s.revoked_at IS NULL AND s.expires_at > now()
                  AND s.password_version = u.password_version
                  AND EXISTS (SELECT 1 FROM auth_refresh_tokens t WHERE t.session_id = s.id
                              AND t.used_at IS NULL AND t.expires_at > now())
                ORDER BY s.last_used_at DESC""",
            (normalize_username(username),),
        )
        rows = await cur.fetchall()
    for row in rows:
        row["id"] = str(row["id"])
    return rows


# --- Events and counters -------------------------------------------------------------------------


async def record_event(
    pool, event: str, *, username: str | None = None, actor: str | None = None, ip: str | None = None, **detail
) -> None:
    """Append to the account activity log; never fails the request that triggered it."""
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO auth_events (event, username, actor, ip, detail) VALUES (%s, %s, %s, %s, %s)",
                (event, username, actor, ip, Jsonb(json.loads(json.dumps(detail, default=str)))),
            )
    except Exception:
        logger.exception("Could not record auth event %s", event)


async def recent_events(pool, limit: int = 100, username: str | None = None) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        cur = await _query(
            conn,
            "SELECT id, at, username, actor, event, ip, detail FROM auth_events"
            " WHERE (%s::text IS NULL OR username = %s) ORDER BY at DESC, id DESC LIMIT %s",
            (username, username, limit),
        )
        return await cur.fetchall()


async def hit_counter(pool, key: str, window_seconds: int) -> tuple[int, datetime]:
    """Count one event in the current fixed window; return the new count and when the window ends."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO rate_limit_counters (key, window_start, count)
               VALUES (%(key)s, to_timestamp(floor(extract(epoch FROM now()) / %(w)s) * %(w)s), 1)
               ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limit_counters.count + 1
               RETURNING count, window_start + make_interval(secs => %(w)s)""",
            {"key": key, "w": window_seconds},
        )
        count, ends = await cur.fetchone()
        return count, ends
