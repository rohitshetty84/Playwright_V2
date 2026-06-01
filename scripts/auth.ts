/**
 * scripts/auth.ts — run ONCE to create an authenticated session.
 *
 * Usage (local):
 *   npx ts-node scripts/auth.ts
 *   # or
 *   npx playwright test --config=scripts/auth.config.ts
 *
 * This script logs into each configured application and saves the browser
 * storage state (cookies + localStorage) to studio/.auth/<appName>.json.
 * Test scripts reference that file via `use.storageState` — they never
 * touch credentials themselves.
 *
 * Add one entry to AUTH_TARGETS for every app that needs login.
 */

import { chromium, type Page } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import * as dotenv from 'dotenv';

dotenv.config();               // loads root .env
dotenv.config({ path: path.join(__dirname, '../studio/.env'), override: false });

const AUTH_DIR = path.join(__dirname, '../studio/.auth');

// ── Auth target definitions ─────────────────────────────────────────────────
// Each entry describes how to log in to one application.
// Add / remove entries to match your test targets.

interface AuthTarget {
  name: string;      // used as the filename stem: studio/.auth/<name>.json
  url: string;
  user: string;
  pass: string;
  loginFn: (page: Page, user: string, pass: string) => Promise<void>;
}

const AUTH_TARGETS: AuthTarget[] = [
  // ── Example: generic username + password form ────────────────────────────
  // {
  //   name: 'myapp',
  //   url:  process.env.APP_MYAPP_URL  || '',
  //   user: process.env.APP_MYAPP_USER || '',
  //   pass: process.env.APP_MYAPP_PASS || '',
  //   loginFn: async (page, user, pass) => {
  //     await page.getByLabel('Username').fill(user);
  //     await page.getByLabel('Password').fill(pass);
  //     await page.getByRole('button', { name: /sign in/i }).click();
  //     await page.waitForURL('**/dashboard**');
  //   },
  // },

  // ── SAP SuccessFactors (SSO redirect pattern) ────────────────────────────
  // {
  //   name: 'successfactors',
  //   url:  process.env.APP_SF_URL  || '',
  //   user: process.env.APP_SF_USER || '',
  //   pass: process.env.APP_SF_PASS || '',
  //   loginFn: async (page, user, pass) => {
  //     await page.getByPlaceholder('User Name').fill(user);
  //     await page.getByPlaceholder('Password').fill(pass);
  //     await page.getByRole('button', { name: 'Log In' }).click();
  //     await page.waitForURL('**/home**', { timeout: 30_000 });
  //   },
  // },
];

// ── Runner ───────────────────────────────────────────────────────────────────
(async () => {
  fs.mkdirSync(AUTH_DIR, { recursive: true });

  const skipped = AUTH_TARGETS.filter(t => !t.url || !t.user || !t.pass);
  if (skipped.length) {
    console.warn(`[auth] Skipping ${skipped.map(t => t.name).join(', ')} — missing env vars`);
  }

  const targets = AUTH_TARGETS.filter(t => t.url && t.user && t.pass);
  if (!targets.length) {
    console.log('[auth] No targets configured — nothing to do.');
    console.log('[auth] Uncomment an entry in AUTH_TARGETS and set the env vars.');
    process.exit(0);
  }

  const browser = await chromium.launch();

  for (const target of targets) {
    const outFile = path.join(AUTH_DIR, `${target.name}.json`);
    console.log(`[auth] Logging in to ${target.name} (${target.url}) …`);
    try {
      const ctx  = await browser.newContext();
      const page = await ctx.newPage();
      await page.goto(target.url, { waitUntil: 'domcontentloaded' });
      await target.loginFn(page, target.user, target.pass);
      await ctx.storageState({ path: outFile });
      await ctx.close();
      console.log(`[auth] ✅  Saved session → ${outFile}`);
    } catch (err) {
      console.error(`[auth] ❌  ${target.name}: ${err}`);
    }
  }

  await browser.close();
})();
