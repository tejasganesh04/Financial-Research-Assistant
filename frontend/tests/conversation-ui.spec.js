import { test, expect } from "@playwright/test";

// Behaviour of the conversation view that unit tests miss: scrolling in long conversations,
// moving between pages, double submits, layout overflow and dropped connections.
// Every API request is intercepted; nothing reaches a server or a model.

const table = [
  "| Year | Revenue | Cost of revenue | Gross margin | Operating income | Net income | Notes |",
  "| --- | --- | --- | --- | --- | --- | --- |",
  ...Array.from(
    { length: 6 },
    (_, i) =>
      `| FY${2016 + i} | $${85 + i},320 million | $${32 + i},780 million | ${61 + i}.6% | $${20 + i},182 million | $${16 + i},798 million | restated in the following year's 10-K filing |`,
  ),
].join("\n");

function longAnswer(n) {
  return [
    `Answer ${n}. Microsoft's revenue grew steadily over the period.`,
    "Revenue figures come from the income statement. ".repeat(12),
    table,
    `Bottom line for question ${n}: margins expanded.【Source 1: MICROSOFT_2016_10K.pdf, Page 28】`,
  ].join("\n\n");
}

function history(id, pairs) {
  const messages = [];
  for (let i = 1; i <= pairs; i += 1) {
    messages.push({ role: "user", content: `Question ${i} in ${id}` });
    messages.push({
      role: "assistant",
      content: longAnswer(i),
      sources: [{ file: "MICROSOFT_2016_10K.pdf", page: 28 + (i % 5) }],
      confidence: 0.95,
    });
  }
  return {
    thread_id: id,
    is_interrupted: false,
    interrupt_payload: null,
    messages,
  };
}

async function mock(page, { conversations = 3, pairs = 30 } = {}) {
  const state = { asks: 0, release: null, drop: false, held: false };
  const threads = Array.from({ length: conversations }, (_, i) => ({
    thread_id: `thread-${i + 1}`,
    title: `Conversation ${i + 1} about revenue, margins and the cost of revenue over several years`,
    is_interrupted: false,
    updated_at: "2026-11-16T10:00:00Z",
  }));
  const user = {
    user_id: "analyst",
    name: "Demo analyst",
    role: "analyst",
    department: "Research",
    permissions: {
      allowed_doc_types: ["10k"],
      allowed_confidentiality: ["public"],
    },
  };
  const session = {
    access_token: "t",
    token_type: "bearer",
    expires_in: 3600,
    user,
  };
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body, status = 200) => route.fulfill({ status, json: body });
    if (path === "/v1/auth/refresh" || path === "/v1/auth/login")
      return json(session);
    if (path === "/v1/auth/logout") return route.fulfill({ status: 204 });
    if (path === "/v1/auth/sessions") return json({ sessions: [] });
    if (path === "/v1/auth/me") return json(user);
    if (path === "/v1/oauth/server")
      return json({
        enabled: true,
        mcp_url: "https://research.example.com/mcp",
      });
    if (path === "/v1/conversations") return json({ threads });
    if (path.endsWith("/messages/stream")) {
      state.asks += 1;
      const { message } = request.postDataJSON();
      if (state.held)
        await new Promise((resolve) => {
          state.release = resolve;
        });
      if (state.drop)
        return route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          body: `data: ${JSON.stringify({ type: "node_start", label: "Searching filings…" })}\n\n`,
        });
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body:
          `data: ${JSON.stringify({ type: "node_start", label: "Searching filings…" })}\n\n` +
          `data: ${JSON.stringify({
            type: "final",
            response: `New answer to “${message}”.\n\n${longAnswer(99)}`,
            sources: [{ file: "MICROSOFT_2016_10K.pdf", page: 28 }],
            confidence: 0.95,
          })}\n\n`,
      });
    }
    const match = path.match(/^\/v1\/conversations\/([^/]+)$/);
    if (match && request.method() === "GET")
      return json(history(match[1], pairs));
    if (match && request.method() === "DELETE")
      return route.fulfill({ status: 204 });
    return json({ detail: "not mocked" }, 404);
  });
  await page.goto("/");
  return state;
}

const scrollBox = (page) => page.locator(".chat-scroll");
const metrics = (page) =>
  scrollBox(page).evaluate((el) => ({
    top: Math.round(el.scrollTop),
    max: Math.round(el.scrollHeight - el.clientHeight),
  }));
const atBottom = async (page) => {
  const { top, max } = await metrics(page);
  return max - top <= 2;
};

