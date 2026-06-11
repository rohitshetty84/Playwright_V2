#!/usr/bin/env python3
"""
Generate a self-contained HTML test execution report from Playwright JSON results
and POST it to the Studio server.

Usage (called by playwright.yml after npx playwright test):
  python scripts/generate_report.py \
    --results results.json \
    --test-results-dir test-results \
    --golden-id <id> \
    --golden-name "My Test" \
    --github-run-id <id> \
    --github-run-url <url> \
    --github-run-num <num> \
    --studio-url https://... \        # optional — skip POST if absent
    --output report.html              # optional — default: report.html
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


# ── Helpers ───────────────────────────────────────────────────────────────────

def _b64(path: str) -> str | None:
    try:
        return base64.b64encode(Path(path).read_bytes()).decode()
    except Exception:
        return None

def _strip_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text or '')

def _ms(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms/1000:.1f}s"


# ── Parse Playwright JSON results ─────────────────────────────────────────────

def extract_tests(data: dict) -> list[dict]:
    tests = []

    def walk(suites):
        for suite in suites:
            for spec in suite.get('specs', []):
                results = []
                for t in spec.get('tests', []):
                    results.extend(t.get('results', []))
                if not results:
                    continue
                result = results[-1]   # most recent attempt

                shots = [
                    a['path'] for a in result.get('attachments', [])
                    if a.get('contentType', '').startswith('image/') and a.get('path')
                    and Path(a['path']).exists()
                ]
                errors = result.get('errors', [])
                error_text = ''
                if errors:
                    raw = errors[0].get('message', '') or errors[0].get('value', '')
                    error_text = _strip_ansi(str(raw))[:600]

                tests.append({
                    'name':        spec.get('title', 'Unknown test'),
                    'file':        suite.get('file', ''),
                    'status':      result.get('status', 'unknown'),
                    'duration':    result.get('duration', 0),
                    'error':       error_text,
                    'screenshots': shots,
                    'retry':       result.get('retry', 0),
                })

            if 'suites' in suite:
                walk(suite['suites'])

    walk(data.get('suites', []))
    return tests


# ── HTML generation ───────────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #f1f5f9; color: #0f172a; }
header { background: #0f172a; color: #f1f5f9; padding: 28px 32px 24px; }
header h1 { font-size: 20px; font-weight: 700; margin-bottom: 6px; }
.meta { font-size: 12px; color: #94a3b8; margin-bottom: 16px; line-height: 1.8; }
.meta b { color: #cbd5e1; }
.bar-wrap { background: #1e293b; border-radius: 6px; height: 10px; overflow: hidden; }
.bar-pass { height: 100%; background: #22c55e; border-radius: 6px; transition: width .4s; }
.bar-all-fail .bar-pass { background: #ef4444; }
.summary-row { display: flex; gap: 20px; margin-top: 10px; font-size: 13px; }
.s-pass { color: #4ade80; font-weight: 700; }
.s-fail { color: #f87171; font-weight: 700; }
.s-total { color: #94a3b8; }
main { max-width: 1200px; margin: 28px auto; padding: 0 20px 40px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
.card { background: #fff; border-radius: 10px; overflow: hidden;
        box-shadow: 0 1px 4px rgba(0,0,0,.08); border: 1px solid #e2e8f0; }
.card.fail { border-left: 4px solid #ef4444; }
.card.pass { border-left: 4px solid #22c55e; }
.card.skipped { border-left: 4px solid #94a3b8; }
.card-head { padding: 14px 16px 10px; display: flex; align-items: flex-start; gap: 10px; }
.badge { font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 4px;
         letter-spacing: .05em; white-space: nowrap; flex-shrink: 0; margin-top: 2px; }
.badge.pass   { background: #dcfce7; color: #15803d; }
.badge.fail   { background: #fee2e2; color: #b91c1c; }
.badge.skipped { background: #f1f5f9; color: #64748b; }
.test-name { font-size: 13px; font-weight: 600; color: #0f172a; line-height: 1.4; flex: 1; }
.duration { font-size: 11px; color: #94a3b8; white-space: nowrap; margin-top: 3px; }
.shot-wrap { position: relative; overflow: hidden; background: #f8fafc;
             border-top: 1px solid #f1f5f9; cursor: zoom-in; }
.shot-wrap img { width: 100%; display: block; transition: transform .2s; }
.shot-wrap:hover img { transform: scale(1.015); }
.no-shot { padding: 18px 16px; font-size: 11px; color: #94a3b8; text-align: center; }
.error-box { margin: 10px 14px 14px; background: #fef2f2; border: 1px solid #fecaca;
             border-radius: 6px; padding: 10px 12px; }
.error-box pre { font-size: 11px; color: #991b1b; white-space: pre-wrap;
                 word-break: break-all; font-family: 'JetBrains Mono', monospace;
                 line-height: 1.5; max-height: 180px; overflow-y: auto; }
.retry-note { font-size: 10px; color: #f59e0b; padding: 0 16px 8px; }
/* Lightbox */
#lb { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.85);
      z-index: 9999; align-items: center; justify-content: center; cursor: zoom-out; }
#lb.open { display: flex; }
#lb img { max-width: 90vw; max-height: 90vh; border-radius: 6px;
          box-shadow: 0 8px 40px rgba(0,0,0,.6); }
footer { text-align: center; padding: 20px; font-size: 11px; color: #94a3b8; }
"""

