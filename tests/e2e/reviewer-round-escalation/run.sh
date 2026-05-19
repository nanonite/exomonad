#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export E2E_REVIEWER_SCENARIO=round-escalation
exec "$SCRIPT_DIR/../reviewer-convergence-loop/run.sh"
