// End-to-end check of a running deployment, through a real browser and the real server.
//
//   ADMIN_PASSWORD=... node tests/live-check.mjs                       # http://localhost:8000
//   BASE=https://research.example.com ADMIN_PASSWORD=... node tests/live-check.mjs
//
// It creates a temporary analyst account (e2e-<time>), exercises sign-in, a real question, the
// account page, every admin action and the lockout, then deactivates the account. ASK=0 skips the
// question (the only step that calls the answer model). Failures save a screenshot next to this file.
import { chromium } from "@playwright/test";
import crypto from "node:crypto";

const BASE = (process.env.BASE || "http://localhost:8000").replace(/\/$/, "");
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD;
const ASK = process.env.ASK !== "0";
const QUESTION =
  process.env.QUESTION ||
  "What was Microsoft's total revenue in fiscal year 2016? Cite the filing.";
if (!ADMIN_PASSWORD) {
  console.error("Set ADMIN_PASSWORD to the admin account's password.");
  process.exit(2);
}

const username = `e2e-${Date.now().toString(36)}`;
const results = [];
let current = null;

async function step(name, run) {
  const started = Date.now();
  try {
    await run();
    results.push([name, true]);
    console.log(
      `  ok    ${name} (${((Date.now() - started) / 1000).toFixed(1)}s)`,
    );
  } catch (error) {
    results.push([name, false]);
    console.log(
      `  FAIL  ${name}\n        ${String(error.message).split("\n")[0]}`,
    );
    if (current) {
      const file = `live-check-failure-${results.length}.png`;
      await current.screenshot({ path: file }).catch(() => {});
      console.log(`        screenshot: ${file}`);
    }
    throw error;
  }
}

const expectVisible = (locator, timeout = 15000) =>
  locator.first().waitFor({ state: "visible", timeout });

async function signIn(page, user, password) {
  await page.goto(BASE + "/");
  await expectVisible(page.locator("#username"));
  await page.fill("#username", user);
  await page.fill("#password", password);
  await page.getByRole("button", { name: /^Sign in/ }).click();
}

// Sign-in attempts are limited per address; wait out the limit rather than fail on it.
async function signInOk(page, user, password) {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await signIn(page, user, password);
    const composer = page.locator("#question");
    const alert = page.locator(".alert.error");
    await Promise.race([
      composer.waitFor({ state: "visible", timeout: 20000 }),
      alert.waitFor({ state: "visible", timeout: 20000 }),
    ]);
    if (await composer.isVisible()) return;
    const message = (await alert.textContent()) || "";
    if (!/try again in \d+ seconds/i.test(message))
      throw new Error(`Sign-in as ${user} failed: ${message}`);
    await page.waitForTimeout(61000);
  }
  throw new Error(`Sign-in as ${user} kept being rate limited.`);
}

async function openAccountMenu(page) {
  await page.getByRole("button", { name: /^Account:/ }).click();
  await expectVisible(page.getByRole("dialog", { name: "Account" }));
}

async function signOut(page) {
  await openAccountMenu(page);
  await page
    .getByRole("dialog", { name: "Account" })
    .getByRole("button", { name: "Sign out" })
    .click();
  await expectVisible(
    page.getByRole("heading", { name: "Sign in", exact: true }),
  );
}

async function openUserPanel(admin) {
  await admin.goto(BASE + "/admin/users");
  const row = admin.locator(".data-table tbody tr", { hasText: username });
  await expectVisible(row);
  await row.getByRole("button", { name: "Manage" }).click();
  await expectVisible(admin.locator(".drawer"));
  return admin.locator(".drawer");
}

async function readNewPassword(admin) {
  const dialog = admin.locator(".modal");
  await expectVisible(dialog.getByText("Copy these now."));
  const password = (
    await dialog.locator(".copy-field code").nth(2).textContent()
  ).trim();
  if (password.length < 12) throw new Error("No generated password was shown.");
  await dialog.getByRole("button", { name: "Done" }).click();
  await dialog.waitFor({ state: "hidden" });
  return password;
}

