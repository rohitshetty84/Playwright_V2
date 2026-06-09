#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# Playwright AI Studio — Azure Container Apps provisioning
#
# Run ONCE from the repo root to create all Azure resources:
#   bash deploy/provision.sh
#
# Prerequisites:
#   - az CLI logged in  (az login)
#   - .env exists at repo root with Azure OpenAI + GitHub values filled in
#   - No Docker needed — image is built in the cloud via az acr build
#
# What it creates:
#   • Resource group        rg-playwright-mcp  (eastus)
#   • Container Registry    (Basic, ~$5/month)
#   • Storage Account       + File Share for persistent data (~$1/month)
#   • Container Apps Env    (Consumption — scale-to-zero)
#   • Container App         (2 CPU / 4 GB, min 0 replicas)
#   • Service Principal     for GitHub Actions CI/CD
#
# At the end it prints the GitHub secrets you need to add.
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Config — edit these if you want different names ───────────────────────────
RG="rg-playwright-mcp"
LOCATION="eastus"
ACR_NAME="acrplaywrightmcp"        # must be globally unique, lowercase, 5-50 chars
STORAGE_NAME="stplaywrightmcp"     # must be globally unique, lowercase, 3-24 chars
SHARE_NAME="studio-data"
CAE_NAME="cae-playwright-studio"
CA_NAME="ca-playwright-studio"
IMAGE_NAME="playwright-studio"
SP_NAME="sp-playwright-studio-cicd"

# .env lives at repo root (not studio/) — fall back to studio/.env for compat
ENV_FILE=".env"
[ ! -f "$ENV_FILE" ] && ENV_FILE="studio/.env"

# ── Helpers ───────────────────────────────────────────────────────────────────
info()  { echo -e "\033[0;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[0;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
die()   { echo -e "\033[0;31m[FAIL]\033[0m  $*"; exit 1; }
hr()    { echo "────────────────────────────────────────────────────────────"; }

hr
info "Playwright AI Studio — Azure Container Apps provisioning"
hr

# ── Preflight ──────────────────────────────────────────────────────────────────
command -v az >/dev/null 2>&1 || die "az CLI not found. Install: https://docs.microsoft.com/cli/azure/install-azure-cli"

SUBSCRIPTION=$(az account show --query id -o tsv)
info "Subscription : $SUBSCRIPTION"
info "Region       : $LOCATION"
info "Resource group: $RG"
echo ""

# Read secrets from .env — fail loudly if missing
if [ ! -f "$ENV_FILE" ]; then
    die "$ENV_FILE not found. Copy .env.example → studio/.env and fill in your values."
fi

read_env() {
    grep -E "^${1}=" "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true
}

OPENAI_ENDPOINT=$(read_env AZURE_OPENAI_ENDPOINT)
OPENAI_KEY=$(read_env AZURE_OPENAI_API_KEY)
OPENAI_VERSION=$(read_env AZURE_OPENAI_API_VERSION)
OPENAI_DEPLOYMENT=$(read_env AZURE_OPENAI_DEPLOYMENT)
GITHUB_TOKEN_VAL=$(read_env GITHUB_TOKEN)
GITHUB_OWNER=$(read_env GITHUB_OWNER)
GITHUB_REPO=$(read_env GITHUB_REPO)
GITHUB_WORKFLOW=$(read_env GITHUB_WORKFLOW)
GITHUB_BRANCH=$(read_env GITHUB_BRANCH)
VISION_HOSTS=$(read_env VISION_ALLOWED_HOSTS)

[ -z "$OPENAI_ENDPOINT" ]    && die "AZURE_OPENAI_ENDPOINT missing in $ENV_FILE"
[ -z "$OPENAI_KEY" ]         && die "AZURE_OPENAI_API_KEY missing in $ENV_FILE"
[ -z "$OPENAI_DEPLOYMENT" ]  && die "AZURE_OPENAI_DEPLOYMENT missing in $ENV_FILE"

# ── 1. Resource group ──────────────────────────────────────────────────────────
hr; info "Step 1/7 — Resource group"
if az group show --name "$RG" &>/dev/null; then
    ok "Resource group $RG already exists"
else
    az group create --name "$RG" --location "$LOCATION" -o none
    ok "Created resource group $RG"
fi

# ── 2. Container Registry ──────────────────────────────────────────────────────
hr; info "Step 2/7 — Azure Container Registry (Basic)"
if az acr show --name "$ACR_NAME" --resource-group "$RG" &>/dev/null; then
    ok "ACR $ACR_NAME already exists"
else
    az acr create \
        --resource-group "$RG" \
        --name "$ACR_NAME" \
        --sku Basic \
        --admin-enabled true \
        -o none
    ok "Created ACR $ACR_NAME"
fi

ACR_LOGIN_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
ACR_USERNAME=$(az acr credential show --name "$ACR_NAME" --query username -o tsv)
ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)
ok "ACR login server: $ACR_LOGIN_SERVER"

