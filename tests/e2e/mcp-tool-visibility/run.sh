#!/usr/bin/env bash
set -euo pipefail

# E2E MCP Tool Visibility Test
# Verifies the live devswarm WASM tool list matches docs/architecture/agent-system.md.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

cd "$PROJECT_ROOT"

echo ">>> [Phase 0] Checking preconditions..."

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-devswarm.wasm &>/dev/null; then
    echo "ERROR: No devswarm WASM plugin found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-devswarm.wasm)"

if [[ ! -f "$PROJECT_ROOT/docs/architecture/agent-system.md" ]]; then
    echo "ERROR: docs/architecture/agent-system.md not found."
    exit 1
fi
echo "  Matrix: docs/architecture/agent-system.md"

echo ">>> [Phase 1] Running matrix assertion..."

cargo test -p exomonad-core --test wasm_integration     mcp_tool_visibility_matrix_matches_live_wasm_tools -- --nocapture
