"""
studio/services/git_sync.py — safe golden sync, git CLI or GitHub API.

Two paths depending on the runtime environment:

  • Local (`.git` present)  → git add / commit / push via subprocess.
    Same hardened behaviour as before (branch check, behind check, etc.)

  • Container (no `.git`)   → GitHub Contents API via GITHUB_TOKEN.
    Each new or changed golden file is PUT directly to the repo.
    No git binary or credentials file needed in the image.

This module is intentionally synchronous — callers wrap with asyncio.to_thread.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
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
    skipped: bool = False
    files_staged: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# GitHub Contents API helpers (container / no-git path)
# ─────────────────────────────────────────────────────────────────────────────

def _gh_request(method: str, path: str, token: str, body: Optional[dict] = None):
    """Make a GitHub API call. Returns (response_dict, error_str)."""
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")[:300]
        return None, f"HTTP {e.code}: {body_text}"
    except Exception as e:
        return None, str(e)


def _gh_file_sha(token: str, owner: str, repo: str, path: str, branch: str) -> Optional[str]:
    """Return the blob SHA of a file on GitHub, or None if it doesn't exist."""
    resp, _ = _gh_request("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={branch}", token)
    return resp.get("sha") if resp else None


def _api_sync_goldens(
    golden_dir: Path,
    *,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    github_prefix: str,  # path inside the repo, e.g. "studio/golden"
    message: str,
) -> SyncResult:
    """Upload new / changed golden files to GitHub via the Contents API."""
    if not golden_dir.exists():
        return SyncResult(
            pushed=False, committed=False, branch=branch,
            message="Golden directory not found",
            error=str(golden_dir),
        )

    # Collect all golden files (JSON specs + generated TS)
    files = sorted(
        f for f in golden_dir.iterdir()
        if f.is_file() and f.suffix in (".json", ".ts", ".md")
    )
    if not files:
        return SyncResult(
            pushed=False, committed=False, branch=branch,
            message="Nothing to sync — golden directory is empty",
        )

    uploaded = 0
    skipped  = 0
    errors: list[str] = []

    for local_file in files:
        content_bytes = local_file.read_bytes()
        content_b64   = base64.b64encode(content_bytes).decode()
        api_path      = f"{github_prefix}/{local_file.name}"

        # Check whether the file already exists on GitHub (need SHA to update)
        existing_sha = _gh_file_sha(token, owner, repo, api_path, branch)

        # Skip if content is identical (compare blob SHA = sha1("blob {len}\0{content}"))
        if existing_sha:
            blob_input  = f"blob {len(content_bytes)}\0".encode() + content_bytes
            local_sha   = hashlib.sha1(blob_input).hexdigest()
            if local_sha == existing_sha:
                skipped += 1
                continue

        body: dict = {"message": message, "content": content_b64, "branch": branch}
        if existing_sha:
            body["sha"] = existing_sha  # required for updates

        _, err = _gh_request(
            "PUT", f"/repos/{owner}/{repo}/contents/{api_path}", token, body
        )
        if err:
            errors.append(f"{local_file.name}: {err}")
        else:
            uploaded += 1

    if errors:
        return SyncResult(
            pushed=uploaded > 0, committed=uploaded > 0, branch=branch,
            files_staged=uploaded,
            message=f"Partial sync: {uploaded} uploaded, {len(errors)} error(s)",
            error="; ".join(errors[:3]),
        )
    if uploaded == 0:
        return SyncResult(
            pushed=False, committed=False, branch=branch,
            message=f"Nothing to push — all {skipped} golden file(s) already up to date",
        )
    return SyncResult(
        pushed=True, committed=True, branch=branch,
        files_staged=uploaded,
        message=f"Synced {uploaded} golden file(s) to {owner}/{repo}@{branch} via GitHub API",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Git CLI helpers (local / .git present path)
# ─────────────────────────────────────────────────────────────────────────────

def _git(*args: str, cwd: Path, timeout: int = 15) -> subprocess.CompletedProcess:
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
    args = ["status", "--porcelain"]
    if path:
        args += ["--", path]
    r = _git(*args, cwd=repo)
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line.strip()]


