"""Account administration (administrators only).

Changing a password, signing someone out and deactivating an account end that person's sessions
in the web app and in MCP clients at once: every request checks the account and session, so
access tokens already issued stop working on their next use.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from src.api.dependencies import client_ip, get_pool, require_admin
from src.api.routes.auth import session_view
from src.config.rbac_config import ROLE_LABELS, ROLE_PERMISSIONS
from src.config.settings import settings
from src.services import passwords, user_store

router = APIRouter(prefix="/admin", tags=["admin"])


class NewAccount(BaseModel):
    username: str = Field(max_length=40)
    name: str = Field(max_length=80)
    role: str
    department: str = Field(default="", max_length=80)
    # Omit to generate a strong password, returned once in the response.
    password: str | None = Field(default=None, max_length=256)


class PasswordReset(BaseModel):
    # Omit to generate a strong password, returned once in the response.
    password: str | None = Field(default=None, max_length=256)


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    department: str | None = Field(default=None, max_length=80)


def _account_view(row: dict | user_store.Account) -> dict:
    data = row if isinstance(row, dict) else row.__dict__
    locked_until = data.get("locked_until")
    return {
        "username": data["username"],
        "name": data["name"],
        "role": data["role"],
        "role_label": ROLE_LABELS.get(data["role"], data["role"]),
        "department": data["department"],
        "active": data["active"],
        "locked_until": locked_until if locked_until and locked_until > datetime.now(timezone.utc) else None,
        "created_at": data["created_at"],
        "password_changed_at": data["password_changed_at"],
        "last_login_at": data["last_login_at"],
        "last_seen_at": data["last_seen_at"],
        "web_sessions": data.get("web_sessions", 0),
        "mcp_sessions": data.get("mcp_sessions", 0),
    }


async def _target(pool, username: str) -> user_store.Account:
    account = await user_store.get_account(pool, username)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such account.")
    return account


def _bad_request(error: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))


@router.get("/roles")
async def roles(admin=Depends(require_admin)) -> dict:
    return {
        "roles": [
            {
                "role": role,
                "label": ROLE_LABELS.get(role, role),
                "document_types": perms["allowed_doc_types"],
                "confidentiality": perms["allowed_confidentiality"],
            }
            for role, perms in ROLE_PERMISSIONS.items()
        ],
        "access_token_minutes": settings.ACCESS_TOKEN_MINUTES,
    }


@router.get("/users")
async def list_users(admin=Depends(require_admin), pool=Depends(get_pool)) -> dict:
    return {"users": [_account_view(row) for row in await user_store.list_accounts(pool)]}


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(body: NewAccount, request: Request, admin=Depends(require_admin), pool=Depends(get_pool)):
    password = body.password or passwords.generate_password()
    try:
        account = await user_store.create_account(
            pool, body.username, body.name, body.role, password, body.department, actor=admin.username
        )
    except user_store.AccountError as error:
        raise _bad_request(error) from error
    return {"user": _account_view(account), "password": None if body.password else password}


@router.patch("/users/{username}")
async def update_user(username: str, body: ProfileUpdate, admin=Depends(require_admin), pool=Depends(get_pool)):
    await _target(pool, username)
    try:
        account = await user_store.update_profile(
            pool, username, name=body.name, department=body.department, actor=admin.username
        )
    except user_store.AccountError as error:
        raise _bad_request(error) from error
    return {"user": _account_view(account)}


@router.post("/users/{username}/password")
async def reset_password(
    username: str, body: PasswordReset, request: Request, admin=Depends(require_admin), pool=Depends(get_pool)
):
    """Set a new password (given, or generated and returned once). Ends all of the user's sessions."""
    target = await _target(pool, username)
    password = body.password or passwords.generate_password()
    try:
        account = await user_store.set_password(pool, target.username, password, actor=admin.username)
    except user_store.AccountError as error:
        raise _bad_request(error) from error
    return {"user": _account_view(account), "password": None if body.password else password}


@router.post("/users/{username}/sign-out")
async def sign_out_everywhere(username: str, request: Request, admin=Depends(require_admin), pool=Depends(get_pool)):
    target = await _target(pool, username)
    ended = await user_store.revoke_sessions(pool, target.username, "signed_out_by_admin")
    await user_store.record_event(
        pool,
        "signed_out_everywhere",
        username=target.username,
        actor=admin.username,
        ip=client_ip(request),
        ended=ended,
    )
    return {"ended_sessions": ended}


@router.post("/users/{username}/deactivate")
async def deactivate(username: str, admin=Depends(require_admin), pool=Depends(get_pool)):
    target = await _target(pool, username)
    if target.username == admin.username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot deactivate your own account.")
    account = await user_store.set_active(pool, target.username, False, actor=admin.username)
    return {"user": _account_view(account)}


@router.post("/users/{username}/activate")
async def activate(username: str, admin=Depends(require_admin), pool=Depends(get_pool)):
    target = await _target(pool, username)
    account = await user_store.set_active(pool, target.username, True, actor=admin.username)
    return {"user": _account_view(account)}


@router.post("/users/{username}/unlock")
async def unlock(username: str, admin=Depends(require_admin), pool=Depends(get_pool)):
    target = await _target(pool, username)
    await user_store.unlock(pool, target.username, actor=admin.username)
    return {"user": _account_view(await _target(pool, username))}


@router.get("/users/{username}/sessions")
async def user_sessions(username: str, admin=Depends(require_admin), pool=Depends(get_pool)) -> dict:
    target = await _target(pool, username)
    return {"sessions": [session_view(s) for s in await user_store.list_sessions(pool, target.username)]}


@router.delete("/users/{username}/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def end_session(
    username: str, session_id: str, request: Request, admin=Depends(require_admin), pool=Depends(get_pool)
):
    target = await _target(pool, username)
    if not await user_store.revoke_session(pool, session_id, "ended_by_admin", username=target.username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such session.")
    await user_store.record_event(
        pool, "session_ended", username=target.username, actor=admin.username, ip=client_ip(request), session=session_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/activity")
async def activity(
    admin=Depends(require_admin),
    pool=Depends(get_pool),
    limit: int = Query(default=100, ge=1, le=500),
    username: str | None = Query(default=None, max_length=40),
) -> dict:
    events = await user_store.recent_events(pool, limit, user_store.normalize_username(username) if username else None)
    return {"events": events}
