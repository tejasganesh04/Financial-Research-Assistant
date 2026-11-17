import { test, expect } from "@playwright/test";
import { sample } from "./health-sample.js";

// The admin Health page, with every API request intercepted.

const people = {
  admin: {
    user_id: "admin",
    name: "Avery Admin",
    role: "admin",
    department: "IT",
  },
  analyst: {
    user_id: "analyst",
    name: "Riley Analyst",
    role: "analyst",
    department: "Research",
  },
};

async function mock(page, { role = "admin", state = "busy", respond } = {}) {
  const calls = [];
  const user = {
    ...people[role],
    permissions: {
      allowed_doc_types: ["10k"],
      allowed_confidentiality: ["public"],
    },
  };
  await page.route("**/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const json = (body, status = 200) => route.fulfill({ status, json: body });
    if (url.pathname === "/v1/auth/refresh")
      return json({
        access_token: "t",
        token_type: "bearer",
        expires_in: 3600,
        user,
      });
    if (url.pathname === "/v1/admin/health") {
      const range = url.searchParams.get("range");
      calls.push({ range, tz: url.searchParams.get("tz") });
      if (respond) return respond(route, range, calls.length);
      return json(sample(range, state));
    }
    if (url.pathname === "/v1/conversations") return json({ threads: [] });
    if (url.pathname === "/v1/approvals") return json({ approvals: [] });
    return json({ detail: "not mocked" }, 404);
  });
  return calls;
}

