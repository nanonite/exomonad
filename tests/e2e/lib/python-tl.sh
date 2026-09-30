#!/usr/bin/env bash
# Shared assertions for E2E scenarios driven by the Python TL controller.
#
# `exomonad init` no longer launches an interactive Codex root TL. The project
# root controller is `tl_loop`, a bounded Python process that reads
# `.exo/tl-loop/plan.json` and dispatches Codex leaves, workers, and reviewers
# through the ExoMonad MCP tools. Every Codex configuration ExoMonad writes
# therefore belongs to a *dispatched child*, and the root identity
# (`.exo/agents/root`) deliberately has no Codex config at all
# (`codex_lifecycle::provision_codex_agent` is the only writer, and nothing
# provisions `root`).
#
# A validator for that architecture asserts four things, all of them
# observable from a running run:
#
#   1. the controller reached a durable terminal state in its own checkpoint
#      (`.exo/tl-loop/<run>/run.json`), not merely "something was printed";
#   2. the retired interactive-root model left no artefact -- neither
#      `.codex/config.toml` in the project root nor one under the root agent
#      identity;
#   3. each dispatched Codex child got the config its role requires, with the
#      ExoMonad MCP identity, the hooks feature, and the matching role
#      protocol;
#   4. Codex hook trust and project trust landed in the *isolated* CODEX_HOME
#      that was exported before any ExoMonad process started, which is also
#      what proves CODEX_HOME reached the tmux session and every spawned pane.
#
# Source this file from a scenario's `run.sh`/`validate.sh` rather than
# restating the checks: the checks are only meaningful together, because each
# one rules out a specific way the migration could silently regress.

if [[ -n "${E2E_PYTHON_TL_HELPER_LOADED:-}" ]]; then
    return 0
fi
E2E_PYTHON_TL_HELPER_LOADED=1

# Make a freshly scaffolded fixture dispatchable, and commit it.
#
# `spawn_worker` refuses to dispatch into a dirty worktree
# (`ensure_clean_spawn_worktree`), and `exomonad init` itself writes
# `.mcp.json` and `.claude/rules/exomonad.md` after the fixture repository is
# created. `exomonad new` ignores the rest of its runtime state but not those
# two, so a fixture that does not ignore them can never dispatch its first
# worker. Ignoring them is what a real project does: both are regenerated from
# `.exo/config.toml` and are not project state.
e2e_python_tl_commit_scaffold() {
    local repo_dir="$1"
    local message="${2:-Configure ExoMonad fixture for the Python TL controller}"

    for pattern in '.mcp.json' '.claude/rules/exomonad.md'; do
        if ! grep -Fxq "$pattern" "$repo_dir/.gitignore" 2>/dev/null; then
            printf '%s\n' "$pattern" >> "$repo_dir/.gitignore"
        fi
    done

    git -C "$repo_dir" add -A
    git -C "$repo_dir" commit -m "$message" -q
    git -C "$repo_dir" push -q origin HEAD
}

# Relative path of the durable checkpoint the controller writes for a run.
e2e_python_tl_run_state() {
    local repo_dir="$1"
    local run_id="${2:-root}"
    printf '%s\n' "$repo_dir/.exo/tl-loop/$run_id/run.json"
}

# Assert the controller persisted `expected` as its durable phase.
#
# The checkpoint is the authority for "the Python TL reached a state", exactly
# the way `tl.action_*` ledger records are the authority for a merge decision.
# Reading the phase out of a log line would pass for a controller that printed
# a phase and then died before checkpointing it.
e2e_python_tl_assert_phase() {
    local repo_dir="$1"
    local expected="$2"
    local run_id="${3:-root}"
    local state_file
    state_file="$(e2e_python_tl_run_state "$repo_dir" "$run_id")"

    if [[ ! -f "$state_file" ]]; then
        printf '  FAIL: no durable TL checkpoint at %s\n' "$state_file" >&2
        return 1
    fi

    local phase
    phase="$(python3 - "$state_file" <<'PY'
import json
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(state["fsm"]["phase"])
PY
)"
    if [[ "$phase" != "$expected" ]]; then
        printf '  FAIL: TL phase is %s, expected %s (%s)\n' "$phase" "$expected" "$state_file" >&2
        return 1
    fi
    printf '  OK: TL phase %s\n' "$phase"
}

