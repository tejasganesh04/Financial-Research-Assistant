// End-to-end check of the review flow on a running server, through two real browsers.
//
//   BASE=http://localhost:8000 ADMIN_PASSWORD=... node tests/review-check.mjs
//
// A temporary HR account (whose role sees no filings) asks two questions with an explicit
// access request. An administrator approves one and rejects the other; the HR user sees the
// released answer and the rejection. Asks the answer model twice. Deactivates the account after.
import { chromium } from "@playwright/test";

const BASE = (process.env.BASE || "http://localhost:8000").replace(/\/$/, "");
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD;
if (!ADMIN_PASSWORD) {
  console.error("Set ADMIN_PASSWORD.");
  process.exit(2);
}
const results = [];
let current = null;
async function step(name, run) {
  const started = Date.now();
  try {
    await run();
    results.push(true);
    console.log(
      `  ok    ${name} (${((Date.now() - started) / 1000).toFixed(1)}s)`,
    );
  } catch (error) {
    results.push(false);
    console.log(
      `  FAIL  ${name}\n        ${String(error.message).split("\n")[0]}`,
    );
    if (current)
      await current
        .screenshot({ path: `review-check-failure-${results.length}.png` })
        .catch(() => {});
    throw error;
  }
}
const visible = (locator, timeout = 20000) =>
  locator.first().waitFor({ state: "visible", timeout });

async function signIn(page, username, password) {
  await page.goto(BASE + "/");
  await visible(page.locator("#username"));
  await page.fill("#username", username);
  await page.fill("#password", password);
  await page.getByRole("button", { name: /^Sign in/ }).click();
  await visible(page.locator("#question"));
}