# ── 3. Build & push image via ACR (no local Docker needed) ────────────────────
hr; info "Step 3/7 — Build image in Azure (az acr build — ~8 min first time)"
GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "initial")
az acr build \
    --registry "$ACR_NAME" \
    --resource-group "$RG" \
    --image "$IMAGE_NAME:latest" \
    --image "$IMAGE_NAME:$GIT_SHA" \
    --file deploy/Dockerfile \
    .
ok "Image built and pushed: $ACR_LOGIN_SERVER/$IMAGE_NAME:latest"

# ── 4. Storage account + File Share ───────────────────────────────────────────
hr; info "Step 4/7 — Storage account + Azure File Share (persistent data)"
if az storage account show --name "$STORAGE_NAME" --resource-group "$RG" &>/dev/null; then
    ok "Storage account $STORAGE_NAME already exists"
else
    az storage account create \
        --name "$STORAGE_NAME" \
        --resource-group "$RG" \
        --location "$LOCATION" \
        --sku Standard_LRS \
        --kind StorageV2 \
        -o none
    ok "Created storage account $STORAGE_NAME"
fi

STORAGE_KEY=$(az storage account keys list \
    --resource-group "$RG" \
    --account-name "$STORAGE_NAME" \
    --query "[0].value" -o tsv)

# Create the file share
az storage share create \
    --name "$SHARE_NAME" \
    --account-name "$STORAGE_NAME" \
    --account-key "$STORAGE_KEY" \
    --quota 5 \
    -o none 2>/dev/null || true
ok "File share $SHARE_NAME ready (5 GB quota)"

# ── 5. Container Apps environment ─────────────────────────────────────────────
hr; info "Step 5/7 — Container Apps environment (Consumption)"

# Install extension if needed
az extension add --name containerapp --upgrade -y 2>/dev/null || true
az provider register --namespace Microsoft.App --wait -o none 2>/dev/null || true
az provider register --namespace Microsoft.OperationalInsights --wait -o none 2>/dev/null || true

if az containerapp env show --name "$CAE_NAME" --resource-group "$RG" &>/dev/null; then
    ok "Container Apps environment $CAE_NAME already exists"
else
    az containerapp env create \
        --name "$CAE_NAME" \
        --resource-group "$RG" \
        --location "$LOCATION" \
        -o none
    ok "Created Container Apps environment $CAE_NAME"
fi

# Link Azure File Share to the environment
az containerapp env storage set \
    --name "$CAE_NAME" \
    --resource-group "$RG" \
    --storage-name "studio-data" \
    --azure-file-account-name "$STORAGE_NAME" \
    --azure-file-account-key "$STORAGE_KEY" \
    --azure-file-share-name "$SHARE_NAME" \
    --access-mode ReadWrite \
    -o none
ok "File share linked to Container Apps environment"

# ── 6. Container App ───────────────────────────────────────────────────────────
hr; info "Step 6/7 — Container App"

