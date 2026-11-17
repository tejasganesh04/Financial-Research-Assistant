import { test, expect } from "@playwright/test";

// Every API request is intercepted. Browser tests cannot submit a paid question.
async function mockBackend(page, { role = "analyst", mode = "answer" } = {}) {
  const state = {
    submissions: 0,
    reads: 0,
    reviewed: false,
    messages: [],
    pending: mode === "pending",
    signedIn: false,
    refreshes: 0,
    rejectNextToken: false,
    users: [
      {
        username: "admin",
        name: "Avery Admin",
        role: "admin",
        role_label: "Administrator",
        department: "IT",
        active: true,
        locked_until: null,
        created_at: "2026-11-01T10:00:00Z",
        password_changed_at: "2026-11-01T10:00:00Z",
        last_login_at: "2026-11-16T10:00:00Z",
        last_seen_at: "2026-11-16T10:00:00Z",
        web_sessions: 1,
        mcp_sessions: 0,
      },
      {
        username: "analyst",
        name: "Riley Analyst",
        role: "analyst",
        role_label: "Analyst",
        department: "Research",
        active: true,
        locked_until: "2099-01-01T00:00:00Z",
        created_at: "2026-11-01T10:00:00Z",
        password_changed_at: "2026-11-01T10:00:00Z",
        last_login_at: null,
        last_seen_at: null,
        web_sessions: 0,
        mcp_sessions: 1,
      },
    ],
  };
  const permissions = {
    allowed_doc_types: role === "admin" ? ["*"] : ["10k", "10q", "8k"],
    allowed_confidentiality: role === "admin" ? ["*"] : ["public"],
  };
  const user = {
    user_id: role,
    name: `Demo ${role}`,
    role,
    department: "Research",
    permissions,
  };
  const session = () => ({
    access_token: "test-token",
    token_type: "bearer",
    expires_in: 3600,
    user,
  });
  const source = { file: "MICROSOFT_2016_10K.pdf", page: 52, doc_type: "10k" };
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    const json = (body, status = 200) => route.fulfill({ status, json: body });
    if (path === "/v1/auth/login") {
      const body = request.postDataJSON();
      if (body.password === "wrong-password")
        return json({ detail: "Incorrect username or password." }, 401);
      state.signedIn = true;
      return json(session());
    }
    if (path === "/v1/auth/refresh") {
      state.refreshes += 1;
      return state.signedIn && !state.expired
        ? json(session())
        : json({ detail: "Your session has ended. Sign in again." }, 401);
    }
    if (path === "/v1/auth/logout") {
      state.signedIn = false;
      return route.fulfill({ status: 204 });
    }
    if (path === "/v1/oauth/server")
      return json({
        enabled: true,
        mcp_url: "https://research.example.com/mcp",
      });
    if (path.startsWith("/v1/oauth/requests/") && method === "GET")
      return path.endsWith("/hosted")
        ? json({
            client_name: "Claude",
            redirect_host: "claude.ai",
            redirect_is_local: false,
            scopes: [
              {
                scope: "documents:read",
                description:
                  "Search and read the documents your role can access",
              },
            ],
            resource: "https://research.example.com/mcp",
            access_token_minutes: 60,
          })
        : path.endsWith("/expired")
          ? json(
              {
                detail:
                  "This connection request has expired or was already used.",
              },
              404,
            )
          : json({
              client_name: "Claude Code",
              redirect_host: "localhost",
              redirect_is_local: true,
              scopes: [
                {
                  scope: "documents:read",
                  description:
                    "Search and read the documents your role can access",
                },
              ],
              resource: "https://research.example.com/mcp",
              access_token_minutes: 60,
            });
    expect(request.headers().authorization).toBe("Bearer test-token");
    if (state.expired || state.rejectNextToken) {
      state.rejectNextToken = false;
      return route.fulfill({
        status: 401,
        json: { detail: "Token has expired" },
      });
    }
    if (path === "/v1/auth/me") return json(user);
    if (path === "/v1/auth/sessions")
      return json({
        sessions: [
          {
            id: "s1",
            kind: "web",
            user_agent: "Mozilla/5.0 (Macintosh; Mac OS X) Chrome/140",
            created_at: "2026-11-16T10:00:00Z",
            last_used_at: "2026-11-16T10:00:00Z",
            current: true,
          },
          {
            id: "s2",
            kind: "mcp",
            client_name: "Claude Code",
            created_at: "2026-11-15T10:00:00Z",
            last_used_at: "2026-11-16T09:00:00Z",
            current: false,
          },
        ],
      });
    if (path === "/v1/auth/password") {
      const body = request.postDataJSON();
      if (body.current_password !== "old-password-123")
        return json({ detail: "Your current password is incorrect." }, 400);
      return json(session());
    }
    if (path === "/v1/admin/users" && method === "GET")
      return json({ users: state.users });
    if (path === "/v1/admin/users" && method === "POST") {
      const body = request.postDataJSON();
      state.created = body;
      return json(
        {
          user: {
            ...state.users[1],
            username: body.username,
            name: body.name,
            locked_until: null,
          },
          password: "Abcd-Efgh-Jkmn-Pqrs",
        },
        201,
      );
    }
    if (/^\/v1\/admin\/users\/[^/]+\/sessions$/.test(path))
      return json({ sessions: [] });
    if (/^\/v1\/admin\/users\/[^/]+\/password$/.test(path)) {
      state.reset = request.postDataJSON();
      return json({
        user: state.users[1],
        password: state.reset.password ? null : "Wxyz-Abcd-Efgh-Jkmn",
      });
    }
    if (/^\/v1\/admin\/users\/[^/]+\/deactivate$/.test(path)) {
      state.users[1] = { ...state.users[1], active: false };
      return json({ user: state.users[1] });
    }
    if (/^\/v1\/admin\/users\/[^/]+\/unlock$/.test(path)) {
      state.users[1] = { ...state.users[1], locked_until: null };
      return json({ user: state.users[1] });
    }
    if (path === "/v1/admin/activity")
      return json({
        events: [
          {
            id: 2,
            at: "2026-11-16T10:00:00Z",
            username: "analyst",
            actor: null,
            event: "login_failed",
            ip: "10.0.0.2",
            detail: { reason: "password" },
          },
          {
            id: 1,
            at: "2026-11-16T09:00:00Z",
            username: "analyst",
            actor: null,
            event: "mcp_connected",
            ip: null,
            detail: { client: "Claude Code" },
          },
        ],
      });
    if (path === "/v1/conversations")
      return json({ threads: state.threads || [] });
    if (/^\/v1\/conversations\/[^/]+$/.test(path) && method === "DELETE") {
      state.deleted = path.split("/")[3];
      state.threads = (state.threads || []).filter(
        (t) => t.thread_id !== state.deleted,
      );
      return route.fulfill({ status: 204 });
    }
    if (/^\/v1\/admin\/users\/[^/]+$/.test(path) && method === "PATCH") {
      state.updated = request.postDataJSON();
      state.users[1] = { ...state.users[1], ...state.updated };
      return json({ user: state.users[1] });
    }
    if (/\/messages\/stream$/.test(path) && request.method() === "POST") {
      state.submissions += 1;
      const id = path.split("/")[3];
      const question = request.postDataJSON().message;
      state.lastSubmitted = question;
      const sanitized = question.replace(
        "tejas@example.com",
        "<EMAIL_ADDRESS>",
      );
      state.messages = [
        { role: "user", content: sanitized, request_id: "request-test" },
      ];
      if (mode === "rate-limit") {
        const events = [
          {
            type: "request_started",
            request_id: "request-limit",
            thread_id: id,
            sanitized_message: sanitized,
          },
          {
            type: "node_start",
            node: "grader",
            label: "Evaluating relevance",
            request_id: "request-limit",
          },
          {
            type: "error",
            code: "provider_rate_limited",
            status: 429,
            provider: "Groq",
            model: "openai/gpt-oss-20b",
            node: "grader",
            retry_after_seconds: 17,
            request_id: "request-limit",
            message:
              "Groq model openai/gpt-oss-20b was rate limited at node grader. Wait at least 17 seconds before trying again. No answer was released for this request.",
          },
        ];
        return route.fulfill({
          contentType: "text/event-stream",
          body: events
            .map((event) => `data: ${JSON.stringify(event)}\n\n`)
            .join(""),
        });
      }
      if (mode === "disconnect")
        return route.fulfill({
          contentType: "text/event-stream",
          body: `data: ${JSON.stringify({ type: "error", message: "The answer could not be completed." })}\n\n`,
        });
      if (["scroll", "waiting"].includes(mode))
        await new Promise((resolve) => {
          state.releaseAnswer = resolve;
        });
      const response =
        mode === "scroll"
          ? Array.from(
              { length: 60 },
              (_, i) =>
                `Answer paragraph ${i + 1}: Revenue figures from the filing.`,
            ).join("\n\n")
          : mode === "cited"
            ? "Revenue was **$85,320 million**【Source 5: MICROSOFT_2016_10K.pdf, Page 28】. " +
              "Cost of revenue rose [Source: MICROSOFT_2016_10K.pdf, Page 52]."
            : "Microsoft’s cost of revenue was **$32,780 million**.";
      if (mode !== "pending")
        state.messages.push({
          role: "assistant",
          content: response,
          sources: [source],
        });
      const events = [
        {
          type: "request_started",
          thread_id: id,
          request_id: "request-test",
          sanitized_message: sanitized,
        },
        { type: "node_start", node: "retrieval", label: "Searching documents" },
        mode === "pending"
          ? { type: "pending_approval", thread_id: id }
          : { type: "final", response, sources: [source], confidence: 0.95 },
      ];
      return route.fulfill({
        contentType: "text/event-stream",
        body: events
          .map((event) => `data: ${JSON.stringify(event)}\n\n`)
          .join(""),
      });
    }
    if (/\/conversations\/[^/]+\/result$/.test(path)) {
      state.reads += 1;
      if (state.reads < 2) return json({ status: "pending" });
      state.pending = false;
      state.messages.push({
        role: "assistant",
        content: "Approved answer: $32,780 million.",
        sources: [source],
      });
      return json({ status: "approved" });
    }
    if (/\/conversations\/[^/]+$/.test(path))
      return json({
        thread_id: path.split("/")[3],
        messages: state.messages,
        is_interrupted: state.pending,
      });
    if (path === "/v1/approvals")
      return json({
        approvals: state.reviewed
          ? []
          : [
              {
                thread_id: "review-one",
                query: "Review this financial answer",
                requester_role: "finance",
                requester_name: "Finance user",
                sources_count: 1,
              },
            ],
      });
    if (path === "/v1/approvals/review-one")
      return json({
        thread_id: "review-one",
        query: "Review this financial answer",
        requester_role: "finance",
        requester_name: "Finance user",
        draft_answer: "The draft answer.",
        access_scope: {
          doc_types: ["10k", "10q", "8k"],
          confidentiality: ["public"],
          escalated: true,
        },
        sources: [source],
      });
    if (/\/approvals\/review-one\/(approve|reject)$/.test(path)) {
      state.reviewed = true;
      state.decision = request.postDataJSON();
      return json({ status: "approved" });
    }
    return route.fulfill({
      status: 404,
      json: { detail: "Not available in test" },
    });
  });
  return state;
}

