import { defineConfig, devices } from '@playwright/test';
import * as path from 'path';

/**
 * Playwright config tuned for GitHub Actions.
 *
 * Defaults assume:
 *   • specs are materialized from golden/*.json into tests/ by
 *     ci/export_goldens.py during the `prepare:goldens` job
 *   • CI environment is detected via the standard CI=true env var
 *   • Edge is the primary browser (matches the FastAPI service default)
 *   • LOCAL_VALIDATION=true shows the browser during local validation runs
 *
 * Authentication:
 *   Run `npx ts-node scripts/auth.ts` once to create studio/.auth/<app>.json.
 *   Tests that need a logged-in session set `use.storageState` to one of those
 *   files — credentials never appear in test code or golden JSON.
 */

// Detect if running local validation (from auto-heal system)
const isLocalValidation = process.env.LOCAL_VALIDATION === 'true';
const isCI = !!process.env.CI;

// Convenience helper — resolves a storageState path relative to studio/.auth/
// Usage in a test: test.use({ storageState: authState('myapp') });
export const authState = (appName: string) =>
  path.join(__dirname, 'studio/.auth', `${appName}.json`);

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
    // Show browser during LOCAL validation, hide in CI/headless mode
    // Set LOCAL_VALIDATION=true when running validation from auto-heal
    headless: isCI ? true : !isLocalValidation,

    // Increase timeouts for better element visibility
    actionTimeout: 20_000,
    navigationTimeout: 45_000,

    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    // Uncomment to also run against msedge:
    // { name: 'msedge', use: { ...devices['Desktop Edge'], channel: 'msedge' } },
  ],
});
