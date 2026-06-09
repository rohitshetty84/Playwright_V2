# ── Playwright AI Studio — Azure Container Apps image ─────────────────────────
# Uses the official Microsoft Playwright Python base image (Ubuntu 22.04 Jammy)
# which ships Chromium + every system dependency pre-installed.
# We only need to add Node.js 20, @playwright/mcp, and our Python packages.
#
# Build context must be the repo root:
#   az acr build --registry <ACR> --image playwright-studio:latest \
#                --file deploy/Dockerfile .
# ---------------------------------------------------------------------------

FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NPM_CONFIG_PREFIX=/usr/local

# ── 1. Node.js 20 (required by @playwright/mcp) ───────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── 2. Python dependencies ────────────────────────────────────────────────────
WORKDIR /app
COPY studio/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ── 3. Pre-install @playwright/mcp and its browser ───────────────────────────
# Newer @playwright/mcp uses "chrome-for-testing" as the internal browser type
# (distinct from the Python Playwright Chromium in the base image).
# Install it here so runtime doesn't fail with "browser not installed".
RUN npm install -g @playwright/mcp && \
    npx @playwright/mcp install-browser chromium

# ── 4. Application code ───────────────────────────────────────────────────────
COPY studio/ ./studio/
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app/studio
EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
