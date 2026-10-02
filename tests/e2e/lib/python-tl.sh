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
# These expectations are transcribed from a real provisioned run, and the same
# shape is pinned against live product output by
# `codex_lifecycle::provisioning_writes_hook_commands_into_the_config_not_the_user_config`.
# A previous version of this file asserted a global ExoMonad hooks block in the
# Codex user config; that writer was removed in 8934378f (#210), so the
# assertion failed every live run. A helper that encodes a shape the product
# does not write is worse than no assertion at all, because it turns a working
# run red and reads as a product defect.
#
# Source this file from a scenario's `run.sh`/`validate.sh` rather than
# restating the checks: the checks are only meaningful together, because each
# one rules out a specific way the migration could silently regress.

if [[ -n "${E2E_PYTHON_TL_HELPER_LOADED:-}" ]]; then
    return 0
fi
E2E_PYTHON_TL_HELPER_LOADED=1

# Point this run at its own tmux server, so the server's captured environment is
# this run's.
#
# `exomonad init` hands `CODEX_HOME` to `tmux new-session` itself, as
# `-e CODEX_HOME=<path>` (rust/exomonad-core/src/services/tmux_ipc.rs), because
# tmux snapshots a pane's environment when it spawns the pane's process. A value
# written afterwards reaches every window created later and never the window
# `new-session` already created -- the one `init` renames to `Server` and runs
# `exomonad serve` in, whose spawned agents then seed no hook trust. `init`
# verifies the spawned pane's own environment rather than the session's, so a run
# cannot report the right value while resolving a different one.
#
# This helper keeps a run off the host's shared server for the rest of the run's
# tmux traffic as well: its sessions, its harness teardown, and the validator
# companion all reach one server the run owns rather than one another workspace,
# a parallel scenario, or a developer may already be using. It is the same
# isolation `tests/e2e/lib/e2e_harness/tmuxio.py` gives the Python acceptances
# through `TMUX_TMPDIR`.
#
# Must run before the first `tmux` call in the script, and before `init`; every
# `tmux` call in the run inherits TMUX_TMPDIR, so harness cleanup and the
# validator companion all reach this one server.
e2e_python_tl_isolate_tmux_server() {
    local work_dir="$1"

    if [[ "${E2E_PYTHON_TL_TMUX_ISOLATION:-1}" == "0" ]]; then
        printf '  SKIP: tmux server isolation disabled by E2E_PYTHON_TL_TMUX_ISOLATION=0\n'
        return 0
    fi
    export TMUX_TMPDIR="$work_dir/tmux"
    mkdir -p "$TMUX_TMPDIR"
    printf '  OK: tmux server isolated to %s\n' "$TMUX_TMPDIR"
}

# The run's declared token budget, from the shipped plan.
e2e_python_tl_plan_token_budget() {
    local plan_path="$1"

    python3 - "$plan_path" <<'PY'
import json
import sys
from pathlib import Path

print(int(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["budgets"]["tokens"]))
PY
}

# Write a harness policy whose role ceilings sit above the run's token budget.
#
# The controller's selector conservatively attributes a role's whole share of
# the run budget to that role whenever no per-role spend has been recorded yet
# (`tl_loop/select/agent_type.py::_spent`, the `BudgetLedger` branch), so the
# first dispatch of a run fits only when
# `budgets.tokens + estimated_cost <= roles.<role>.token_budget`. With
# `exomonad new`'s scaffold (worker ceiling 120000) and any plan declaring a
# 120000-token run, that is false and the slice parks with `over_budget` before
# an agent is ever spawned.
#
# The ceiling is therefore derived from the plan rather than left to the
# scaffold, and the relationship is asserted by the contract tests. This is a
# property of the shipped selector, not a choice these scenarios make about how
# much to spend: the run still stops at the budget its plan declares.
# Report a fixture-setup failure the same way the assertions do, so a run that
# cannot even start says why instead of failing somewhere downstream with a
# symptom.
e2e_python_tl_fail() {
    printf '  FAIL: %s\n' "$1" >&2
    return 1
}

