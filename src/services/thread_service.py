"""Read conversation lists and ownership from PostgreSQL checkpoint metadata.

LangGraph saves many checkpoints per thread. These queries group them into
sidebar rows and filter by the verified user ID; there is no conversations table.
The conversation route uses LangGraph to reconstruct readable messages."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def ts_from_checkpoint_id(cid: str | None) -> str | None:
    """Read a UUIDv6 checkpoint timestamp for conversation activity. Return None for an unsupported or malformed ID."""
    if not cid:
        return None
    try:
        h = cid.replace("-", "")
        if len(h) != 32:
            return None
        time_high = int(h[0:8], 16)
        time_mid = int(h[8:12], 16)
        vh = int(h[12:16], 16)
        if ((vh >> 12) & 0xF) != 6:
            return None
        time_low = vh & 0xFFF
        ts_100ns_since_1582 = (time_high << 28) | (time_mid << 12) | time_low
        unix_100ns = ts_100ns_since_1582 - 122192928000000000  # 1582-10-15 → 1970-01-01 in 100ns units
        return datetime.fromtimestamp(unix_100ns / 10_000_000, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


# SIDEBAR QUERY: Filter checkpoint metadata by owner and group many saved snapshots into one row per thread.
# There is no separate conversations table; LIMIT/OFFSET paginate these grouped threads.
_LIST_SQL = """
SELECT
    c.thread_id,
    MIN(c.checkpoint_id) AS first_checkpoint_id,
    MAX(c.checkpoint_id) AS last_checkpoint_id,
    COUNT(*) AS checkpoint_count,
    -- Owner metadata is identical for every checkpoint of a given thread
    -- (we write it at chat-route time and never mutate). MIN picks one.
    MIN(c.metadata->>'user_id') AS user_id,
    MIN(c.metadata->>'name') AS name,
    MIN(c.metadata->>'role') AS role,
    MIN(c.metadata->>'department') AS department
FROM checkpoints c
WHERE c.metadata->>'user_id' = %s
GROUP BY c.thread_id
ORDER BY MAX(c.checkpoint_id) DESC
LIMIT %s OFFSET %s
"""

_COUNT_SQL = """
SELECT COUNT(DISTINCT thread_id) FROM checkpoints WHERE metadata->>'user_id' = %s
"""

# Use the earliest recorded owner to check who created a conversation, rather than accepting a browser-supplied owner.
_OWNERSHIP_SQL = """
SELECT metadata->>'user_id' FROM checkpoints
WHERE thread_id = %s AND metadata->>'user_id' IS NOT NULL
ORDER BY checkpoint_id ASC
LIMIT 1
"""

_DELETE_SQL = """
DELETE FROM checkpoints WHERE thread_id = %s;
DELETE FROM checkpoint_writes WHERE thread_id = %s;
DELETE FROM checkpoint_blobs WHERE thread_id = %s;
"""


# Called by GET /conversations: borrow a pool connection and return this user's thread summaries plus a total count.
async def list_threads_for_user(
    pool, user_id: str, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """Return paginated thread summaries and a count for this owner, borrowing a connection from the startup pool."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_LIST_SQL, (user_id, limit, offset))
            rows = await cur.fetchall()
            await cur.execute(_COUNT_SQL, (user_id,))
            (total,) = await cur.fetchone()

    return [
        {
            "thread_id": r[0],
            "first_checkpoint_id": r[1],
            "last_checkpoint_id": r[2],
            "checkpoint_count": r[3],
            "user_id": r[4],
            "name": r[5],
            "role": r[6],
            "department": r[7],
            "created_at": ts_from_checkpoint_id(r[1]),
            "last_activity_at": ts_from_checkpoint_id(r[2]),
        }
        for r in rows
    ], int(total)


_LIST_ALL_PAGED_SQL = """
SELECT
    c.thread_id,
    MIN(c.checkpoint_id) AS first_checkpoint_id,
    MAX(c.checkpoint_id) AS last_checkpoint_id,
    COUNT(*) AS checkpoint_count,
    MIN(c.metadata->>'user_id') AS user_id,
    MIN(c.metadata->>'name') AS name,
    MIN(c.metadata->>'role') AS role,
    MIN(c.metadata->>'department') AS department
FROM checkpoints c
GROUP BY c.thread_id
ORDER BY MAX(c.checkpoint_id) DESC
LIMIT %s OFFSET %s
"""

