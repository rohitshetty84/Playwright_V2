import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright config tuned for GitHub Actions.
 *
 * Defaults assume:
 *   • specs are materialized from golden/*.json into tests/ by
 *     ci/export_goldens.py during the `prepare:goldens` job
 *   • CI environment is detected via the standard CI=true env var
 *   • Edge is the primary browser (matches the FastAPI service default)
 */
export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 2 : undefined,
  timeout: 60_000,
  expect: { timeout: 15_000 },

  // Multiple reporters: JUnit for the Checks tab, HTML for browsing, JSON for POST.
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
    ['json', { outputFile: 'results.json' }],
    ['junit', { outputFile: 'junit.xml' }],
  ],

  use: {
    headless: true,
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },

  projects: [
    {
      name: 'msedge',
      use: { ...devices['Desktop Edge'], channel: 'msedge' },
    },
    // Uncomment to also run against Chromium in CI:
    // { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
