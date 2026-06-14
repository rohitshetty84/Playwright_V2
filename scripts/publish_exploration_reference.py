#!/usr/bin/env python3
"""
Generate a self-contained HTML reference file for a completed exploration and
commit it to the repository so it persists in GitHub permanently.

Called by explore.yml after the exploration runner finishes.
Reads:
  studio/explorations/{EXPLORATION_ID}/steps.json
  studio/explorations/{EXPLORATION_ID}/screenshots/*.png
Writes:
  explorations/{EXPLORATION_ID}/reference.html  (in repo root, not studio/)

Required env vars:
  EXPLORATION_ID   — the exploration being published
"""

import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPLORATION_ID = os.environ.get("EXPLORATION_ID", "").strip()

if not EXPLORATION_ID:
    print("[reference] EXPLORATION_ID not set — skipping", file=sys.stderr)
    sys.exit(0)

EXPL_DIR  = REPO_ROOT / "studio" / "explorations" / EXPLORATION_ID
SHOTS_DIR = EXPL_DIR / "screenshots"
STEPS_FILE = EXPL_DIR / "steps.json"
OUT_DIR  = REPO_ROOT / "explorations" / EXPLORATION_ID
OUT_FILE = OUT_DIR / "reference.html"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _b64_img(path: Path) -> str | None:
    try:
        return base64.b64encode(path.read_bytes()).decode()
    except Exception:
        return None


