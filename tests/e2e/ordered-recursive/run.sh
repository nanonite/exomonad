#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/exomonad-ordered-recursive.XXXXXXXX")"
REPO_DIR="$WORK_DIR/repo"
REMOTE_DIR="$WORK_DIR/remote.git"
MOCK_PID=""
MOCK_URL=""

cleanup() {
    local status=$?
    if [[ -n "$MOCK_PID" ]]; then
        kill "$MOCK_PID" >/dev/null 2>&1 || true
        for _ in $(seq 1 20); do
            if ! kill -0 "$MOCK_PID" >/dev/null 2>&1; then
                break
            fi
            sleep 0.1
        done
        kill -KILL "$MOCK_PID" >/dev/null 2>&1 || true
        wait "$MOCK_PID" >/dev/null 2>&1 || true
    fi
    rm -rf -- "$WORK_DIR"
    return "$status"
}
trap cleanup EXIT

command -v git >/dev/null || { echo "ERROR: git is required" >&2; exit 1; }
command -v tmux >/dev/null || { echo "ERROR: tmux is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 is required" >&2; exit 1; }

if [[ "${EXOMONAD_FORGEJO_E2E_MOCK:-0}" == "1" ]]; then
    git init --bare "$REMOTE_DIR" -q
    git init "$REPO_DIR" -q -b main
    git -C "$REPO_DIR" remote add origin "$REMOTE_DIR"
    git -C "$REPO_DIR" config user.name "ordered-recursive-e2e"
    git -C "$REPO_DIR" config user.email "ordered-recursive-e2e@example.com"
    printf '# Ordered recursive fixture\n' > "$REPO_DIR/README.md"
    git -C "$REPO_DIR" add README.md
    git -C "$REPO_DIR" commit -m "Create ordered recursive fixture" -q
    git -C "$REPO_DIR" push -u origin main -q

    MOCK_PORT="$(python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
    MOCK_LOG="$WORK_DIR/mock.log" REMOTE_DIR="$REMOTE_DIR" \
        python3 "$PROJECT_ROOT/tests/e2e/mock_github.py" --port "$MOCK_PORT" \
        >"$WORK_DIR/mock.stdout" 2>"$WORK_DIR/mock.stderr" &
    MOCK_PID=$!
    MOCK_URL="http://127.0.0.1:$MOCK_PORT"
    for _ in $(seq 1 50); do
        if curl -fsS "$MOCK_URL/api/v1/repos/e2e-owner/e2e-repo/pulls" >/dev/null 2>&1; then
            break
        fi
        sleep 0.1
    done
    FORGEJO_URL="$MOCK_URL"
    FORGEJO_TOKEN="test-token"
    FORGEJO_OWNER="e2e-owner"
    FORGEJO_REPO="e2e-repo"
else
    : "${EXOMONAD_FORGEJO_E2E_URL:?set EXOMONAD_FORGEJO_E2E_URL for a real Forgejo run}"
    : "${EXOMONAD_FORGEJO_E2E_TOKEN:?set EXOMONAD_FORGEJO_E2E_TOKEN for a real Forgejo run}"
    : "${EXOMONAD_FORGEJO_E2E_OWNER:?set the dedicated test repository owner}"
    : "${EXOMONAD_FORGEJO_E2E_REPO:?set the dedicated test repository name}"
    : "${EXOMONAD_FORGEJO_E2E_GIT_REMOTE:?set a pre-authenticated or SSH Git remote for the test repository}"
    FORGEJO_URL="$EXOMONAD_FORGEJO_E2E_URL"
    FORGEJO_TOKEN="$EXOMONAD_FORGEJO_E2E_TOKEN"
    FORGEJO_OWNER="$EXOMONAD_FORGEJO_E2E_OWNER"
    FORGEJO_REPO="$EXOMONAD_FORGEJO_E2E_REPO"
    git clone --quiet "$EXOMONAD_FORGEJO_E2E_GIT_REMOTE" "$REPO_DIR"
    git -C "$REPO_DIR" config user.name "ordered-recursive-e2e"
    git -C "$REPO_DIR" config user.email "ordered-recursive-e2e@example.com"
fi

export ORDERED_E2E_REPO="$REPO_DIR"
export ORDERED_E2E_WORK="$WORK_DIR"
export ORDERED_E2E_FORGEJO_URL="$FORGEJO_URL"
export ORDERED_E2E_FORGEJO_TOKEN="$FORGEJO_TOKEN"
export ORDERED_E2E_FORGEJO_OWNER="$FORGEJO_OWNER"
export ORDERED_E2E_FORGEJO_REPO="$FORGEJO_REPO"
export ORDERED_E2E_FORGEJO_MOCK="${EXOMONAD_FORGEJO_E2E_MOCK:-0}"

PYTHONPATH="$PROJECT_ROOT" python3 "$SCRIPT_DIR/ordered_recursive.py"