_JS = """
document.querySelectorAll('.shot-wrap').forEach(w => {
  w.addEventListener('click', () => {
    const lb = document.getElementById('lb');
    document.getElementById('lb-img').src = w.querySelector('img').src;
    lb.classList.add('open');
  });
});
document.getElementById('lb').addEventListener('click', () =>
  document.getElementById('lb').classList.remove('open'));
"""


def _card(t: dict) -> str:
    status = t['status']   # passed / failed / skipped / timedOut
    css_status = 'pass' if status == 'passed' else ('skipped' if status == 'skipped' else 'fail')
    label = 'PASS' if css_status == 'pass' else ('SKIP' if css_status == 'skipped' else 'FAIL')

    retry_html = (f'<div class="retry-note">⚠ Passed on retry {t["retry"]}</div>'
                  if t['retry'] > 0 and css_status == 'pass' else '')

    if t['screenshots']:
        b64 = _b64(t['screenshots'][-1])
        shot_html = (f'<div class="shot-wrap"><img src="data:image/png;base64,{b64}" '
                     f'alt="screenshot" loading="lazy"></div>'
                     if b64 else '<div class="no-shot">screenshot file unreadable</div>')
    else:
        shot_html = '<div class="no-shot">no screenshot captured</div>'

    error_html = ''
    if t['error']:
        error_html = f'<div class="error-box"><pre>{t["error"]}</pre></div>'

    return f"""
<div class="card {css_status}">
  <div class="card-head">
    <span class="badge {css_status}">{label}</span>
    <div style="flex:1;min-width:0">
      <div class="test-name">{_esc(t["name"])}</div>
      <div class="duration">⏱ {_ms(t["duration"])}</div>
    </div>
  </div>
  {retry_html}
  {shot_html}
  {error_html}
</div>"""


def _esc(s: str) -> str:
    return (s.replace('&', '&amp;').replace('<', '&lt;')
             .replace('>', '&gt;').replace('"', '&quot;'))