_COUNT_ALL_SQL = "SELECT COUNT(DISTINCT thread_id) FROM checkpoints"


async def list_all_threads_paged(
    pool, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """Return paginated summaries across owners for an authorized Admin request, using the same shape as the user listing."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_LIST_ALL_PAGED_SQL, (limit, offset))
            rows = await cur.fetchall()
            await cur.execute(_COUNT_ALL_SQL)
            (total,) = await cur.fetchone()

    return [
        {
            "thread_id": r[0],
            "first_checkpoint_id": r[1],
            "last_checkpoint_id": r[2],
            "checkpoint_count": r[3],
            "user_id": r[4],
            "name": r[5],
            "role": r[6],
            "department": r[7],
            "created_at": ts_from_checkpoint_id(r[1]),
            "last_activity_at": ts_from_checkpoint_id(r[2]),
        }
        for r in rows
    ], int(total)


_HITL_DECISION_COUNT_SQL = """
SELECT COUNT(*) FROM checkpoints
WHERE thread_id = %s
  AND (metadata->>'source' = 'update'
       OR metadata::text LIKE '%%human_decision%%')
"""


async def count_hitl_decisions_on_thread(pool, thread_id: str) -> int:
    """Count earlier approval decisions so the reviewer sees whether this thread has already been reviewed."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_HITL_DECISION_COUNT_SQL, (thread_id,))
            (count,) = await cur.fetchone()
    return int(count or 0)


async def get_thread_owner(pool, thread_id: str) -> str | None:
    """Return the user_id that created this thread, or None if not found.

    Used by route handlers to enforce ownership before returning thread
    contents — cross-user access returns 403, not 404, because the thread
    *exists* but is not the caller's.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_OWNERSHIP_SQL, (thread_id,))
            row = await cur.fetchone()
    if row is None:
        return None
    return row[0]


_LIST_ALL_SQL = """
SELECT
    c.thread_id,
    MIN(c.metadata->>'user_id') AS user_id,
    MIN(c.metadata->>'name') AS name,
    MIN(c.metadata->>'department') AS department,
    MIN(c.metadata->>'role') AS role,
    MAX(c.checkpoint_id) AS latest_checkpoint_id
FROM checkpoints c
GROUP BY c.thread_id
ORDER BY MAX(c.checkpoint_id) DESC
LIMIT %s
"""

_OWNERSHIP_ROLE_SQL = """
SELECT metadata->>'user_id', metadata->>'role', metadata->>'name', metadata->>'department'
FROM checkpoints
WHERE thread_id = %s AND metadata->>'user_id' IS NOT NULL
ORDER BY checkpoint_id ASC
LIMIT 1
"""


async def list_all_threads(pool, limit: int = 200) -> list[dict[str, Any]]:
    """List all threads (any user) with their {thread_id, user_id, name,
    department, role} metadata. Used by the approvals inbox to find pending
    HITL interrupts submitted by other users."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_LIST_ALL_SQL, (limit,))
            rows = await cur.fetchall()
    return [
        {
            "thread_id": r[0],
            "user_id": r[1],
            "name": r[2],
            "department": r[3],
            "role": r[4],
            "latest_checkpoint_id": r[5],
        }
        for r in rows
    ]


# Return persisted owner identity for viewing history and authorizing review. Demo account definitions still live in auth.py.
async def get_thread_owner_role(pool, thread_id: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (user_id, role, name, department) for the thread's creator.
    All None if not found. Older callers using a 2-tuple unpacking should
    expand to 4-tuple after this commit."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_OWNERSHIP_ROLE_SQL, (thread_id,))
            row = await cur.fetchone()
    if row is None:
        return None, None, None, None
    return row[0], row[1], row[2], row[3]


async def delete_thread(pool, thread_id: str) -> int:
    """Delete every checkpoint row associated with this thread.

    Returns the total number of rows removed (across all three checkpoint
    tables). Caller is responsible for the ownership check.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,))
            n1 = cur.rowcount
            await cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (thread_id,))
            n2 = cur.rowcount
            await cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,))
            n3 = cur.rowcount
        await conn.commit()
    return int((n1 or 0) + (n2 or 0) + (n3 or 0))
