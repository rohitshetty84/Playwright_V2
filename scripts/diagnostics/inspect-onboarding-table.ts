/**
 * Diagnostic script — inspects the real DOM structure of the SF Onboarding
 * dashboard table after searching for Matthew Moraga.
 *
 * Run: npx ts-node scripts/diagnostics/inspect-onboarding-table.ts
 *
 * Outputs:
 *   scripts/diagnostics/out/screenshot.png   — page at point of failure
 *   scripts/diagnostics/out/a11y.json        — full accessibility tree
 *   scripts/diagnostics/out/dom-structure.json — element counts + row HTML
 *   scripts/diagnostics/out/row-details.json   — every row's text + child tags
 */

import { chromium } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

const OUT_DIR = path.join(__dirname, 'out');
const AUTH    = path.join(__dirname, '../../studio/.auth/successfactors.json');
const SF_URL  = 'https://performancemanager8.successfactors.com/sf/start';
const CANDIDATE = 'Matthew Moraga';

(async () => {
  fs.mkdirSync(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: false });   // headed so you can watch
  const ctx  = await browser.newContext({ storageState: AUTH });
  const page = await ctx.newPage();

  console.log('[1] Navigating to SF start…');
  await page.goto(SF_URL, { waitUntil: 'domcontentloaded', timeout: 60_000 });
  await page.waitForTimeout(2000);

  console.log('[2] Opening Home menu…');
  await page.getByRole('button', { name: 'Home' }).click();
  await page.waitForTimeout(1000);

  console.log('[3] Clicking Onboarding…');
  await page.getByRole('menuitem', { name: 'Onboarding' }).click();
  await page.waitForTimeout(4000);   // dashboard renders async

  console.log('[4] Waiting for search field…');
  await page.locator('input[placeholder="Search for new recruit"]')
    .waitFor({ state: 'visible', timeout: 30_000 });

  console.log('[5] Filling candidate name…');
  await page.locator('input[placeholder="Search for new recruit"]').fill(CANDIDATE);
  await page.waitForTimeout(2000);

  console.log('[6] Clicking suggestion…');
  await page.getByRole('option', { name: new RegExp('^' + CANDIDATE) }).click();
  await page.waitForTimeout(4000);   // table loads after selection

  // ── Screenshot ──────────────────────────────────────────────────────────────
  const shot = await page.screenshot({ fullPage: false });
  fs.writeFileSync(path.join(OUT_DIR, 'screenshot.png'), shot);
  console.log('[✓] Screenshot saved');

  // ── Accessibility tree ───────────────────────────────────────────────────────
  const a11y = await page.accessibility.snapshot();
  fs.writeFileSync(path.join(OUT_DIR, 'a11y.json'), JSON.stringify(a11y, null, 2));
  console.log('[✓] Accessibility tree saved');

  // ── DOM structure — element type counts ─────────────────────────────────────
  const domStructure = await page.evaluate(() => {
    const candidates = [
      'ui5-table', 'ui5-table-row', 'ui5-table-cell', 'ui5-table-header-row',
      'ui5-list', 'ui5-li', 'ui5-li-custom',
      '[role="row"]', '[role="cell"]', '[role="gridcell"]', '[role="grid"]',
      '[role="list"]', '[role="listitem"]',
      'tr', 'td', 'table',
    ];
    const counts: Record<string, number> = {};
    for (const sel of candidates) {
      try { counts[sel] = document.querySelectorAll(sel).length; } catch { counts[sel] = -1; }
    }
    return counts;
  });
  fs.writeFileSync(path.join(OUT_DIR, 'dom-structure.json'), JSON.stringify(domStructure, null, 2));
  console.log('[✓] DOM structure saved');
  console.log('Element counts:', domStructure);

  // ── Row details — text + direct child tags for every candidate row ──────────
  const rowDetails = await page.evaluate((candidate: string) => {
    const results: object[] = [];

    // Try every plausible row selector
    const rowSelectors = [
      'ui5-table-row', '[role="row"]', 'tr',
      'ui5-li', 'ui5-li-custom', '[role="listitem"]',
    ];

    for (const rowSel of rowSelectors) {
      const rows = Array.from(document.querySelectorAll(rowSel));
      const matching = rows.filter(r => r.textContent?.includes(candidate));

      if (matching.length > 0) {
        for (const row of matching) {
          // Get all direct children and their tag names + text
          const children = Array.from(row.children).map((child, i) => ({
            index:   i + 1,
            tag:     child.tagName.toLowerCase(),
            role:    child.getAttribute('role') || '',
            text:    child.textContent?.trim().substring(0, 100) || '',
            classes: child.className || '',
          }));

          results.push({
            rowSelector:  rowSel,
            rowTag:       row.tagName.toLowerCase(),
            rowText:      row.textContent?.trim().substring(0, 300) || '',
            childCount:   row.children.length,
            children,
          });
        }
      }
    }

    // Also dump the full HTML of the first matching row (truncated)
    const anyRow = document.querySelector(`ui5-table-row, [role="row"], tr`);
    return {
      matchingRows: results,
      sampleRowHTML: anyRow ? anyRow.outerHTML.substring(0, 2000) : 'none found',
    };
  }, CANDIDATE);

  fs.writeFileSync(path.join(OUT_DIR, 'row-details.json'), JSON.stringify(rowDetails, null, 2));
  console.log('[✓] Row details saved');

  if ((rowDetails.matchingRows as object[]).length === 0) {
    console.warn(`\n⚠️  No row containing "${CANDIDATE}" found with any row selector.`);
    console.warn('   The table may use a non-standard structure. Check dom-structure.json.');
  } else {
    console.log('\n✅ Found matching row(s). Check row-details.json for selector + column structure.');
  }

  await browser.close();
  console.log(`\nAll output in: ${OUT_DIR}`);
})();
