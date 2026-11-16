"""MCP sign-in with OAuth, end to end: discovery, registration, consent, PKCE code exchange, tool
calls, refresh-token rotation, reuse detection, revocation and the account checks at renewal.

Runs against the isolated TEST_POSTGRES_URL database (skipped without it); no network or models.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.api.main import app
from src.config.settings import settings
from src.mcp_server import knowledge
from src.mcp_server.server import http_lifespan
from src.services import user_store
from tests.unit.test_mcp_server import DOCS, HEADERS, INIT

PASSWORD = "correct-horse-battery"
REDIRECT = "http://localhost:33418/callback"


@pytest.fixture
async def mcp(auth_pool, monkeypatch):
    monkeypatch.setattr(knowledge, "load_documents", lambda: DOCS)
    for username, role in (("admin", "admin"), ("analyst", "analyst"), ("hr", "hr")):
        await user_store.create_account(auth_pool, username, username.title(), role, PASSWORD)
    # The session manager's task group must start and stop in one task, so it runs in its own.
    started, stop = asyncio.Event(), asyncio.Event()

    async def serve():
        async with http_lifespan():
            started.set()
            await stop.wait()

    task = asyncio.create_task(serve())
    await started.wait()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as client:
            yield client
    finally:
        stop.set()
        await task


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def register(c: httpx.AsyncClient, redirect: str = REDIRECT, name: str = "Claude Code") -> dict:
    response = await c.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def start(c: httpx.AsyncClient, client_id: str, challenge: str, state: str = "xyz", **extra) -> str:
    """Begin authorization; return the consent request id."""
    response = await c.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": settings.mcp_resource_url,
            **extra,
        },
    )
    assert response.status_code == 302, response.text
    location = urlparse(response.headers["location"])
    assert location.path == "/oauth/consent"
    return parse_qs(location.query)["request"][0]


async def web_login(c: httpx.AsyncClient, username: str = "analyst", password: str = PASSWORD) -> dict:
    response = await c.post("/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def connect(c: httpx.AsyncClient, username: str = "analyst") -> tuple[dict, dict]:
    """The whole browser flow; returns the registered client and the token response."""
    client = await register(c)
    verifier, challenge = pkce()
    request_id = await start(c, client["client_id"], challenge)
    approved = await c.post(f"/v1/oauth/requests/{request_id}/approve", headers=await web_login(c, username))
    assert approved.status_code == 200, approved.text
    redirect = urlparse(approved.json()["redirect_url"])
    params = parse_qs(redirect.query)
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == REDIRECT and params["state"] == ["xyz"]
    tokens = await c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": params["code"][0],
            "redirect_uri": REDIRECT,
            "client_id": client["client_id"],
            "code_verifier": verifier,
            "resource": settings.mcp_resource_url,
        },
    )
    assert tokens.status_code == 200, tokens.text
    return client, tokens.json()


async def refresh(c: httpx.AsyncClient, client: dict, token: str) -> httpx.Response:
    return await c.post(
        "/token", data={"grant_type": "refresh_token", "refresh_token": token, "client_id": client["client_id"]}
    )


def mcp_headers(tokens: dict) -> dict:
    return {**HEADERS, "Authorization": f"Bearer {tokens['access_token']}"}


async def list_documents(c: httpx.AsyncClient, tokens: dict) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_documents", "arguments": {}}}
    return await c.post("/mcp", json=body, headers=mcp_headers(tokens))


# ── Discovery and registration ───────────────────────────────────────────────


async def test_discovery_points_clients_to_sign_in(mcp):
    challenge = await mcp.post("/mcp", json=INIT, headers=HEADERS)
    assert challenge.status_code == 401
    assert "/.well-known/oauth-protected-resource/mcp" in challenge.headers["www-authenticate"]
    resource = (await mcp.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert resource["authorization_servers"] == [settings.public_url + "/"]
    metadata = (await mcp.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["authorization_endpoint"] == settings.public_url + "/authorize"
    assert set(metadata["grant_types_supported"]) == {"authorization_code", "refresh_token"}


@pytest.mark.parametrize("redirect", ["http://evil.example/callback", "javascript:alert(1)"])
async def test_registration_refuses_unsafe_redirects(mcp, redirect):
    response = await mcp.post("/register", json={"redirect_uris": [redirect], "token_endpoint_auth_method": "none"})
    assert response.status_code == 400


async def test_registration_is_rate_limited_per_address(mcp, monkeypatch):
    route = next(r for r in app.router.routes if getattr(r, "path", None) == "/register")
    monkeypatch.setattr(route.endpoint.window, "limit", 1)
    route.endpoint.window.reset()
    await register(mcp)
    response = await mcp.post("/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"})
    assert response.status_code == 429


# ── Authorization ────────────────────────────────────────────────────────────


async def test_full_flow_gives_a_role_scoped_mcp_session(mcp):
    client, tokens = await connect(mcp, "hr")
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == settings.ACCESS_TOKEN_MINUTES * 60
    assert tokens["scope"] == "documents:read" and tokens["refresh_token"].startswith("fra_rt_")
    assert (await mcp.post("/mcp", json=INIT, headers=mcp_headers(tokens))).status_code == 200
    result = (await list_documents(mcp, tokens)).json()["result"]["structuredContent"]
    assert result["caller"]["role"] == "hr"
    # MCP tokens cannot use the web API.
    assert (
        await mcp.get("/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    ).status_code == 401
    sessions = await user_store.list_sessions(app.state.pool, "hr")
    assert [(s["kind"], s["client_name"]) for s in sessions if s["kind"] == "mcp"] == [("mcp", "Claude Code")]


async def test_consent_page_describes_the_request(mcp):
    client = await register(mcp)
    _, challenge = pkce()
    request_id = await start(mcp, client["client_id"], challenge)
    described = (await mcp.get(f"/v1/oauth/requests/{request_id}")).json()
    assert described["client_name"] == "Claude Code" and described["redirect_is_local"] is True
    assert described["scopes"][0]["scope"] == "documents:read"
    assert (await mcp.get("/v1/oauth/requests/not-a-real-request")).status_code == 404


async def test_approval_needs_a_signed_in_active_account(mcp):
    client = await register(mcp)
    _, challenge = pkce()
    request_id = await start(mcp, client["client_id"], challenge)
    assert (await mcp.post(f"/v1/oauth/requests/{request_id}/approve")).status_code == 401
    headers = await web_login(mcp)
    await user_store.set_active(app.state.pool, "analyst", False)
    assert (await mcp.post(f"/v1/oauth/requests/{request_id}/approve", headers=headers)).status_code == 401


async def test_declining_returns_access_denied(mcp):
    client = await register(mcp)
    _, challenge = pkce()
    request_id = await start(mcp, client["client_id"], challenge, state="s1")
    denied = await mcp.post(f"/v1/oauth/requests/{request_id}/deny", headers=await web_login(mcp))
    params = parse_qs(urlparse(denied.json()["redirect_url"]).query)
    assert params["error"] == ["access_denied"] and params["state"] == ["s1"]
    # A request is single use.
    assert (await mcp.post(f"/v1/oauth/requests/{request_id}/approve", headers=await web_login(mcp))).status_code == 404


async def test_tokens_for_another_resource_are_refused(mcp):
    client = await register(mcp)
    _, challenge = pkce()
    response = await mcp.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "state": "s",
            "resource": "https://other.example/mcp",
        },
    )
    assert response.status_code == 302
    assert parse_qs(urlparse(response.headers["location"]).query)["error"] == ["invalid_request"]


async def test_wrong_pkce_verifier_and_reused_codes_are_refused(mcp):
    client = await register(mcp)
    verifier, challenge = pkce()
    request_id = await start(mcp, client["client_id"], challenge)
    approved = await mcp.post(f"/v1/oauth/requests/{request_id}/approve", headers=await web_login(mcp))
    code = parse_qs(urlparse(approved.json()["redirect_url"]).query)["code"][0]
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": client["client_id"],
    }
    wrong = await mcp.post("/token", data={**form, "code_verifier": pkce()[0]})
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    first = await mcp.post("/token", data={**form, "code_verifier": verifier})
    assert first.status_code == 200
    again = await mcp.post("/token", data={**form, "code_verifier": verifier})
    assert again.status_code == 400
    # Using a code twice also ends the session the first use created.
    assert (await refresh(mcp, client, first.json()["refresh_token"])).status_code == 400


# ── Renewal ──────────────────────────────────────────────────────────────────


async def test_refresh_rotates_and_reuse_ends_the_connection(mcp, monkeypatch):
    monkeypatch.setattr(settings, "REFRESH_REUSE_GRACE_SECONDS", 0)
    client, tokens = await connect(mcp)
    renewed = await refresh(mcp, client, tokens["refresh_token"])
    assert renewed.status_code == 200
    new = renewed.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    assert (await list_documents(mcp, new)).status_code == 200
    # The old refresh token is used again: the connection ends, including the newest token.
    assert (await refresh(mcp, client, tokens["refresh_token"])).status_code == 400
    assert (await refresh(mcp, client, new["refresh_token"])).status_code == 400


async def test_another_client_cannot_use_the_refresh_token(mcp):
    _, tokens = await connect(mcp)
    other = await register(mcp, name="Other")
    assert (await refresh(mcp, other, tokens["refresh_token"])).status_code == 400


@pytest.mark.parametrize("action", ["password", "deactivate", "sign_out"])
async def test_account_changes_stop_mcp_renewal(mcp, action):
    client, tokens = await connect(mcp)
    pool = app.state.pool
    if action == "password":
        await user_store.set_password(pool, "analyst", "a-new-long-password", actor="admin")
    elif action == "deactivate":
        await user_store.set_active(pool, "analyst", False, actor="admin")
    else:
        await user_store.revoke_sessions(pool, "analyst", "signed_out_by_admin")
    response = await refresh(mcp, client, tokens["refresh_token"])
    assert response.status_code == 400 and response.json()["error"] == "invalid_grant"


async def test_client_revocation_ends_the_connection(mcp):
    client, tokens = await connect(mcp)
    revoked = await mcp.post(
        # The SDK's revocation form requires the client_secret field, empty for public clients.
        "/revoke",
        data={"token": tokens["refresh_token"], "client_id": client["client_id"], "client_secret": ""},
    )
    assert revoked.status_code == 200
    assert (await refresh(mcp, client, tokens["refresh_token"])).status_code == 400
    events = [e["event"] for e in await user_store.recent_events(app.state.pool)]
    assert "mcp_connected" in events and "mcp_disconnected" in events


async def test_users_can_disconnect_their_mcp_clients_from_the_web(mcp):
    client, tokens = await connect(mcp)
    headers = await web_login(mcp)
    sessions = (await mcp.get("/v1/auth/sessions", headers=headers)).json()["sessions"]
    mcp_session = next(s for s in sessions if s["kind"] == "mcp")
    assert (await mcp.delete(f"/v1/auth/sessions/{mcp_session['id']}", headers=headers)).status_code == 204
    assert (await refresh(mcp, client, tokens["refresh_token"])).status_code == 400