def _ahead_behind(repo: Path, branch: str) -> Optional[tuple[int, int]]:
    r = _git("rev-list", "--left-right", "--count",
             f"origin/{branch}...HEAD", cwd=repo)
    if r.returncode != 0:
        return None
    try:
        behind_str, ahead_str = r.stdout.strip().split()
        return int(ahead_str), int(behind_str)
    except (ValueError, IndexError):
        return None


def _cli_sync_goldens(
    repo_root: Path,
    *,
    message: str,
    golden_subdir: str,
    expected_branch: str,
    fetch_first: bool,
) -> SyncResult:
    """Git CLI path — only used when .git exists (local development)."""
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

    if fetch_first:
        fetch = _git("fetch", "origin", branch, cwd=repo_root, timeout=20)
        if fetch.returncode == 0:
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

    stage = _git("add", "--", golden_subdir, cwd=repo_root)
    if stage.returncode != 0:
        return SyncResult(
            pushed=False, committed=False, branch=branch,
            message="git add failed",
            error=stage.stderr.strip()[:300],
        )

    diff = _git("diff", "--cached", "--quiet", "--", golden_subdir, cwd=repo_root)
    files_staged = len(_porcelain(repo_root, golden_subdir))
    if diff.returncode == 0:
        return SyncResult(
            pushed=False, committed=False, branch=branch,
            message="Nothing to commit — goldens already up to date",
            files_staged=0,
        )

    commit = _git("commit", "-m", message, "--", golden_subdir, cwd=repo_root)
    if commit.returncode != 0:
        return SyncResult(
            pushed=False, committed=False, branch=branch,
            message="git commit failed",
            error=commit.stderr.strip()[:300],
            files_staged=files_staged,
        )

    sha_proc = _git("rev-parse", "--short", "HEAD", cwd=repo_root)
    commit_sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else None

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


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def sync_goldens(
    repo_root: Path,
    *,
    message: str,
    golden_subdir: str = "studio/golden/",
    expected_branch: str = "main",
    fetch_first: bool = True,
) -> SyncResult:
    """
    Stage, commit, and push the goldens directory.

    Automatically selects the right transport:
      • .git present  → git CLI (local dev)
      • .git absent   → GitHub Contents API (container / Azure deployment)
    """
    if os.getenv("GIT_SYNC_DISABLED", "").lower() in ("1", "true", "yes"):
        return SyncResult(
            pushed=False, committed=False, skipped=True,
            message="GIT_SYNC_DISABLED is set — golden saved locally only.",
        )

    # ── Local dev: use git CLI ────────────────────────────────────────────────
    if (repo_root / ".git").exists():
        return _cli_sync_goldens(
            repo_root,
            message=message,
            golden_subdir=golden_subdir,
            expected_branch=expected_branch,
            fetch_first=fetch_first,
        )

    # ── Container / no .git: use GitHub API ──────────────────────────────────
    token  = os.getenv("GITHUB_TOKEN", "")
    owner  = os.getenv("GITHUB_OWNER", "")
    repo   = os.getenv("GITHUB_REPO",  "")
    branch = os.getenv("GITHUB_BRANCH", expected_branch)

    if not all([token, owner, repo]):
        return SyncResult(
            pushed=False, committed=False,
            message="No .git directory and GITHUB_TOKEN/OWNER/REPO not set — cannot sync",
            error=(
                "Running in container mode but GitHub credentials are missing. "
                "Set GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO in the app environment."
            ),
        )

    # golden_subdir is e.g. "studio/golden/" — strip trailing slash for the API path
    github_prefix = golden_subdir.strip("/")
    golden_dir    = repo_root / golden_subdir.strip("/")

    return _api_sync_goldens(
        golden_dir,
        token=token,
        owner=owner,
        repo=repo,
        branch=branch,
        github_prefix=github_prefix,
        message=message,
    )
