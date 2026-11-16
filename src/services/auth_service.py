"""Short-lived access tokens for the web app.

An access token is a signed JWT valid for ACCESS_TOKEN_MINUTES. It is checked by signature alone
(no database lookup), which is why it is short-lived: the browser renews it with its refresh cookie
(see src/api/routes/auth.py), and renewal is where the database decides whether the session, the
account and its password are still current.

Its audience is the web API, so MCP access tokens (audience: the MCP server) are refused here and
web tokens are refused by the MCP server.
"""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException, status

from src.config.settings import settings
from src.models.auth import User

ISSUER = "rag-agent-auth"
WEB_AUDIENCE = "fra-web"


# Signed, not encrypted: never put secrets in the payload.
def create_token(user_id: str, name: str, role: str, department: str = "", session_id: str | None = None) -> str:
    """Create a web access token for a user."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "name": name,
        "role": role,
        "department": department,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_MINUTES),
        "iss": ISSUER,
        "aud": WEB_AUDIENCE,
    }
    if session_id:
        payload["sid"] = session_id
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> User:
    """Decode and validate a web access token. Returns User on success, raises HTTPException on failure."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            audience=WEB_AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
        return User(
            user_id=payload["sub"],
            name=payload.get("name", ""),
            role=payload.get("role", "analyst"),
            department=payload.get("department", ""),
            session_id=payload.get("sid"),
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
