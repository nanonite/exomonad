#!/usr/bin/env bash
# Shared helpers for ExoMonad E2E harnesses.
# Source this file from tests/e2e/<name>/run.sh after enabling set -euo pipefail.

if [[ -z "${BASH_VERSION:-}" ]]; then
    echo "ERROR: tests/e2e/lib/harness.sh requires bash." >&2
    exit 1
fi

E2E_HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$E2E_HARNESS_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

E2E_CACHE_ROOT="${E2E_CACHE_ROOT:-$HOME/.cache/exomonad-e2e}"
E2E_SOCKET_WAIT_ATTEMPTS="${E2E_SOCKET_WAIT_ATTEMPTS:-40}"
E2E_SOCKET_WAIT_SECONDS="${E2E_SOCKET_WAIT_SECONDS:-0.5}"

e2e_phase() {
    printf '>>> [%s] %s\n' "$1" "$2"
}

e2e_log() {
    printf '  %s\n' "$*"
}

e2e_fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

e2e_find_exomonad() {
    EXOMONAD_BIN=""
    if [[ -x "$PROJECT_ROOT/target/debug/exomonad" ]]; then
        EXOMONAD_BIN="$PROJECT_ROOT/target/debug/exomonad"
        export PATH="$PROJECT_ROOT/target/debug:$PATH"
    elif command -v exomonad &>/dev/null; then
        EXOMONAD_BIN="$(command -v exomonad)"
    else
        e2e_fail "exomonad binary not found. Run 'just install-all-dev' or 'cargo build -p exomonad'."
    fi
    export EXOMONAD_BIN
    e2e_log "exomonad: $EXOMONAD_BIN"
}

e2e_require_commands() {
    local missing=()
    for cmd in "$@"; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if (( ${#missing[@]} > 0 )); then
        e2e_fail "missing required command(s): ${missing[*]}"
    fi
    e2e_log "$*: OK"
}

e2e_require_wasm() {
    if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
        e2e_fail "No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    fi
    e2e_log "WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"
}

e2e_preflight() {
    e2e_phase "Phase 0" "Checking preconditions..."
    e2e_find_exomonad
    e2e_require_commands "$@"
    e2e_require_wasm
}

e2e_create_work_dir() {
    local name="${1:?test name required}"
    mkdir -p "$E2E_CACHE_ROOT"
    WORK_DIR="$(mktemp -d "$E2E_CACHE_ROOT/$name.XXXXXXXX")"
    REPO_DIR="$WORK_DIR/repo"
    SERVER_LOG="$WORK_DIR/server.log"
    export WORK_DIR REPO_DIR SERVER_LOG
    e2e_log "Work dir: $WORK_DIR"
}

e2e_cleanup() {
    local code=$?
    echo ""
    e2e_phase "Cleanup" "Tearing down..."
    if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
        e2e_log "Stopped exomonad serve"
    fi
    if [[ -n "${RESULT_FILE:-}" && -f "$RESULT_FILE" ]]; then
        e2e_log "Validator result:"
        sed 's/^/    /' "$RESULT_FILE"
    fi
    if [[ -n "${SERVER_LOG:-}" && -f "$SERVER_LOG" ]]; then
        e2e_log "Server log tail:"
        tail -n 20 "$SERVER_LOG" | sed 's/^/    /'
    fi
    if [[ "${KEEP_E2E_WORKDIR:-0}" == "1" ]]; then
        e2e_log "Keeping work dir: ${WORK_DIR:-unset}"
    elif [[ -n "${WORK_DIR:-}" ]]; then
        rm -rf "$WORK_DIR"
        e2e_log "Removed $WORK_DIR"
    fi
    echo ">>> Done."
    exit "$code"
}

e2e_install_cleanup_trap() {
    trap e2e_cleanup EXIT
}

e2e_init_repo() {
    local user_name="${1:-Exomonad E2E}"
    local user_email="${2:-e2e@example.com}"
    mkdir -p "$REPO_DIR"
    cd "$REPO_DIR"
    git init -q -b main
    git config user.name "$user_name"
    git config user.email "$user_email"
    git commit --allow-empty -m "initial commit" -q
}

e2e_run_exomonad_new() {
    if ! "$EXOMONAD_BIN" new 2>&1 | sed 's/^/  /'; then
        e2e_fail "'exomonad new' failed during E2E setup."
    fi
}

e2e_install_project_wasm_and_roles() {
    mkdir -p .exo/wasm
    for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
        ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
    done
    if [[ -d "$PROJECT_ROOT/.exo/roles" ]]; then
        rm -rf .exo/roles
        cp -r "$PROJECT_ROOT/.exo/roles" .exo/roles
    fi
}

e2e_chainlink_init() {
    if ! chainlink init 2>&1 | sed 's/^/  /'; then
        e2e_fail "chainlink init failed during E2E setup."
    fi
}

e2e_write_basic_config() {
    local session="${1:?tmux session required}"
    cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$session"
yolo = true
EOF
}

e2e_start_server() {
    local env_args=("$@")
    env \
        RUST_LOG="${RUST_LOG:-info}" \
        EXOMONAD_HOOK_TRACE="${EXOMONAD_HOOK_TRACE:-1}" \
        "${env_args[@]}" \
        "$EXOMONAD_BIN" serve >"$SERVER_LOG" 2>&1 &
    SERVER_PID=$!
    export SERVER_PID
    e2e_wait_for_server_socket
}

e2e_wait_for_server_socket() {
    for _ in $(seq 1 "$E2E_SOCKET_WAIT_ATTEMPTS"); do
        if [[ -S "$REPO_DIR/.exo/server.sock" ]]; then
            e2e_log "Server socket ready"
            return 0
        fi
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo "ERROR: exomonad serve exited before socket was ready."
            cat "$SERVER_LOG"
            exit 1
        fi
        sleep "$E2E_SOCKET_WAIT_SECONDS"
    done

    echo "ERROR: timed out waiting for .exo/server.sock"
    cat "$SERVER_LOG"
    exit 1
}
