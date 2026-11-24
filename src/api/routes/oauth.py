"""The consent step of MCP sign-in (see src/mcp_server/auth.py).

The consent page (/oauth/consent in the web app) reads the pending request, signs the user in with
the ordinary web sign-in if needed, and then approves or declines here. The response carries the
URL to send the browser back to the MCP client with a one-time code (or an access_denied error).
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.api.dependencies import client_ip, current_account, get_pool
from src.config.settings import settings
from src.services import user_store

router = APIRouter(prefix="/oauth", tags=["oauth"])

SCOPE_DESCRIPTIONS = {
    "documents:read": "Search and read the documents your role can access",
}


def _provider():
    if not settings.MCP_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connecting AI assistants is turned off.")
    from src.mcp_server.auth import provider

    return provider


@router.get("/server")
async def server_info() -> dict:
    """Where MCP clients connect (shown on the Connect page)."""
    return {"enabled": settings.MCP_ENABLED, "mcp_url": settings.mcp_resource_url}


EXPIRED = "This connection request has expired or was already used. Start the connection again from your client."


@router.get("/requests/{request_id}")
async def describe_request(request_id: str, pool=Depends(get_pool)) -> dict:
    """What is asking for access, for the consent page (no sign-in needed; the id is unguessable)."""
    pending = await _provider().pending_request(request_id)
    if pending is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=EXPIRED)
    client, params = pending
    redirect = urlparse(str(params.redirect_uri))
    scopes = params.scopes or ["documents:read"]
    return {
        "client_name": client.client_name or "An MCP client",
        "redirect_host": redirect.hostname or redirect.scheme,
        "redirect_is_local": (redirect.hostname or "") in ("localhost", "127.0.0.1", "::1"),
        "scopes": [{"scope": s, "description": SCOPE_DESCRIPTIONS.get(s, s)} for s in scopes],
        "resource": settings.mcp_resource_url,
        "access_token_minutes": settings.ACCESS_TOKEN_MINUTES,
    }


@router.post("/requests/{request_id}/approve")
async def approve(
    request_id: str,
    request: Request,
    account: user_store.Account = Depends(current_account),
    pool=Depends(get_pool),
) -> dict:
    redirect_url = await _provider().approve(request_id, account)
    if redirect_url is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=EXPIRED)
    await user_store.record_event(pool, "mcp_approved", username=account.username, ip=client_ip(request))
    return {"redirect_url": redirect_url}


@router.post("/requests/{request_id}/deny")
async def deny(request_id: str, account: user_store.Account = Depends(current_account)) -> dict:
    redirect_url = await _provider().deny(request_id)
    if redirect_url is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=EXPIRED)
    return {"redirect_url": redirect_url}