def generate_html(tests: list[dict], meta: dict) -> str:
    total   = len(tests)
    passed  = sum(1 for t in tests if t['status'] == 'passed')
    failed  = sum(1 for t in tests if t['status'] not in ('passed', 'skipped'))
    skipped = sum(1 for t in tests if t['status'] == 'skipped')
    pct     = round(passed / total * 100) if total else 0
    bar_cls = 'bar-all-fail' if passed == 0 and failed > 0 else ''

    cards = '\n'.join(_card(t) for t in tests)

    ts   = meta.get('timestamp', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    name = _esc(meta.get('golden_name', 'Unknown'))
    gh_link = ''
    if meta.get('github_run_url'):
        gh_link = (f' &nbsp;·&nbsp; <a href="{meta["github_run_url"]}" '
                   f'target="_blank" style="color:#7c3aed">GitHub Run #{meta.get("github_run_num","")}</a>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test Report — {name}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>📋 Test Execution Report</h1>
  <div class="meta">
    <b>Golden:</b> {name}{gh_link}<br>
    <b>Run at:</b> {_esc(ts)} &nbsp;·&nbsp;
    <b>Browser:</b> {_esc(meta.get('browser','chromium'))} &nbsp;·&nbsp;
    <b>Runner:</b> GitHub Actions
  </div>
  <div class="bar-wrap {bar_cls}">
    <div class="bar-pass" style="width:{pct}%"></div>
  </div>
  <div class="summary-row">
    <span class="s-pass">✓ {passed} passed</span>
    {'<span class="s-fail">✗ ' + str(failed) + ' failed</span>' if failed else ''}
    {'<span style="color:#94a3b8">⊘ ' + str(skipped) + ' skipped</span>' if skipped else ''}
    <span class="s-total">{total} total &nbsp;·&nbsp; {pct}% pass rate</span>
  </div>
</header>

<main>
  <div class="grid">
    {cards}
  </div>
</main>

<div id="lb"><img id="lb-img" src="" alt="screenshot fullscreen"></div>

<footer>Generated by Playwright AI Studio · {_esc(ts)}</footer>

<script>{_JS}</script>
</body>
</html>"""


# ── POST to Studio ────────────────────────────────────────────────────────────

def post_to_studio(studio_url: str, payload: dict) -> bool:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{studio_url.rstrip('/')}/api/runs/report",
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            print(f"[report] Posted to Studio: {body}")
            return True
    except urllib.error.URLError as e:
        print(f"[report] Warning: could not POST to Studio ({e}) — report saved locally only",
              file=sys.stderr)
        return False


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--results',          default='results.json')
    ap.add_argument('--test-results-dir', default='test-results')
    ap.add_argument('--golden-id',        required=True)
    ap.add_argument('--golden-name',      default='')
    ap.add_argument('--github-run-id',    default='')
    ap.add_argument('--github-run-url',   default='')
    ap.add_argument('--github-run-num',   default='')
    ap.add_argument('--browser',          default='chromium')
    ap.add_argument('--studio-url',       default=os.environ.get('STUDIO_PUBLIC_URL', ''))
    ap.add_argument('--output',           default='report.html')
    args = ap.parse_args()

    # Load results
    if not Path(args.results).exists():
        print(f"[report] {args.results} not found — skipping report generation", file=sys.stderr)
        sys.exit(0)

    data = json.loads(Path(args.results).read_text())
    tests = extract_tests(data)
    stats = data.get('stats', {})

    if not tests:
        print("[report] No test results found in JSON — skipping", file=sys.stderr)
        sys.exit(0)

    print(f"[report] {len(tests)} tests found — generating report…")

    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    meta = {
        'golden_id':      args.golden_id,
        'golden_name':    args.golden_name or args.golden_id,
        'github_run_id':  args.github_run_id,
        'github_run_url': args.github_run_url,
        'github_run_num': args.github_run_num,
        'browser':        args.browser,
        'timestamp':      ts,
    }

    html = generate_html(tests, meta)
    Path(args.output).write_text(html, encoding='utf-8')
    print(f"[report] Saved → {args.output} ({len(html)//1024}KB)")

    # Build candidates list for Studio run record
    candidates = [{
        'name':     t['name'],
        'path':     args.golden_id,
        'status':   'pass' if t['status'] == 'passed' else 'fail',
        'duration': round(t['duration'] / 1000, 1),
        'error':    t['error'],
    } for t in tests]

    if args.studio_url:
        post_to_studio(args.studio_url, {
            'golden_id':      args.golden_id,
            'golden_name':    meta['golden_name'],
            'github_run_id':  args.github_run_id,
            'github_run_url': args.github_run_url,
            'github_run_num': args.github_run_num,
            'browser':        args.browser,
            'candidates':     candidates,
            'report_html':    html,
        })
    else:
        print("[report] No STUDIO_PUBLIC_URL — skipping Studio POST (report saved as artifact only)")


if __name__ == '__main__':
    main()