# Assert every named slice is present in the checkpoint.
#
# Presence proves the controller turned each `plan.json` entry into a slice
# before asserting anything about its phase: a controller that silently dropped
# a plan entry could otherwise reach `tl_done` trivially.
e2e_python_tl_assert_slices() {
    local repo_dir="$1"
    shift
    local state_file
    state_file="$(e2e_python_tl_run_state "$repo_dir")"

    python3 - "$state_file" "$@" <<'PY'
import json
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
slices = state.get("slices", {})
missing = [name for name in sys.argv[2:] if name not in slices]
if missing:
    raise SystemExit(f"  FAIL: plan slices missing from the checkpoint: {missing}")
found = len(sys.argv) - 2
print(f"  OK: {found} plan slice(s) present in the checkpoint")
PY
}

# Assert the retired interactive Codex root TL left nothing behind.
#
# Two paths, because both used to exist: the project-root config an interactive
# root Codex session needed, and a config under the root agent identity. The
# Python controller is the root controller, so neither may be generated.
e2e_python_tl_assert_no_codex_root_tl() {
    local repo_dir="$1"
    local retired=(
        "$repo_dir/.codex/config.toml"
        "$repo_dir/.exo/agents/root/.codex/config.toml"
    )
    local found=0
    local config

    for config in "${retired[@]}"; do
        if [[ -f "$config" ]]; then
            printf '  FAIL: retired interactive Codex root TL config exists: %s\n' "$config" >&2
            found=1
        fi
    done

    if (( found != 0 )); then
        return 1
    fi
    printf '  OK: no interactive Codex root TL config was generated\n'
}

# Assert one dispatched Codex child got a role-correct generated config.
#
# Args: <config> <label> <role> <agent-name> <protocol-marker>
#
# The MCP identity and the role protocol are the two properties that make this
# a *Codex child of the Python controller* rather than any Codex process: the
# identity is what routes a child's `notify_parent` back to the controller, and
# the protocol is what tells the model which tools it may use.
e2e_python_tl_assert_codex_child_config() {
    local config="$1"
    local label="$2"
    local role="$3"
    local agent_name="$4"
    local protocol_marker="$5"

    if [[ ! -f "$config" ]]; then
        printf '  FAIL: %s Codex config missing at %s\n' "$label" "$config" >&2
        return 1
    fi

    grep -Fq 'approval_policy = "never"' "$config" \
        || { printf '  FAIL: %s config missing approval_policy\n' "$label" >&2; return 1; }
    grep -Fq 'hooks = true' "$config" \
        || { printf '  FAIL: %s config missing hooks feature\n' "$label" >&2; return 1; }
    grep -Fq '"mcp-stdio"' "$config" \
        || { printf '  FAIL: %s config missing mcp-stdio\n' "$label" >&2; return 1; }
    grep -Fq "$protocol_marker" "$config" \
        || { printf '  FAIL: %s config missing protocol marker %s\n' "$label" "$protocol_marker" >&2; return 1; }

    # The MCP identity is the last check, so its exit status must be the
    # function's: without an explicit `return` the following `printf` would
    # overwrite a failed assertion with success.
    python3 - "$config" "$label" "$role" "$agent_name" <<'PY' || return 1
import sys
import tomllib

config_path, label, role, agent_name = sys.argv[1:5]
with open(config_path, "rb") as config_file:
    config = tomllib.load(config_file)

args = config.get("mcp_servers", {}).get("exomonad", {}).get("args", [])
expected = ["mcp-stdio", "--role", role, "--name", agent_name]
if args != expected:
    raise SystemExit(f"  FAIL: {label} MCP identity is {args}, expected {expected}")
PY

    # Per-agent `hooks.json` is the retired shape; the shared Codex user config
    # carries the hook block now, and a stale per-agent file means the config
    # was written by a superseded code path.
    if [[ -f "$(dirname "$config")/hooks.json" ]]; then
        printf '  FAIL: %s should not carry a per-agent hooks.json\n' "$label" >&2
        return 1
    fi

    printf '  OK: %s Codex config (role=%s name=%s)\n' "$label" "$role" "$agent_name"
}

