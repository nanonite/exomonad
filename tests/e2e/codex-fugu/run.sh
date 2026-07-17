#!/usr/bin/env bash
set -euo pipefail

# Deterministic Codex-Fugu integration fixture.
# It tests explicit low/medium rejection before side effects and runs a high-only
# init against a fake codex-fugu executable, so CI never needs provider credentials.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
EXOMONAD_BIN="${EXOMONAD_BIN:-$PROJECT_ROOT/target/debug/exomonad}"

if [[ ! -x "$EXOMONAD_BIN" ]]; then
    echo "ERROR: exomonad binary not found at $EXOMONAD_BIN. Run 'cargo build -p exomonad'." >&2
    exit 1
fi
for command in git python3 tmux; do
    command -v "$command" >/dev/null || {
        echo "ERROR: required command not found: $command" >&2
        exit 1
    }
done

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/exomonad-codex-fugu.XXXXXXXX")"
session="e2e-codex-fugu"
fake_bin="$work_dir/bin"
repo_dir="$work_dir/repo"
fugu_log="$work_dir/fugu.log"
old_path="$PATH"

cleanup() {
    local status=$?
    tmux kill-session -t "$session" 2>/dev/null || true
    if [[ -f "$repo_dir/.exo/server.pid" ]]; then
        local pid
        pid="$(python3 - "$repo_dir/.exo/server.pid" <<'PYJSON'
import json
import sys
print(json.loads(open(sys.argv[1]).read())["pid"])
PYJSON
)"
        kill "$pid" 2>/dev/null || true
    fi
    export PATH="$old_path"
    rm -rf "$work_dir"
    exit "$status"
}
trap cleanup EXIT

mkdir -p "$fake_bin" "$repo_dir/.exo/wasm"
cat > "$fake_bin/codex-fugu" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FUGU_LOG:?FUGU_LOG is required}"
if [[ "${1:-}" == "--version" ]]; then
    echo "codex-fugu fake 0.1"
fi
exit 0
EOF
chmod +x "$fake_bin/codex-fugu"
export FUGU_LOG="$fugu_log"
export CODEX_HOME="$work_dir/codex-home"
export PATH="$fake_bin:$PROJECT_ROOT/target/debug:$old_path"
mkdir -p "$CODEX_HOME"

cd "$repo_dir"
git init -q -b main
git config user.name "Exomonad Fugu E2E"
git config user.email "fugu-e2e@example.com"
git commit --allow-empty -m "initial fixture" -q
ln -s "$PROJECT_ROOT/.exo/wasm/wasm-guest-devswarm.wasm" .exo/wasm/wasm-guest-devswarm.wasm

cat > .exo/config.toml <<'EOF'
default_role = "tl"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "e2e-codex-fugu"
root_agent_type = "codex-fugu"
spawn_agent_type = "codex-fugu"
model = "fugu"
tl_effort_level = "high"
worker_effort_level = "high"

[reviewer]
agent_type = "codex-fugu"
model = "fugu-ultra"
effort_level = "high"

[[companions]]
name = "fugu-companion"
agent_type = "codex-fugu"
role = "worker"
command = "codex-fugu"
model = "fugu-ultra"
EOF

run_rejection() {
    local label="$1"
    local expected_flag="$2"
    shift 2
    local output
    local status=0
    output="$($EXOMONAD_BIN init --recreate --tl codex-fugu --worker codex-fugu --reviewer codex-fugu "$@" 2>&1)" || status=$?
    if (( status == 0 )); then
        echo "ERROR: $label unexpectedly succeeded" >&2
        printf '%s\n' "$output" >&2
        exit 1
    fi
    grep -Fq -- "$expected_flag" <<<"$output" || {
        echo "ERROR: $label did not identify $expected_flag" >&2
        printf '%s\n' "$output" >&2
        exit 1
    }
    [[ ! -e .codex/config.toml ]] || { echo "ERROR: $label wrote root Codex config" >&2; exit 1; }
    [[ ! -e .exo/logs/init.jsonl ]] || { echo "ERROR: $label wrote init log" >&2; exit 1; }
    [[ ! -e .exo/server.sock ]] || { echo "ERROR: $label created server socket" >&2; exit 1; }
    [[ ! -s "$fugu_log" ]] || { echo "ERROR: $label ran codex-fugu before validation" >&2; exit 1; }
    echo "OK: $label rejected before side effects"
}

