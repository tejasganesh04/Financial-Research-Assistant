// API-level security checks against a running server: who can reach what.
//
//   BASE=http://localhost:8000 ADMIN_PASSWORD=... node tests/security-check.mjs
//
// It creates two temporary accounts (an analyst and an HR user), checks that each is confined to
// its own data and role, and deactivates both at the end. It asks no questions of the answer model.
import crypto from "node:crypto";

const BASE = (process.env.BASE || "http://localhost:8000").replace(/\/$/, "");
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD;
if (!ADMIN_PASSWORD) {
  console.error("Set ADMIN_PASSWORD.");
  process.exit(2);
}
const results = [];
function check(name, ok, detail = "") {
  results.push(ok);
  console.log(
    `  ${ok ? "ok  " : "FAIL"}  ${name}${ok || !detail ? "" : `\n        ${detail}`}`,
  );
}

async function call(method, path, { token, body, headers = {}, raw } = {}) {
  const response = await fetch(BASE + path, {
    method,
    redirect: "manual",
    headers: {
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(body && !raw ? { "content-type": "application/json" } : {}),
      ...headers,
    },
    body: raw ?? (body ? JSON.stringify(body) : undefined),
  });
  let data = null;
  const text = await response.text();
  try {
    data = JSON.parse(text);
  } catch {
    data = text;
  }
  return { status: response.status, data, headers: response.headers };
}

async function signIn(username, password) {
  const response = await call("POST", "/v1/auth/login", {
    body: { username, password },
  });
  if (response.status !== 200)
    throw new Error(`sign-in ${username}: ${response.status}`);
  return {
    token: response.data.access_token,
    cookie: response.headers.get("set-cookie"),
  };
}

const stamp = Date.now().toString(36);
const created = [];
const admin = await signIn("admin", ADMIN_PASSWORD);

async function makeUser(role) {
  const username = `e2e-${role}-${stamp}`;
  const response = await call("POST", "/v1/admin/users", {
    token: admin.token,
    body: { username, name: `E2E ${role}`, role, department: "" },
  });
  if (response.status !== 201 && response.status !== 200)
    throw new Error(
      `create ${role}: ${response.status} ${JSON.stringify(response.data)}`,
    );
  created.push(username);
  return { username, ...(await signIn(username, response.data.password)) };
}

