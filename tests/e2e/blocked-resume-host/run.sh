#!/usr/bin/env bash
# Compiled-host acceptance for resume_blocked_leaf recovery boundaries.
#
# The run owns its repository, mock forge, Chainlink database, tmux server
# (always addressed through the harness -S socket), compiled serve processes,
# and the controller used to adopt a launch. A missing binary, WASM, or tmux is
# an explicit SKIP, never a silent pass.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

BINARY="$PROJECT_ROOT/target/debug/exomonad"
WASM="$PROJECT_ROOT/.exo/wasm/wasm-guest-devswarm.wasm"
[ -x "$BINARY" ] || {
    echo "SKIP: build the compiled host first: nix develop -c cargo build -p exomonad" >&2
    exit 77
}
[ -f "$WASM" ] || {
    echo "SKIP: build the devswarm WASM first: just wasm devswarm" >&2
    exit 77
}
command -v tmux >/dev/null || {
    echo "SKIP: tmux is required for the compiled-host acceptance" >&2
    exit 77
}

export EXOMONAD_E2E_BIN="$BINARY"
export EXOMONAD_E2E_WASM="$WASM"

cd "$PROJECT_ROOT"
exec python3 "$SCRIPT_DIR/run.py" "$@"
