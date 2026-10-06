import { existsSync } from "node:fs";
import path from "node:path";

import { defineConfig } from "@playwright/test";

/* Browser E2E for the critical scenarios (brief §200–203), fully offline: `pnpm test:e2e`.
 * `e2e/stack.mjs` (webServer) resets a dedicated database and starts the offline world, the API (+ workers) and a
 * second `next dev` on its own ports — next to a running `pnpm dev` / API, which are left alone. */

const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 3020);
const OUTPUT = path.resolve(process.env.E2E_OUTPUT_DIR ?? "e2e/.output");

/** A pinned Chromium when the installed Playwright browsers don't match this Playwright version (CI images). */
function chromiumPath(): string | undefined {
  if (process.env.E2E_CHROMIUM) return process.env.E2E_CHROMIUM;
  const bundled = process.env.PLAYWRIGHT_BROWSERS_PATH && path.join(process.env.PLAYWRIGHT_BROWSERS_PATH, "chromium");
  return bundled && existsSync(bundled) ? bundled : undefined;
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts$/,
  outputDir: path.join(OUTPUT, "test-results"),
  // One shared API + worker pool: scenarios run one after the other (each in its own fresh account).
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 5 * 60_000,
  expect: { timeout: 20_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: path.join(OUTPUT, "report") }]],
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    viewport: { width: 1440, height: 900 },
    locale: "en-US",
    timezoneId: "Europe/Paris",
    reducedMotion: "reduce",
    acceptDownloads: true,
    actionTimeout: 20_000,
    navigationTimeout: 90_000, // first visit of a route compiles it (next dev)
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: { executablePath: chromiumPath() },
  },
  webServer: {
    command: "node e2e/stack.mjs",
    url: `http://localhost:${WEB_PORT}/sign-in`,
    reuseExistingServer: !process.env.CI,
    timeout: 300_000,
    stdout: "pipe",
    stderr: "pipe",
    gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
  },
});
