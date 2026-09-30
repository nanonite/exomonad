# E2E Status

Last updated: 2026-09-30

This document tracks the high-signal E2E coverage needed before continuing the Codex and runtime-role test work. Use `just` targets as the test entrypoint unless a row explicitly says it is a planning-only release gate.

## Current Tests

| Test | Target | Scope | Last observed result | Status | Notes / next action |
| --- | --- | --- | --- | --- | --- |
| Codex home isolation | `just check-e2e-codex-home-isolation` | Every e2e that can generate Codex configuration uses a per-run `CODEX_HOME` beneath its work dir; the host `~/.codex/config.toml` is proven byte-for-byte unchanged; `KEEP_E2E_WORKDIR=1` still preserves isolated state | Added under Chainlink #1122. `101 passed`. Covers the shell and Python helpers, the sentinel (including that it *fails* on a tampered host config), and the wiring for every Codex-generating harness. | Green | No live scenario: the property is the *absence* of a host side effect, so a real Codex run cannot demonstrate it better than it can demonstrate that a `sleep` returned. `e2e-codex-messaging`, `e2e-python-tl-worker-notify`, `e2e-codex-reviewer-sandbox`, and the real-server acceptances are the live coverage that exercises it. |
| Codex messaging | `just e2e-codex-messaging`; `just check-e2e-codex-messaging`; `just check-e2e-python-tl-controller` | Python TL controller consumes `plan.json` and dispatches a Codex worker that reaches a Codex companion peer with `send_tmux_message` and reports to the controller with `notify_parent`; both Codex agents get role-correct configs and trusted hooks in the isolated `CODEX_HOME`; the run reaches `tl_done` | Migrated under Chainlink #1127. `just check-e2e-python-tl-controller` passes (129 tests). **Not run live in the migrated form.** A live run of the sibling worker-notify scenario reached a dispatched, fully provisioned Codex worker, so the fixture is known to start the controller, select a harness, and dispatch; this row's own end-to-end result is unproven. A run whose validator reports a failure -- or reports nothing -- now fails the scenario, so this row cannot read Green by accident. | Blocked | No interactive Codex root TL: `init` always starts the Python controller, and `initial_prompt`, if set, must be a JSON WorkPlan. The retired project-root `.codex/config.toml` assertions were removed; the validator now asserts that no such config is generated. The peer is a companion rather than a second plan worker because `spawn_worker` refuses a second worker in the same parent window. |
| Chainlink role tool scope | `just test-wasm-integration` | Static WASM assertions for Chainlink MCP tool exposure by role: TL, dev, worker, root, reviewer, testrunner | Passed: `30 passed` before sqlite hook additions; later full run passed `32 passed`. | Green | Added under Chainlink #172. This pins the role contract before Codex Chainlink MCP E2E work. |
| Chainlink sqlite block | `just e2e-chainlink-sqlite-block` | PreToolUse denies direct `.chainlink/issues.db` access for Claude-shaped, Codex-shaped, and OpenCode-shaped hook invocations | Passed. Runtime probes denied all three payload shapes and the fake `sqlite3` marker was absent. Static preflight `just check-e2e-chainlink-sqlite-block` also passed. | Green | Added under Chainlink #174. Uses `/tmp` temp repo/server only and validates hook trace logs for all three runtimes. |
| Chainlink Codex flow | `just e2e-chainlink-codex`; `just check-e2e-chainlink-codex`; `just check-e2e-python-tl-controller` | Python TL controller consumes `plan.json` and dispatches a Codex worker that runs the role-scoped Chainlink session workflow (`chainlink_session_start`, `chainlink_session_work`, `chainlink_issue_comment`, `chainlink_session_end`, `notify_parent`) on an issue owned by the harness | Migrated under Chainlink #1127. `just check-e2e-python-tl-controller` passes (129 tests), including the worker-role tool preflight that replaces the retired TL/root check, the doc/helper agreement test, and the tmux-isolation placement contract. **Not run live in the migrated form.** | Blocked | The ownership assertion is now negative and stronger: the worker role is granted neither `chainlink_issue_create` nor `chainlink_issue_close`, so the issue must still be open afterwards and no `.chainlink/.locks-cache` worktree may exist. Chainlink agent/sync/lock tools remain out of the role workflow. |
| Chainlink timer role scope | `just check-e2e-chainlink-timer-role-scope` | Static role-scope assertions for TL-only timer tools, coordinator close semantics, dev subissue close, worker telemetry-only tools, and no lock/agent/sync role exposure. | Passed after the Chainlink timer/role-scope refactor. Final preflight also paired this with `just check-e2e-chainlink-codex`, `bash -n tests/e2e/chainlink/run.sh`, and `bash -n tests/e2e/chainlink-close/run.sh`. | Green | Added under Chainlink #196. Keep this cheap preflight paired with `just test-wasm-integration` for Chainlink MCP surface changes. |
| Claude-only bounded smoke | `just e2e-claude-only` / `just check-e2e-claude-only` | Claude Code root TL on Haiku with explicit role-safe `initial_prompt`; validates server startup, root SessionStart registration, TeamCreate, and Teams metadata registration without spawning children | Passed on 2026-05-27. Harness used `port = 0`, pretrusted the temp workspace, observed root Claude session registration, TeamCreate, new Teams directory, and `Registered team: exomonad-smoke-test`. | Green | This is intentionally bounded to root TL startup/Teams registration. Full Claude TL/worker/dev-leaf/reviewer matrix remains tracked by #421 to avoid unbounded token use. |
| Python TL worker notify | `just e2e-python-tl-worker-notify`; `just check-e2e-python-tl-worker-notify`; `just check-e2e-python-tl-controller` | The Python TL controller reads `plan.json`, dispatches a Codex worker into a pane of its own `TL` window, and the worker's `notify_parent` reaches that window | Added under Chainlink #1127. `just check-e2e-python-tl-controller` passes (134 tests). A **live run** (2026-09-30) drove the controller through plan load, harness selection, dispatch, and Codex provisioning. Ten validator assertions pass against real product output, including `Codex worker trust is in the isolated home` -- three `hooks.state` entries with a `trusted_hash`, project trust, no retired global hooks block, and the live `codex` process carrying the run's `CODEX_HOME`. One fails, and the first diagnosis of it was wrong: the stall was not a missing `notify_parent`. The worker's rollout held nine events, ended 5.4s in with a 400 rejecting the provisioned `gpt-luna` model, and made zero tool calls -- see #1149. With that fixed, the worker completes its turn and both MCP tools return `success: true`, but the controller quarantines its own `agent.spawned` event as `integrity_conflict` / `intent_mismatch` and parks the slice in `spawned`. That is #1148. | Blocked | Renamed from the sub-TL worker notify scenario: there is no sub-TL in the shipped architecture, the root controller is the Python process. The retired "active pane is not pane 0" assertion was dropped; see the scenario's `e2e-test.md` for why. |
| Codex reviewer sandbox consistency | `just e2e-codex-reviewer-sandbox` | The real worktree watcher auto-spawns a Codex reviewer for an observed PR and the generated config's sandbox profile and verdict instructions agree | Unchanged by #1127. It drives `exomonad serve` and the watcher rather than an interactive root agent, so it already describes the shipped architecture. | Green | Review coverage is intentionally left here: the migrated scenarios dispatch workers, and the reviewer shape is proven by the watcher-driven path. |
| Runtime-role matrix | `docs/architecture/runtime-role-e2e-matrix.md` | Release-gate matrix for TL/dev/reviewer/worker coverage across Claude Code, Codex, and OpenCode | Defined under Chainlink #171. | Planned | Regular development gates cover static contracts and local Codex/OpenCode paths; release-only gates cover Claude credit-burning and Forgejo reviewer provenance flows. |