async function api(method, path, token, body) {
  const response = await fetch(BASE + path, {
    method,
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  return {
    status: response.status,
    data: await response.json().catch(() => null),
  };
}

const login = await (
  await fetch(BASE + "/v1/auth/login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ username: "admin", password: ADMIN_PASSWORD }),
  })
).json();
const adminToken = login.access_token;
const username = `e2e-hr-${Date.now().toString(36)}`;
const created = await api("POST", "/v1/admin/users", adminToken, {
  username,
  name: "E2E Reviewer Test",
  role: "hr",
});
const password = created.data.password;

const browser = await chromium.launch({
  channel: process.env.PLAYWRIGHT_CHANNEL || "chrome",
});
const hrPage = await (
  await browser.newContext({ viewport: { width: 1366, height: 860 } })
).newPage();
const adminPage = await (
  await browser.newContext({ viewport: { width: 1366, height: 860 } })
).newPage();
const QUESTION_A = "What was Microsoft's total revenue in fiscal year 2016?";
const QUESTION_B = "What was Microsoft's cost of revenue in fiscal year 2016?";

// Wait until the chosen conversation has replaced the one on screen.
async function openHrConversation(title) {
  await hrPage.locator(".conversation-item", { hasText: title }).click();
  await visible(hrPage.locator("h1.topbar-title", { hasText: title }), 60000);
  await hrPage
    .locator(".research[aria-busy]")
    .waitFor({ state: "detached", timeout: 60000 });
}

async function escalate(question) {
  await hrPage.getByRole("button", { name: "New conversation" }).click();
  await hrPage.fill(
    "#question",
    `/escalate doc_type=filings confidentiality=public\n${question}`,
  );
  await hrPage.keyboard.press("Enter");
  await visible(
    hrPage.getByRole("heading", { name: "Waiting for review" }),
    240000,
  );
}

console.log(`Review flow on ${BASE} with ${username}`);
try {
  current = hrPage;
  await step(
    "HR signs in; without a request their role finds nothing",
    async () => {
      await signIn(hrPage, username, password);
      const r = await api(
        "GET",
        "/v1/auth/me",
        (
          await (
            await fetch(BASE + "/v1/auth/login", {
              method: "POST",
              headers: { "content-type": "application/json" },
              body: JSON.stringify({ username, password }),
            })
          ).json()
        ).access_token,
      );
      if (
        !r.data.permissions.allowed_doc_types.every(
          (t) => t === "expense_policy",
        )
      )
        throw new Error(
          `unexpected permissions ${JSON.stringify(r.data.permissions)}`,
        );
    },
  );

  await step(
    "HR's explicit request is held for review, with no answer shown",
    async () => {
      await escalate(QUESTION_A);
      const answers = await hrPage
        .locator(".message.assistant .answer")
        .count();
      if (answers) throw new Error("an answer was shown before review");
      await visible(hrPage.locator(".pending-dot"));
    },
  );

  await step(
    "the composer is closed while the conversation waits",
    async () => {
      if (!(await hrPage.locator("#question").isDisabled()))
        throw new Error("the question box is still open");
    },
  );

  await step("a second request is held as well", async () => {
    await escalate(QUESTION_B);
  });

  current = adminPage;
  await step(
    "admin sees both requests under Approvals, with the draft and sources",
    async () => {
      await signIn(adminPage, "admin", ADMIN_PASSWORD);
      await adminPage.getByRole("link", { name: /Approvals/ }).click();
      await visible(
        adminPage.locator(".queue-item", { hasText: "total revenue" }),
      );
      await visible(
        adminPage.locator(".queue-item", { hasText: "cost of revenue" }),
      );
      await adminPage
        .locator(".queue-item", { hasText: "total revenue" })
        .click();
      await visible(adminPage.locator(".draft .answer"));
      await visible(adminPage.locator(".draft .source"));
    },
  );

  await step("rejecting needs a reason", async () => {
    await adminPage
      .locator(".queue-item", { hasText: "cost of revenue" })
      .click();
    await adminPage.getByRole("button", { name: "Reject answer" }).click();
    await visible(
      adminPage.getByText("Add a reason before rejecting this answer."),
    );
  });

  await step("admin rejects one request with a reason", async () => {
    await adminPage.fill(
      "#review-reason",
      "Please ask your manager for filings access first.",
    );
    await adminPage.getByRole("button", { name: "Reject answer" }).click();
    await visible(adminPage.getByText(/Answer rejected/));
  });

  await step("admin approves the other; the queue empties", async () => {
    await adminPage
      .locator(".queue-item", { hasText: "total revenue" })
      .click();
    await adminPage.getByRole("button", { name: /Approve/ }).click();
    await visible(adminPage.getByText(/released|approved/i));
    await adminPage.goto(BASE + "/approvals");
    await visible(adminPage.locator(".page-header"));
    await adminPage.waitForTimeout(1500);
    const left = await adminPage
      .locator(".queue-item", { hasText: /total revenue|cost of revenue/ })
      .count();
    if (left) throw new Error(`${left} decided request(s) still listed`);
  });

  current = hrPage;
  await step(
    "HR sees the approved answer appear, with its figure",
    async () => {
      await openHrConversation(/total revenue/i);
      await visible(hrPage.locator(".message.assistant .answer"), 60000);
      const text = await hrPage
        .locator(".message.assistant .answer")
        .first()
        .textContent();
      if (!/\d/.test(text))
        throw new Error(`no figure in: ${text.slice(0, 120)}`);
      console.log(
        `        released: ${text.replace(/\s+/g, " ").slice(0, 120)}…`,
      );
    },
  );

  await step("HR sees the rejection and its reason", async () => {
    await openHrConversation(/cost of revenue/i);
    await visible(
      hrPage.getByText("The reviewer rejected this answer."),
      60000,
    );
    await visible(
      hrPage.getByText("Please ask your manager for filings access first."),
    );
  });

  await step(
    "HR still cannot open the PDF behind the released answer",
    async () => {
      const t = (
        await (
          await fetch(BASE + "/v1/auth/login", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ username, password }),
          })
        ).json()
      ).access_token;
      const r = await fetch(BASE + "/v1/documents/MICROSOFT_2016_10K.pdf", {
        headers: { authorization: `Bearer ${t}` },
      });
      if (r.status !== 403) throw new Error(`got ${r.status}`);
    },
  );

  await step(
    "the activity feed and approval decisions leave HR's account usable",
    async () => {
      await hrPage.getByRole("button", { name: "New conversation" }).click();
      if (await hrPage.locator("#question").isDisabled())
        throw new Error("question box closed");
    },
  );
} catch {
  // reported above
} finally {
  await api("POST", `/v1/admin/users/${username}/deactivate`, adminToken);
  console.log(`  ok    temporary account ${username} deactivated`);
  await browser.close();
}
const failed = results.filter((ok) => !ok).length;
console.log(
  failed
    ? `\n${failed} step failed; later steps were not run.`
    : `\nAll ${results.length} steps passed.`,
);
process.exit(failed ? 1 : 0);
