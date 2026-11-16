"""Access tokens for MCP clients.

An MCP token is a signed JWT like an app login, with three differences:
- its audience is the MCP server, so the app's API rejects it (PyJWT refuses a token with an
  audience the API does not expect), and an app login token is rejected here;
- its scope only allows reading documents, never approvals or conversations;
- it lasts days rather than hours, and carries an id that can be revoked in settings.
"""

from __future__ import annotations

import logging
import time
import uuid

import jwt
from mcp.server.auth.provider import AccessToken

from src.config.rbac_config import ROLE_PERMISSIONS
from src.config.settings import settings
from src.mcp_server.knowledge import Caller

logger = logging.getLogger(__name__)

AUDIENCE = "financial-research-mcp"
ISSUER = "rag-agent-auth"
SCOPE = "documents:read"


class InvalidMcpToken(Exception):
    """The token cannot be used; the message is safe to show."""


def create_mcp_token(
    user_id: str, name: str, role: str, department: str = "", days: int | None = None
) -> tuple[str, int, str]:
    """Return the token, its expiry (Unix seconds) and its id."""
    days = settings.MCP_TOKEN_EXPIRY_DAYS if days is None else days
    if not 1 <= days <= settings.MCP_TOKEN_MAX_DAYS:
        raise ValueError(f"Token lifetime must be between 1 and {settings.MCP_TOKEN_MAX_DAYS} days.")
    now = int(time.time())
    expires = now + days * 86_400
    token_id = uuid.uuid4().hex
    payload = {
        "sub": user_id,
        "name": name,
        "role": role,
        "department": department,
        "iat": now,
        "exp": expires,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "scope": SCOPE,
        "jti": token_id,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM), expires, token_id


def decode_mcp_token(token: str) -> tuple[Caller, dict]:
    """Validate an MCP token and return the caller and its claims."""
    try:
        claims = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "aud", "iss", "jti"]},
        )
    except jwt.ExpiredSignatureError as error:
        raise InvalidMcpToken("The MCP token has expired. Create a new one.") from error
    except jwt.InvalidTokenError as error:
        raise InvalidMcpToken("The MCP token is not valid.") from error
    if SCOPE not in str(claims.get("scope", "")).split():
        raise InvalidMcpToken("The MCP token does not allow reading documents.")
    if claims["jti"] in settings.MCP_REVOKED_TOKEN_IDS:
        raise InvalidMcpToken("The MCP token has been revoked.")
    role = claims.get("role")
    if role not in ROLE_PERMISSIONS:
        raise InvalidMcpToken("The MCP token has an unknown role.")
    caller = Caller(
        user_id=str(claims["sub"]),
        name=str(claims.get("name", "")),
        role=role,
        department=str(claims.get("department", "")),
        token_id=str(claims["jti"]),
    )
    return caller, claims


class McpTokenVerifier:
    """TokenVerifier for the MCP SDK's bearer-token middleware."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            caller, claims = decode_mcp_token(token)
        except InvalidMcpToken as error:
            logger.info("MCP token rejected: %s", error)
            return None
        return AccessToken(
            token=token,
            client_id=caller.user_id,
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            subject=caller.user_id,
            claims={"iss": claims.get("iss"), "jti": caller.token_id, "role": caller.role},
        )