e2e_python_tl_codex_model() {
    # The model these fixtures provision their Codex worker with.
    #
    # The harness policy key is `codex/<model>`, and the controller splits it
    # into agent type plus model, which becomes `model = ...` in the generated
    # child config. So the key is what decides which model the worker asks for.
    #
    # It must therefore be a model the account can actually run. `gpt-luna`, the
    # name in the `exomonad new` scaffold, is not one of them for a ChatGPT
    # login: the worker gets
    #
    #   400 invalid_request_error: The 'gpt-luna' model is not supported when
    #   using Codex with a ChatGPT account.
    #
    # before its first inference, so it never reaches a tool call. The default
    # is read from the host Codex config -- the same place the operator's own
    # working `codex` invocation gets its model -- so the fixtures follow the
    # account instead of hard-coding a name that can rot. Chainlink #1149.
    local host_config model
    if [[ -n "${E2E_CODEX_MODEL:-}" ]]; then
        model="$E2E_CODEX_MODEL"
    else
        host_config="${CODEX_HOST_CONFIG:-$HOME/.codex/config.toml}"
        if [[ ! -f "$host_config" ]]; then
            e2e_python_tl_fail "cannot resolve a Codex model: no host config at $host_config. Set E2E_CODEX_MODEL to a model your account can run."
            return 1
        fi
        model="$(sed -n 's/^[[:space:]]*model[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$host_config" | head -1)"
        if [[ -z "$model" ]]; then
            e2e_python_tl_fail "cannot resolve a Codex model: $host_config sets no top-level model. Set E2E_CODEX_MODEL to a model your account can run."
            return 1
        fi
    fi

    if [[ "$model" == "gpt-luna" ]]; then
        e2e_python_tl_fail "the model 'gpt-luna' comes from the exomonad new scaffold and is rejected by a ChatGPT-account Codex login (400 invalid_request_error). Set E2E_CODEX_MODEL to a model your account can run. See Chainlink #1149."
        return 1
    fi
    printf '%s' "$model"
}

# The harness identifier the fixture's policy and capability map both name.
#
# `agent_type/model`, because that is the shape the controller splits
# (`parse_harness_identifier`): the model half becomes `model = ...` in the
# generated child config, and the whole string is the key both
# `harness_policy.toml` and `harness_capability.toml` are written in.
e2e_python_tl_harness() {
    # The inner failure has to be re-raised, not just interpolated away.
    # `printf 'codex/%s' "$(e2e_python_tl_codex_model)"` exits 0 even when the
    # resolution failed, because `printf` succeeds on an empty argument -- so
    # the caller saw `codex/` as a valid harness and wrote a policy whose
    # allowlist was `["codex/"]`. The run then failed downstream on a harness
    # key nothing can dispatch, which is the opposite of failing closed.
    local model
    model="$(e2e_python_tl_codex_model)" || return 1
    printf 'codex/%s' "$model"
}

# Prove the account can run the model before the run starts.
#
# A model rejection surfaces inside the worker's rollout as a `task_complete`
# carrying a 400, after the controller has already dispatched, provisioned and
# booted the worker. Nothing in the harness notices for the validator's whole
# 600s budget, so the run looks like a `notify_parent` stall and sends whoever
# reads it to the wrong bug. One throwaway turn here turns a ten-minute
# ambiguity into a five-second message.
e2e_python_tl_assert_codex_model_runnable() {
    local model="$1"
    local output status
    set +e
    output="$(cd "$2" 2>/dev/null || cd "$HOME"; \
        CODEX_HOME="${CODEX_HOME:?}" timeout 120 codex exec \
            --model "$model" --skip-git-repo-check \
            "Reply with the single word OK." 2>&1)"
    status=$?
    set -e

    if [[ "$output" == *"not supported when using Codex"* ]]; then
        e2e_python_tl_fail "the account cannot run model '$model': $(printf '%s' "$output" | grep -oE "The '[^']*' model is not supported[^\\\\\"]*" | head -1). Set E2E_CODEX_MODEL to a model your account can run. See Chainlink #1149."
        return 1
    fi
    if (( status != 0 )); then
        e2e_python_tl_fail "codex could not run model '$model' (exit $status): $(printf '%s' "$output" | tail -2 | tr '\n' ' ' | cut -c1-200)"
        return 1
    fi
    printf '  OK: account can run model %s\n' "$model"
}

