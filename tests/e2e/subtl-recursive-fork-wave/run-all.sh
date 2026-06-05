#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for runtime in claude codex opencode; do
    "$SCRIPT_DIR/run.sh" "$runtime"
done