## Blockers (found by a live run, Chainlink #1127)

One remains, and it belongs to the product rather than to the harnesses.

| Blocker | Effect | Status |
| --- | --- | --- |
| #1147: `init` propagates `CODEX_HOME` *after* creating the session's first window | When a tmux server is already running, the window `init` renames to `Server` keeps the existing server's captured `CODEX_HOME`. The session environment then reads correctly, so a `tmux show-environment` check passes, while the server and every agent it spawns resolve a different Codex home and no hook trust is seeded. | **Product defect, worked around in the harness, not fixed here.** The harnesses give each run its own tmux server via `e2e_python_tl_isolate_tmux_server` (`TMUX_TMPDIR` under the work dir), which removes the foreign environment; a live run with that in place reached the controller with the worker on the isolated home. The ordering in `init.rs` is still wrong and wants its own issue. Set `E2E_PYTHON_TL_TMUX_ISOLATION=0` to reproduce the leak. |
| #1149: the fixtures and the `exomonad new` scaffold provision a model the account cannot run | The worker gets `400 invalid_request_error` on its first inference and never makes a tool call, so every live Codex scenario stalls for the validator's whole budget and reads as a messaging failure. | **Worked around in the fixtures; the scaffold default is unchanged.** The model is now resolved from the host Codex config, `gpt-luna` is refused outright, and one throwaway `codex exec` proves the account can run the resolved model before `init` starts -- which turned a 600s ambiguous stall into a 5s message naming the model, and surfaced the policy/capability-map mismatch within ninety seconds. The scaffold in `rust/exomonad/src/new.rs` still defaults to `codex/gpt-luna`, which is the part that needs the product fix. |
| #1148: the controller quarantines the slice's own dispatch confirmation | The slice never leaves `spawned` and the run stays at `tl_running`, so a worker that did its whole job correctly still does not finish. | **Open, and the only blocker.** The action journal ends with `tl.dispatch_event_rejected` (`integrity_conflict` / `intent_mismatch`), and `event-quarantine.json` holds the `agent.spawned` event with `run_id` a UUID where the controller has the literal `root`, and `agent_id` `root` where it expects the slice. The worker is correct: its `send_tmux_message` and `notify_parent` both returned `success: true`, and the task text reached it intact. Needs a product fix; the reproduction is `KEEP_E2E_WORKDIR=1 script -qec ./tests/e2e/codex-messaging/run.sh`, and the quarantine entry is written into every run. |