e2e_python_tl_write_harness_policy() {
    local repo_dir="$1"
    local plan_path="$2"
    local run_tokens ceiling
    run_tokens="$(e2e_python_tl_plan_token_budget "$plan_path")"
    # Four times the run budget: comfortably above the conservative attribution
    # above while still a finite ceiling, and derived rather than magic.
    ceiling=$((run_tokens * 4))
    # The harness is resolved once and used for both files the controller reads.
    # They have to agree: `_require_policy_coverage` (tl_loop/select/capability.py)
    # rejects a run whose policy allows a harness the capability map has no entry
    # for, so writing the policy without the matching rating fails preflight with
    # `missing capability entry for codex/<model>`. Keeping the two writes in one
    # function is what makes that impossible to half-do.
    local harness
    harness="$(e2e_python_tl_harness)" || return 1
    mkdir -p "$repo_dir/.exo"

    cat > "$repo_dir/.exo/harness_capability.toml" <<EOF
# Fixture capability ratings for the Python TL controller scenarios.
#
# One entry per harness the policy allows. \`_require_policy_coverage\` requires
# the policy's allowlists to be a subset of these keys, so this file and
# harness_policy.toml must be written together with the same resolved harness.

[capabilities]
# Basis: the account-resolved Codex worker model; these fixtures dispatch
# bounded, single-slice plans, which is what a standard rating is for.
"$harness" = "standard"
EOF

    cat > "$repo_dir/.exo/harness_policy.toml" <<EOF
# Fixture harness policy for the Python TL controller scenarios.
#
# The controller attributes a role's whole share of the run budget to that role
# until it has recorded per-role spend, so each role ceiling is set above the
# run's declared token budget. See tests/e2e/lib/python-tl.sh for the full
# explanation; the run still stops at the budget its plan declares.
#
# The harness is resolved from the account rather than hard-coded. See
# e2e_python_tl_codex_model.


[roles.tl]
allow = ["$harness"]
cost_rank = { "$harness" = 1 }
token_budget = $ceiling
escalate_after_attempts = 1

[roles.worker]
allow = ["$harness"]
cost_rank = { "$harness" = 1 }
token_budget = $ceiling
per_harness_budget = { "$harness" = $ceiling }
escalate_after_attempts = 1

[roles.reviewer]
allow = ["$harness"]
cost_rank = { "$harness" = 1 }
token_budget = $ceiling
escalate_after_attempts = 1
EOF
    printf '  OK: harness policy written with role ceilings of %s (run budget %s)\n' \
        "$ceiling" "$run_tokens"
}

# Give the fixture a remote the controller can resolve, and a push target that
# actually works.
#
# `repository_identity` refuses a local-path remote
# (`rust/exomonad-core/src/services/repo.rs`: "Remote is a local path"), and the
# controller resolves identity during startup, so a bare local `origin` stops
# the run before a child is dispatched. The retired interactive-root scenarios
# never started a controller and never had to satisfy this.
#
# So `origin` carries an HTTP URL for identity resolution while pushes go to a
# local bare repository, which keeps the scenario hermetic and local-only: no
# Forgejo, no credentials, no network. This is the same split
# `tests/e2e/one-shot-lifecycle/run.sh` uses.
e2e_python_tl_configure_remote() {
    local repo_dir="$1"
    local bare_remote="$2"
    local slug="$3"
    local base_branch="${4:-main}"

    git -C "$repo_dir" remote remove origin >/dev/null 2>&1 || true
    git -C "$repo_dir" remote add origin "http://127.0.0.1:1/e2e/$slug"
    git -C "$repo_dir" remote set-url --push origin "$bare_remote"
    git -C "$repo_dir" push -q -u origin "$base_branch"
}

