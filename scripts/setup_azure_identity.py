#!/usr/bin/env python3
"""
One-shot script — enables Managed Identity on your Azure Container App
and grants it the Contributor role on the resource group so the Studio
can call the Azure Management API to update its own env vars.

Run this ONCE from your local machine before deploying, or right after
the Container App is created. After it completes you never need Azure
credentials stored anywhere — the managed identity handles everything.

Usage:
  python scripts/setup_azure_identity.py

Prerequisites (pick one):
  A) Azure CLI installed and logged in:
       az login
       az account set --subscription <your-sub-id>

  B) Service principal env vars set in .env:
       AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET
       (needs Owner or User Access Administrator role on the resource group)

Required in .env:
  AZURE_SUBSCRIPTION_ID
  AZURE_RESOURCE_GROUP
  AZURE_CONTAINER_APP_NAME
"""

import os
import sys
import json
import uuid
import subprocess
import time
from pathlib import Path

try:
    import requests
    from dotenv import load_dotenv, set_key
except ImportError as e:
    print(f"❌  Missing dependency: {e}")
    print("    Run:  pip install requests python-dotenv")
    sys.exit(1)

# ── Load .env ─────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE  = REPO_ROOT / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

SUB  = os.getenv("AZURE_SUBSCRIPTION_ID", "").strip()
RG   = os.getenv("AZURE_RESOURCE_GROUP",  "").strip()
APP  = os.getenv("AZURE_CONTAINER_APP_NAME", "").strip()

# ── Contributor role definition ID (fixed Azure built-in) ─────────────────────
CONTRIBUTOR_ROLE_ID = "b24988ac-6180-42a0-ab88-20f7382dd24c"

# ── Azure management API version ─────────────────────────────────────────────
ACA_API   = "2024-03-01"
AUTH_API  = "2022-04-01"
MGMT_BASE = "https://management.azure.com"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_prereqs() -> None:
    missing = [k for k, v in {
        "AZURE_SUBSCRIPTION_ID":   SUB,
        "AZURE_RESOURCE_GROUP":    RG,
        "AZURE_CONTAINER_APP_NAME": APP,
    }.items() if not v]
    if missing:
        print(f"\n❌  Missing in .env: {', '.join(missing)}")
        print("    Add them and re-run.")
        sys.exit(1)