async function openConversation(page, n) {
  const link = page.locator(".conversation-item").nth(n - 1);
  await expect(link).toBeAttached();
  if (page.viewportSize().width < 860)
    await page.getByRole("button", { name: "Open menu" }).click();
  await link.click();
  await expect(page.getByText(`Question 30 in thread-${n}`)).toBeVisible();
}

async function ask(page, text) {
  await page.getByLabel("Ask a financial question").fill(text);
  await page.keyboard.press("Enter");
}

test("a long conversation opens at its latest answer", async ({ page }) => {
  await mock(page);
  await openConversation(page, 1);
  await expect.poll(() => atBottom(page)).toBe(true);
  expect((await metrics(page)).max).toBeGreaterThan(5000);
});

test("a new question in a long conversation follows to its answer", async ({
  page,
}) => {
  await mock(page);
  await openConversation(page, 1);
  await ask(page, "What about FY2023?");
  await expect(
    page.getByText("New answer to “What about FY2023?”."),
  ).toBeVisible();
  await expect.poll(() => atBottom(page)).toBe(true);
});

test("an answer arriving while the reader scrolled up does not move the page", async ({
  page,
}) => {
  const state = await mock(page);
  await openConversation(page, 1);
  state.held = true;
  await ask(page, "Hold this one");
  await expect.poll(() => state.asks).toBe(1);
  await scrollBox(page).evaluate((el) => (el.scrollTop = 1200));
  await page.waitForTimeout(150);
  const before = (await metrics(page)).top;
  state.release();
  await expect(page.getByText("New answer to “Hold this one”.")).toBeAttached();
  await page.waitForTimeout(300);
  expect(Math.abs((await metrics(page)).top - before)).toBeLessThanOrEqual(2);
});

test("switching conversations opens each at its latest answer", async ({
  page,
}) => {
  await mock(page);
  await openConversation(page, 1);
  await scrollBox(page).evaluate((el) => (el.scrollTop = 0));
  await openConversation(page, 2);
  await expect.poll(() => atBottom(page)).toBe(true);
});

test("a new conversation starts at the top of the welcome page", async ({
  page,
}) => {
  await mock(page);
  await openConversation(page, 1);
  await page.getByRole("button", { name: "New conversation" }).click();
  await expect(
    page.getByRole("heading", { name: "What would you like to research?" }),
  ).toBeVisible();
  expect((await metrics(page)).top).toBe(0);
});

test("the reading position survives a visit to another page", async ({
  page,
}) => {
  await mock(page);
  await openConversation(page, 1);
  await scrollBox(page).evaluate((el) => (el.scrollTop = 2000));
  await page.waitForTimeout(100);
  await page.getByRole("link", { name: "Connect Claude Code" }).click();
  await expect(
    page.getByRole("heading", { name: "Connect Claude Code" }),
  ).toBeVisible();
  await page.goBack();
  await expect(page.getByText("Question 30 in thread-1")).toBeVisible();
  expect(Math.abs((await metrics(page)).top - 2000)).toBeLessThanOrEqual(2);
});

test("pressing Enter twice sends one question", async ({ page }) => {
  const state = await mock(page);
  await openConversation(page, 1);
  state.held = true;
  await page.getByLabel("Ask a financial question").fill("Only once");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  await page
    .getByRole("button", { name: "Send question" })
    .click({ force: true });
  await page.waitForTimeout(300);
  state.release?.();
  await expect(page.getByText("New answer to “Only once”.")).toBeVisible();
  expect(state.asks).toBe(1);
  await expect(
    page.locator(".message.user", { hasText: "Only once" }),
  ).toHaveCount(1);
});

test("Enter while composing with an input method does not send", async ({
  page,
}) => {
  const state = await mock(page);
  const box = page.getByLabel("Ask a financial question");
  await box.fill("日本語");
  await box.evaluate((el) =>
    el.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        bubbles: true,
        cancelable: true,
        isComposing: true,
      }),
    ),
  );
  await page.waitForTimeout(200);
  expect(state.asks).toBe(0);
  await expect(box).toHaveValue("日本語");
});

