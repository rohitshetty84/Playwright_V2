"""
studio/services/git_sync.py — P2-2: safe git push of saved goldens.

Replaces the inline `git_sync_goldens` helper that lived in server.py.

Hardening over the old version:
  - Refuses to run on a branch other than the configured one (default: main).
  - Pre-flight `git status --porcelain` so we never sweep up unrelated changes
    that happen to be staged.
  - `git fetch` then check if HEAD is behind origin — fail with a clear message
    instead of leaving the push to be rejected by GitHub.
  - Returns a rich result dict so the API + UI can surface failures clearly.
  - Single env-var escape hatch (`GIT_SYNC_DISABLED=true`) for users who'd
    rather push manually.

This module is intentionally synchronous — `subprocess.run` is blocking and
wrapping it in `asyncio.to_thread` is the caller's choice.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class SyncResult:
    pushed: bool
    committed: bool
    message: str
    error: Optional[str] = None
    commit_sha: Optional[str] = None
    branch: Optional[str] = None
    skipped: bool = False        # True when GIT_SYNC_DISABLED is set
    files_staged: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _git(*args: str, cwd: Path, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a git command. Caller checks returncode."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _current_branch(repo: Path) -> Optional[str]:
    r = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    return r.stdout.strip() if r.returncode == 0 else None


def _porcelain(repo: Path, path: Optional[str] = None) -> list[str]:
    """Lines from `git status --porcelain`, optionally filtered to a path."""
    args = ["status", "--porcelain"]
    if path:
        args += ["--", path]
    r = _git(*args, cwd=repo)
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line.strip()]


def _ahead_behind(repo: Path, branch: str) -> Optional[tuple[int, int]]:
    """Return (ahead, behind) vs origin/<branch>, or None if can't determine."""
    r = _git("rev-list", "--left-right", "--count",
             f"origin/{branch}...HEAD", cwd=repo)
    if r.returncode != 0:
        return None
    try:
        behind_str, ahead_str = r.stdout.strip().split()
        return int(ahead_str), int(behind_str)
    except (ValueError, IndexError):
        return None


# ── Public entry point ────────────────────────────────────────────────────────

def sync_goldens(
    repo_root: Path,
    *,
    message: str,
    golden_subdir: str = "studio/golden/",
    expected_branch: str = "main",
    fetch_first: bool = True,
) -> SyncResult:
    """
    Stage, commit, and push the goldens directory with safety checks.

    The result has detailed fields so the API can show actionable feedback.
    """
    # ── Escape hatch ──────────────────────────────────────────────────────────
    if os.getenv("GIT_SYNC_DISABLED", "").lower() in ("1", "true", "yes"):
        return SyncResult(
            pushed=False, committed=False, skipped=True,
            message="GIT_SYNC_DISABLED is set — golden saved locally only.",
        )

    if not (repo_root / ".git").exists():
        return SyncResult(
            pushed=False, committed=False,
            message="Not a git repo — skipping sync",
            error=f"No .git directory at {repo_root}",
        )

    # ── 1. Verify branch ─────────────────────────────────────────────────────
    branch = _current_branch(repo_root)
    if branch is None:
        return SyncResult(
            pushed=False, committed=False,
            message="Could not determine current branch",
            error="git rev-parse failed",
        )
    if branch != expected_branch:
        return SyncResult(
            pushed=False, committed=False, branch=branch,
            message=f"Refusing to push from '{branch}' (expected '{expected_branch}')",
            error=(
                f"Server is on branch '{branch}', not '{expected_branch}'. "
                f"Check out {expected_branch} or update GITHUB_BRANCH in .env."
            ),
        )

    # ── 2. Pre-flight: warn if unrelated paths are dirty ──────────────────────
    all_dirty = _porcelain(repo_root)
    golden_dirty = _porcelain(repo_root, golden_subdir)
    other_dirty = [ln for ln in all_dirty if ln not in golden_dirty]
    # We don't fail on `other_dirty` — that would block the user from saving
    # goldens during normal development. But we DO scope the `git add` to just
    # the golden subdir below so we can't accidentally commit them.

    # ── 3. Fetch + behind check (best-effort — network can fail) ─────────────
    if fetch_first:
        fetch = _git("fetch", "origin", branch, cwd=repo_root, timeout=20)
        if fetch.returncode != 0:
            # Don't hard-fail — user may be offline; just note it.
            note = fetch.stderr.strip().splitlines()[-1] if fetch.stderr else "unknown"
            # Fall through and try to push; GitHub will reject if needed.
            pass
        else:
            ab = _ahead_behind(repo_root, branch)
            if ab and ab[1] > 0:
                return SyncResult(
                    pushed=False, committed=False, branch=branch,
                    message=f"Local {branch} is {ab[1]} commit(s) behind origin",
                    error=(
                        f"Run `git pull --rebase` first, then save the golden again. "
                        f"(behind={ab[1]}, ahead={ab[0]})"
                    ),
                )

    # ── 4. Stage ONLY the golden subdir ──────────────────────────────────────
    stage = _git("add", "--", golden_subdir, cwd=repo_root)
    if stage.returncode != 0:
        return SyncResult(
            pushed=False, committed=False, branch=branch,
            message="git add failed",
            error=stage.stderr.strip()[:300],
        )

    # ── 5. Anything to commit? ────────────────────────────────────────────────
    # `git diff --cached --quiet` exits 0 if no staged changes, 1 if changes.
    diff = _git("diff", "--cached", "--quiet", "--", golden_subdir, cwd=repo_root)
    files_staged = len(_porcelain(repo_root, golden_subdir))
    if diff.returncode == 0:
        return SyncResult(
            pushed=False, committed=False, branch=branch,
            message="Nothing to commit — goldens already up to date",
            files_staged=0,
        )

    # ── 6. Commit ─────────────────────────────────────────────────────────────
    commit = _git("commit", "-m", message, "--",
                  golden_subdir, cwd=repo_root)
    if commit.returncode != 0:
        return SyncResult(
            pushed=False, committed=False, branch=branch,
            message="git commit failed",
            error=commit.stderr.strip()[:300],
            files_staged=files_staged,
        )

    sha_proc = _git("rev-parse", "--short", "HEAD", cwd=repo_root)
    commit_sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else None

    # ── 7. Push ───────────────────────────────────────────────────────────────
    push = _git("push", "origin", branch, cwd=repo_root, timeout=30)
    if push.returncode != 0:
        return SyncResult(
            pushed=False, committed=True, branch=branch,
            commit_sha=commit_sha, files_staged=files_staged,
            message=f"Committed {commit_sha} locally but push failed",
            error=push.stderr.strip()[:400],
        )

    return SyncResult(
        pushed=True, committed=True, branch=branch,
        commit_sha=commit_sha, files_staged=files_staged,
        message=f"Pushed {commit_sha} to origin/{branch}",
    )
