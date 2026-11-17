import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "*.spec.js",
  workers: 1,
  // On GitHub, failures also appear as annotations on the run.
  reporter: process.env.CI ? [["list"], ["github"]] : "list",
  use: {
    baseURL: "http://127.0.0.1:5175",
    // BROWSER=webkit or BROWSER=firefox runs the same tests in Safari's or Firefox's engine.
    browserName: process.env.BROWSER || "chromium",
    channel: process.env.BROWSER
      ? undefined
      : process.env.PLAYWRIGHT_CHANNEL || "chrome",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "npm run dev -- --port 5175 --strictPort",
    url: "http://127.0.0.1:5175",
    reuseExistingServer: false,
  },
});
