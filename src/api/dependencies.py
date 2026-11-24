from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config.settings import settings
from src.models.auth import User
from src.services import user_store
from src.services.auth_service import decode_token
from src.services.rate_limit import QuotaExceeded, quota_check

security = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security)
) -> User:
    """Validate the bearer access token and return the authenticated user.

    The account must still be active and the token's session still open, so an administrator's
    deactivation, password reset or sign-out applies to the very next request.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not signed in",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = decode_token(credentials.credentials)
    pool = getattr(request.app.state, "pool", None)
    if pool is not None:
        try:
            allowed = await user_store.access_allowed(pool, user.user_id, user.session_id)
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The account database is unavailable. Try again shortly.",
            ) from error
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Your session has ended. Sign in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
    return user


def get_pool(request: Request):
    """The PostgreSQL pool, or 503 when the database is unavailable."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The account database is unavailable. Try again shortly.",
        )
    return pool


def client_ip(request: Request) -> str:
    """The caller's address (behind a proxy, uvicorn's --proxy-headers sets it from X-Forwarded-For)."""
    return request.client.host if request.client else "unknown"


async def current_account(user: User = Depends(get_current_user), pool=Depends(get_pool)) -> user_store.Account:
    """The signed-in user's account, checked against the database (for sensitive actions)."""
    account = await user_store.get_account(pool, user.user_id)
    if account is None or not account.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="This account is no longer active.")
    return account


async def require_admin(account: user_store.Account = Depends(current_account)) -> user_store.Account:
    if account.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrators only.")
    return account


async def question_quota(request: Request, user: User = Depends(get_current_user)) -> None:
    """Per-user limits on questions to the assistant (each one makes several model calls)."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return
    try:
        await quota_check(
            pool,
            f"questions:{user.user_id}",
            [
                (settings.QUESTIONS_PER_HOUR, 3600, "questions per hour"),
                (settings.QUESTIONS_PER_DAY, 86400, "questions per day"),
            ],
        )
    except QuotaExceeded as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
            headers={"Retry-After": str(error.retry_after)},
        ) from error