run_rejection "root medium" "--tl-effort-level" --tl-effort-level medium --worker-effort-level high --reviewer-effort-level high
run_rejection "worker medium" "--worker-effort-level" --tl-effort-level high --worker-effort-level medium --reviewer-effort-level high
run_rejection "reviewer medium" "--reviewer-effort-level" --tl-effort-level high --worker-effort-level high --reviewer-effort-level medium
run_rejection "root low" "--tl-effort-level" --tl-effort-level low --worker-effort-level high --reviewer-effort-level high
run_rejection "worker low" "--worker-effort-level" --tl-effort-level high --worker-effort-level low --reviewer-effort-level high
run_rejection "reviewer low" "--reviewer-effort-level" --tl-effort-level high --worker-effort-level high --reviewer-effort-level low
PATH="$PROJECT_ROOT/target/debug:/usr/bin:/bin" run_rejection \
    "missing Fugu executable" "not found on PATH" \
    --tl-effort-level high --worker-effort-level high --reviewer-effort-level high

rm -f "$fugu_log"
init_status=0
output="$($EXOMONAD_BIN init --recreate --tl codex-fugu --worker codex-fugu --reviewer codex-fugu \
    --tl-model fugu --worker-model fugu-ultra --reviewer-model fugu-ultra \
    --tl-effort-level high --worker-effort-level high --reviewer-effort-level high 2>&1)" || init_status=$?
if (( init_status != 0 )); then
    grep -Fq 'open terminal failed: not a terminal' <<<"$output" || {
        printf '%s\n' "$output" >&2
        exit 1
    }
    echo "OK: high-effort init reached expected headless attach boundary"
fi

root_config=.codex/config.toml
companion_config=.exo/agents/fugu-companion/.codex/config.toml
for config in "$root_config" "$companion_config"; do
    [[ -f "$config" ]] || { echo "ERROR: missing Fugu config: $config" >&2; exit 1; }
    grep -Fq 'model_reasoning_effort = "high"' "$config" || {
        echo "ERROR: missing high effort in $config" >&2
        exit 1
    }
    if grep -Fq 'model_reasoning_effort = "medium"' "$config"; then
        echo "ERROR: accidental medium effort in $config" >&2
        exit 1
    fi
done

grep -Fq -- '--version' "$fugu_log" || { echo "ERROR: Fugu PATH preflight was not executed" >&2; exit 1; }
grep -Fq 'model = "fugu-ultra"' "$companion_config" || {
    echo "ERROR: Fugu companion model was not rendered" >&2
    exit 1
}
grep -Fq '"reviewer_agent_type":"codex-fugu"' .exo/logs/init.jsonl || {
    echo "ERROR: resolved reviewer harness missing from init log" >&2
    exit 1
}
python3 - .exo/logs/init.jsonl <<'PYJSON'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as log_file:
    entries = [json.loads(line) for line in log_file if line.strip()]
resolved = entries[-1]["resolved"]
for role in ("tl_effort", "worker_effort", "reviewer_effort"):
    value = resolved[role]
    if value["level"] != "high" or value["source"] != "Cli":
        raise SystemExit(f"unexpected {role}: {value!r}")
PYJSON
if grep -R --include='config.toml' -Fq 'model_reasoning_effort = "medium"' .codex .exo/agents 2>/dev/null; then
    echo "ERROR: generated Fugu runtime state contains medium effort" >&2
    exit 1
fi

echo "PASS: Codex-Fugu role matrix uses high effort and rejects explicit medium before side effects"
