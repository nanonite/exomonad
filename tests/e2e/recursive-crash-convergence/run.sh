#!/usr/bin/env bash
# Recreated-publication acceptance (chainlink #1117): PR A -> recreate -> PR B.
#
# The run owns everything it touches: its own Forgejo under its own compose
# project with an ephemeral port, its own Chainlink database inside its own
# temporary directory, its own tmux session on its own socket directory, and
# the `exomonad init` it starts from this worktree's build. Nothing outside this
# worktree's build output and this run's temporary directory is read or written,
# and the live Forgejo, the operator's sessions, and the operator's Chainlink
# database are never participants.
#
# Pass --keep to leave a failed run's state in place for inspection.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

for required in git tmux docker python3 chainlink; do
    command -v "$required" >/dev/null || {
        echo "ERROR: $required is required for this acceptance" >&2
        exit 1
    }
done

BINARY="$PROJECT_ROOT/target/debug/exomonad"
WASM="$PROJECT_ROOT/.exo/wasm/wasm-guest-devswarm.wasm"
[ -x "$BINARY" ] || {
    echo "ERROR: build the acceptance binary first: nix develop -c cargo build -p exomonad" >&2
    exit 1
}
[ -f "$WASM" ] || {
    echo "ERROR: build the acceptance WASM first: just wasm devswarm" >&2
    exit 1
}

export EXOMONAD_E2E_BIN="$BINARY"
export EXOMONAD_E2E_WASM="$WASM"

cd "$PROJECT_ROOT"
exec python3 "$SCRIPT_DIR/driver.py" "$@"