// One JSON-RPC call to the MCP endpoint; replies arrive as JSON or as a server-sent event.
async function mcpCall(token, body, session) {
  const response = await fetch(BASE + "/mcp", {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      ...(session ? { "mcp-session-id": session } : {}),
    },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  const payload = text.trimStart().startsWith("{")
    ? text
    : (
        text
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .pop() || ""
      ).slice(5);
  return {
    status: response.status,
    session: response.headers.get("mcp-session-id") || session,
    data: payload.trim() ? JSON.parse(payload) : null,
  };
}

async function tokenRequest(fields) {
  const response = await fetch(BASE + "/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(fields),
  });
  return {
    status: response.status,
    body: await response.json().catch(() => ({})),
  };
}

const mcp = { clientId: "", refreshToken: "", code: null };

const browser = await chromium.launch({ channel: "chrome" });
const newContext = (options = {}) =>
  browser.newContext({ viewport: { width: 1366, height: 860 }, ...options });
const adminContext = await newContext();
const userContext = await newContext();
const admin = await adminContext.newPage();
const user = await userContext.newPage();
let password = "";

console.log(`Checking ${BASE} with temporary account ${username}`);
try {
  await step("server reports healthy", async () => {
    const response = await fetch(BASE + "/v1/health");
    if (!response.ok) throw new Error(`health returned ${response.status}`);
  });

  await step("pages carry the security headers", async () => {
    const response = await fetch(BASE + "/");
    const headers = response.headers;
    for (const name of [
      "content-security-policy",
      "x-content-type-options",
      "x-frame-options",
    ])
      if (!headers.get(name)) throw new Error(`missing ${name}`);
    if (BASE.startsWith("https://")) {
      if (!headers.get("strict-transport-security"))
        throw new Error("missing HSTS");
      const docs = await fetch(BASE + "/docs");
      if (docs.status !== 404)
        throw new Error(`/docs returned ${docs.status}, expected 404`);
    }
  });

  await step(
    "MCP endpoint demands sign-in and points to the OAuth metadata",
    async () => {
      const response = await fetch(BASE + "/mcp", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      });
      if (response.status !== 401)
        throw new Error(`/mcp returned ${response.status}`);
      const challenge = response.headers.get("www-authenticate") || "";
      const pointer = challenge.match(/resource_metadata="([^"]+)"/)?.[1];
      if (!pointer) throw new Error("no resource_metadata in the challenge");
      if (!pointer.startsWith(BASE))
        throw new Error(`metadata address is ${pointer}`);
      const metadata = await (await fetch(pointer)).json();
      if (metadata.resource !== BASE + "/mcp")
        throw new Error(`resource is ${metadata.resource}`);
      const server = await (
        await fetch(BASE + "/.well-known/oauth-authorization-server")
      ).json();
      if (!server.code_challenge_methods_supported?.includes("S256"))
        throw new Error("PKCE S256 is not advertised");
    },
  );

  current = admin;
  await step("a wrong password is refused with a clear message", async () => {
    await signIn(admin, "admin", "definitely-not-the-password");
    await expectVisible(admin.getByText("Incorrect username or password."));
  });

  await step("admin signs in; the app starts in the light theme", async () => {
    await signInOk(admin, "admin", ADMIN_PASSWORD);
    const theme = await admin.locator("html").getAttribute("data-theme");
    if (theme !== "light") throw new Error(`theme is ${theme}`);
  });

  await step(
    "admin adds a user and is shown a generated password once",
    async () => {
      await admin.goto(BASE + "/admin/users");
      await admin.getByRole("button", { name: "Add user" }).click();
      await admin.getByLabel("Full name").fill("E2E Check");
      await admin.getByLabel("Username").fill(username);
      await admin.getByLabel("Role").selectOption("analyst");
      await admin.getByRole("button", { name: "Create user" }).click();
      password = await readNewPassword(admin);
      await expectVisible(
        admin.locator(".data-table tbody tr", { hasText: username }),
      );
    },
  );

  current = user;
  await step("the new user signs in", async () => {
    await signInOk(user, username, password);
  });

  await step("the account page lists this browser's session", async () => {
    await openAccountMenu(user);
    await user.getByRole("link", { name: "Manage your account" }).click();
    await expectVisible(
      user.getByRole("heading", { name: "Account", exact: true }),
    );
    await expectVisible(
      user.locator(".session-list").getByText("This browser"),
    );
  });

  if (ASK) {
    await step("a real question gets a cited answer", async () => {
      await user.goto(BASE + "/");
      await user.fill("#question", QUESTION);
      await user.keyboard.press("Enter");
      await expectVisible(user.locator(".message.assistant .answer"), 240000);
      await expectVisible(user.locator(".message.assistant .source"), 240000);
      await user
        .locator("#question:not([disabled])")
        .waitFor({ timeout: 240000 });
      const answer = await user
        .locator(".message.assistant .answer")
        .first()
        .textContent();
      if (!/\d/.test(answer))
        throw new Error(`answer has no figure: ${answer.slice(0, 120)}`);
      console.log(
        `        answer: ${answer.replace(/\s+/g, " ").slice(0, 140)}…`,
      );
    });

    await step("the conversation is saved and survives a reload", async () => {
      await user.reload();
      await expectVisible(user.locator("#question"));
      await expectVisible(user.locator(".conversation-row"));
    });

    await step("the conversation can be deleted", async () => {
      const row = user.locator(".conversation-row").first();
      await row.hover();
      await row.getByRole("button", { name: /^Delete conversation/ }).click();
      await user
        .locator(".modal")
        .getByRole("button", { name: "Delete" })
        .click();
      await user
        .locator(".conversation-row")
        .first()
        .waitFor({ state: "detached" });
    });
  }

  await step(
    "an MCP client connects with OAuth and calls the tools",
    async () => {
      const callback = "http://localhost:53999/callback";
      const registration = await (
        await fetch(BASE + "/register", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            client_name: "Live check",
            redirect_uris: [callback],
            grant_types: ["authorization_code", "refresh_token"],
            response_types: ["code"],
            token_endpoint_auth_method: "none",
          }),
        })
      ).json();
      mcp.clientId = registration.client_id;
      const verifier = crypto.randomBytes(32).toString("base64url");
      const challenge = crypto
        .createHash("sha256")
        .update(verifier)
        .digest("base64url");
      const state = crypto.randomBytes(8).toString("hex");
      const authorize = new URL(BASE + "/authorize");
      authorize.search = new URLSearchParams({
        client_id: mcp.clientId,
        redirect_uri: callback,
        response_type: "code",
        scope: "documents:read",
        state,
        code_challenge: challenge,
        code_challenge_method: "S256",
        resource: BASE + "/mcp",
      });

      // Stand in for the client's local callback page and capture the code it is sent.
      let returned = "";
      await user.route("http://localhost:53999/**", (route) => {
        returned = route.request().url();
        return route.fulfill({
          status: 200,
          contentType: "text/plain",
          body: "received",
        });
      });
      await user.goto(authorize.toString());
      await expectVisible(
        user.getByRole("heading", { name: "Connect Live check" }),
      );
      await user.getByRole("button", { name: "Allow access" }).click();
      await user.waitForURL("http://localhost:53999/**", { timeout: 20000 });
      await user.unroute("http://localhost:53999/**");
      const reply = new URL(returned).searchParams;
      if (reply.get("state") !== state)
        throw new Error("state did not come back unchanged");

      const tokens = await tokenRequest({
        grant_type: "authorization_code",
        code: reply.get("code"),
        redirect_uri: callback,
        client_id: mcp.clientId,
        code_verifier: verifier,
      });
      if (tokens.status !== 200)
        throw new Error(
          `token exchange returned ${tokens.status}: ${JSON.stringify(tokens.body)}`,
        );
      if (tokens.body.expires_in > 3600)
        throw new Error("access tokens outlive an hour");

      mcp.code = { code: reply.get("code"), callback, verifier };

      const token = tokens.body.access_token;
      const started = await mcpCall(token, {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2025-06-18",
          capabilities: {},
          clientInfo: { name: "live-check", version: "1" },
        },
      });
      if (started.status !== 200)
        throw new Error(`initialize returned ${started.status}`);
      await mcpCall(
        token,
        { jsonrpc: "2.0", method: "notifications/initialized" },
        started.session,
      );
      const listed = await mcpCall(
        token,
        { jsonrpc: "2.0", id: 2, method: "tools/list" },
        started.session,
      );
      const names = (listed.data?.result?.tools || [])
        .map((tool) => tool.name)
        .sort();
      if (names.join() !== "list_documents,read_pages,search_documents")
        throw new Error(
          `tools are ${names.join() || JSON.stringify(listed.data)}`,
        );
      const found = await mcpCall(
        token,
        {
          jsonrpc: "2.0",
          id: 3,
          method: "tools/call",
          params: {
            name: "search_documents",
            arguments: { query: "Microsoft total revenue fiscal year 2016" },
          },
        },
        started.session,
      );
      const text = JSON.stringify(found.data?.result || found.data);
      if (found.data?.result?.isError || !/MICROSOFT/i.test(text))
        throw new Error(`search returned ${text.slice(0, 200)}`);

      // Refresh tokens rotate, and a bad token is turned away.
      const renewed = await tokenRequest({
        grant_type: "refresh_token",
        refresh_token: tokens.body.refresh_token,
        client_id: mcp.clientId,
      });
      if (renewed.status !== 200)
        throw new Error(`refresh returned ${renewed.status}`);
      mcp.refreshToken = renewed.body.refresh_token;
      const anonymous = await mcpCall("not-a-token", {
        jsonrpc: "2.0",
        id: 4,
        method: "tools/list",
      });
      if (anonymous.status !== 401)
        throw new Error(`a bad token got ${anonymous.status}`);
    },
  );

  await step(
    "the user changes their password and signs in with the new one",
    async () => {
      const next = `e2e changed ${Date.now()}`;
      await user.goto(BASE + "/account");
      await user.getByLabel("Current password").fill(password);
      await user.getByLabel("New password", { exact: true }).fill(next);
      await user.getByLabel("Confirm new password").fill(next);
      await user.getByRole("button", { name: "Change password" }).click();
      await expectVisible(user.getByText(/^Password changed\./));
      password = next;
      await signOut(user);
      await signInOk(user, username, password);
      // Changing the password also disconnects MCP clients.
      if (mcp.refreshToken) {
        const cutOff = await tokenRequest({
          grant_type: "refresh_token",
          refresh_token: mcp.refreshToken,
          client_id: mcp.clientId,
        });
        if (cutOff.status === 200)
          throw new Error("the MCP client could still renew its access");
        // An authorization code works once; replaying it is refused.
        const replayed = await tokenRequest({
          grant_type: "authorization_code",
          code: mcp.code.code,
          redirect_uri: mcp.code.callback,
          client_id: mcp.clientId,
          code_verifier: mcp.code.verifier,
        });
        if (replayed.status === 200)
          throw new Error("the authorization code was accepted twice");
      }
    },
  );

  current = admin;
  let drawer;
  await step("admin resets the password; the user's session ends", async () => {
    drawer = await openUserPanel(admin);
    await drawer.getByRole("button", { name: "Reset", exact: true }).click();
    await admin
      .locator(".modal")
      .getByRole("button", { name: "Reset password" })
      .click();
    password = await readNewPassword(admin);
    current = user;
    await user.reload();
    await expectVisible(
      user.getByRole("heading", { name: "Sign in", exact: true }),
    );
    await signInOk(user, username, password);
  });

  current = admin;
  await step("admin signs the user out everywhere", async () => {
    await drawer.getByRole("button", { name: "Sign out", exact: true }).click();
    await admin
      .locator(".modal")
      .getByRole("button", { name: "Sign out everywhere" })
      .click();
    await admin.locator(".modal").waitFor({ state: "hidden" });
    current = user;
    await user.reload();
    await expectVisible(
      user.getByRole("heading", { name: "Sign in", exact: true }),
    );
  });

  current = admin;
  await step(
    "a deactivated account cannot sign in; reactivating restores it",
    async () => {
      await drawer
        .getByRole("button", { name: "Deactivate", exact: true })
        .click();
      await admin
        .locator(".modal")
        .getByRole("button", { name: "Deactivate" })
        .click();
      await admin.locator(".modal").waitFor({ state: "hidden" });
      current = user;
      await signIn(user, username, password);
      await expectVisible(user.locator(".alert.error"));
      current = admin;
      await drawer
        .getByRole("button", { name: "Reactivate", exact: true })
        .click();
      await expectVisible(
        drawer.getByRole("button", { name: "Deactivate", exact: true }),
      );
      current = user;
      await signInOk(user, username, password);
      await signOut(user);
    },
  );

  await step(
    "five wrong passwords lock the account; admin unlocks it",
    async () => {
      current = user;
      await user.waitForTimeout(61000); // start with a fresh per-address allowance
      for (let attempt = 0; attempt < 5; attempt += 1) {
        await signIn(user, username, `wrong-password-${attempt}`);
        await expectVisible(user.locator(".alert.error"));
      }
      await signIn(user, username, password);
      await expectVisible(user.locator(".alert.error"));
      const message = await user.locator(".alert.error").textContent();
      if (!/failed sign-in attempts/i.test(message))
        throw new Error(`expected the lockout message, saw: ${message}`);
      current = admin;
      drawer = await openUserPanel(admin);
      await drawer.getByRole("button", { name: "Unlock", exact: true }).click();
      await drawer
        .getByRole("button", { name: "Unlock", exact: true })
        .waitFor({ state: "detached" });
      current = user;
      await user.waitForTimeout(61000);
      await signInOk(user, username, password);
    },
  );

  current = admin;
  await step(
    "the activity log records what happened to the account",
    async () => {
      await admin.goto(BASE + "/admin/activity");
      await expectVisible(
        admin.locator(".data-table tbody tr", { hasText: username }),
      );
      const rows = await admin
        .locator(".data-table tbody tr", { hasText: username })
        .count();
      if (rows < 5) throw new Error(`only ${rows} events for ${username}`);
    },
  );

  await step("the phone layout opens its menu and navigates", async () => {
    const phone = await newContext({ viewport: { width: 390, height: 844 } });
    const page = await phone.newPage();
    current = page;
    await signInOk(page, username, password);
    await page.getByRole("button", { name: "Open menu" }).click();
    await page.getByRole("link", { name: "Connect Claude Code" }).click();
    await expectVisible(
      page.getByRole("heading", { name: "Connect Claude Code" }),
    );
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth + 1,
    );
    if (overflow) throw new Error("the page scrolls sideways on a phone");
    await phone.close();
  });

  current = admin;
  await step("the dark theme can be chosen and is remembered", async () => {
    await admin.goto(BASE + "/");
    await openAccountMenu(admin);
    await admin.getByRole("button", { name: "Dark" }).click();
    await admin.reload();
    if ((await admin.locator("html").getAttribute("data-theme")) !== "dark")
      throw new Error("dark was not remembered");
    await openAccountMenu(admin);
    await admin.getByRole("button", { name: "Light" }).click();
  });
} catch {
  // The failing step has already been reported.
} finally {
  // Leave no usable test account behind.
  try {
    current = admin;
    const drawer = await openUserPanel(admin);
    const deactivate = drawer.getByRole("button", {
      name: "Deactivate",
      exact: true,
    });
    if (await deactivate.count()) {
      await deactivate.click();
      await admin
        .locator(".modal")
        .getByRole("button", { name: "Deactivate" })
        .click();
      await admin.locator(".modal").waitFor({ state: "hidden" });
    }
    console.log(`  ok    temporary account ${username} deactivated`);
  } catch (error) {
    console.log(
      `  note  could not deactivate ${username}: ${String(error.message).split("\n")[0]}`,
    );
  }
  await browser.close();
}

const failed = results.filter(([, ok]) => !ok);
console.log(
  failed.length
    ? `\n${failed.length} step failed; later steps were not run.`
    : `\nAll ${results.length} steps passed.`,
);
process.exit(failed.length ? 1 : 0);