def _esc(s) -> str:
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ── Styles & scripts ──────────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #f1f5f9; color: #0f172a; }
header { background: #0f172a; color: #f1f5f9; padding: 28px 32px 24px; }
header h1 { font-size: 20px; font-weight: 700; margin-bottom: 4px; }
.meta { font-size: 12px; color: #94a3b8; margin-top: 8px; line-height: 2; }
.meta b { color: #cbd5e1; }
.bar-wrap { background: #1e293b; border-radius: 6px; height: 8px; overflow: hidden; margin: 12px 0 8px; }
.bar-fill { height: 100%; background: #22c55e; border-radius: 6px; transition: width .4s; }
.summary { display: flex; gap: 20px; font-size: 13px; margin-top: 4px; }
.s-pass { color: #4ade80; font-weight: 700; }
.s-fail { color: #f87171; font-weight: 700; }
.test-case { font-size: 13px; color: #e2e8f0; margin-top: 14px; padding: 12px 16px;
             background: #1e293b; border-radius: 6px; border-left: 3px solid #7c3aed;
             white-space: pre-wrap; word-break: break-word; line-height: 1.6; }
main { max-width: 900px; margin: 28px auto; padding: 0 20px 48px; }
h2 { font-size: 15px; font-weight: 700; color: #1e293b; margin: 28px 0 12px;
     padding-bottom: 8px; border-bottom: 2px solid #e2e8f0; }
.step { background: #fff; border-radius: 10px; overflow: hidden;
        box-shadow: 0 1px 4px rgba(0,0,0,.07); border: 1px solid #e2e8f0;
        margin-bottom: 14px; }
.step.pass { border-left: 4px solid #22c55e; }
.step.fail { border-left: 4px solid #ef4444; }
.step-head { padding: 14px 16px 10px; display: flex; align-items: flex-start; gap: 10px; }
.num { font-size: 11px; font-weight: 700; background: #1e293b; color: #f8fafc;
       padding: 4px 9px; border-radius: 4px; white-space: nowrap; flex-shrink: 0; }
.step-desc { font-size: 13px; font-weight: 600; color: #0f172a; flex: 1; line-height: 1.45; }
.badge { font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 4px;
         letter-spacing: .05em; white-space: nowrap; flex-shrink: 0; margin-top: 1px; }
.badge.pass { background: #dcfce7; color: #15803d; }
.badge.fail { background: #fee2e2; color: #b91c1c; }
.tags { padding: 2px 16px 10px; display: flex; flex-wrap: wrap; gap: 6px; }
.tag { font-size: 11px; background: #f1f5f9; border: 1px solid #e2e8f0;
       border-radius: 4px; padding: 3px 8px; color: #475569; font-family: 'JetBrains Mono', monospace; }
.tag-label { color: #94a3b8; margin-right: 3px; }
.obs { font-size: 12px; color: #475569; padding: 0 16px 10px; font-style: italic; line-height: 1.5; }
.err { margin: 0 14px 12px; background: #fef2f2; border: 1px solid #fecaca;
       border-radius: 6px; padding: 8px 12px; font-size: 11px; color: #991b1b;
       font-family: monospace; white-space: pre-wrap; word-break: break-all; }
.shot-wrap { background: #f8fafc; border-top: 1px solid #f1f5f9; cursor: zoom-in; overflow: hidden; }
.shot-wrap img { width: 100%; display: block; max-height: 500px; object-fit: contain;
                 transition: transform .2s; }
.shot-wrap:hover img { transform: scale(1.01); }
.no-shot { padding: 16px; font-size: 11px; color: #94a3b8; text-align: center; }
/* Selector table */
.sel-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 4px; }
.sel-table th { background: #0f172a; color: #f8fafc; padding: 8px 12px; text-align: left;
                font-weight: 600; font-size: 11px; letter-spacing: .04em; }
.sel-table td { padding: 8px 12px; border-bottom: 1px solid #e2e8f0; font-family: 'JetBrains Mono', monospace; }
.sel-table tr:nth-child(even) td { background: #f8fafc; }
/* Lightbox */
#lb { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.88);
      z-index: 9999; align-items: center; justify-content: center; cursor: zoom-out; }
#lb.open { display: flex; }
#lb img { max-width: 94vw; max-height: 94vh; border-radius: 6px;
          box-shadow: 0 8px 40px rgba(0,0,0,.6); }
footer { text-align: center; padding: 24px; font-size: 11px; color: #94a3b8; }
"""

_JS = """
document.querySelectorAll('.shot-wrap').forEach(w => {
  w.addEventListener('click', () => {
    document.getElementById('lb-img').src = w.querySelector('img').src;
    document.getElementById('lb').classList.add('open');
  });
});
document.getElementById('lb').addEventListener('click', () =>
  document.getElementById('lb').classList.remove('open'));
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.getElementById('lb').classList.remove('open');
});
"""


# ── Step card renderer ────────────────────────────────────────────────────────

def _step_card(s: dict) -> str:
    num    = s.get("step_num", "?")
    desc   = _esc(s.get("description", ""))
    ok     = s.get("success", False)
    css    = "pass" if ok else "fail"
    badge  = "PASS" if ok else "FAIL"
    action = s.get("action", "")
    sel    = s.get("selector", "")
    val    = s.get("value", "")
    obs    = _esc(s.get("observation", "") or "")
    err    = _esc(s.get("error", "") or "")
    readv  = _esc(s.get("read_value", "") or "")
    path   = s.get("path", "")
    shot   = s.get("screenshot_file", "")

    tags = []
    if action:
        tags.append(f'<span class="tag"><span class="tag-label">action</span>{_esc(action)}</span>')
    if sel:
        tags.append(f'<span class="tag"><span class="tag-label">selector</span>{_esc(sel)}</span>')
    if val:
        tags.append(f'<span class="tag"><span class="tag-label">value</span>{_esc(val)}</span>')
    if readv:
        tags.append(f'<span class="tag"><span class="tag-label">read</span>{readv}</span>')
    if path and path not in ("both", None, ""):
        tags.append(f'<span class="tag"><span class="tag-label">path</span>{_esc(path)}</span>')
    tags_html = f'<div class="tags">{"".join(tags)}</div>' if tags else ""

    obs_html = f'<div class="obs">{obs}</div>' if obs else ""
    err_html = f'<div class="err">{err}</div>' if err else ""

    if shot:
        img_path = SHOTS_DIR / shot
        if img_path.exists():
            b64 = _b64_img(img_path)
            shot_html = (
                f'<div class="shot-wrap">'
                f'<img src="data:image/png;base64,{b64}" alt="Step {_esc(str(num))} screenshot" loading="lazy">'
                f'</div>'
                if b64 else '<div class="no-shot">screenshot file unreadable</div>'
            )
        else:
            shot_html = f'<div class="no-shot">screenshot not found: {_esc(shot)}</div>'
    else:
        shot_html = '<div class="no-shot">no screenshot captured for this step</div>'

    return f"""<div class="step {css}">
  <div class="step-head">
    <span class="num">Step {_esc(str(num))}</span>
    <span class="step-desc">{desc}</span>
    <span class="badge {css}">{badge}</span>
  </div>
  {tags_html}{obs_html}{err_html}{shot_html}
</div>"""


# ── HTML builder ──────────────────────────────────────────────────────────────

def generate_html(exploration_id: str, test_case: str, steps: list, completed_at: str) -> str:
    total  = len(steps)
    passed = sum(1 for s in steps if s.get("success"))
    failed = total - passed
    pct    = round(passed / total * 100) if total else 0
    ts     = completed_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    cards_html = "\n".join(_step_card(s) for s in steps)

    # Verified selector reference table
    verified = [s for s in steps if s.get("selector") and s.get("success")]
    sel_table_html = ""
    if verified:
        rows = "".join(
            f"<tr>"
            f"<td>{_esc(s.get('step_num','?'))}</td>"
            f"<td>{_esc(s.get('action',''))}</td>"
            f"<td>{_esc(s.get('selector',''))}</td>"
            f"<td>{_esc(s.get('value') or '')}</td>"
            f"<td>{_esc(s.get('notes') or '')}</td>"
            f"</tr>"
            for s in verified
        )
        sel_table_html = f"""
<h2>Verified Selector Reference</h2>
<table class="sel-table">
  <thead><tr><th>Step</th><th>Action</th><th>Selector</th><th>Value</th><th>Notes</th></tr></thead>
  <tbody>{rows}</tbody>
</table>"""

    shots_found = sum(
        1 for s in steps
        if s.get("screenshot_file") and (SHOTS_DIR / s["screenshot_file"]).exists()
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Exploration Reference — {_esc(exploration_id)}</title>
<style>{_CSS}</style>
</head>
<body>

<header>
  <h1>Exploration Reference — {_esc(exploration_id)}</h1>
  <div class="meta">
    <b>Completed:</b> {_esc(ts)}&nbsp;&nbsp;·&nbsp;&nbsp;
    <b>Steps:</b> {total}&nbsp;&nbsp;·&nbsp;&nbsp;
    <b>Screenshots:</b> {shots_found}
  </div>
  <div class="bar-wrap">
    <div class="bar-fill" style="width:{pct}%"></div>
  </div>
  <div class="summary">
    <span class="s-pass">✓ {passed} passed</span>
    {"<span class='s-fail'>✗ " + str(failed) + " failed</span>" if failed else ""}
    <span style="color:#64748b">{pct}% success rate</span>
  </div>
  <div class="test-case">{_esc(test_case)}</div>
</header>

<main>
  <h2>Step-by-Step Evidence</h2>
  {cards_html}
  {sel_table_html}
</main>

<div id="lb"><img id="lb-img" src="" alt="fullscreen screenshot"></div>

<footer>
  Generated by TestMind &nbsp;·&nbsp; {_esc(ts)} &nbsp;·&nbsp; Exploration {_esc(exploration_id)}
</footer>

<script>{_JS}</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not STEPS_FILE.exists():
        print(f"[reference] steps.json not found at {STEPS_FILE} — skipping")
        sys.exit(0)

    data       = json.loads(STEPS_FILE.read_text(encoding="utf-8"))
    steps      = data.get("steps", [])
    test_case  = data.get("testCase", "")
    completed_at = data.get("completedAt", "")

    if not steps:
        print("[reference] No steps in result — skipping reference generation")
        sys.exit(0)

    passed = sum(1 for s in steps if s.get("success"))
    shots  = sum(1 for s in steps if s.get("screenshot_file"))
    print(f"[reference] {len(steps)} steps ({passed} passed, {shots} screenshots) — generating…")

    html = generate_html(EXPLORATION_ID, test_case, steps, completed_at)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(html, encoding="utf-8")
    size_kb = len(html.encode()) // 1024
    print(f"[reference] Saved → {OUT_FILE} ({size_kb} KB)")


if __name__ == "__main__":
    main()
