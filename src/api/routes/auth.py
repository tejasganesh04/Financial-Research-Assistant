"""Sign-in for the web app.

- POST /auth/login: username and password. Returns a 60-minute access token in the body and sets
  the refresh token as an httpOnly cookie that page scripts cannot read.
- POST /auth/refresh: uses the cookie to issue a new access token (and a new refresh token).
- POST /auth/logout: ends the session and clears the cookie.

The refresh cookie is `SameSite=Strict` and only sent to /v1/auth, so other sites cannot make the
browser use it, and ordinary API calls carry only the short-lived access token in a header.
There is no sign-up: accounts are created by an administrator.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from src.api.dependencies import client_ip, current_account, get_current_user, get_pool
from src.config.rbac_config import get_permissions
from src.config.settings import settings
from src.models.auth import User
from src.models.schemas import (
    LoginRequest,
    PasswordChangeRequest,
    SessionResponse,
    UserMeResponse,
    UserPermissions,
)
from src.services import passwords, user_store
from src.services.auth_service import create_token
from src.services.rate_limit import SlidingWindowLimiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "fra_refresh"
COOKIE_PATH = "/v1/auth"
INVALID_CREDENTIALS = "Incorrect username or password."

login_limiter = SlidingWindowLimiter(settings.LOGIN_ATTEMPTS_PER_MINUTE, 60)


def identity(username: str, name: str, role: str, department: str) -> UserMeResponse:
    perms = get_permissions(role)
    return UserMeResponse(
        user_id=username,
        name=name,
        role=role,
        department=department,
        permissions=UserPermissions(
            allowed_doc_types=perms["allowed_doc_types"],
            allowed_confidentiality=perms["allowed_confidentiality"],
        ),
    )


def _session_response(account: user_store.Account, session_id: str) -> SessionResponse:
    return SessionResponse(
        access_token=create_token(
            account.username, account.name, account.role, account.department, session_id=session_id
        ),
        expires_in=settings.ACCESS_TOKEN_MINUTES * 60,
        user=identity(account.username, account.name, account.role, account.department),
    )


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.SESSION_IDLE_DAYS * 86400,
        path=COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE, path=COOKIE_PATH, httponly=True, secure=settings.cookie_secure, samesite="strict"
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _minutes_until(moment: datetime) -> int:
    return max(1, math.ceil((moment - datetime.now(timezone.utc)).total_seconds() / 60))


async def _start_web_session(
    pool, account: user_store.Account, request: Request, response: Response
) -> SessionResponse:
    session, refresh = await user_store.create_session(
        pool, account, "web", user_agent=request.headers.get("user-agent"), ip=client_ip(request)
    )
    _set_refresh_cookie(response, refresh)
    _no_store(response)
    return _session_response(account, session.id)


@router.post("/login", response_model=SessionResponse)
async def login(body: LoginRequest, request: Request, response: Response, pool=Depends(get_pool)):
    ip = client_ip(request)
    wait = login_limiter.hit(f"ip:{ip}")
    if wait:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many sign-in attempts. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )

    username = user_store.normalize_username(body.username)
    account = await user_store.get_account(pool, username) if username else None
    if account is None:
        passwords.verify_password(body.password, passwords.DUMMY_HASH)  # same timing as a real account
        await user_store.record_event(pool, "login_failed", ip=ip, attempted=username[:40], reason="unknown_user")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS)

    if account.locked():
        await user_store.record_event(pool, "login_failed", username=account.username, ip=ip, reason="locked")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed sign-in attempts. "
            f"Try again in {_minutes_until(account.locked_until)} minutes, or ask your administrator.",
        )

    if not passwords.verify_password(body.password, account.password_hash):
        updated = await user_store.record_failed_login(pool, account.username)
        await user_store.record_event(pool, "login_failed", username=account.username, ip=ip, reason="password")
        if updated is not None and updated.locked():
            await user_store.record_event(pool, "account_locked", username=account.username, ip=ip)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed sign-in attempts. "
                f"Try again in {settings.LOGIN_LOCKOUT_MINUTES} minutes, or ask your administrator.",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS)

    if not account.active:
        await user_store.record_event(pool, "login_failed", username=account.username, ip=ip, reason="deactivated")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated. Ask your administrator.",
        )

    new_hash = passwords.hash_password(body.password) if passwords.needs_rehash(account.password_hash) else None
    await user_store.record_login(pool, account.username, new_hash)
    # The browser keeps one refresh cookie, so the new one replaces its previous session. End that
    # session now rather than leave it open but unreachable, still usable by tabs holding its token.
    previous = request.cookies.get(REFRESH_COOKIE)
    if previous:
        ended_for = await user_store.revoke_by_refresh_token(pool, previous, "web", "replaced_by_sign_in")
        if ended_for:
            await user_store.record_event(pool, "logout", username=ended_for, ip=ip, reason="replaced_by_sign_in")
    result = await _start_web_session(pool, account, request, response)
    await user_store.record_event(pool, "login", username=account.username, ip=ip, client="web")
    return result


@router.post("/refresh", response_model=SessionResponse, responses={401: {"description": "Session ended"}})
async def refresh(request: Request, response: Response, pool=Depends(get_pool)):
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in.")
    try:
        session, account, new_token = await user_store.rotate_refresh_token(pool, token, "web")
    except user_store.SessionEnded as ended:
        logger.info("Web session renewal refused: %s", ended.reason)
        refused = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "Your session has ended. Sign in again."}
        )
        if ended.reason != "concurrent_refresh":
            # Keep the cookie when another tab has just renewed it; otherwise it is useless.
            _clear_refresh_cookie(refused)
        _no_store(refused)
        return refused
    _set_refresh_cookie(response, new_token)
    _no_store(response)
    return _session_response(account, session.id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, pool=Depends(get_pool)):
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        username = await user_store.revoke_by_refresh_token(pool, token, "web", "signed_out")
        if username:
            await user_store.record_event(pool, "logout", username=username, ip=client_ip(request))
    _clear_refresh_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserMeResponse)
async def me(user: User = Depends(get_current_user)) -> UserMeResponse:
    """Return the signed-in identity and its role permissions."""
    return identity(user.user_id, user.name, user.role, user.department)


@router.post("/password", response_model=SessionResponse)
async def change_password(
    body: PasswordChangeRequest,
    request: Request,
    response: Response,
    account: user_store.Account = Depends(current_account),
    pool=Depends(get_pool),
):
    """Change your own password. Every other session (browsers and MCP clients) is signed out."""
    if not passwords.verify_password(body.current_password, account.password_hash):
        await user_store.record_event(pool, "password_change_failed", username=account.username, ip=client_ip(request))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Your current password is incorrect.")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a different password.")
    try:
        account = await user_store.set_password(pool, account.username, body.new_password, actor=account.username)
    except user_store.AccountError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return await _start_web_session(pool, account, request, response)


@router.get("/sessions")
async def my_sessions(user: User = Depends(get_current_user), pool=Depends(get_pool)) -> dict:
    """Your signed-in browsers and connected MCP clients."""
    sessions = await user_store.list_sessions(pool, user.user_id)
    return {"sessions": [session_view(s, current=user.session_id) for s in sessions]}


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def end_my_session(
    session_id: str, request: Request, user: User = Depends(get_current_user), pool=Depends(get_pool)
):
    if not await user_store.revoke_session(pool, session_id, "ended_by_user", username=user.user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such session.")
    await user_store.record_event(
        pool, "session_ended", username=user.user_id, actor=user.user_id, ip=client_ip(request), session=session_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/sign-out-others")
async def end_my_other_sessions(request: Request, user: User = Depends(get_current_user), pool=Depends(get_pool)):
    """Sign out every other browser and disconnect every MCP client; this browser stays signed in."""
    if not user.session_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This browser has no session to keep.")
    ended = await user_store.revoke_sessions(pool, user.user_id, "ended_by_user", except_session=user.session_id)
    await user_store.record_event(
        pool, "signed_out_everywhere", username=user.user_id, actor=user.user_id, ip=client_ip(request), ended=ended
    )
    return {"ended": ended}


def session_view(row: dict, current: str | None = None) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "client_name": row.get("client_name"),
        "user_agent": row.get("user_agent"),
        "ip": row.get("ip"),
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "expires_at": row["expires_at"],
        "current": row["id"] == current,
    }
