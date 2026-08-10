#!/usr/bin/env bash
# run.sh: One-command entry point for ExoMonad on any GitHub repo.
#
# Usage:
#   ./try-exomonad/run.sh <github-repo-url>
#
# Auth is provided by mounting credential files from the host (read-only).
# No API keys needed — uses your existing subscription/credentials.
#
# Environment (optional):
#   GITHUB_TOKEN  — enables gh CLI + PR workflows
set -euo pipefail

REPO_URL="${1:?Usage: ./try-exomonad/run.sh <github-repo-url>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$ROOT_DIR"

# ---- Build volume mount args ----
# Full directory mounts (rw) — Claude Code writes settings, session history,
# and projects metadata into ~/.claude/. The binary is at ~/.local/bin/claude
# (outside ~/.claude/) so the mount doesn't clobber it.
mkdir -p "$HOME/.claude" "$HOME/.codex"

MOUNTS=(
    -v "$HOME/.claude:/home/exo/.claude"
    -v "$HOME/.codex:/home/exo/.codex"
)

# Claude state file (auth tokens, onboarding flags, trust dialog state).
# Must be rw — claude writes to this after trust dialog acceptance and hangs if read-only.
if [ -f "$HOME/.claude.json" ]; then
    MOUNTS+=(-v "$HOME/.claude.json:/home/exo/.claude.json")
fi

# ---- Build Docker image ----
echo "Building Docker image (cached after first run)..."
# Use a BuildKit builder with explicit public DNS (avoids issues with
# Tailscale/VPN DNS not being available inside BuildKit containers).
if ! docker buildx inspect exomonad-builder >/dev/null 2>&1; then
    buildkit_config=$(mktemp)
    printf '[dns]\n  nameservers = ["1.1.1.1", "8.8.8.8"]\n' > "$buildkit_config"
    docker buildx create --name exomonad-builder --driver docker-container --config "$buildkit_config"
    rm "$buildkit_config"
fi
docker buildx build --builder exomonad-builder --load -t exomonad-try -f try-exomonad/Dockerfile .

# ---- Launch container ----
echo "Launching container with $REPO_URL ..."
docker run -it --rm \
    "${MOUNTS[@]}" \
    -e GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
    exomonad-try \
    bash -c "
        git clone '$REPO_URL' /workspace/project && \
        exomonad-bootstrap /workspace/project
    "
