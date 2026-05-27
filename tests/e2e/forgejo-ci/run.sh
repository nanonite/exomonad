#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
FORGEJO_DIR="$ROOT_DIR/forgejo"
WORK_DIR="$ROOT_DIR/.e2e-work/forgejo-ci"
LOG_DIR="$WORK_DIR/logs"
mkdir -p "$WORK_DIR" "$LOG_DIR"

cd "$FORGEJO_DIR"
docker compose up -d

# Best-effort readiness probe
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:3000/api/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

cd "$WORK_DIR"
rm -rf workspace
mkdir -p workspace
cd workspace

git init >/dev/null

echo "# forgejo-ci-e2e" > README.md
git add README.md
git commit -m "init" >/dev/null

export EXOMONAD_FORGEJO_URL="http://127.0.0.1:3000"
export EXOMONAD_FORGEJO_TOKEN="${EXOMONAD_FORGEJO_TOKEN:-}"
export EXOMONAD_FORGEJO_WEBHOOK_SECRET="${EXOMONAD_FORGEJO_WEBHOOK_SECRET:-e2e-secret}"
export EXOMONAD_SERVER_URL="http://127.0.0.1:3001"

if [[ -z "${EXOMONAD_FORGEJO_TOKEN}" ]]; then
  echo "ERROR: EXOMONAD_FORGEJO_TOKEN is required for forgejo-ci e2e"
  exit 1
fi

exomonad new > "$LOG_DIR/new.log" 2>&1

if [[ ! -f .forgejo/workflows/ci.yml ]]; then
  echo "ERROR: .forgejo/workflows/ci.yml was not generated"
  exit 1
fi

if ! git remote | grep -q '^forgejo$'; then
  echo "ERROR: forgejo git remote was not configured"
  exit 1
fi

# Minimal smoke for webhook endpoint wiring expectations in this migration stage.
if ! grep -q '/ci' "$LOG_DIR/new.log" && ! grep -q 'Forgejo repo registration skipped' "$LOG_DIR/new.log"; then
  echo "WARN: no explicit webhook registration signal in new.log"
fi

echo "forgejo-ci e2e scaffold checks passed"
