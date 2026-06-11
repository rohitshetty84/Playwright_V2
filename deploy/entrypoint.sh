#!/bin/bash
# Playwright AI Studio — container entrypoint
#
# On every start:
#   1. If the Azure File Share is mounted at /mnt/studio-data, initialise it
#      on first boot (copy committed golden files + create dirs), then symlink
#      the mutable studio subdirs to it so data survives container restarts.
#   2. If no volume is mounted (local Docker run / dev), use the container's
#      own filesystem as-is.
#   3. Start the FastAPI server.
# ---------------------------------------------------------------------------

set -euo pipefail

PERSISTENT="/mnt/studio-data"
STUDIO="/app/studio"

# Dirs and files that must survive container restarts
PERSISTENT_DIRS=("golden" ".auth" "explorations" "logs" "runs" "healing_history" "batches")
PERSISTENT_FILES=("selector_memory.json" "exploration_patterns.json" "learned_rules.json")

if [ -d "$PERSISTENT" ] && mountpoint -q "$PERSISTENT" 2>/dev/null; then
    echo "[entrypoint] Persistent volume detected at $PERSISTENT"

    # ── First-boot initialisation ─────────────────────────────────────────────
    if [ ! -f "$PERSISTENT/.initialized" ]; then
        echo "[entrypoint] First boot — seeding persistent storage from image…"

        for dir in "${PERSISTENT_DIRS[@]}"; do
            mkdir -p "$PERSISTENT/$dir"
            # Seed golden files from the image (they come from git)
            if [ "$dir" = "golden" ] && [ -d "$STUDIO/golden" ]; then
                cp -rn "$STUDIO/golden/." "$PERSISTENT/golden/" 2>/dev/null || true
            fi
        done

        for f in "${PERSISTENT_FILES[@]}"; do
            if [ -f "$STUDIO/$f" ]; then
                cp "$STUDIO/$f" "$PERSISTENT/$f"
            else
                echo '{}' > "$PERSISTENT/$f"
            fi
        done

        touch "$PERSISTENT/.initialized"
        echo "[entrypoint] Persistent storage initialised"
    fi

    # ── Ensure all persistent dirs exist on volume (handles dirs added after first boot) ──
    # Use || true so a permission error on the file share never crashes the container
    for dir in "${PERSISTENT_DIRS[@]}"; do
        mkdir -p "$PERSISTENT/$dir" 2>/dev/null || true
    done

    # ── Symlink mutable dirs/files → persistent volume ────────────────────────
    for dir in "${PERSISTENT_DIRS[@]}"; do
        rm -rf "$STUDIO/$dir"
        ln -sfn "$PERSISTENT/$dir" "$STUDIO/$dir"
    done

    for f in "${PERSISTENT_FILES[@]}"; do
        rm -f "$STUDIO/$f"
        ln -sfn "$PERSISTENT/$f" "$STUDIO/$f"
    done

    echo "[entrypoint] Symlinks established — data persists across restarts"
else
    echo "[entrypoint] No persistent volume — using container filesystem (data lost on restart)"
    # Ensure dirs exist for a clean local run
    for dir in "${PERSISTENT_DIRS[@]}"; do
        mkdir -p "$STUDIO/$dir"
    done
fi

# ── Clear stale @playwright/mcp browser locks ─────────────────────────────────
# If the container was killed mid-exploration, @playwright/mcp leaves a lock
# directory at /ms-playwright/mcp-*/. On next start those dirs cause:
#   "Browser is already in use ... use --isolated to run multiple instances"
# We already pass --isolated in mcp_bridge.py, but belt-and-suspenders: wipe
# any leftover lock dirs at startup so they can't interfere.
if [ -d "/ms-playwright" ]; then
    find /ms-playwright -maxdepth 1 -type d -name "mcp-*" -exec rm -rf {} + 2>/dev/null || true
    echo "[entrypoint] Cleared stale @playwright/mcp lock dirs"
fi

PORT="${PORT:-8000}"
echo "[entrypoint] Starting server on port $PORT…"
exec python3 -m uvicorn server:app --host 0.0.0.0 --port "$PORT"
