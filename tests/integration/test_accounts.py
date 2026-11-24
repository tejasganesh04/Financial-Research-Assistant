"""Accounts, web sign-in sessions, refresh-token rotation, lockout, admin actions and quotas.

Runs against the isolated TEST_POSTGRES_URL database (skipped without it); no model calls.
"""

from __future__ import annotations

import httpx
import pytest

from src.api.main import app
from src.api.routes.auth import COOKIE_PATH, REFRESH_COOKIE
from src.config.settings import settings
from src.services import passwords, user_store
from src.services.rate_limit import QuotaExceeded, quota_check

PASSWORD = "correct-horse-battery"


@pytest.fixture
async def accounts(auth_pool):
    for username, role in (("admin", "admin"), ("analyst", "analyst"), ("finance", "finance")):
        await user_store.create_account(auth_pool, username, username.title(), role, PASSWORD, "Dept")
    return auth_pool


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000")


async def sign_in(c: httpx.AsyncClient, username="analyst", password=PASSWORD) -> dict:
    response = await c.post("/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def bearer(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


# ── Sign-in ──────────────────────────────────────────────────────────────────


async def test_sign_in_returns_access_token_and_sets_a_locked_down_cookie(accounts):
    async with client() as c:
        response = await c.post("/v1/auth/login", json={"username": " Analyst ", "password": PASSWORD})
        assert response.status_code == 200
        body = response.json()
        assert body["user"]["user_id"] == "analyst" and body["user"]["role"] == "analyst"
        assert body["expires_in"] == settings.ACCESS_TOKEN_MINUTES * 60
        assert "refresh" not in response.text.lower().replace("fra_refresh", "")  # never in the body
        cookie = response.headers["set-cookie"]
        assert cookie.startswith(f"{REFRESH_COOKIE}=fra_rt_")
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and f"Path={COOKIE_PATH}" in cookie
        assert response.headers["cache-control"] == "no-store"
        me = await c.get("/v1/auth/me", headers=bearer(body))
        assert me.status_code == 200 and me.json()["user_id"] == "analyst"


@pytest.mark.parametrize(("username", "password"), [("analyst", "wrong-password-123"), ("nobody", PASSWORD)])
async def test_wrong_password_and_unknown_user_get_the_same_answer(accounts, username, password):
    async with client() as c:
        response = await c.post("/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect username or password."
    assert "set-cookie" not in response.headers


async def test_account_locks_after_repeated_failures(accounts, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 3)
    async with client() as c:
        codes = [
            (await c.post("/v1/auth/login", json={"username": "analyst", "password": "nope-nope-nope"})).status_code
            for _ in range(3)
        ]
        assert codes == [401, 401, 429]
        locked = await c.post("/v1/auth/login", json={"username": "analyst", "password": PASSWORD})
        assert locked.status_code == 429 and "Too many failed" in locked.json()["detail"]
        await user_store.unlock(accounts, "analyst")
        await sign_in(c)


async def test_sign_in_attempts_are_limited_per_address(accounts, monkeypatch):
    from src.api.routes import auth

    monkeypatch.setattr(auth.login_limiter, "limit", 2)
    async with client() as c:
        await sign_in(c)
        await sign_in(c)
        response = await c.post("/v1/auth/login", json={"username": "analyst", "password": PASSWORD})
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0


async def test_deactivated_accounts_cannot_sign_in_or_renew(accounts):
    async with client() as c:
        await sign_in(c)
        await user_store.set_active(accounts, "analyst", False)
        assert (await c.post("/v1/auth/refresh")).status_code == 401
        response = await c.post("/v1/auth/login", json={"username": "analyst", "password": PASSWORD})
        assert response.status_code == 403 and "deactivated" in response.json()["detail"]


async def test_old_password_hashes_are_upgraded_at_sign_in(accounts):
    salt = b"0123456789abcdef"
    older = f"scrypt$16384$8$1${passwords._b64(salt)}${passwords._b64(passwords._derive(PASSWORD, salt, 16384, 8, 1))}"
    async with accounts.connection() as conn:
        await conn.execute("UPDATE app_users SET password_hash = %s WHERE username = 'analyst'", (older,))
    async with client() as c:
        await sign_in(c)
    upgraded = (await user_store.get_account(accounts, "analyst")).password_hash
    assert upgraded != older and not passwords.needs_rehash(upgraded)
    assert passwords.verify_password(PASSWORD, upgraded)


# ── Refresh and sign-out ─────────────────────────────────────────────────────


async def test_refresh_rotates_the_cookie_and_issues_a_new_access_token(accounts):
    async with client() as c:
        first = await sign_in(c)
        old_cookie = c.cookies.get(REFRESH_COOKIE)
        renewed = await c.post("/v1/auth/refresh")
        assert renewed.status_code == 200
        assert renewed.json()["user"]["user_id"] == "analyst"
        assert c.cookies.get(REFRESH_COOKIE) != old_cookie
        assert (await c.get("/v1/auth/me", headers=bearer(renewed.json()))).status_code == 200
        assert (await c.get("/v1/auth/me", headers=bearer(first))).status_code == 200  # valid until it expires


async def test_reusing_an_old_refresh_token_ends_the_session(accounts, monkeypatch):
    monkeypatch.setattr(settings, "REFRESH_REUSE_GRACE_SECONDS", 0)
    async with client() as c:
        await sign_in(c)
        stolen = c.cookies.get(REFRESH_COOKIE)
        assert (await c.post("/v1/auth/refresh")).status_code == 200
        # The copy is used again: the whole session ends, including the legitimate new token.
        c2 = client()
        async with c2:
            c2.cookies.set(REFRESH_COOKIE, stolen, domain="localhost.local", path=COOKIE_PATH)
            assert (await c2.post("/v1/auth/refresh")).status_code == 401
        assert (await c.post("/v1/auth/refresh")).status_code == 401
    events = [e["event"] for e in await user_store.recent_events(accounts)]
    assert "refresh_token_reused" in events


async def test_two_tabs_renewing_at_once_do_not_end_the_session(accounts):
    async with client() as c:
        await sign_in(c)
        token = c.cookies.get(REFRESH_COOKIE)
        assert (await c.post("/v1/auth/refresh")).status_code == 200
        # A second tab sends the same (now used) token within the grace period: refused, session kept.
        with pytest.raises(user_store.SessionEnded, match="concurrent_refresh"):
            await user_store.rotate_refresh_token(accounts, token, "web")
        assert (await c.post("/v1/auth/refresh")).status_code == 200


async def test_sign_out_ends_the_session(accounts):
    async with client() as c:
        await sign_in(c)
        token = c.cookies.get(REFRESH_COOKIE)
        response = await c.post("/v1/auth/logout")
        assert response.status_code == 204
        assert REFRESH_COOKIE not in c.cookies
        with pytest.raises(user_store.SessionEnded):
            await user_store.rotate_refresh_token(accounts, token, "web")


async def test_refresh_without_a_cookie_is_refused(accounts):
    async with client() as c:
        assert (await c.post("/v1/auth/refresh")).status_code == 401


# ── Own password and sessions ────────────────────────────────────────────────


async def test_changing_your_password_signs_out_other_sessions(accounts):
    async with client() as laptop, client() as phone:
        session = await sign_in(laptop)
        await sign_in(phone)
        wrong = await laptop.post(
            "/v1/auth/password",
            json={"current_password": "not-it-at-all", "new_password": "a-brand-new-passphrase"},
            headers=bearer(session),
        )
        assert wrong.status_code == 400
        weak = await laptop.post(
            "/v1/auth/password", json={"current_password": PASSWORD, "new_password": "short"}, headers=bearer(session)
        )
        assert weak.status_code == 400 and "at least" in weak.json()["detail"]
        changed = await laptop.post(
            "/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": "a-brand-new-passphrase"},
            headers=bearer(session),
        )
        assert changed.status_code == 200
        assert (await laptop.post("/v1/auth/refresh")).status_code == 200  # this browser got a new session
        assert (await phone.post("/v1/auth/refresh")).status_code == 401
        await sign_in(phone, password="a-brand-new-passphrase")


async def test_users_see_and_end_their_own_sessions(accounts):
    async with client() as laptop, client() as phone:
        session = await sign_in(laptop)
        await sign_in(phone)
        listed = (await laptop.get("/v1/auth/sessions", headers=bearer(session))).json()["sessions"]
        assert len(listed) == 2 and sum(s["current"] for s in listed) == 1
        other = next(s for s in listed if not s["current"])
        assert (await laptop.delete(f"/v1/auth/sessions/{other['id']}", headers=bearer(session))).status_code == 204
        assert (await phone.post("/v1/auth/refresh")).status_code == 401
        # Someone else's session id is not found.
        admin = await sign_in(phone, "admin")
        admin_session = (await phone.get("/v1/auth/sessions", headers=bearer(admin))).json()["sessions"][0]
        response = await laptop.delete(f"/v1/auth/sessions/{admin_session['id']}", headers=bearer(session))
        assert response.status_code == 404


async def test_signing_out_of_all_other_sessions_keeps_this_one(accounts):
    async with client() as laptop, client() as phone, client() as tablet, client() as admin_browser:
        session = await sign_in(laptop)
        await sign_in(phone)
        await sign_in(tablet)
        admin = await sign_in(admin_browser, "admin")
        response = await laptop.post("/v1/auth/sessions/sign-out-others", headers=bearer(session))
        assert response.status_code == 200 and response.json() == {"ended": 2}
        assert (await phone.post("/v1/auth/refresh")).status_code == 401
        assert (await tablet.post("/v1/auth/refresh")).status_code == 401
        assert (await laptop.post("/v1/auth/refresh")).status_code == 200
        # Other people's sessions are untouched.
        assert (await admin_browser.get("/v1/auth/me", headers=bearer(admin))).status_code == 200
        assert (await admin_browser.post("/v1/auth/refresh")).status_code == 200


# ── Administration ───────────────────────────────────────────────────────────


async def test_admin_pages_are_for_admins_only(accounts):
    async with client() as c:
        analyst = await sign_in(c)
        assert (await c.get("/v1/admin/users", headers=bearer(analyst))).status_code == 403
        assert (await c.get("/v1/admin/users")).status_code == 401


async def test_admin_lists_users_with_session_counts(accounts):
    async with client() as c, client() as other:
        await sign_in(other)
        admin = await sign_in(c, "admin")
        users = {u["username"]: u for u in (await c.get("/v1/admin/users", headers=bearer(admin))).json()["users"]}
        assert set(users) == {"admin", "analyst", "finance"}
        assert users["analyst"]["web_sessions"] == 1 and users["finance"]["web_sessions"] == 0
        assert users["analyst"]["role_label"] == "Analyst" and users["analyst"]["last_login_at"]
        assert "password_hash" not in users["analyst"]


async def test_admin_resets_a_password_and_the_user_is_signed_out(accounts):
    async with client() as c, client() as user:
        await sign_in(user)
        admin = await sign_in(c, "admin")
        reset = await c.post("/v1/admin/users/analyst/password", json={}, headers=bearer(admin))
        assert reset.status_code == 200
        generated = reset.json()["password"]
        assert len(generated) == 19 and passwords.password_problem(generated) is None
        assert (await user.post("/v1/auth/refresh")).status_code == 401
        await sign_in(user, password=generated)
        chosen = await c.post(
            "/v1/admin/users/analyst/password", json={"password": "chosen-by-the-admin"}, headers=bearer(admin)
        )
        assert chosen.json()["password"] is None
        await sign_in(user, password="chosen-by-the-admin")


async def test_admin_signs_a_user_out_everywhere(accounts):
    async with client() as c, client() as user:
        await sign_in(user)
        admin = await sign_in(c, "admin")
        response = await c.post("/v1/admin/users/analyst/sign-out", headers=bearer(admin))
        assert response.json() == {"ended_sessions": 1}
        assert (await user.post("/v1/auth/refresh")).status_code == 401
        await sign_in(user)  # the password still works


async def test_admin_deactivates_and_reactivates(accounts):
    async with client() as c, client() as user:
        await sign_in(user)
        admin = await sign_in(c, "admin")
        assert (await c.post("/v1/admin/users/admin/deactivate", headers=bearer(admin))).status_code == 400
        off = await c.post("/v1/admin/users/analyst/deactivate", headers=bearer(admin))
        assert off.json()["user"]["active"] is False
        assert (await user.post("/v1/auth/refresh")).status_code == 401
        on = await c.post("/v1/admin/users/analyst/activate", headers=bearer(admin))
        assert on.json()["user"]["active"] is True
        await sign_in(user)


@pytest.mark.parametrize("action", ["deactivate", "password", "sign-out"])
async def test_admin_actions_stop_the_access_the_user_already_holds(accounts, action):
    """Not only renewal: the access token in the person's browser stops working on the next request."""
    async with client() as c, client() as user:
        held = await sign_in(user)
        admin = await sign_in(c, "admin")
        # Signed in: their identity loads and a document request gets past sign-in (to "not found").
        assert (await user.get("/v1/auth/me", headers=bearer(held))).status_code == 200
        assert (await user.get("/v1/documents/none.pdf", headers=bearer(held))).status_code == 404
        assert (await c.post(f"/v1/admin/users/analyst/{action}", json={}, headers=bearer(admin))).status_code == 200
        for path in ("/v1/auth/me", "/v1/documents/none.pdf"):
            refused = await user.get(path, headers=bearer(held))
            assert refused.status_code == 401, path
            assert refused.json()["detail"] == "Your session has ended. Sign in again."
        # The administrator's own session is untouched.
        assert (await c.get("/v1/auth/me", headers=bearer(admin))).status_code == 200


async def test_admin_ends_one_session_and_the_others_keep_working(accounts):
    async with client() as c, client() as laptop, client() as phone:
        on_laptop = await sign_in(laptop)
        on_phone = await sign_in(phone)
        mine = (await laptop.get("/v1/auth/sessions", headers=bearer(on_laptop))).json()["sessions"]
        laptop_id = next(s["id"] for s in mine if s["current"])
        phone_id = next(s["id"] for s in mine if not s["current"])
        admin = await sign_in(c, "admin")
        sessions = (await c.get("/v1/admin/users/analyst/sessions", headers=bearer(admin))).json()["sessions"]
        assert {s["id"] for s in sessions} == {laptop_id, phone_id}
        ended = await c.delete(f"/v1/admin/users/analyst/sessions/{laptop_id}", headers=bearer(admin))
        assert ended.status_code == 204
        assert (await laptop.get("/v1/auth/me", headers=bearer(on_laptop))).status_code == 401
        assert (await laptop.post("/v1/auth/refresh")).status_code == 401
        assert (await phone.get("/v1/auth/me", headers=bearer(on_phone))).status_code == 200
        # A session cannot be ended through another person's account.
        other = await c.delete(f"/v1/admin/users/finance/sessions/{phone_id}", headers=bearer(admin))
        assert other.status_code == 404
        assert (await phone.get("/v1/auth/me", headers=bearer(on_phone))).status_code == 200


async def test_admin_unlocks_a_locked_account(accounts, monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 2)
    async with client() as c, client() as user:
        admin = await sign_in(c, "admin")
        for _ in range(2):
            await user.post("/v1/auth/login", json={"username": "analyst", "password": "nope-nope-nope"})
        locked = await user.post("/v1/auth/login", json={"username": "analyst", "password": PASSWORD})
        assert locked.status_code == 429
        listed = (await c.get("/v1/admin/users", headers=bearer(admin))).json()["users"]
        assert next(u for u in listed if u["username"] == "analyst")["locked_until"] is not None
        unlocked = await c.post("/v1/admin/users/analyst/unlock", headers=bearer(admin))
        assert unlocked.status_code == 200 and unlocked.json()["user"]["locked_until"] is None
        await sign_in(user)
        events = [e["event"] for e in await user_store.recent_events(accounts)]
        assert "account_unlocked" in events


async def test_admin_creates_users_and_rejects_bad_ones(accounts):
    async with client() as c:
        admin = await sign_in(c, "admin")
        created = await c.post(
            "/v1/admin/users",
            json={"username": "Jane.Doe", "name": "Jane Doe", "role": "finance", "department": "FP&A"},
            headers=bearer(admin),
        )
        assert created.status_code == 201
        assert created.json()["user"]["username"] == "jane.doe" and created.json()["password"]
        for bad in (
            {"username": "jane.doe", "name": "Again", "role": "finance"},
            {"username": "x", "name": "Too short", "role": "finance"},
            {"username": "bob", "name": "Bob", "role": "superuser"},
            {"username": "bob", "name": "Bob", "role": "hr", "password": "short"},
        ):
            response = await c.post("/v1/admin/users", json=bad, headers=bearer(admin))
            assert response.status_code == 400, bad
        await sign_in(c, "jane.doe", created.json()["password"])


async def test_a_deactivated_admin_loses_admin_actions_at_once(accounts):
    async with client() as c:
        admin = await sign_in(c, "admin")
        await user_store.create_account(accounts, "admin2", "Second", "admin", PASSWORD)
        await user_store.set_active(accounts, "admin", False, actor="admin2")
        # The access token is still within its hour, but admin actions check the database.
        assert (await c.get("/v1/admin/users", headers=bearer(admin))).status_code == 401


async def test_activity_log_records_sign_ins_and_admin_actions(accounts):
    async with client() as c:
        await c.post("/v1/auth/login", json={"username": "analyst", "password": "wrong-wrong-wrong"})
        admin = await sign_in(c, "admin")
        await c.post("/v1/admin/users/analyst/sign-out", headers=bearer(admin))
        events = (await c.get("/v1/admin/activity", headers=bearer(admin))).json()["events"]
    kinds = [(e["event"], e["username"], e["actor"]) for e in events]
    assert ("login_failed", "analyst", None) in kinds
    assert ("login", "admin", None) in kinds
    assert ("signed_out_everywhere", "analyst", "admin") in kinds


# ── Quotas ───────────────────────────────────────────────────────────────────


async def test_question_quota_counts_per_user_and_window(accounts):
    limits = [(2, 3600, "questions per hour")]
    await quota_check(accounts, "questions:analyst", limits)
    await quota_check(accounts, "questions:analyst", limits)
    with pytest.raises(QuotaExceeded, match="limit of 2 questions per hour") as error:
        await quota_check(accounts, "questions:analyst", limits)
    assert 0 < error.value.retry_after <= 3600
    await quota_check(accounts, "questions:finance", limits)


async def test_question_endpoint_answers_429_over_quota(accounts, monkeypatch):
    monkeypatch.setattr(settings, "QUESTIONS_PER_HOUR", 1)
    async with client() as c:
        session = await sign_in(c)
        # The quota is checked before the question is read, so a blank question exercises it without a model.
        first = await c.post("/v1/conversations/messages", json={"message": " "}, headers=bearer(session))
        assert first.status_code == 422
        second = await c.post("/v1/conversations/messages", json={"message": " "}, headers=bearer(session))
        assert second.status_code == 429 and "Retry-After" in second.headers
        assert "1 questions per hour" in second.json()["detail"]


# ── Schema and housekeeping ──────────────────────────────────────────────────


async def test_schema_setup_is_repeatable_and_purge_runs(accounts):
    await user_store.ensure_schema(accounts)
    await user_store.purge_expired(accounts)
    assert await user_store.count_accounts(accounts) == 3


async def test_admin_edits_profiles_and_passwords_cannot_contain_the_username(accounts):
    async with client() as c:
        admin = await sign_in(c, "admin")
        edited = await c.patch(
            "/v1/admin/users/analyst", json={"name": "Riley Chen", "department": "Equity"}, headers=bearer(admin)
        )
        assert edited.status_code == 200
        assert edited.json()["user"]["name"] == "Riley Chen" and edited.json()["user"]["department"] == "Equity"
        assert (await c.patch("/v1/admin/users/nobody", json={"name": "X"}, headers=bearer(admin))).status_code == 404
        weak = await c.post(
            "/v1/admin/users/analyst/password", json={"password": "my-analyst-password"}, headers=bearer(admin)
        )
        assert weak.status_code == 400 and "username" in weak.json()["detail"]
