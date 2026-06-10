import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import requests
import logging
from dotenv import load_dotenv
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ROOT_ENV = REPO_ROOT / ".env"


def parse_github_remote(remote_url: str) -> Optional[Tuple[str, str]]:
    if remote_url.startswith("git@github.com:"):
        path = remote_url[len("git@github.com:") :]
    elif remote_url.startswith("https://") or remote_url.startswith("http://"):
        if "github.com/" not in remote_url:
            return None
        path = remote_url.split("github.com/", 1)[1]
    else:
        return None

    if path.endswith(".git"):
        path = path[:-4]
    parts = path.strip("/").split("/")
    return tuple(parts) if len(parts) == 2 else None


def get_github_config() -> tuple[str, str, str, str, str]:
    if ROOT_ENV.exists():
        load_dotenv(ROOT_ENV, override=True)

    gh_token = os.getenv("GITHUB_TOKEN")
    gh_owner = os.getenv("GITHUB_OWNER")
    gh_repo = os.getenv("GITHUB_REPO")
    gh_workflow = os.getenv("GITHUB_WORKFLOW", "playwright.yml")
    gh_branch = os.getenv("GITHUB_BRANCH", "main")

    if not gh_owner or not gh_repo:
        try:
            remote_url = subprocess.check_output(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=REPO_ROOT,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            parsed = parse_github_remote(remote_url)
            if parsed:
                gh_owner, gh_repo = parsed
        except Exception:
            pass

    if not all([gh_token, gh_owner, gh_repo]):
        raise HTTPException(
            status_code=500,
            detail="GitHub credentials not configured in .env (GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO)",
        )

    return gh_token, gh_owner, gh_repo, gh_workflow, gh_branch


def dispatch_github_workflow(inputs: dict) -> dict:
    gh_token, gh_owner, gh_repo, gh_workflow, gh_branch = get_github_config()
    url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}/actions/workflows/{gh_workflow}/dispatches"
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "ref": gh_branch,
        "inputs": inputs,
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        if response.status_code == 204:
            logging.info("GitHub workflow dispatched: %s inputs=%s", gh_workflow, inputs)
            return {
                "status": "success",
                "message": "GitHub workflow dispatched",
                "inputs": inputs,
            }

        try:
            resp_json = response.json()
            error_detail = resp_json.get("message") if isinstance(resp_json, dict) else resp_json
        except Exception:
            error_detail = response.text

        logging.error(
            "GitHub dispatch failed: status=%s workflow=%s branch=%s url=%s payload=%s response=%s",
            response.status_code,
            gh_workflow,
            gh_branch,
            url,
            inputs,
            str(error_detail),
        )

        if response.status_code == 401:
            raise HTTPException(status_code=401, detail="Invalid GitHub token")
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Workflow not found: {gh_workflow}")
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"GitHub API error: {error_detail}",
            )

    except requests.Timeout:
        raise HTTPException(
            status_code=504,
            detail="GitHub API timeout (>15s) - workflow may still be triggered",
        )
    except requests.ConnectionError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot reach GitHub API - check internet connection: {str(e)}",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected error triggering workflow: {str(e)}",
        )


def list_workflow_runs(params: Optional[dict] = None) -> list:
    gh_token, gh_owner, gh_repo, gh_workflow, gh_branch = get_github_config()
    url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}/actions/workflows/{gh_workflow}/runs"
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    response = requests.get(url, headers=headers, timeout=15, params=params or {"per_page": 50})
    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed to fetch workflow runs: {response.text}",
        )
    return response.json().get("workflow_runs", [])


def find_latest_run_for_golden_id(golden_id: str) -> Optional[dict]:
    runs = list_workflow_runs()
    for run in runs:
        inputs = run.get("inputs") or {}
        display_title = run.get("display_title", "") or ""
        if inputs.get("golden_id") == golden_id or golden_id in display_title or f"[{golden_id}]" in display_title:
            return run
    return None


def dispatch_exploration_workflow(
    exploration_id: str,
    studio_url: str,
    test_case: str,
    storage_state: str = "",
    max_steps: int = 25,
    app_context: str = "SAP SuccessFactors Onboarding 2.0",
    headless: bool = True,
) -> dict:
    """Dispatch the explore.yml workflow to run an AI exploration on a GitHub runner."""
    gh_token, gh_owner, gh_repo, _wf, gh_branch = get_github_config()

    url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}/actions/workflows/explore.yml/dispatches"
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "ref": gh_branch,
        "inputs": {
            "exploration_id": exploration_id,
            "studio_url":     studio_url,
            "test_case":      test_case,
            "storage_state":  storage_state or "",
            "max_steps":      str(max_steps),
            "app_context":    app_context,
            "headless":       "true" if headless else "false",
        },
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        if response.status_code == 204:
            logging.info(
                "Exploration workflow dispatched: exploration_id=%s studio_url=%s",
                exploration_id, studio_url,
            )
            return {"status": "dispatched", "exploration_id": exploration_id}

        try:
            error_detail = response.json().get("message", response.text)
        except Exception:
            error_detail = response.text

        logging.error(
            "Exploration dispatch failed: status=%s detail=%s",
            response.status_code, error_detail,
        )
        if response.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail="explore.yml not found — ensure .github/workflows/explore.yml is committed and pushed",
            )
        raise HTTPException(status_code=response.status_code, detail=f"GitHub API error: {error_detail}")

    except requests.Timeout:
        raise HTTPException(status_code=504, detail="GitHub API timeout — workflow may still be triggered")
    except requests.ConnectionError as exc:
        raise HTTPException(status_code=503, detail=f"Cannot reach GitHub API: {exc}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unexpected error: {exc}")
