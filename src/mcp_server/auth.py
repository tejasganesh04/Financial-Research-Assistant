"""OAuth sign-in for MCP clients, with the same accounts as the web app.

The app is its own OAuth 2.1 authorization server (the MCP SDK provides the HTTP handlers; this
module provides the storage and decisions):

1. A client such as Claude Code reads /.well-known/oauth-protected-resource/mcp, registers itself
   (dynamic client registration) and opens /authorize in the user's browser, with PKCE.
2. `authorize` saves the request and sends the browser to the app's consent page
   (/oauth/consent?request=...), where the user signs in with their username and password and
   approves the connection (src/api/routes/oauth.py).
3. The approval creates a one-time code; the client exchanges it at /token for a 60-minute access
   token and a refresh token, and renews with the refresh token (rotated on every use).

Access tokens are JWTs whose audience is the MCP server's URL, so the web API refuses them and the
MCP server refuses web tokens. Renewal checks the account in the database, so a password change,
deactivation or "sign out everywhere" stops an MCP client within an hour at most.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import jwt
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg.types.json import Jsonb

from src.config.rbac_config import ROLE_PERMISSIONS
from src.config.settings import settings
from src.mcp_server.knowledge import Caller
from src.services import user_store
from src.services.user_store import _query

logger = logging.getLogger(__name__)

SCOPE = "documents:read"
REQUEST_MINUTES = 10  # time to sign in and approve on the consent page
CODE_SECONDS = 120  # time for the client to exchange the code
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _issuer() -> str:
    return settings.public_url


def _same_resource(a: str | None, b: str) -> bool:
    return (a or "").rstrip("/").lower() == b.rstrip("/").lower()


def create_access_token(account: user_store.Account, session_id: str, client_id: str, scopes: list[str]) -> str:
    now = int(time.time())
    payload = {
        "sub": account.username,
        "name": account.name,
        "role": account.role,
        "department": account.department,
        "sid": session_id,
        "client_id": client_id,
        "scope": " ".join(scopes),
        "iat": now,
        "exp": now + settings.ACCESS_TOKEN_MINUTES * 60,
        "iss": _issuer(),
        "aud": settings.mcp_resource_url,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.mcp_resource_url,
            issuer=_issuer(),
            options={"require": ["exp", "iat", "sub", "aud", "iss", "sid", "client_id"]},
        )
    except jwt.InvalidTokenError as error:
        logger.info("MCP access token rejected: %s", type(error).__name__)
        return None


def caller_from_claims(claims: dict) -> Caller | None:
    role = claims.get("role")
    if role not in ROLE_PERMISSIONS or SCOPE not in str(claims.get("scope", "")).split():
        return None
    return Caller(
        user_id=str(claims["sub"]),
        name=str(claims.get("name", "")),
        role=role,
        department=str(claims.get("department", "")),
        token_id=str(claims.get("sid", "")),
    )


def _check_redirect_uris(client: OAuthClientInformationFull) -> None:
    for uri in client.redirect_uris or []:
        parsed = urlparse(str(uri))
        if parsed.fragment:
            raise RegistrationError("invalid_redirect_uri", "Redirect URIs must not contain a fragment.")
        if parsed.scheme == "http" and (parsed.hostname or "") not in LOOPBACK_HOSTS:
            raise RegistrationError(
                "invalid_redirect_uri", "Plain http redirect URIs are only allowed for localhost; use https."
            )
        if parsed.scheme in ("javascript", "data", "file"):
            raise RegistrationError("invalid_redirect_uri", f"Redirect URI scheme {parsed.scheme!r} is not allowed.")


class OAuthProvider:
    """OAuthAuthorizationServerProvider backed by PostgreSQL (the API's pool, set at startup)."""

    def __init__(self):
        self.pool = None

    def _pool(self):
        if self.pool is None:
            raise TokenError("invalid_request", "Sign-in is temporarily unavailable.")
        return self.pool

    # --- Clients --------------------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if self.pool is None or not client_id or len(client_id) > 100:
            return None
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT info FROM oauth_clients WHERE client_id = %s", (client_id,))
            row = await cur.fetchone()
        return OAuthClientInformationFull.model_validate(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        _check_redirect_uris(client_info)
        if client_info.client_name and len(client_info.client_name) > 120:
            client_info.client_name = client_info.client_name[:120]
        async with self._pool().connection() as conn:
            await conn.execute(
                "INSERT INTO oauth_clients (client_id, info) VALUES (%s, %s)",
                (client_info.client_id, Jsonb(client_info.model_dump(mode="json", exclude_none=True))),
            )
        logger.info("OAuth client registered: %s (%s)", client_info.client_id, client_info.client_name)

    # --- Authorization --------------------------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource is not None and not _same_resource(params.resource, settings.mcp_resource_url):
            raise AuthorizeError("invalid_request", "This server only issues tokens for its own MCP endpoint.")
        request_id = secrets.token_urlsafe(24)
        async with self._pool().connection() as conn:
            await conn.execute(
                "INSERT INTO oauth_requests (id, client_id, params, expires_at)"
                " VALUES (%s, %s, %s, now() + make_interval(mins => %s))",
                (request_id, client.client_id, Jsonb(params.model_dump(mode="json")), REQUEST_MINUTES),
            )
        return f"{settings.public_url}/oauth/consent?request={request_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        async with self._pool().connection() as conn:
            cur = await _query(
                conn,
                "SELECT client_id, username, data, extract(epoch FROM expires_at) AS expires_at, used_at"
                " FROM oauth_codes WHERE code_hash = %s AND client_id = %s",
                (user_store.token_hash(authorization_code), client.client_id),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        if row["used_at"] is not None:
            # A code used twice: end whatever the first use created (RFC 6749 section 4.1.2).
            session_id = row["data"].get("session_id")
            if session_id:
                await user_store.revoke_session(self.pool, session_id, "authorization_code_reused")
            return None
        data = row["data"]
        return AuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=float(row["expires_at"]),
            client_id=row["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            resource=data.get("resource"),
            subject=row["username"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        pool = self._pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE oauth_codes SET used_at = now() WHERE code_hash = %s AND used_at IS NULL RETURNING username",
                (user_store.token_hash(authorization_code.code),),
            )
            row = await cur.fetchone()
        if row is None:
            raise TokenError("invalid_grant", "The authorization code has already been used.")
        account = await user_store.get_account(pool, row[0])
        if account is None or not account.active:
            raise TokenError("invalid_grant", "This account cannot sign in.")
        session, refresh = await user_store.create_session(
            pool,
            account,
            "mcp",
            client_id=client.client_id,
            client_name=client.client_name or "MCP client",
            scopes=authorization_code.scopes,
            resource=authorization_code.resource or settings.mcp_resource_url,
        )
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE oauth_codes SET data = data || jsonb_build_object('session_id', %s::text) WHERE code_hash = %s",
                (session.id, user_store.token_hash(authorization_code.code)),
            )
        await user_store.record_event(
            pool, "mcp_connected", username=account.username, client=client.client_name, session=session.id
        )
        return OAuthToken(
            access_token=create_access_token(account, session.id, client.client_id, authorization_code.scopes),
            expires_in=settings.ACCESS_TOKEN_MINUTES * 60,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh,
        )

    # --- Refresh and access tokens ----------------------------------------------------------------

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        try:
            session = await user_store.check_refresh_token(self._pool(), refresh_token, "mcp", client.client_id)
        except user_store.SessionEnded as ended:
            logger.info("MCP refresh refused: %s", ended.reason)
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=session.scopes,
            resource=session.resource,
            subject=session.username,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        try:
            session, account, new_refresh = await user_store.rotate_refresh_token(
                self._pool(), refresh_token.token, "mcp", client.client_id
            )
        except user_store.SessionEnded as ended:
            logger.info("MCP refresh refused: %s", ended.reason)
            raise TokenError("invalid_grant", "This sign-in has ended. Sign in again.") from ended
        return OAuthToken(
            access_token=create_access_token(account, session.id, client.client_id, scopes),
            expires_in=settings.ACCESS_TOKEN_MINUTES * 60,
            scope=" ".join(scopes),
            refresh_token=new_refresh,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        claims = decode_access_token(token)
        if claims is None or caller_from_claims(claims) is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(claims["client_id"]),
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            resource=str(claims["aud"]),
            subject=str(claims["sub"]),
            claims=claims,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        pool = self._pool()
        if isinstance(token, RefreshToken):
            username = await user_store.revoke_by_refresh_token(pool, token.token, "mcp", "revoked_by_client")
        else:
            session_id = (token.claims or {}).get("sid")
            username = (
                token.subject
                if session_id and await user_store.revoke_session(pool, session_id, "revoked_by_client")
                else None
            )
        if username:
            await user_store.record_event(pool, "mcp_disconnected", username=username, client=token.client_id)

    # --- Consent page (src/api/routes/oauth.py) ---------------------------------------------------

    async def pending_request(self, request_id: str) -> tuple[OAuthClientInformationFull, AuthorizationParams] | None:
        if not request_id or len(request_id) > 64:
            return None
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT client_id, params FROM oauth_requests WHERE id = %s AND expires_at > now()", (request_id,)
            )
            row = await cur.fetchone()
        if row is None:
            return None
        client = await self.get_client(row[0])
        if client is None:
            return None
        return client, AuthorizationParams.model_validate(row[1])

    async def _take_request(self, request_id: str):
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "DELETE FROM oauth_requests WHERE id = %s AND expires_at > now() RETURNING client_id, params",
                (request_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        client = await self.get_client(row[0])
        return (client, AuthorizationParams.model_validate(row[1])) if client else None

    async def approve(self, request_id: str, account: user_store.Account) -> str | None:
        """Issue a one-time code for the request and return the client's redirect URL."""
        taken = await self._take_request(request_id)
        if taken is None:
            return None
        client, params = taken
        code = secrets.token_urlsafe(32)
        data = {
            "scopes": params.scopes or [SCOPE],
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
        }
        async with self._pool().connection() as conn:
            await conn.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, username, data, expires_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    user_store.token_hash(code),
                    client.client_id,
                    account.username,
                    Jsonb(data),
                    datetime.now(timezone.utc) + timedelta(seconds=CODE_SECONDS),
                ),
            )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def deny(self, request_id: str) -> str | None:
        taken = await self._take_request(request_id)
        if taken is None:
            return None
        _client, params = taken
        return construct_redirect_uri(
            str(params.redirect_uri),
            error="access_denied",
            error_description="The user declined the connection.",
            state=params.state,
        )


provider = OAuthProvider()