# Assert Codex project trust and hook trust for one child landed in CODEX_HOME.
#
# Args: <codex-home> <child-config>
#
# The hook-trust key is the child's own generated config path, so finding
# `<config>:pre_tool_use:0:0` under the isolated home proves three things at
# once: the lifecycle wrote the config, it computed trust from those exact
# bytes, and it wrote that trust to the CODEX_HOME this run exported -- which
# is the only way Codex in the spawned pane can load the hooks without the
# "hooks need review" gate.
e2e_python_tl_assert_codex_trust() {
    local codex_home="$1"
    local child_config="$2"
    local user_config="$codex_home/config.toml"

    if [[ ! -f "$user_config" ]]; then
        printf '  FAIL: isolated Codex user config missing at %s\n' "$user_config" >&2
        return 1
    fi

    grep -Fq '# BEGIN EXOMONAD CODEX HOOKS' "$user_config" \
        || { printf '  FAIL: %s missing the ExoMonad hooks block\n' "$user_config" >&2; return 1; }
    grep -Fq 'exomonad hook pre-tool-use --runtime codex' "$user_config" \
        || { printf '  FAIL: %s missing the PreToolUse hook command\n' "$user_config" >&2; return 1; }
    grep -Fq 'exomonad hook post-tool-use --runtime codex' "$user_config" \
        || { printf '  FAIL: %s missing the PostToolUse hook command\n' "$user_config" >&2; return 1; }
    grep -Fq 'exomonad hook stop --runtime codex' "$user_config" \
        || { printf '  FAIL: %s missing the Stop hook command\n' "$user_config" >&2; return 1; }
    grep -Fq "$child_config:pre_tool_use:0:0" "$user_config" \
        || { printf '  FAIL: %s has no trusted hook state for %s\n' "$user_config" "$child_config" >&2; return 1; }

    # Project trust is keyed by the directory the agent runs in, not by the
    # config file, so a child with project trust but no hook trust (or the
    # reverse) is a partial lifecycle write.
    local agent_dir
    agent_dir="$(dirname "$(dirname "$child_config")")"
    grep -Fq "[projects.\"$agent_dir\"]" "$user_config" \
        || { printf '  FAIL: %s has no project trust for %s\n' "$user_config" "$agent_dir" >&2; return 1; }
    grep -Fq 'trust_level = "trusted"' "$user_config" \
        || { printf '  FAIL: %s does not mark the child project trusted\n' "$user_config" >&2; return 1; }

    printf '  OK: Codex project + hook trust in %s\n' "$codex_home"
}

# Assert CODEX_HOME reached the tmux session environment.
#
# `init` propagates CODEX_HOME into the tmux session env; without that, a Codex
# pane spawned into a session whose tmux server was already running falls back
# to `~/.codex` and the run edits the operator's config. Asserting the session
# value is therefore a propagation test, not a restatement of the isolation
# helper.
e2e_python_tl_assert_session_codex_home() {
    local session="$1"
    local expected="$2"
    local actual
    actual="$(tmux show-environment -t "$session" CODEX_HOME 2>/dev/null | tail -n 1)"

    if [[ "$actual" != "$expected" ]]; then
        printf '  FAIL: tmux session %s CODEX_HOME is %s, expected %s\n' \
            "$session" "${actual:-unset}" "$expected" >&2
        return 1
    fi
    printf '  OK: CODEX_HOME propagated into tmux session %s\n' "$session"
}