async function login(page, account = "analyst") {
  await page.goto("/");
  await page.getByLabel("Username").fill(account);
  await page.getByLabel("Password").fill("a-test-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByLabel("Ask a financial question")).toBeVisible();
}

const SIGN_IN_HEADING = { name: "Sign in", exact: true };

// Signing out and account settings live in the menu behind the avatar.
async function openAccountMenu(page) {
  await page.getByRole("button", { name: /^Account:/ }).click();
  await expect(page.getByRole("dialog", { name: "Account" })).toBeVisible();
}

async function signOut(page) {
  await openAccountMenu(page);
  await page
    .getByRole("dialog", { name: "Account" })
    .getByRole("button", { name: "Sign out" })
    .click();
}

test("message length is shown only after rejection and the question remains editable", async ({
  page,
}) => {
  const state = await mockBackend(page);
  await page.route("**/messages/stream", async (route) => {
    const message = route.request().postDataJSON().message;
    if (message.length <= 2000) return route.fallback();
    return route.fulfill({
      status: 422,
      json: {
        detail: [
          {
            type: "string_too_long",
            loc: ["body", "message"],
            msg: "String should have at most 2000 characters",
            ctx: { max_length: 2000 },
          },
        ],
      },
    });
  });
  await login(page);
  const input = page.getByLabel("Ask a financial question");
  const longQuestion = "x".repeat(2001);
  await input.fill(longQuestion);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(input).not.toHaveAttribute("maxlength", /.+/);
  await page
    .getByRole("button", { name: "Send question", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "2,000 characters or fewer",
  );
  await expect(input).toHaveValue(longQuestion);
  await expect(page.locator(".message.user")).toHaveCount(0);
  expect(state.submissions).toBe(0);
  await input.fill("Microsoft revenue?");
  await page
    .getByRole("button", { name: "Send question", exact: true })
    .click();
  await expect(
    page.getByText("$32,780 million", { exact: true }),
  ).toBeVisible();
  expect(state.submissions).toBe(1);
});

for (const streamedError of [false, true]) {
  test(`full conversation shows a limit only on submit (${streamedError ? "SSE" : "HTTP"})`, async ({
    page,
  }) => {
    const state = await mockBackend(page);
    let limitedPath;
    const message =
      "This conversation has reached 20 questions. Please start a new conversation.";
    await page.route("**/messages/stream", async (route) => {
      const path = new URL(route.request().url()).pathname;
      limitedPath ||= path;
      if (path !== limitedPath) return route.fallback();
      return streamedError
        ? route.fulfill({
            contentType: "text/event-stream",
            body: `data: ${JSON.stringify({ type: "error", status: 409, message })}\n\n`,
          })
        : route.fulfill({ status: 409, json: { detail: message } });
    });
    await login(page);
    await expect(page.getByText(/20 questions/)).toHaveCount(0);
    const input = page.getByLabel("Ask a financial question");
    await input.fill("Microsoft revenue?");
    await page
      .getByRole("button", { name: "Send question", exact: true })
      .click();
    await expect(page.getByRole("alert")).toContainText(message);
    await expect(page.getByRole("alert")).not.toContainText(
      "Check saved history",
    );
    await expect(input).toHaveValue("Microsoft revenue?");
    await expect(page.locator(".message.user")).toHaveCount(0);
    expect(state.submissions).toBe(0);
    await page
      .getByRole("button", { name: "New conversation", exact: true })
      .click();
    await input.fill("Microsoft revenue?");
    await page
      .getByRole("button", { name: "Send question", exact: true })
      .click();
    await expect(
      page.getByText("$32,780 million", { exact: true }),
    ).toBeVisible();
    expect(state.submissions).toBe(1);
  });
}

test("login, sample fill, complete answer, sources, refresh and logout", async ({
  page,
}) => {
  const state = await mockBackend(page);
  await login(page);
  await expect(page.getByRole("link", { name: "Approvals" })).toHaveCount(0);
  await page.getByRole("button", { name: /Find a figure/ }).click();
  expect(state.submissions).toBe(0);
  await page
    .getByRole("button", { name: "Send question", exact: true })
    .click();
  await expect(
    page.getByText("$32,780 million", { exact: true }),
  ).toBeVisible();
  await page.getByText("Request details", { exact: true }).click();
  await expect(page.getByText("request-test", { exact: true })).toBeVisible();
  await expect(
    page.getByText("logs/traces/request-test/summary.md", { exact: true }),
  ).toBeVisible();
  expect(state.submissions).toBe(1);
  await page.getByRole("button", { name: /MICROSOFT_2016_10K.pdf/ }).click();
  await expect(page.getByText(/original PDF is not stored/)).toBeVisible();
  await page.reload();
  await page.getByText("Request details", { exact: true }).click();
  await expect(page.getByText("request-test", { exact: true })).toBeVisible();
  await expect(
    page.getByText("$32,780 million", { exact: true }),
  ).toBeVisible();
  expect(state.submissions).toBe(1);
  await signOut(page);
  await expect(page.getByRole("heading", SIGN_IN_HEADING)).toBeVisible();
  expect(state.signedIn).toBe(false);
  // Signing out clears the refresh cookie on the server, so a reload stays signed out.
  await page.reload();
  await expect(page.getByRole("heading", SIGN_IN_HEADING)).toBeVisible();
});

test("a failed request shows an error without resubmitting", async ({
  page,
}) => {
  const state = await mockBackend(page, { mode: "disconnect" });
  await login(page);
  await page.getByLabel("Ask a financial question").fill("Microsoft revenue?");
  await page
    .getByRole("button", { name: "Send question", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("could not be completed");
  expect(state.submissions).toBe(1);
});

test("rate limits name the model and node, keep the trace link and never resubmit", async ({
  page,
}) => {
  const state = await mockBackend(page, { mode: "rate-limit" });
  await login(page);
  await page.getByLabel("Ask a financial question").fill("Microsoft revenue?");
  await page
    .getByRole("button", { name: "Send question", exact: true })
    .click();
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("Groq model openai/gpt-oss-20b");
  await expect(alert).toContainText("rate limited at node grader");
  await expect(alert).toContainText("17 seconds");
  await page.getByText("Request details", { exact: true }).click();
  await expect(page.getByText("request-limit", { exact: true })).toBeVisible();
  await page.screenshot({
    path: "../logs/rate-limit-browser.png",
    fullPage: true,
  });
  await page.getByLabel("Ask a financial question").fill("Another question?");
  await expect(
    page.getByRole("button", { name: "Send question", exact: true }),
  ).toBeEnabled();
  expect(state.submissions).toBe(1);
  await expect(
    page.getByText("No relevant information", { exact: false }),
  ).toHaveCount(0);
});

test("pending answer is hidden and later released by read-only polling", async ({
  page,
}) => {
  const state = await mockBackend(page, { role: "finance", mode: "pending" });
  await login(page, "finance");
  await page.getByLabel("Ask a financial question").fill("Microsoft revenue?");
  await page
    .getByRole("button", { name: "Send question", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Waiting for review" }),
  ).toBeVisible();
  await expect(page.getByLabel("Ask a financial question")).toBeDisabled();
  await expect(page.getByText("Approved answer: $32,780 million.")).toBeVisible(
    { timeout: 12000 },
  );
  await expect(page.getByLabel("Ask a financial question")).toBeEnabled();
  expect(state.submissions).toBe(1);
});

test("admin can review; rejection requires a reason", async ({ page }) => {
  const state = await mockBackend(page, { role: "admin" });
  await login(page, "admin");
  await page.getByRole("link", { name: /Approvals/ }).click();
  await page
    .getByRole("button", { name: /Review this financial answer/ })
    .click();
  await expect(page.getByText("The draft answer.")).toBeVisible();
  await page.getByRole("button", { name: "Reject answer" }).click();
  await expect(page.getByRole("alert")).toContainText("Add a reason");
  expect(state.reviewed).toBe(false);
  await page.getByLabel(/Review note/).fill("Needs a better source.");
  await page.getByRole("button", { name: "Reject answer" }).click();
  await expect(
    page.getByText("Answer rejected. The requester will see your reason."),
  ).toBeVisible();
  expect(state.decision.reason).toBe("Needs a better source.");
  expect(state.submissions).toBe(0);
});

test("small screen has no horizontal overflow", async ({ page }) => {
  await mockBackend(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("admin can approve without a rejection note", async ({ page }) => {
  const state = await mockBackend(page, { role: "admin" });
  await login(page, "admin");
  await page.getByRole("link", { name: /Approvals/ }).click();
  await page
    .getByRole("button", { name: /Review this financial answer/ })
    .click();
  await page.getByRole("button", { name: "Approve & release" }).click();
  await expect(
    page.getByText("Answer approved and released to the requester."),
  ).toBeVisible();
  expect(state.decision.reason).toBe("");
  expect(state.submissions).toBe(0);
});

test("expired sessions return to login without submitting a question", async ({
  page,
}) => {
  const state = await mockBackend(page);
  await login(page);
  // Let the first loads finish, so the refresh below is the request that meets the expired session.
  await expect(page.getByText("No conversations yet.")).toBeVisible();
  await page.waitForLoadState("networkidle");
  state.expired = true;
  await page.getByRole("button", { name: "Refresh conversation list" }).click();
  await expect(page.getByRole("heading", SIGN_IN_HEADING)).toBeVisible();
  await expect(page.getByText("Your session has ended")).toBeVisible();
  expect(state.submissions).toBe(0);
});

test("capture the demo layout for visual review", async ({ page }) => {
  await mockBackend(page);
  await page.setViewportSize({ width: 1360, height: 900 });
  await page.goto("/");
  await expect(page.getByRole("heading", SIGN_IN_HEADING)).toBeVisible();
  await page.screenshot({ path: "../logs/frontend-login.png", fullPage: true });
  await login(page);
  await page.screenshot({
    path: "../logs/frontend-workspace.png",
    fullPage: true,
  });
  await page
    .getByLabel("Ask a financial question")
    .fill("What was Microsoft’s FY2016 cost of revenue?");
  await page
    .getByRole("button", { name: "Send question", exact: true })
    .click();
  await expect(
    page.getByText("$32,780 million", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "../logs/frontend-answer.png",
    fullPage: true,
  });
});

test("management cannot see approvals", async ({ page }) => {
  await mockBackend(page, { role: "c_level" });
  await login(page, "executive");
  await expect(page.getByRole("link", { name: "Approvals" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Users" })).toHaveCount(0);
});

test("HR can fill an explicit access request without submitting it", async ({
  page,
}) => {
  const state = await mockBackend(page, { role: "hr" });
  await login(page, "hr");
  await page
    .getByText("Request access outside your role", { exact: true })
    .click();
  await page.getByRole("button", { name: "Fill example" }).click();
  await expect(page.getByLabel("Ask a financial question")).toHaveValue(
    /\/escalate doc_type=filings confidentiality=public\n/,
  );
  expect(state.submissions).toBe(0);
});

test("question display uses backend redaction and stays redacted after refresh", async ({
  page,
}) => {
  const state = await mockBackend(page);
  await login(page);
  const question =
    "My email is tejas@example.com. What was Microsoft's revenue in FY2016?";
  await page.getByLabel("Ask a financial question").fill(question);
  await page
    .getByRole("button", { name: "Send question", exact: true })
    .click();
  await expect(page.locator(".message.user .message-body")).toContainText(
    "<EMAIL_ADDRESS>",
  );
  await expect(page.locator(".message.user .message-body")).not.toContainText(
    "tejas@example.com",
  );
  expect(state.lastSubmitted).toBe(question);
  expect(state.submissions).toBe(1);
  await page.reload();
  await expect(page.locator(".message.user .message-body")).toContainText(
    "<EMAIL_ADDRESS>",
  );
  expect(state.submissions).toBe(1);
});

for (const readEarlier of [false, true]) {
  test(`answer scrolling: ${readEarlier ? "keeps the reader's position" : "follows the new answer"}`, async ({
    page,
  }) => {
    const state = await mockBackend(page, { mode: "scroll" });
    state.messages = Array.from({ length: 12 }, (_, i) => ({
      role: i % 2 ? "assistant" : "user",
      content:
        `Earlier message ${i}: ` + "Earlier financial research. ".repeat(40),
    }));
    await login(page);
    await page.evaluate(() =>
      sessionStorage.setItem("financial-research-conversation", "scroll-test"),
    );
    await page.reload();
    const panel = page.locator(".chat-scroll");
    await expect(page.getByText(/Earlier message 11:/)).toBeVisible();
    const distance = () =>
      panel.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop);
    await expect.poll(distance).toBeLessThan(2);
    await page
      .getByLabel("Ask a financial question")
      .fill("Show the new financial answer");
    await page
      .getByRole("button", { name: "Send question", exact: true })
      .click();
    await expect.poll(() => typeof state.releaseAnswer).toBe("function");
    if (readEarlier) {
      await panel.evaluate((el) => {
        el.scrollTop = 0;
        el.dispatchEvent(new Event("scroll"));
      });
    }
    state.releaseAnswer();
    await expect(
      page.getByText("Answer paragraph 60: Revenue figures from the filing.", {
        exact: true,
      }),
    ).toBeAttached();
    if (readEarlier) {
      await expect
        .poll(() => panel.evaluate((el) => el.scrollTop))
        .toBeLessThan(2);
    } else {
      await expect.poll(distance).toBeLessThan(2);
      expect(await page.evaluate(() => window.scrollY)).toBe(0);
    }
    expect(state.submissions).toBe(1);
  });
}

test("one request waits before displaying the complete answer", async ({
  page,
}) => {
  const state = await mockBackend(page, { mode: "waiting" });
  await login(page);
  await page.getByLabel("Ask a financial question").fill("Microsoft revenue?");
  await page
    .getByRole("button", { name: "Send question", exact: true })
    .click();
  await expect.poll(() => typeof state.releaseAnswer).toBe("function");
  await expect(page.locator(".thinking")).toContainText("Researching");
  await expect(page.getByLabel("Ask a financial question")).toBeDisabled();
  await expect(page.getByText("$32,780 million", { exact: true })).toHaveCount(
    0,
  );
  expect(state.submissions).toBe(1);
  state.releaseAnswer();
  await expect(
    page.getByText("$32,780 million", { exact: true }),
  ).toBeVisible();
  await expect(page.locator(".thinking")).toHaveCount(0);
  await expect(page.getByLabel("Ask a financial question")).toBeEnabled();
  expect(state.submissions).toBe(1);
});

test("progress stages arrive before the complete answer", async ({ page }) => {
  await mockBackend(page);
  await login(page);
  await page.evaluate(() => {
    const originalFetch = window.fetch;
    window.stageRequests = 0;
    window.fetch = async (input, init) => {
      const request = new Request(input, init);
      if (
        request.method !== "POST" ||
        !request.url.endsWith("/messages/stream")
      )
        return originalFetch(input, init);
      window.stageRequests++;
      const encoder = new TextEncoder();
      const body = new ReadableStream({
        start(controller) {
          const send = (event) =>
            controller.enqueue(
              encoder.encode(`data: ${JSON.stringify(event)}\n\n`),
            );
          send({ type: "request_started", request_id: "progress-test" });
          send({
            type: "node_start",
            node: "retrieval",
            label: "Searching documents",
          });
          window.nextStage = () =>
            send({
              type: "node_start",
              node: "hallucination_checker",
              label: "Verifying accuracy",
            });
          window.finishStages = () => {
            send({
              type: "final",
              response: "The complete checked answer.",
              sources: [],
            });
            controller.close();
          };
        },
      });
      return new Response(body, {
        headers: { "Content-Type": "text/event-stream" },
      });
    };
  });
  await page.getByLabel("Ask a financial question").fill("Microsoft revenue?");
  await page
    .getByRole("button", { name: "Send question", exact: true })
    .click();
  await expect(page.locator(".thinking")).toContainText("Searching documents");
  await expect(
    page.getByText("The complete checked answer.", { exact: true }),
  ).toHaveCount(0);
  await page.evaluate(() => window.nextStage());
  await expect(page.locator(".thinking")).toContainText("Verifying accuracy");
  await expect(
    page.getByText("The complete checked answer.", { exact: true }),
  ).toHaveCount(0);
  await page.evaluate(() => window.finishStages());
  await expect(
    page.getByText("The complete checked answer.", { exact: true }),
  ).toBeVisible();
  expect(await page.evaluate(() => window.stageRequests)).toBe(1);
});

// ── Sessions ────────────────────────────────────────────────────────────────

test("the access token is never written to browser storage", async ({
  page,
}) => {
  await mockBackend(page);
  await login(page);
  const stored = await page.evaluate(() =>
    JSON.stringify({ ...localStorage, ...sessionStorage }),
  );
  expect(stored).not.toContain("test-token");
});

test("a wrong password shows a clear error and stays on sign-in", async ({
  page,
}) => {
  await mockBackend(page);
  await page.goto("/");
  await page.getByLabel("Username").fill("analyst");
  await page.getByLabel("Password").fill("wrong-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Incorrect username or password.",
  );
  await expect(page.getByRole("heading", SIGN_IN_HEADING)).toBeVisible();
});

test("an expired access token is renewed silently and the request retried", async ({
  page,
}) => {
  const state = await mockBackend(page);
  await login(page);
  const before = state.refreshes;
  state.rejectNextToken = true;
  await page.getByRole("button", { name: "Refresh conversation list" }).click();
  await expect.poll(() => state.refreshes).toBe(before + 1);
  await expect(page.getByLabel("Ask a financial question")).toBeVisible();
  await expect(page.getByRole("heading", SIGN_IN_HEADING)).toHaveCount(0);
});

test("there is no sign-up and no demo account picker", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await expect(
    page.getByText(/sign up|create an account|demo account/i),
  ).toHaveCount(0);
  await expect(page.getByText("There is no public sign-up.")).toBeVisible();
});

// ── Administration ──────────────────────────────────────────────────────────

test("admin manages users: reset a password and see it once", async ({
  page,
}) => {
  const state = await mockBackend(page, { role: "admin" });
  await login(page, "admin");
  await page.getByRole("link", { name: "Users" }).click();
  await expect(
    page.getByRole("heading", { name: "Users", level: 1 }),
  ).toBeVisible();
  await expect(page.getByText("Locked", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Manage analyst" }).click();
  const panel = page.getByRole("dialog", { name: "Manage Riley Analyst" });
  await panel.getByRole("button", { name: "Reset", exact: true }).click();
  await page.getByRole("button", { name: "Reset password" }).click();
  await expect(page.getByText("Wxyz-Abcd-Efgh-Jkmn")).toBeVisible();
  expect(state.reset.password).toBeNull();
  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByText("Wxyz-Abcd-Efgh-Jkmn")).toHaveCount(0);
});

test("admin deactivation asks for confirmation; unlock is offered for locked users", async ({
  page,
}) => {
  const state = await mockBackend(page, { role: "admin" });
  await login(page, "admin");
  await page.goto("/admin/users");
  await page.getByRole("button", { name: "Manage analyst" }).click();
  await page.getByRole("button", { name: "Unlock", exact: true }).click();
  await expect(
    page.getByText("Riley Analyst can sign in again."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Deactivate", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "Deactivate Riley Analyst?" }),
  ).toBeVisible();
  await page
    .getByRole("dialog", { name: "Deactivate Riley Analyst?" })
    .getByRole("button", { name: "Deactivate" })
    .click();
  await expect(
    page.getByText("Riley Analyst has been deactivated."),
  ).toBeVisible();
  expect(state.users[1].active).toBe(false);
});

test("admin adds a user and gets their first password", async ({ page }) => {
  const state = await mockBackend(page, { role: "admin" });
  await login(page, "admin");
  await page.goto("/admin/users");
  await page.getByRole("button", { name: "Add user" }).click();
  await page.getByLabel("Full name").fill("Jane Doe");
  await page.getByLabel("Username").fill("Jane.Doe");
  await page.getByLabel("Role").selectOption("finance");
  await page.getByRole("button", { name: "Create user" }).click();
  await expect(
    page.getByRole("dialog", { name: "User created" }),
  ).toBeVisible();
  await expect(page.getByText("Abcd-Efgh-Jkmn-Pqrs")).toBeVisible();
  expect(state.created).toMatchObject({
    username: "jane.doe",
    name: "Jane Doe",
    role: "finance",
  });
});

test("admin activity lists sign-in events in plain words", async ({ page }) => {
  await mockBackend(page, { role: "admin" });
  await login(page, "admin");
  await page.getByRole("link", { name: "Activity" }).click();
  await expect(page.getByText("Failed sign-in")).toBeVisible();
  await expect(page.getByText("wrong password · from 10.0.0.2")).toBeVisible();
  await expect(page.getByText("Connected an MCP client")).toBeVisible();
  // Empty details are left out rather than shown as dashes.
  await expect(page.locator(".activity-item").getByText("—")).toHaveCount(0);
  await page.getByRole("button", { name: "Problems" }).click();
  await expect(page.getByText("Connected an MCP client")).toBeHidden();
  await expect(page.getByText("Failed sign-in")).toBeVisible();
});

test("non-admins are sent back from admin pages", async ({ page }) => {
  await mockBackend(page);
  await login(page);
  await page.goto("/admin/users");
  await expect(page.getByLabel("Ask a financial question")).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
});

// ── Account, Connect and MCP consent ────────────────────────────────────────

test("account page changes the password and lists sessions", async ({
  page,
}) => {
  await mockBackend(page);
  await login(page);
  await openAccountMenu(page);
  await page.getByRole("link", { name: "Manage your account" }).click();
  await expect(
    page.locator(".session-list").getByText("Claude Code"),
  ).toBeVisible();
  await expect(page.getByText("This browser")).toBeVisible();
  await page.getByLabel("Current password").fill("not-the-password");
  await page
    .getByLabel("New password", { exact: true })
    .fill("a-new-long-passphrase");
  await page.getByLabel("Confirm new password").fill("a-different-passphrase");
  await expect(page.getByText("The new passwords do not match.")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Change password" }),
  ).toBeDisabled();
  await page.getByLabel("Confirm new password").fill("a-new-long-passphrase");
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "current password is incorrect",
  );
  await page.getByLabel("Current password").fill("old-password-123");
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByText(/Password changed/)).toBeVisible();
});

test("connect page shows the server's MCP address", async ({ page }) => {
  await mockBackend(page);
  await login(page);
  await page.getByRole("link", { name: "Connect Claude Code" }).click();
  await expect(
    page.getByText(
      "claude mcp add --transport http financial-research https://research.example.com/mcp",
    ),
  ).toBeVisible();
});

test("MCP consent: sign in, allow, and return to the client", async ({
  page,
}) => {
  await mockBackend(page);
  let approved = false;
  await page.route("**/v1/oauth/requests/req-1/approve", (route) => {
    approved = true;
    route.fulfill({
      json: {
        redirect_url: "http://localhost:53682/callback?code=abc&state=xyz",
      },
    });
  });
  let returned = "";
  await page.route("http://localhost:53682/**", (route) => {
    returned = route.request().url();
    route.fulfill({ body: "Authentication complete" });
  });
  await page.goto("/oauth/consent?request=req-1");
  await expect(
    page.getByRole("heading", { name: "Connect Claude Code" }),
  ).toBeVisible();
  await page.getByLabel("Username").fill("analyst");
  await page.getByLabel("Password").fill("a-test-password");
  await page.getByRole("button", { name: "Sign in and continue" }).click();
  await expect(
    page.getByText("Search and read the documents your role can access"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Allow access" }).click();
  await expect.poll(() => returned).toContain("code=abc");
  expect(approved).toBe(true);
});

test("MCP consent explains an expired request", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/oauth/consent?request=expired");
  await expect(
    page.getByRole("heading", { name: "Connection request unavailable" }),
  ).toBeVisible();
});

test("MCP consent warns when access goes to a hosted client", async ({
  page,
}) => {
  await mockBackend(page);
  await login(page);
  await page.goto("/oauth/consent?request=hosted");
  await expect(
    page.getByText("Access will be sent to claude.ai"),
  ).toBeVisible();
});

test("signing out in one tab signs out the others", async ({ context }) => {
  const first = await context.newPage();
  await mockBackend(first);
  await login(first);
  const second = await context.newPage();
  await mockBackend(second);
  // The mock's sign-in state is per page, so sign the second tab in directly.
  await login(second);
  await signOut(first);
  await expect(second.getByRole("heading", SIGN_IN_HEADING)).toBeVisible();
  await expect(
    second.getByText("You signed out in another tab."),
  ).toBeVisible();
});

test("a conversation can be deleted after confirmation", async ({ page }) => {
  const state = await mockBackend(page);
  state.threads = [
    { thread_id: "t-1", title: "Amazon payables", is_interrupted: false },
    { thread_id: "t-2", title: "Held for review", is_interrupted: true },
  ];
  await login(page);
  await expect(
    page.getByRole("button", { name: "Delete conversation: Held for review" }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Delete conversation: Amazon payables" })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Delete this conversation?",
  });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Delete" }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.deleted).toBe("t-1");
  await expect(
    page.getByRole("button", { name: "Amazon payables" }),
  ).toHaveCount(0);
});

test("admin edits a user's name and department", async ({ page }) => {
  const state = await mockBackend(page, { role: "admin" });
  await login(page, "admin");
  await page.goto("/admin/users");
  await page.getByRole("button", { name: "Manage analyst" }).click();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByLabel("Full name").fill("Riley Chen");
  await page.getByLabel("Department").fill("Equity Research");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Details saved.")).toBeVisible();
  expect(state.updated).toEqual({
    name: "Riley Chen",
    department: "Equity Research",
  });
});

test("sign-in explains how to recover a forgotten password", async ({
  page,
}) => {
  await mockBackend(page);
  await page.goto("/");
  await page.getByText("Forgot your password?").click();
  await expect(
    page.getByText("Ask your administrator to reset it."),
  ).toBeVisible();
});

test("the account menu shows who is signed in and closes with Escape", async ({
  page,
}) => {
  await mockBackend(page);
  await login(page);
  await openAccountMenu(page);
  const menu = page.getByRole("dialog", { name: "Account" });
  await expect(menu.getByText("Hi, Demo analyst")).toBeVisible();
  await expect(menu.getByText("Analyst", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
});

test("light is the default appearance and the dark choice is remembered", async ({
  page,
}) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await mockBackend(page);
  await login(page);
  // The device prefers dark, but the app starts light until the person chooses otherwise.
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await openAccountMenu(page);
  await page.getByRole("button", { name: "Dark" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await openAccountMenu(page);
  await page.getByRole("button", { name: "Light" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});

test("citations in an answer are shown as compact chips", async ({ page }) => {
  await mockBackend(page, { mode: "cited" });
  await login(page);
  await page.getByLabel("Ask a financial question").fill("Microsoft revenue?");
  await page.getByRole("button", { name: "Send question" }).click();
  const chips = page.locator(".answer .cite");
  await expect(chips).toHaveText([
    "MICROSOFT_2016_10K · p. 28",
    "MICROSOFT_2016_10K · p. 52",
  ]);
  await expect(chips.first()).toHaveAttribute(
    "title",
    "MICROSOFT_2016_10K, page 28",
  );
  await expect(page.locator(".answer")).not.toContainText("Source 5");
  await expect(page.locator(".answer")).not.toContainText("【");
});