console.log(`Security checks against ${BASE}`);
try {
  const analyst = await makeUser("analyst");
  const hr = await makeUser("hr");

  // ── Authentication ───────────────────────────────────────────────────────
  for (const path of [
    "/v1/conversations",
    "/v1/auth/me",
    "/v1/approvals",
    "/v1/admin/users",
  ]) {
    const r = await call("GET", path);
    check(
      `no token: GET ${path} is refused`,
      r.status === 401,
      `got ${r.status}`,
    );
  }
  {
    const [h, p] = analyst.token.split(".");
    const payload = JSON.parse(Buffer.from(p, "base64url").toString());
    payload.role = "admin";
    const forged = `${h}.${Buffer.from(JSON.stringify(payload)).toString("base64url")}.${analyst.token.split(".")[2]}`;
    const r = await call("GET", "/v1/admin/users", { token: forged });
    check(
      "a token edited to say admin is refused",
      r.status === 401,
      `got ${r.status}`,
    );
    const none = `${Buffer.from(JSON.stringify({ alg: "none", typ: "JWT" })).toString("base64url")}.${Buffer.from(JSON.stringify(payload)).toString("base64url")}.`;
    const r2 = await call("GET", "/v1/auth/me", { token: none });
    check(
      "an unsigned (alg=none) token is refused",
      r2.status === 401,
      `got ${r2.status}`,
    );
  }
  {
    const r = await call("POST", "/mcp", {
      token: analyst.token,
      body: { jsonrpc: "2.0", id: 1, method: "tools/list" },
      headers: { accept: "application/json, text/event-stream" },
    });
    check(
      "a web access token is not accepted by /mcp",
      r.status === 401,
      `got ${r.status}`,
    );
  }

  // ── Admin endpoints ──────────────────────────────────────────────────────
  for (const [method, path, body] of [
    ["GET", "/v1/admin/users"],
    ["GET", "/v1/admin/activity"],
    [
      "POST",
      "/v1/admin/users",
      { username: `e2e-x-${stamp}`, name: "X", role: "admin" },
    ],
    ["POST", `/v1/admin/users/${hr.username}/password`, {}],
    ["POST", `/v1/admin/users/${hr.username}/deactivate`, {}],
    ["PATCH", `/v1/admin/users/${analyst.username}`, { role: "admin" }],
  ]) {
    const r = await call(method, path, { token: analyst.token, body });
    check(
      `analyst: ${method} ${path} is refused`,
      r.status === 403,
      `got ${r.status}`,
    );
  }
  {
    const r = await call("GET", "/v1/approvals", { token: analyst.token });
    check(
      "analyst: the approvals list shows nothing",
      r.status === 403 || (r.status === 200 && r.data.count === 0),
      `got ${r.status} ${JSON.stringify(r.data).slice(0, 120)}`,
    );
  }
  {
    const r = await call("PATCH", "/v1/auth/me", {
      token: analyst.token,
      body: { role: "admin" },
    });
    const me = await call("GET", "/v1/auth/me", { token: analyst.token });
    check(
      "a user cannot raise their own role",
      me.data.role === "analyst",
      `role is ${me.data.role} (${r.status})`,
    );
  }

  // ── Other people's conversations ─────────────────────────────────────────
  const adminThreads = await call("GET", "/v1/conversations", {
    token: admin.token,
  });
  const list = Array.isArray(adminThreads.data)
    ? adminThreads.data
    : adminThreads.data?.threads || [];
  const someone = list[0]?.thread_id;
  if (someone) {
    for (const [method, path, body] of [
      ["GET", `/v1/conversations/${someone}`],
      ["GET", `/v1/conversations/${someone}/result`],
      ["DELETE", `/v1/conversations/${someone}`],
      ["POST", `/v1/conversations/${someone}/messages`, { message: "hello" }],
      [
        "POST",
        `/v1/conversations/${someone}/messages/stream`,
        { message: "hello" },
      ],
      [
        "POST",
        "/v1/conversations/messages",
        { message: "hello", thread_id: someone },
      ],
      [
        "POST",
        "/v1/conversations/messages/stream",
        { message: "hello", thread_id: someone },
      ],
      ["GET", `/v1/approvals/${someone}`],
      ["POST", `/v1/approvals/${someone}/approve`, {}],
    ]) {
      const r = await call(method, path, { token: analyst.token, body });
      check(
        `analyst cannot ${method} another user's ${path.replace(someone, ":id")}${body?.thread_id ? " (thread_id in body)" : ""}`,
        [403, 404].includes(r.status),
        `got ${r.status} ${JSON.stringify(r.data).slice(0, 160)}`,
      );
    }
    const still = await call("GET", `/v1/conversations/${someone}`, {
      token: admin.token,
    });
    check(
      "the owner's conversation is untouched",
      still.status === 200,
      `got ${still.status}`,
    );
  } else
    console.log("  note  admin has no conversations; skipped ownership checks");
  {
    const mine = await call("GET", "/v1/conversations", {
      token: analyst.token,
    });
    const items = Array.isArray(mine.data)
      ? mine.data
      : mine.data?.threads || [];
    check(
      "a new user's conversation list is empty",
      items.length === 0,
      JSON.stringify(mine.data).slice(0, 120),
    );
  }

  // ── Documents by role ────────────────────────────────────────────────────
  for (const [who, user, expectOk] of [
    ["analyst", analyst, true],
    ["hr", hr, false],
  ]) {
    const r = await call("GET", "/v1/documents/MICROSOFT_2016_10K.pdf", {
      token: user.token,
    });
    const ok = expectOk ? [200, 404].includes(r.status) : r.status === 403;
    check(
      `${who}: a 10-K PDF is ${expectOk ? "allowed (or not stored)" : "refused"}`,
      ok,
      `got ${r.status}`,
    );
  }
  for (const evil of [
    "..%2F..%2Fetc%2Fpasswd",
    "%2Fetc%2Fpasswd",
    "....%2F%2F.env",
    "..%5C..%5C.env",
  ]) {
    const r = await call("GET", `/v1/documents/${evil}`, {
      token: admin.token,
    });
    check(
      `path trick ${decodeURIComponent(evil)} is refused`,
      [400, 403, 404, 422].includes(r.status),
      `got ${r.status}`,
    );
  }

  // ── Session cookie and renewal ───────────────────────────────────────────
  {
    const cookie = admin.cookie || "";
    check("refresh cookie is httpOnly", /httponly/i.test(cookie), cookie);
    check(
      "refresh cookie is SameSite=Strict",
      /samesite=strict/i.test(cookie),
      cookie,
    );
    check(
      "refresh cookie is limited to /v1/auth",
      /path=\/v1\/auth/i.test(cookie),
      cookie,
    );
    if (BASE.startsWith("https://"))
      check("refresh cookie is Secure", /;\s*secure/i.test(cookie), cookie);
    const value = cookie.split(";")[0];
    const first = await call("POST", "/v1/auth/refresh", {
      headers: { cookie: value },
    });
    check(
      "the refresh cookie renews the session once",
      first.status === 200,
      `got ${first.status}`,
    );
    await new Promise((r) => setTimeout(r, 31000));
    const again = await call("POST", "/v1/auth/refresh", {
      headers: { cookie: value },
    });
    check(
      "reusing an old refresh cookie is refused",
      again.status === 401,
      `got ${again.status}`,
    );
    const next = (first.headers.get("set-cookie") || "").split(";")[0];
    const after = await call("POST", "/v1/auth/refresh", {
      headers: { cookie: next },
    });
    check(
      "…and ends that whole session (theft detection)",
      after.status === 401,
      `got ${after.status}`,
    );
  }
  {
    const r = await call("POST", "/v1/auth/refresh", {
      headers: {
        cookie:
          "fra_refresh=fra_rt_" + crypto.randomBytes(32).toString("base64url"),
      },
    });
    check(
      "a made-up refresh cookie is refused",
      r.status === 401,
      `got ${r.status}`,
    );
  }

  // ── Input validation ─────────────────────────────────────────────────────
  {
    const r = await call("POST", "/v1/auth/login", {
      body: { username: "admin", password: "x".repeat(5000) },
    });
    check(
      "an enormous password is refused quickly",
      [401, 422].includes(r.status),
      `got ${r.status}`,
    );
    const bad = await call("POST", "/v1/auth/login", {
      raw: "{not json",
      headers: { "content-type": "application/json" },
    });
    check(
      "malformed sign-in JSON is a 422, not a crash",
      bad.status === 422,
      `got ${bad.status}`,
    );
    for (const name of [
      "../admin",
      "a",
      "x".repeat(60),
      "white space",
      "<script>",
    ]) {
      const u = await call("POST", "/v1/admin/users", {
        token: admin.token,
        body: { username: name, name: "Bad", role: "analyst" },
      });
      check(
        `username ${JSON.stringify(name.slice(0, 12))} is rejected`,
        [400, 422].includes(u.status),
        `got ${u.status}`,
      );
      if (u.status < 300) created.push(name);
    }
    const dup = await call("POST", "/v1/admin/users", {
      token: admin.token,
      body: {
        username: analyst.username.toUpperCase(),
        name: "Dup",
        role: "analyst",
      },
    });
    check(
      "a duplicate username (different case) is rejected",
      [400, 409, 422].includes(dup.status),
      `got ${dup.status}`,
    );
    const role = await call("POST", "/v1/admin/users", {
      token: admin.token,
      body: { username: `e2e-r-${stamp}`, name: "R", role: "superuser" },
    });
    check(
      "an unknown role is rejected",
      [400, 422].includes(role.status),
      `got ${role.status}`,
    );
    if (role.status < 300) created.push(`e2e-r-${stamp}`);
    const long = await call("POST", "/v1/conversations/messages/stream", {
      token: analyst.token,
      body: { message: "x".repeat(20000) },
    });
    check(
      "an over-long question is refused before reaching the model",
      [400, 413, 422].includes(long.status),
      `got ${long.status}`,
    );
    const empty = await call("POST", "/v1/conversations/messages/stream", {
      token: analyst.token,
      body: { message: "   " },
    });
    check(
      "an empty question is refused",
      [400, 422].includes(empty.status),
      `got ${empty.status}`,
    );
  }

  // ── OAuth ────────────────────────────────────────────────────────────────
  for (const uri of [
    "https://evil.example/callback",
    "http://example.com/cb",
    "https://claude.ai.evil.example/cb",
    "http://user@localhost:5000/cb",
    "javascript:alert(1)",
  ]) {
    const r = await call("POST", "/register", {
      body: {
        client_name: "x",
        redirect_uris: [uri],
        token_endpoint_auth_method: "none",
      },
    });
    check(`OAuth refuses redirect ${uri}`, r.status === 400, `got ${r.status}`);
  }
  {
    const r = await call("POST", "/register", {
      body: {
        client_name: "ok",
        redirect_uris: ["http://localhost:51000/cb"],
        token_endpoint_auth_method: "none",
      },
    });
    const id = r.data.client_id;
    const challenge = crypto
      .createHash("sha256")
      .update("v".repeat(50))
      .digest("base64url");
    const q = (extra) =>
      new URLSearchParams({
        client_id: id,
        response_type: "code",
        code_challenge: challenge,
        code_challenge_method: "S256",
        state: "s",
        ...extra,
      });
    const wrongUri = await call(
      "GET",
      `/authorize?${q({ redirect_uri: "http://localhost:51999/other" })}`,
    );
    check(
      "authorize refuses a redirect address that was not registered",
      wrongUri.status >= 400,
      `got ${wrongUri.status} ${wrongUri.headers.get("location") || ""}`,
    );
    const noPkce = await call(
      "GET",
      `/authorize?${new URLSearchParams({ client_id: id, response_type: "code", redirect_uri: "http://localhost:51000/cb", state: "s" })}`,
    );
    const loc = noPkce.headers.get("location") || "";
    check(
      "authorize refuses a request without PKCE",
      noPkce.status >= 400 || /error=/.test(loc),
      `got ${noPkce.status} ${loc}`,
    );
    const badScope = await call(
      "GET",
      `/authorize?${q({ redirect_uri: "http://localhost:51000/cb", scope: "admin" })}`,
    );
    const loc2 = badScope.headers.get("location") || "";
    check(
      "authorize refuses an unknown scope",
      badScope.status >= 400 || /error=/.test(loc2),
      `got ${badScope.status} ${loc2}`,
    );
  }
  {
    const r = await call("GET", "/v1/oauth/requests/not-a-real-request", {
      token: analyst.token,
    });
    check(
      "an unknown consent request is a clean 404",
      r.status === 404,
      `got ${r.status}`,
    );
  }
} catch (error) {
  console.log(`  FAIL  ${error.message}`);
  results.push(false);
} finally {
  for (const username of created) {
    await call(
      "POST",
      `/v1/admin/users/${encodeURIComponent(username)}/deactivate`,
      { token: admin.token },
    );
  }
  console.log(`  ok    deactivated ${created.length} temporary account(s)`);
}
const failed = results.filter((ok) => !ok).length;
console.log(
  failed
    ? `\n${failed} check(s) failed.`
    : `\nAll ${results.length} checks passed.`,
);
process.exit(failed ? 1 : 0);
