#!/usr/bin/env bash
# Starts the local spindle binary, subscribed to the knot at localhost:5555.
# Start this in a separate terminal before running: just e2e-tangled-ci
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SPINDLE="$PROJECT_ROOT/tangled-core/cmd/spindle/spindle"

if [[ ! -x "$SPINDLE" ]]; then
    echo "ERROR: spindle binary not found at $SPINDLE"
    echo "Build it: cd tangled-core && go build -o cmd/spindle/spindle ./cmd/spindle"
    exit 1
fi

mkdir -p /tmp/spindle-logs

echo "Starting spindle on :6555 (dev mode, knot=localhost:5555)..."
echo "Subscribe URL for exomonad: ws://localhost:6555/events"
echo ""

SPINDLE_SERVER_HOSTNAME=localhost \
SPINDLE_SERVER_LISTEN_ADDR=0.0.0.0:6555 \
SPINDLE_SERVER_DB_PATH="$PROJECT_ROOT/spindle.db" \
SPINDLE_SERVER_OWNER=did:plc:localdev \
SPINDLE_SERVER_DEV=true \
SPINDLE_SERVER_LOG_DIR=/tmp/spindle-logs \
SPINDLE_SERVER_JETSTREAM_ENDPOINT="ws://localhost:5555/events" \
SPINDLE_NIXERY_PIPELINES_NIXERY=nixery.tangled.sh \
SPINDLE_NIXERY_PIPELINES_WORKFLOW_TIMEOUT=30m \
SPINDLE_NIXERY_PIPELINES_MAX_JOB_MEMORY_MB=6144 \
    "$SPINDLE"