# Create the fixture's Chainlink database before the controller starts.
#
# The controller opens the project's Chainlink database during startup, not
# lazily: without one it exits with `unable to open database file` and
# `exomonad init` reports the TL window exiting before startup completed. The
# retired interactive-root scenarios never started a controller, so none of them
# needed a database, and this is the failure that only a live run surfaces.
#
# Must run before `e2e_python_tl_commit_scaffold`, so the files `chainlink init`
# creates (`.claude/` hooks, `.chainlink/rules/`) are committed rather than
# leaving the worktree dirty for `spawn_worker` to refuse.
e2e_python_tl_init_chainlink() {
    local repo_dir="$1"

    if ! (cd "$repo_dir" && CHAINLINK_DB="$repo_dir/.chainlink" chainlink init >/dev/null 2>&1); then
        printf 'ERROR: chainlink init failed in %s\n' "$repo_dir" >&2
        return 1
    fi
    if [[ ! -d "$repo_dir/.chainlink" ]]; then
        printf 'ERROR: chainlink init created no .chainlink directory in %s\n' "$repo_dir" >&2
        return 1
    fi
    printf '  OK: Chainlink database initialised at %s/.chainlink\n' "$repo_dir"
}

# Plan slice names for a scenario's shipped plan.json.
#
# These are the `plan.json` `workers`/`leaves` names, which is what the
# controller keys `run.json`'s `slices` by. They are deliberately *not* the
# agent identities: `_initial_slices` keys on the plan name, while the agent
# identity adds the harness suffix (`<plan name>-codex`) and is only used for
# the agent directory. Reading the plan is what keeps a validator from
# asserting one and finding the other.
e2e_python_tl_plan_slices() {
    local plan_path="$1"

    python3 - "$plan_path" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["plan"]
for section in ("workers", "leaves", "sub_tls"):
    for entry in plan.get(section) or ():
        print(entry["name"])
PY
}

# The Codex agent identity the controller dispatches a plan slice under.
e2e_python_tl_agent_identity() {
    local slice_name="$1"
    local harness="${2:-codex}"
    printf '%s-%s\n' "$slice_name" "$harness"
}

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

    # The hook commands live in the generated config, not in the Codex user
    # config. `render_codex_config` emits one per event, and
    # `install_codex_hook_trust` derives its trust entries from exactly these
    # three, so asserting them here is what makes the trust assertion in
    # `e2e_python_tl_assert_codex_trust` meaningful.
    #
    # The command is rendered with the absolute path of the running `exomonad`
    # binary, so only the stable tail is matched -- a bare `exomonad hook ...`
    # pattern would fail on every host that does not build to `target/debug`.
    local event
    for event in pre-tool-use post-tool-use stop; do
        grep -Fq "hook $event --runtime codex" "$config" \
            || {
                printf '  FAIL: %s config missing the %s hook command\n' "$label" "$event" >&2
                return 1
            }
    done

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
# What the product actually writes, and therefore what is asserted here:
#
#   * `provision_codex_agent` renders the hook commands into the *generated
#     child config* (`<agent_dir>/.codex/config.toml`).
#   * `trust_codex_project` writes `[projects."<agent_dir>"] trust_level =
#     "trusted"` into the Codex user config, and strips any legacy
#     `# BEGIN EXOMONAD CODEX HOOKS` block it finds there.
#   * `install_codex_hook_trust` writes one
#     `[hooks.state."<child config>:<event>:0:0"] trusted_hash = ...` per hook
#     event into the same user config, keyed by the generated config's path.
#
# The hook-trust key being the child's own config path is what makes this
# meaningful: it proves trust was computed from the bytes just written and
# recorded in the CODEX_HOME this run exported, which is the only way Codex in
# the spawned pane loads those hooks without the "hooks need review" gate.
#
# There is deliberately no assertion for hook *commands* in the user config.
# A global hooks block used to live there and was removed in 8934378f (#210);
# asserting it would fail every run against a shape the product no longer
# writes, and a reappearing block is itself a defect -- so its absence is
# asserted instead.
e2e_python_tl_assert_codex_trust() {
    local codex_home="$1"
    local child_config="$2"
    local user_config="$codex_home/config.toml"
    local agent_dir
    agent_dir="$(dirname "$(dirname "$child_config")")"

    if [[ ! -f "$user_config" ]]; then
        printf '  FAIL: isolated Codex user config missing at %s\n' "$user_config" >&2
        return 1
    fi

    # The three hook events `render_codex_config` emits, and therefore the three
    # trust entries `install_codex_hook_trust` derives from them.
    local event
    for event in pre_tool_use post_tool_use stop; do
        grep -Fq "[hooks.state.\"$child_config:$event:0:0\"]" "$user_config" \
            || {
                printf '  FAIL: %s has no trusted hook state for %s:%s\n' \
                    "$user_config" "$child_config" "$event" >&2
                return 1
            }
    done

    # A trust entry without a hash proves nothing: Codex keys the trust decision
    # on the hash, so an entry without one is not a trusted entry.
    python3 - "$user_config" "$child_config" <<'PY' || return 1
import sys
import tomllib

user_config, child_config = sys.argv[1:3]
with open(user_config, "rb") as handle:
    config = tomllib.load(handle)

state = config.get("hooks", {}).get("state", {})
for event in ("pre_tool_use", "post_tool_use", "stop"):
    entry = state.get(f"{child_config}:{event}:0:0")
    if not isinstance(entry, dict) or not entry.get("trusted_hash"):
        raise SystemExit(
            f"  FAIL: {user_config} has no trusted_hash for {child_config}:{event}:0:0"
        )
PY

    # Project trust is keyed by the directory the agent runs in, not by the
    # config file, so a child with project trust but no hook trust (or the
    # reverse) is a partial lifecycle write.
    grep -Fq "[projects.\"$agent_dir\"]" "$user_config" \
        || {
            printf '  FAIL: %s has no project trust for %s\n' "$user_config" "$agent_dir" >&2
            return 1
        }
    grep -Fq 'trust_level = "trusted"' "$user_config" \
        || {
            printf '  FAIL: %s does not mark the child project trusted\n' "$user_config" >&2
            return 1
        }

    # The retired global hooks block. `trust_codex_project` strips it on every
    # write, so finding one means the user config was edited by a superseded
    # path and Codex would load hooks ExoMonad never hashed.
    if grep -Fq '# BEGIN EXOMONAD CODEX HOOKS' "$user_config"; then
        printf '  FAIL: %s still carries the retired global ExoMonad hooks block\n' \
            "$user_config" >&2
        return 1
    fi

    printf '  OK: Codex project + hook trust in %s\n' "$codex_home"
}

