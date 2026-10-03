#!/usr/bin/env bash
# Crash/restart matrix (chainlink #1057): crash the controller at each of 14
# effect boundaries and prove the resumed run converges.
#
# The run owns everything it touches: its own Forgejo under its own compose
# project with an ephemeral port, one fresh repository per case on it, its own
# Chainlink database inside each case's own directory, its own tmux server per
# case, and the `exomonad serve` built from this worktree. Nothing outside this
# worktree's build output and this run's temporary directory is read or
# written, no operator input is required, and the run fails if anything it
# created outlives it.
#
# Pass --keep to leave a failed run's state in place for inspection, and
# --repetitions 1 for a single diagnostic pass (not the acceptance
# configuration).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

for required in git tmux docker python3 chainlink; do
    command -v "$required" >/dev/null || {
        echo "ERROR: $required is required for this matrix" >&2
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
exec python3 "$SCRIPT_DIR/run.py" "$@"