def _az_available() -> bool:
    try:
        r = subprocess.run(["az", "version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _az(*args) -> str:
    """Run an az CLI command, return stdout. Raise on error."""
    cmd = ["az"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"az {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def _get_token_via_az() -> str:
    raw = _az("account", "get-access-token",
              "--resource", "https://management.azure.com",
              "--output", "json")
    return json.loads(raw)["accessToken"]


def _get_token_via_sp() -> str:
    tenant = os.getenv("AZURE_TENANT_ID", "").strip()
    client = os.getenv("AZURE_CLIENT_ID", "").strip()
    secret = os.getenv("AZURE_CLIENT_SECRET", "").strip()
    if not all([tenant, client, secret]):
        raise RuntimeError(
            "No Azure CLI and no service principal env vars found.\n"
            "Either install and log into the Azure CLI, or set:\n"
            "  AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET"
        )
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     client,
            "client_secret": secret,
            "resource":      "https://management.azure.com/",
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Service principal auth failed: {r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Step 1: Enable system-assigned managed identity ──────────────────────────

def enable_managed_identity(token: str) -> str:
    """
    PATCH the Container App to enable SystemAssigned identity.
    Returns the principal ID of the new identity.
    """
    print(f"\n[1/3] Enabling system-assigned managed identity on '{APP}' …")

    url = (
        f"{MGMT_BASE}/subscriptions/{SUB}/resourceGroups/{RG}"
        f"/providers/Microsoft.App/containerApps/{APP}?api-version={ACA_API}"
    )

    # GET current config first
    r = requests.get(url, headers=_headers(token), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"GET container app failed: {r.status_code} {r.text[:300]}")

    body = r.json()
    current_identity_type = body.get("identity", {}).get("type", "None")

    if "SystemAssigned" in current_identity_type:
        principal_id = body["identity"]["principalId"]
        print(f"     Already enabled. Principal ID: {principal_id}")
        return principal_id

    # PATCH to enable
    patch = {"identity": {"type": "SystemAssigned"}}
    pr = requests.patch(url, headers=_headers(token), json=patch, timeout=30)
    if pr.status_code not in (200, 201, 202):
        raise RuntimeError(f"PATCH identity failed: {pr.status_code} {pr.text[:300]}")

    # Poll until the identity is provisioned (provisioning can take ~15s)
    print("     Waiting for identity provisioning", end="", flush=True)
    for _ in range(24):  # up to 2 minutes
        time.sleep(5)
        print(".", end="", flush=True)
        r2 = requests.get(url, headers=_headers(token), timeout=15)
        if r2.status_code == 200:
            identity = r2.json().get("identity", {})
            pid = identity.get("principalId", "")
            if pid:
                print(f" done\n     Principal ID: {pid}")
                return pid
    raise RuntimeError("Timed out waiting for managed identity to be provisioned")


# ── Step 2: Assign Contributor role on the resource group ────────────────────

def assign_contributor_role(token: str, principal_id: str) -> None:
    print(f"\n[2/3] Assigning Contributor role to identity on resource group '{RG}' …")

    scope      = f"/subscriptions/{SUB}/resourceGroups/{RG}"
    role_def   = f"{MGMT_BASE}{scope}/providers/Microsoft.Authorization/roleDefinitions/{CONTRIBUTOR_ROLE_ID}"
    assignment_id = str(uuid.uuid4())

    url = (
        f"{MGMT_BASE}{scope}/providers/Microsoft.Authorization"
        f"/roleAssignments/{assignment_id}?api-version={AUTH_API}"
    )

    # Check if assignment already exists
    list_url = (
        f"{MGMT_BASE}{scope}/providers/Microsoft.Authorization"
        f"/roleAssignments?api-version={AUTH_API}"
        f"&$filter=assignedTo('{principal_id}')"
    )
    existing = requests.get(list_url, headers=_headers(token), timeout=15)
    if existing.status_code == 200:
        for ra in existing.json().get("value", []):
            if (CONTRIBUTOR_ROLE_ID in ra.get("properties", {}).get("roleDefinitionId", "")
                    and ra["properties"].get("principalId") == principal_id):
                print("     Contributor role already assigned — nothing to do.")
                return

    body = {
        "properties": {
            "roleDefinitionId": role_def,
            "principalId":      principal_id,
            "principalType":    "ServicePrincipal",
        }
    }
    r = requests.put(url, headers=_headers(token), json=body, timeout=15)
    if r.status_code in (200, 201):
        print("     ✅  Role assigned successfully.")
    elif r.status_code == 409:
        print("     Already assigned (409 conflict) — skipping.")
    else:
        raise RuntimeError(f"Role assignment failed: {r.status_code} {r.text[:300]}")


# ── Step 3: Save the three required env vars to .env ─────────────────────────

def save_env_vars() -> None:
    print(f"\n[3/3] Saving Azure identity vars to .env …")
    for key, value in {
        "AZURE_SUBSCRIPTION_ID":    SUB,
        "AZURE_RESOURCE_GROUP":     RG,
        "AZURE_CONTAINER_APP_NAME": APP,
    }.items():
        set_key(str(ENV_FILE), key, value)
        print(f"     {key}={value}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("  Azure Managed Identity Setup")
    print(f"  Container App : {APP}")
    print(f"  Resource Group: {RG}")
    print(f"  Subscription  : {SUB}")
    print("=" * 60)

    _check_prereqs()

    # Get management token
    print("\n[auth] Acquiring Azure management token …")
    if _az_available():
        try:
            token = _get_token_via_az()
            print("[auth] Using Azure CLI credentials")
        except RuntimeError as e:
            print(f"[auth] az CLI token failed ({e}) — trying service principal …")
            token = _get_token_via_sp()
    else:
        print("[auth] az CLI not found — trying service principal …")
        token = _get_token_via_sp()

    # Run the three steps
    principal_id = enable_managed_identity(token)
    assign_contributor_role(token, principal_id)
    save_env_vars()

    print("\n" + "=" * 60)
    print("  ✅  Managed Identity setup complete!")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Deploy / redeploy the container so it picks up the identity.")
    print("  2. Make sure these env vars are set on the Container App:")
    print(f"       AZURE_SUBSCRIPTION_ID    = {SUB}")
    print(f"       AZURE_RESOURCE_GROUP     = {RG}")
    print(f"       AZURE_CONTAINER_APP_NAME = {APP}")
    print("  3. Click '🔑 Sync Secrets to GitHub' in the Studio UI.")
    print("     The container will authenticate via managed identity automatically.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"\n❌  {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