# Assert CODEX_HOME reached the tmux session environment.
#
# `init` hands CODEX_HOME to `tmux new-session` as `-e CODEX_HOME=<path>`, so the
# session carries it from creation and every window created after that inherits it
# from the session. Asserting the session value is therefore a propagation test, not
# a restatement of the isolation helper.
#
# It is not, on its own, proof that a pane sees that home: `show-environment`
# answers for the session and the window, and a value written into either after a
# pane was spawned still reads back. `e2e_python_tl_assert_codex_trust` closes that
# gap from the other end -- the trust entries only exist if a live Codex process
# resolved the run's home -- and `init` itself refuses to start when the spawned
# pane's own `/proc/<pid>/environ` disagrees.
#
# `tmux show-environment` prints `NAME=value`, and prefixes the name with `-`
# when the variable is *unset* in the session. Both are stripped here: comparing
# the whole line to the bare path fails against a correctly propagated session,
# and an unset variable has to be a failure rather than an empty match.
e2e_python_tl_assert_session_codex_home() {
    local session="$1"
    local expected="$2"
    local line value
    line="$(tmux show-environment -t "$session" CODEX_HOME 2>/dev/null | tail -n 1)"

    if [[ -z "$line" ]]; then
        printf '  FAIL: tmux session %s has no CODEX_HOME entry\n' "$session" >&2
        return 1
    fi
    if [[ "${line:0:1}" == "-" ]]; then
        printf '  FAIL: tmux session %s has CODEX_HOME explicitly unset\n' "$session" >&2
        return 1
    fi
    value="${line#CODEX_HOME=}"

    if [[ "$value" != "$expected" ]]; then
        printf '  FAIL: tmux session %s CODEX_HOME is %s, expected %s\n' \
            "$session" "$value" "$expected" >&2
        return 1
    fi
    printf '  OK: CODEX_HOME propagated into tmux session %s\n' "$session"
}
