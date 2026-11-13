#!/usr/bin/env bash
# CI typecheck acceptance (chainlink #1332).
#
# The run owns everything it touches: its own Forgejo, runner, and nested
# dockerd under its own compose project with an ephemeral port, its own scratch
# repository, and its own temporary directory. It copies the seeded job image
# into the nested store and mirrors actions/checkout from that image, so no
# step pulls or builds an image and no step needs the internet.
#
# Pass --keep to leave a failed run's state in place for inspection.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

for required in docker git curl python3; do
    command -v "$required" >/dev/null || {
        echo "ERROR: $required is required for this acceptance" >&2
        exit 1
    }
done

cd "$PROJECT_ROOT"
exec python3 "$SCRIPT_DIR/harness.py" "$@"