test("admins reach Health from the menu and see status, usage and AI figures", async ({
  page,
}) => {
  const calls = await mock(page);
  await page.goto("/");
  await page.getByRole("link", { name: "Health" }).click();
  await expect(page).toHaveURL(/\/admin\/health$/);
  await expect(page).toHaveTitle("Health – Financial Research");
  await expect(
    page.getByRole("heading", { name: "All systems working" }),
  ).toBeVisible();
  const services = page.getByRole("list", { name: "Services" });
  for (const name of ["Website", "Answers", "Search", "Reranker", "Backups"])
    await expect(services.getByText(name, { exact: true })).toBeVisible();
  await expect(services.getByText("41,233 passages")).toBeVisible();
  await expect(page.locator(".health-tile").first()).toContainText("31");
  await expect(page.locator(".health-tile").first()).toContainText(
    "+19% vs the previous 24 hours",
  );
  await expect(page.getByText("about 9 more questions")).toBeVisible();
  await expect(
    page.getByText("941 of 1,000 requests left today"),
  ).toBeVisible();
  const ai = page.getByRole("region", { name: "AI quality and usage" });
  await expect(ai.getByText("Tokens per question")).toBeVisible();
  await expect(ai).toContainText("91%");
  await expect(ai).toContainText("Free tiers, not billed");
  await expect(ai.getByText("Slowed by rate limits")).toBeVisible();
  await expect(ai).toContainText("33%");
  await expect(
    page.getByRole("region", { name: "Where the time goes" }),
  ).toContainText("Checking relevance");
  await expect(
    page.getByRole("img", { name: /Answered: 22 \(71%\)/ }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("region", { name: "Models" })
      .getByText("openai/gpt-oss-20b"),
  ).toBeVisible();
  // The browser's own time zone is sent, so days and hours match the viewer's clock.
  expect(calls[0]).toEqual({ range: "24h", tz: expect.any(String) });
  expect(calls[0].tz.length).toBeGreaterThan(0);
});

test("people who are not administrators cannot open Health", async ({
  page,
}) => {
  const calls = await mock(page, { role: "analyst" });
  await page.goto("/admin/health");
  await expect(page.getByRole("link", { name: "Health" })).toHaveCount(0);
  await expect(page.getByLabel("Ask a financial question")).toBeVisible();
  expect(calls).toHaveLength(0);
});

test("the time range re-reads everything for that period", async ({ page }) => {
  const calls = await mock(page);
  await page.goto("/admin/health");
  await expect(
    page.getByRole("heading", { name: "All systems working" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "7 days" }).click();
  await expect(page.getByRole("button", { name: "7 days" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.getByText("+19% vs the previous 7 days")).toBeVisible();
  // (Development mode opens the page twice; production reads it once.)
  expect(calls.at(-1).range).toBe("7d");
  expect(new Set(calls.map((c) => c.range))).toEqual(new Set(["24h", "7d"]));
  // A week of days in the hidden data table behind the question chart.
  await expect(
    page.locator(".health-tile").first().locator("table tbody tr"),
  ).toHaveCount(7);
});

test("a rate limit and an outage lead the page in words, not colour alone", async ({
  page,
}) => {
  await mock(page, { state: "limited" });
  await page.goto("/admin/health");
  await expect(
    page.getByRole("heading", {
      name: "Answers: Rate-limited · try again in about 6 min",
    }),
  ).toBeVisible();
  await expect(page.getByRole("list", { name: "Services" })).toContainText(
    "Needs attention: Answers",
  );
  await expect(
    page.getByText("Rate-limited now, try again in about 6 min"),
  ).toBeVisible();
  await expect(page.getByText("about 0 more questions")).toBeVisible();

  await page.unrouteAll({ behavior: "wait" });
  await mock(page, { state: "down" });
  await page.reload();
  await expect(
    page.getByRole("heading", {
      name: "Search: Search database not responding",
    }),
  ).toBeVisible();
  await expect(page.getByRole("list", { name: "Services" })).toContainText(
    "Down: Search",
  );
});

test("a quiet period reads calmly, with no placeholder dashes", async ({
  page,
}) => {
  await mock(page, { state: "empty" });
  await page.goto("/admin/health");
  await expect(page.getByText("No answers yet")).toBeVisible();
  await expect(page.getByText("No problems in this period")).toBeVisible();
  await expect(page.getByText("Nobody has asked yet")).toBeVisible();
  await expect(
    page.getByText("Step times appear after the first answer."),
  ).toBeVisible();
  const text = await page.locator(".health-body").innerText();
  expect(text).not.toMatch(/—|–\s*$|NaN|undefined|null/);
});

test("chart values can be read with the keyboard", async ({ page }) => {
  await mock(page);
  await page.goto("/admin/health");
  const trend = page.getByRole("group", { name: /^Questions by hour/ });
  await trend.focus();
  await page.keyboard.press("ArrowLeft");
  const tip = trend.locator(".trend-tip");
  await expect(tip).toBeVisible();
  await expect(tip).toContainText(/question/);
  await expect(tip).toContainText(/Today|Yesterday/);
  await page.keyboard.press("Tab");
  await expect(tip).toHaveCount(0);
});

test("problems link to their LangSmith trace when there is one", async ({
  page,
}) => {
  await mock(page);
  await page.goto("/admin/health");
  const problems = page.getByRole("region", { name: "Recent problems" });
  await expect(problems).toContainText("4 in this period, latest 3 shown");
  const link = problems.getByRole("link", {
    name: /Open the trace for Rate-limited/,
  });
  await expect(link).toHaveAttribute("href", /smith\.langchain\.com/);
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(problems.getByRole("link")).toHaveCount(1);
  await expect(problems).toContainText("Ref 7f3a2c91");
});

test("a failed refresh keeps the last figures and says what went wrong", async ({
  page,
}) => {
  let failing = false;
  await mock(page, {
    respond: (route, range) =>
      failing
        ? route.fulfill({
            status: 503,
            json: {
              detail: "The account database is unavailable. Try again shortly.",
            },
          })
        : route.fulfill({ json: sample(range) }),
  });
  await page.goto("/admin/health");
  await expect(
    page.getByRole("heading", { name: "All systems working" }),
  ).toBeVisible();
  failing = true;
  await page.getByRole("button", { name: "Refresh health" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "The account database is unavailable",
  );
  await expect(
    page.getByRole("heading", { name: "All systems working" }),
  ).toBeVisible();
});

test("without Groq the allowance tile becomes model spend", async ({
  page,
}) => {
  await mock(page, {
    respond: (route, range) => {
      const body = sample(range);
      body.groq = null;
      body.ai.cost_usd = 1.23;
      body.ai.cost_per_question = 0.0456;
      body.ai.models = [
        {
          model: "claude-sonnet-4-6",
          kind: "llm",
          provider: "Anthropic",
          calls: 81,
          input_tokens: 240000,
          output_tokens: 22000,
          errors: 0,
          questions: 27,
          cost_usd: 1.23,
          free_tier: false,
        },
      ];
      return route.fulfill({ json: body });
    },
  });
  await page.goto("/admin/health");
  await expect(
    page.getByRole("heading", { name: "Model spend" }),
  ).toBeVisible();
  await expect(
    page.locator(".health-tile", { hasText: "Model spend" }),
  ).toContainText("$1.23");
  await expect(page.getByText("$0.046 per answered question")).toBeVisible();
  await expect(page.getByRole("region", { name: "Models" })).toContainText(
    "$1.23",
  );
});

test("on a phone the page fits the screen without sideways scrolling", async ({
  page,
}) => {
  await page.setViewportSize({ width: 360, height: 780 });
  await mock(page);
  await page.goto("/admin/health");
  await expect(
    page.getByRole("heading", { name: "All systems working" }),
  ).toBeVisible();
  const overflow = await page.evaluate(() => ({
    page: document.documentElement.scrollWidth - window.innerWidth,
    content: [...document.querySelectorAll(".health-body *")].filter(
      (el) =>
        el.getBoundingClientRect().right > window.innerWidth + 1 &&
        !el.closest(".table-scroll, .sr-only"),
    ).length,
  }));
  expect(overflow).toEqual({ page: 0, content: 0 });
});

for (const theme of ["light", "dark"])
  test(`the page passes an accessibility audit in ${theme} mode`, async ({
    page,
  }) => {
    const { AxeBuilder } = await import("@axe-core/playwright");
    if (theme === "dark")
      await page.addInitScript(() =>
        localStorage.setItem("financial-research-theme", "dark"),
      );
    await mock(page);
    await page.goto("/admin/health");
    await expect(
      page.getByRole("heading", { name: "All systems working" }),
    ).toBeVisible();
    await page.waitForTimeout(900);
    const result = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();
    expect(
      result.violations.map((v) => `${v.id} ${v.nodes[0]?.target}`),
    ).toEqual([]);
  });