test("Shift+Enter adds a line and the box grows only up to its limit", async ({
  page,
}) => {
  await mock(page);
  const box = page.getByLabel("Ask a financial question");
  await box.click();
  const start = (await box.boundingBox()).height;
  await page.keyboard.type("line one");
  await page.keyboard.press("Shift+Enter");
  await page.keyboard.type("line two");
  await expect(box).toHaveValue("line one\nline two");
  expect((await box.boundingBox()).height).toBeGreaterThan(start);
  await box.fill(
    Array.from({ length: 60 }, (_, i) => `pasted line ${i}`).join("\n"),
  );
  const tall = (await box.boundingBox()).height;
  expect(tall).toBeLessThanOrEqual(222);
  expect(await box.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(
    true,
  );
});

test("the page itself never scrolls; only the conversation and sidebar do", async ({
  page,
}) => {
  await mock(page, { conversations: 80 });
  await openConversation(page, 1);
  const page_ = await page.evaluate(() => ({
    vertical: document.documentElement.scrollHeight - window.innerHeight,
    horizontal: document.documentElement.scrollWidth - window.innerWidth,
  }));
  expect(page_.vertical).toBeLessThanOrEqual(0);
  expect(page_.horizontal).toBeLessThanOrEqual(0);
  const list = page.locator(".conversation-list");
  expect(await list.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(
    true,
  );
  await list.evaluate((el) => (el.scrollTop = el.scrollHeight));
  await expect(page.locator(".conversation-item").last()).toBeInViewport();
  // The footer link stays reachable below the long list.
  await expect(
    page.getByRole("link", { name: "Connect Claude Code" }),
  ).toBeInViewport();
});

test("wide tables scroll inside the answer instead of widening the page", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mock(page);
  await openConversation(page, 1);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  const scroller = page.locator(".answer .table-scroll").last();
  expect(await scroller.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(
    true,
  );
});

test("a very long word in a question wraps instead of overflowing", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await mock(page);
  await ask(page, "https://example.com/" + "a".repeat(300));
  await expect.poll(() => state.asks).toBe(1);
  const bubble = page.locator(".message.user .message-body").last();
  const box = await bubble.boundingBox();
  expect(box.x + box.width).toBeLessThanOrEqual(390);
});

test("a dropped connection keeps the question and says to check history", async ({
  page,
}) => {
  const state = await mock(page);
  state.drop = true;
  await ask(page, "This will drop");
  await expect(
    page.getByText(/connection ended before the answer finished/i),
  ).toBeVisible();
  expect(state.asks).toBe(1);
  await expect(page.getByLabel("Ask a financial question")).toBeEnabled();
});

test("resizing the window keeps a reader at the bottom anchored there", async ({
  page,
}) => {
  await mock(page);
  await openConversation(page, 1);
  await expect.poll(() => atBottom(page)).toBe(true);
  await page.setViewportSize({ width: 900, height: 600 });
  await page.waitForTimeout(200);
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.waitForTimeout(200);
  await expect.poll(() => atBottom(page)).toBe(true);
});

test("the phone menu locks the page behind it and closes with Escape", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mock(page, { conversations: 40 });
  await page.getByRole("button", { name: "Open menu" }).click();
  await expect(page.locator(".sidebar")).toBeInViewport();
  await page.keyboard.press("Escape");
  await expect(page.locator(".sidebar")).not.toBeInViewport();
  await expect(page.getByRole("button", { name: "Open menu" })).toBeFocused();
});

test("a dialog keeps keyboard focus inside and returns it when closed", async ({
  page,
}) => {
  await mock(page);
  const row = page.locator(".conversation-row").first();
  await row.hover();
  const trigger = row.getByRole("button", { name: /^Delete conversation/ });
  // Opened from the keyboard: Safari does not focus buttons that are clicked.
  await trigger.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  for (let i = 0; i < 6; i += 1) {
    await page.keyboard.press("Tab");
    expect(
      await dialog.evaluate((el) => el.contains(document.activeElement)),
    ).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("reloading reopens the conversation that was open", async ({ page }) => {
  await mock(page);
  await openConversation(page, 2);
  await page.reload();
  await expect(page.getByText("Question 30 in thread-2")).toBeVisible();
  await expect.poll(() => atBottom(page)).toBe(true);
});

test("the research and connect pages pass an accessibility audit", async ({
  page,
}) => {
  const { AxeBuilder } = await import("@axe-core/playwright");
  await mock(page);
  await openConversation(page, 1);
  for (const path of ["/", "/connect", "/account"]) {
    if (path !== "/") await page.goto(path);
    await page.waitForTimeout(400);
    const result = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();
    expect(
      result.violations.map((v) => `${path}: ${v.id} ${v.nodes[0]?.target}`),
    ).toEqual([]);
  }
});