The tmux isolation is on by default. An earlier revision had reverted it,
believing it broke the server's socket health check. That belief was wrong, and
the check has since separated the two claims:

* The isolation call really was inside `cleanup()`, so it only took effect at
  teardown. Moved to top level it works, and it is what gives the worker the
  run's isolated `CODEX_HOME` -- proven live. `codex-messaging` now runs to the
  validator stage with it on.
* The socket health check still fails for `chainlink-codex` **with the isolation
  disabled**, so it was never the isolation's doing. `exomonad serve` creates
  `.exo/server.sock` and then never becomes healthy inside `init`'s 30s budget.
  Filed as Chainlink #1150. `codex-messaging` passes the same check on the same
  host, so it is specific to that scenario.

`tests/e2e/python-tl-controller/test_isolation_contract.py` pins the placement
either way.

All three migrated scenarios now treat a missing or failing validator result as
a failure. They previously fell back to `exomonad init`'s exit status, and
`init` exits 0 as soon as it attaches the session -- so a 2026-09-30 run that
failed three assertions and wrote no verdict still exited 0.

## Codex scenario migration inventory

`tests/e2e/CODEX-TL-MIGRATION.md` is the step-1 inventory of Chainlink #1127:
every E2E scenario that described or validated the retired interactive Codex
root TL, and what happened to it. Three are migrated onto `plan.json`, one
(`recursive-crash-convergence/`) had an inert `root_agent_type` removed, and
`orphan-pr-guard/` is recorded as superseded and out of scope with its
rationale. The non-Codex scenarios that still carry an inert
`root_agent_type` or a natural-language `initial_prompt` are listed there too,
so the inventory is complete across `tests/e2e/`.
`tests/e2e/python-tl-controller/test_inventory.py` fails if a scenario joins or
leaves that set without the inventory being updated.

## Codex Hooks Feedback

- Hook trust is now asserted where it is written. The migrated Codex scenarios check the generated child config for the three hook commands, and the run's isolated `CODEX_HOME` for one `[hooks.state."<child config>:<event>:0:0"]` entry with a `trusted_hash` per event plus `[projects."<agent dir>"] trust_level = "trusted"`. That is the same property the `3 hooks need review before they can run` warning is about. The retired `# BEGIN EXOMONAD CODEX HOOKS` block is asserted **absent**: its writer was removed in 8934378f (#210) and `trust_codex_project` strips it on every write.
- A stale project-root `.codex/config.toml` is the other half of that gate: Codex applies project config to every session opened in the project, so a leftover one from the Codex root-TL era silently re-enables the retired Root TL protocol for any operator session. The migrated validators assert that normal Python-controller startup generates none, at the project root or under the root agent identity. A historical residue report is tracked separately (chainlink #1145).
- The hook denial path is working: the validator's Codex-shaped `gh auth status` payload received a deny response with `permissionDecision = deny`.
- Reviewer MCP scope is now explicit: reviewer-role `tools/list` includes `approve_pr`, `request_changes`, `post_review_comment`, and `notify_parent`; `just test-wasm-integration` covers this.
- The Codex scenarios must remain local-only: unset Forgejo auth and keep Forgejo-backed PR integration in a separate E2E.
