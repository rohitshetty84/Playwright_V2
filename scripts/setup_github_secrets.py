#!/usr/bin/env python3
"""
One-shot script — reads your local .env and pushes all GitHub Actions secrets
required by explore.yml in a single command.

Usage:
  pip install PyNaCl python-dotenv requests   # if not already installed
  python scripts/setup_github_secrets.py

What it does:
  1. Loads .env from the repo root
  2. Generates STUDIO_CALLBACK_TOKEN if one isn't already set
  3. Gets the repo's public key from GitHub (needed to encrypt secrets)
  4. Encrypts + PUTs each secret via the GitHub REST API
  5. Prints a summary of what was set / skipped / missing

Secrets pushed:
  AZURE_OPENAI_API_KEY       ← from AZURE_OPENAI_API_KEY in .env
  AZURE_OPENAI_ENDPOINT      ← from AZURE_OPENAI_ENDPOINT
  AZURE_OPENAI_API_VERSION   ← from AZURE_OPENAI_API_VERSION
  AZURE_OPENAI_DEPLOYMENT    ← from AZURE_OPENAI_DEPLOYMENT
  AZURE_REASONING_DEPLOYMENT ← from AZURE_REASONING_DEPLOYMENT  (optional)
  AZURE_REASONING_API_VERSION← from AZURE_REASONING_API_VERSION (optional)
  SF_BASE_URL                ← from SF_BASE_URL
  SF_USERNAME                ← from SF_USERNAME
  SF_PASSWORD                ← from SF_PASSWORD
  STUDIO_CALLBACK_TOKEN      ← from STUDIO_CALLBACK_TOKEN (auto-generated if absent)
"""

import os
import sys
import secrets
import json
from base64 import b64encode
from pathlib import Path

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import requests
    from nacl import encoding, public as nacl_public
    from dotenv import load_dotenv, set_key
except ImportError as exc:
    print(f"\n❌  Missing dependency: {exc}")
    print("    Run:  pip install PyNaCl python-dotenv requests")
    sys.exit(1)

# ── Load .env ─────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE  = REPO_ROOT / ".env"

if not ENV_FILE.exists():
    print(f"\n❌  .env not found at {ENV_FILE}")
    print("    Copy .env.example → .env and fill in your values first.")
    sys.exit(1)

load_dotenv(ENV_FILE)

# ── Mapping: env-var name → GitHub secret name ───────────────────────────────
# Tuples of (env_var_name, github_secret_name, required)
SECRET_MAP = [
    ("AZURE_OPENAI_API_KEY",         "AZURE_OPENAI_API_KEY",         True),
    ("AZURE_OPENAI_ENDPOINT",        "AZURE_OPENAI_ENDPOINT",        True),
    ("AZURE_OPENAI_API_VERSION",     "AZURE_OPENAI_API_VERSION",     True),
    ("AZURE_OPENAI_DEPLOYMENT",      "AZURE_OPENAI_DEPLOYMENT",      True),
    ("AZURE_REASONING_DEPLOYMENT",   "AZURE_REASONING_DEPLOYMENT",   False),
    ("AZURE_REASONING_API_VERSION",  "AZURE_REASONING_API_VERSION",  False),
    ("SF_BASE_URL",                  "SF_BASE_URL",                  False),
    ("SF_USERNAME",                  "SF_USERNAME",                  False),
    ("SF_PASSWORD",                  "SF_PASSWORD",                  False),
    ("STUDIO_CALLBACK_TOKEN",        "STUDIO_CALLBACK_TOKEN",        True),
]

# ── Auto-generate STUDIO_CALLBACK_TOKEN if missing ───────────────────────────
if not os.getenv("STUDIO_CALLBACK_TOKEN"):
    token = secrets.token_hex(32)
    set_key(str(ENV_FILE), "STUDIO_CALLBACK_TOKEN", token)
    os.environ["STUDIO_CALLBACK_TOKEN"] = token
    print(f"✨  Generated STUDIO_CALLBACK_TOKEN and saved to .env")
    print(f"    → Set this same value as STUDIO_PUBLIC_URL in your Azure Container App env vars.\n")

# ── GitHub config ─────────────────────────────────────────────────────────────
gh_token = os.getenv("GITHUB_TOKEN")
gh_owner = os.getenv("GITHUB_OWNER")
gh_repo  = os.getenv("GITHUB_REPO")

if not gh_token:
    print("❌  GITHUB_TOKEN not set in .env")
    sys.exit(1)
if not gh_owner or not gh_repo:
    print("❌  GITHUB_OWNER and GITHUB_REPO must be set in .env")
    sys.exit(1)

HEADERS = {
    "Authorization": f"token {gh_token}",
    "Accept": "application/vnd.github.v3+json",
}
BASE_URL = f"https://api.github.com/repos/{gh_owner}/{gh_repo}"

# ── Fetch repo public key ─────────────────────────────────────────────────────
print(f"🔑  Fetching public key for {gh_owner}/{gh_repo} …")
r = requests.get(f"{BASE_URL}/actions/secrets/public-key", headers=HEADERS, timeout=10)
if r.status_code != 200:
    print(f"❌  Could not fetch public key: {r.status_code} {r.text}")
    sys.exit(1)

key_data   = r.json()
key_id     = key_data["key_id"]
public_key = nacl_public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())

def _encrypt(value: str) -> str:
    box       = nacl_public.SealedBox(public_key)
    encrypted = box.encrypt(value.encode("utf-8"))
    return b64encode(encrypted).decode("utf-8")

# ── Push secrets ──────────────────────────────────────────────────────────────
print(f"\n📤  Pushing secrets to {gh_owner}/{gh_repo} …\n")

results = {"set": [], "skipped": [], "missing": []}

for env_var, secret_name, required in SECRET_MAP:
    value = os.getenv(env_var, "").strip()

    if not value:
        tag = "⚠️ " if required else "–  "
        print(f"  {tag} {secret_name:<35} (not set{' — REQUIRED' if required else ', skipping'})")
        (results["missing"] if required else results["skipped"]).append(secret_name)
        continue

    encrypted = _encrypt(value)
    r = requests.put(
        f"{BASE_URL}/actions/secrets/{secret_name}",
        headers=HEADERS,
        json={"encrypted_value": encrypted, "key_id": key_id},
        timeout=10,
    )
    if r.status_code in (201, 204):
        print(f"  ✅  {secret_name:<35} → set")
        results["set"].append(secret_name)
    else:
        print(f"  ❌  {secret_name:<35} → {r.status_code} {r.text[:80]}")
        results["missing"].append(secret_name)

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'─'*55}")
print(f"  Set:     {len(results['set'])}")
print(f"  Skipped: {len(results['skipped'])}  (optional, not in .env)")
print(f"  Failed:  {len(results['missing'])}")
print(f"{'─'*55}")

if results["missing"]:
    print("\n⚠️  Some required secrets were not set. Add them to .env and re-run.")
else:
    print("\n✅  All required secrets are configured.")
    print("\nNext steps:")
    print("  1. Set these env vars on your Azure Container App:")
    print("       EXPLORE_MODE=github_runner")
    print(f"      STUDIO_PUBLIC_URL=https://<your-studio>.azurecontainerapps.io")
    print(f"      STUDIO_CALLBACK_TOKEN={os.getenv('STUDIO_CALLBACK_TOKEN', '<token>')}")
    print("  2. Push this branch so explore.yml is on main.")
    print("  3. Explorations will now run on GitHub runners automatically.")
