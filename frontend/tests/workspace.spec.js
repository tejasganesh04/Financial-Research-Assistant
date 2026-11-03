import { test, expect } from "@playwright/test";

// Every API request is intercepted. Browser tests cannot submit a paid question.
async function mockBackend(page, { role = "analyst", mode = "answer" } = {}) {
  const state = {
    submissions: 0,
    reads: 0,
    reviewed: false,
    messages: [],
    pending: mode === "pending",
  };
  const user = {
    user_id: role,
    name: `Demo ${role}`,
    role,
    department: "Research",
  };
  const source = { file: "MICROSOFT_2016_10K.pdf", page: 52, doc_type: "10k" };
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body) => route.fulfill({ json: body });
    if (path === "/v1/auth/login")
      return json({ ...user, access_token: "test-token" });
    expect(request.headers().authorization).toBe("Bearer test-token");
    if (state.expired)
      return route.fulfill({ status: 401, json: { detail: "Token expired" } });
    if (path === "/v1/auth/me") return json(user);
    if (path === "/v1/conversations") return json({ threads: [] });
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
          { type: "request_started", request_id: "request-limit", thread_id: id, sanitized_message: sanitized },
          { type: "node_start", node: "grader", label: "Evaluating relevance", request_id: "request-limit" },
          { type: "error", code: "provider_rate_limited", status: 429, provider: "Groq",
            model: "openai/gpt-oss-20b", node: "grader", retry_after_seconds: 17, request_id: "request-limit",
            message: "Groq model openai/gpt-oss-20b was rate limited at node grader. Wait at least 17 seconds before trying again. No answer was released for this request." },
        ];
        return route.fulfill({ contentType: "text/event-stream",
          body: events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("") });
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
  await page.getByLabel("Demo accounts").selectOption(account);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Research workspace" }),
  ).toBeVisible();
}

test("message length is shown only after rejection and the question remains editable", async ({ page }) => {
  const state = await mockBackend(page);
  await page.route("**/messages/stream", async (route) => {
    const message = route.request().postDataJSON().message;
    if (message.length <= 2000) return route.fallback();
    return route.fulfill({ status: 422, json: { detail: [{
      type: "string_too_long", loc: ["body", "message"],
      msg: "String should have at most 2000 characters", ctx: { max_length: 2000 },
    }] } });
  });
  await login(page);
  const input = page.getByLabel("Ask a financial question");
  const longQuestion = "x".repeat(2001);
  await input.fill(longQuestion);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(input).not.toHaveAttribute("maxlength", /.+/);
  await page.getByRole("button", { name: "Send question", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("2,000 characters or fewer");
  await expect(input).toHaveValue(longQuestion);
  await expect(page.locator(".message.user")).toHaveCount(0);
  expect(state.submissions).toBe(0);
  await input.fill("Microsoft revenue?");
  await page.getByRole("button", { name: "Send question", exact: true }).click();
  await expect(page.getByText("$32,780 million", { exact: true })).toBeVisible();
  expect(state.submissions).toBe(1);
});

for (const streamedError of [false, true]) {
  test(`full conversation shows a limit only on submit (${streamedError ? "SSE" : "HTTP"})`, async ({ page }) => {
    const state = await mockBackend(page);
    let limitedPath;
    const message = "This conversation has reached 20 questions. Please start a new conversation.";
    await page.route("**/messages/stream", async (route) => {
      const path = new URL(route.request().url()).pathname;
      limitedPath ||= path;
      if (path !== limitedPath) return route.fallback();
      return streamedError
        ? route.fulfill({ contentType: "text/event-stream",
            body: `data: ${JSON.stringify({ type: "error", status: 409, message })}\n\n` })
        : route.fulfill({ status: 409, json: { detail: message } });
    });
    await login(page);
    await expect(page.getByText(/20 questions/)).toHaveCount(0);
    const input = page.getByLabel("Ask a financial question");
    await input.fill("Microsoft revenue?");
    await page.getByRole("button", { name: "Send question", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText(message);
    await expect(page.getByRole("alert")).not.toContainText("Check saved history");
    await expect(input).toHaveValue("Microsoft revenue?");
    await expect(page.locator(".message.user")).toHaveCount(0);
    expect(state.submissions).toBe(0);
    await page.getByRole("button", { name: "New conversation", exact: true }).click();
    await input.fill("Microsoft revenue?");
    await page.getByRole("button", { name: "Send question", exact: true }).click();
    await expect(page.getByText("$32,780 million", { exact: true })).toBeVisible();
    expect(state.submissions).toBe(1);
  });
}

test("login, sample fill, complete answer, sources, refresh and logout", async ({
  page,
}) => {
  const state = await mockBackend(page);
  await login(page);
  await expect(
    page.getByRole("button", { name: "Approvals", exact: true }),
  ).toHaveCount(0);
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
  await expect(
    page.getByText(/original PDF is not stored locally/),
  ).toBeVisible();
  await page.reload();
  await page.getByText("Request details", { exact: true }).click();
  await expect(page.getByText("request-test", { exact: true })).toBeVisible();
  await expect(
    page.getByText("$32,780 million", { exact: true }),
  ).toBeVisible();
  expect(state.submissions).toBe(1);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(
    page.getByRole("heading", { name: "Welcome back" }),
  ).toBeVisible();
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

test("rate limits name the model and node, keep the trace link and never resubmit", async ({ page }) => {
  const state = await mockBackend(page, { mode: "rate-limit" });
  await login(page);
  await page.getByLabel("Ask a financial question").fill("Microsoft revenue?");
  await page.getByRole("button", { name: "Send question", exact: true }).click();
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("Groq model openai/gpt-oss-20b");
  await expect(alert).toContainText("rate limited at node grader");
  await expect(alert).toContainText("17 seconds");
  await page.getByText("Request details", { exact: true }).click();
  await expect(page.getByText("request-limit", { exact: true })).toBeVisible();
  await page.screenshot({ path: "../logs/rate-limit-browser.png", fullPage: true });
  await page.getByLabel("Ask a financial question").fill("Another question?");
  await expect(page.getByRole("button", { name: "Send question", exact: true })).toBeEnabled();
  expect(state.submissions).toBe(1);
  await expect(page.getByText("No relevant information", { exact: false })).toHaveCount(0);
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
  await page.getByRole("button", { name: "Approvals", exact: true }).click();
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
  await page.getByRole("button", { name: "Approvals", exact: true }).click();
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
  state.expired = true;
  await page.getByRole("button", { name: "Refresh conversation list" }).click();
  await expect(
    page.getByRole("heading", { name: "Welcome back" }),
  ).toBeVisible();
  expect(
    await page.evaluate(() =>
      sessionStorage.getItem("financial-research-token"),
    ),
  ).toBeNull();
  expect(state.submissions).toBe(0);
});

test("capture the demo layout for visual review", async ({ page }) => {
  await mockBackend(page);
  await page.setViewportSize({ width: 1360, height: 900 });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Welcome back" }),
  ).toBeVisible();
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
  await login(page, "clevel");
  await expect(
    page.getByRole("button", { name: "Approvals", exact: true }),
  ).toHaveCount(0);
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
