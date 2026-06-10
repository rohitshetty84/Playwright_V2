/**
 * scripts/auth.ts — run ONCE to create an authenticated session.
 *
 * Usage (local):
 *   npx ts-node scripts/auth.ts
 *
 * Logs into each configured application and saves browser storage state
 * (cookies + localStorage) to studio/.auth/<appName>.json.
 * Test scripts reference that file via `use.storageState` — credentials
 * never appear in test code or golden JSON.
 *
 * Add one entry to AUTH_TARGETS for every app that needs login.
 */

import { chromium, type Page } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import * as dotenv from 'dotenv';

dotenv.config();                                                        // loads root .env
dotenv.config({ path: path.join(__dirname, '../studio/.env'), override: false });

const AUTH_DIR = path.join(__dirname, '../studio/.auth');

interface AuthTarget {
  name: string;      // filename stem: studio/.auth/<name>.json
  url: string;
  user: string;
  pass: string;
  loginFn: (page: Page, user: string, pass: string) => Promise<void>;
}

const AUTH_TARGETS: AuthTarget[] = [

  // ── SAP SuccessFactors via BHP IAS SSO ──────────────────────────────────
  // Credentials come from .env (SF_BASE_URL / SF_USERNAME / SF_PASSWORD).
  // The IAS login page uses a two-step flow: username → Continue → password → Log On.
  // After SAML redirect the session lands on the SuccessFactors home page.
  {
    name: 'successfactors',
    url:  process.env.SF_BASE_URL  || '',
    user: process.env.SF_USERNAME  || '',
    pass: process.env.SF_PASSWORD  || '',
    loginFn: async (page, user, pass) => {

      // ── Step 1: username field ───────────────────────────────────────────
      // SAP IAS renders an input with name="j_username" or type="email"
      await page.waitForSelector(
        'input[name="j_username"], input[type="email"], input[id*="username"], input[placeholder*="User"]',
        { timeout: 60_000 }
      );

      const usernameInput = page
        .locator('input[name="j_username"]')
        .or(page.locator('input[type="email"]'))
        .or(page.locator('input[id*="username"]'))
        .first();

      await usernameInput.fill(user);

      // SAP IAS two-step: click Continue/Next to reveal the password screen
      const continueBtn = page.getByRole('button', { name: /continue|next/i });
      const continueBtnVisible = await continueBtn.isVisible({ timeout: 3_000 }).catch(() => false);
      if (continueBtnVisible) {
        await continueBtn.click();
        // Wait for the password field to appear after the transition
        await page.waitForSelector('input[type="password"]', { timeout: 60_000 });
      }

      // ── Step 2: password field ───────────────────────────────────────────
      await page.locator('input[type="password"]').fill(pass);

      // ── Step 3: submit ───────────────────────────────────────────────────
      // IAS uses "Log On", some themes use "Log In" or "Sign In"
      await page
        .getByRole('button', { name: /log\s*on|log\s*in|sign\s*in/i })
        .first()
        .click();

      // ── Step 4: wait for SAML redirect to land on SuccessFactors ────────
      // The URL changes from iasauthentication-mdev.bhp.com → successfactors.com
      await page.waitForFunction(
        () => window.location.hostname.includes('successfactors.com'),
        { timeout: 120_000 }
      );
      console.log('[auth] Landed on SuccessFactors:', page.url());
    },
  },

  // ── Generic username + password form (template) ──────────────────────────
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
];

// ── Runner ────────────────────────────────────────────────────────────────────
(async () => {
  fs.mkdirSync(AUTH_DIR, { recursive: true });

  const skipped = AUTH_TARGETS.filter(t => !t.url || !t.user || !t.pass);
  if (skipped.length) {
    console.warn(`[auth] Skipping ${skipped.map(t => t.name).join(', ')} — missing env vars`);
  }

  const targets = AUTH_TARGETS.filter(t => t.url && t.user && t.pass);
  if (!targets.length) {
    console.log('[auth] No targets configured — nothing to do.');
    console.log('[auth] Set SF_BASE_URL, SF_USERNAME, SF_PASSWORD in .env');
    process.exit(0);
  }

  // CI=true (GitHub Actions) → run headless since there's no display and no MFA
  const headless = process.env.CI === 'true';
  const browser = await chromium.launch({ headless });

  for (const target of targets) {
    const outFile = path.join(AUTH_DIR, `${target.name}.json`);
    console.log(`[auth] Logging in to ${target.name} …`);
    console.log(`[auth]   URL : ${target.url}`);
    console.log(`[auth]   User: ${target.user}`);
    try {
      const ctx  = await browser.newContext();
      const page = await ctx.newPage();
      await page.goto(target.url, { waitUntil: 'domcontentloaded', timeout: 120_000 });
      await target.loginFn(page, target.user, target.pass);
      await ctx.storageState({ path: outFile });
      await ctx.close();
      console.log(`[auth] ✅  Session saved → ${outFile}`);
    } catch (err) {
      console.error(`[auth] ❌  ${target.name}: ${err}`);
      console.error('[auth]    If the browser showed a CAPTCHA or MFA prompt, complete it manually');
      console.error('[auth]    and re-run this script with headless: false.');
    }
  }

  await browser.close();
})();