# Build the env-vars string
ENV_VARS="AZURE_OPENAI_ENDPOINT=$OPENAI_ENDPOINT"
ENV_VARS="$ENV_VARS AZURE_OPENAI_API_VERSION=${OPENAI_VERSION:-2024-02-01}"
ENV_VARS="$ENV_VARS AZURE_OPENAI_DEPLOYMENT=${OPENAI_DEPLOYMENT}"
ENV_VARS="$ENV_VARS PORT=8000"
[ -n "$GITHUB_TOKEN_VAL" ]  && ENV_VARS="$ENV_VARS GITHUB_TOKEN=$GITHUB_TOKEN_VAL"
[ -n "$GITHUB_OWNER" ]      && ENV_VARS="$ENV_VARS GITHUB_OWNER=$GITHUB_OWNER"
[ -n "$GITHUB_REPO" ]       && ENV_VARS="$ENV_VARS GITHUB_REPO=$GITHUB_REPO"
[ -n "$GITHUB_WORKFLOW" ]   && ENV_VARS="$ENV_VARS GITHUB_WORKFLOW=$GITHUB_WORKFLOW"
[ -n "$GITHUB_BRANCH" ]     && ENV_VARS="$ENV_VARS GITHUB_BRANCH=$GITHUB_BRANCH"
[ -n "$VISION_HOSTS" ]      && ENV_VARS="$ENV_VARS VISION_ALLOWED_HOSTS=$VISION_HOSTS"

# Secrets (sensitive values — never in env vars)
SECRETS="openai-key=$OPENAI_KEY"

if az containerapp show --name "$CA_NAME" --resource-group "$RG" &>/dev/null; then
    info "Container App $CA_NAME exists — updating image…"
    az containerapp update \
        --name "$CA_NAME" \
        --resource-group "$RG" \
        --image "$ACR_LOGIN_SERVER/$IMAGE_NAME:latest" \
        -o none
    ok "Container App image updated"
else
    az containerapp create \
        --name "$CA_NAME" \
        --resource-group "$RG" \
        --environment "$CAE_NAME" \
        --image "$ACR_LOGIN_SERVER/$IMAGE_NAME:latest" \
        --registry-server "$ACR_LOGIN_SERVER" \
        --registry-username "$ACR_USERNAME" \
        --registry-password "$ACR_PASSWORD" \
        --cpu 2 \
        --memory 4Gi \
        --min-replicas 0 \
        --max-replicas 1 \
        --target-port 8000 \
        --ingress external \
        --env-vars $ENV_VARS \
        --secrets "$SECRETS" \
        --env-vars "AZURE_OPENAI_API_KEY=secretref:openai-key" \
        --volume-mount "studio-data:/mnt/studio-data:storage-type=AzureFile" \
        -o none
    ok "Created Container App $CA_NAME"
fi

APP_URL=$(az containerapp show \
    --name "$CA_NAME" \
    --resource-group "$RG" \
    --query "properties.configuration.ingress.fqdn" -o tsv)
ok "App URL: https://$APP_URL"

# ── 7. Service principal for GitHub Actions ────────────────────────────────────
hr; info "Step 7/7 — Service principal for GitHub Actions CI/CD"

SUBSCRIPTION_ID=$(az account show --query id -o tsv)
SCOPE="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG"

# Check if SP exists
SP_APP_ID=$(az ad sp list --display-name "$SP_NAME" --query "[0].appId" -o tsv 2>/dev/null || true)

if [ -z "$SP_APP_ID" ]; then
    SP_JSON=$(az ad sp create-for-rbac \
        --name "$SP_NAME" \
        --role contributor \
        --scopes "$SCOPE" \
        --sdk-auth \
        2>/dev/null)
    ok "Service principal created"
else
    ok "Service principal $SP_NAME already exists (appId: $SP_APP_ID)"
    SP_JSON=$(az ad sp create-for-rbac \
        --name "$SP_NAME" \
        --role contributor \
        --scopes "$SCOPE" \
        --sdk-auth \
        2>/dev/null)
fi

# ── Summary ────────────────────────────────────────────────────────────────────
hr
ok "Provisioning complete!"
echo ""
echo "  App URL : https://$APP_URL"
echo ""
echo "Add these secrets to GitHub (Settings → Secrets → Actions):"
echo ""
echo "  AZURE_CREDENTIALS:"
echo "$SP_JSON" | sed 's/^/    /'
echo ""
echo "  ACR_LOGIN_SERVER : $ACR_LOGIN_SERVER"
echo "  ACR_USERNAME     : $ACR_USERNAME"
echo "  ACR_PASSWORD     : $ACR_PASSWORD"
echo ""
echo "  ACA_RESOURCE_GROUP : $RG"
echo "  ACA_APP_NAME       : $CA_NAME"
echo ""
warn "Store the AZURE_CREDENTIALS JSON as a single multi-line secret."
warn "The ACR password is sensitive — add it as a secret, not a variable."
hr
