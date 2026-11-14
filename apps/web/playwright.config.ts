import { defineConfig, devices } from "@playwright/test";

// The end-to-end tests (e2e/): a real browser against the whole stack in containers,
// with a fake model in Ollama's place. `make e2e` starts that stack, runs these, and
// takes it down. See docs/adr/0017-end-to-end-tests-on-the-real-stack.md.
export default defineConfig({
  testDir: "./e2e",
  // Every test is a new visitor with a session of its own, so they run side by side.
  fullyParallel: true,
  // A test that fails now and then is a mistake to find, not to run again.
  retries: 0,
  // On CI, a test marked to run alone is a leftover from debugging.
  forbidOnly: process.env.CI !== undefined,
  reporter:
    process.env.CI !== undefined
      ? [["list"], ["html", { open: "never" }]]
      : "list",
  use: {
    baseURL: process.env.ACTIO_E2E_URL ?? "http://localhost:8090",
    // For a failed test: every step, with the page as it looked, to open afterwards
    // with `playwright show-trace`.
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
