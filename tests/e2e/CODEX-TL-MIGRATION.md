# Codex e2e scenarios and the retired interactive root TL

Step 1 of Chainlink #1127: inventory every E2E scenario that still *describes*
or *validates* an interactive Codex root TL, and record what happened to it.

The retired model, for the purpose of this inventory, is: `exomonad init`
launching an interactive Codex root agent that reads a natural-language
`initial_prompt` from `.exo/config.toml`. It is retired because `init` always
starts the Python TL controller instead (`tl_loop`, a bounded Python process
that consumes `.exo/tl-loop/plan.json`), and `initial_prompt`, if set at all,
must be a JSON `WorkPlan` document. `root_agent_type` is accepted and ignored.

`tests/e2e/python-tl-controller/test_inventory.py` keeps this table honest: it
fails if a scenario sets `root_agent_type = "codex"`, passes `--tl`, or feeds
`init` a natural-language `initial_prompt` without appearing here.

## Migrated to the Python controller

| Scenario | What it was | What it is now |
| --- | --- | --- |
| `codex-messaging/` | An interactive Codex root TL spawned a Codex dev leaf, messaged it, and took `notify_parent` back. | A shipped `plan.json` dispatches one Codex worker that messages a Codex companion peer and reports to the controller. |
| `chainlink-codex/` | An interactive Codex root TL created a Chainlink issue, spawned a dev leaf, and closed the issue. | A shipped `plan.json` dispatches one Codex worker that runs the role-scoped Chainlink session workflow on an issue the harness owns. |
| `python-tl-worker-notify/` (was `subtl-worker-notify/`) | A Codex sub-TL and its worker; a worker pane was split into the TL window. | A shipped `plan.json` dispatches one Codex worker into a pane of the controller's own `TL` window, and the worker's `notify_parent` reaches that window. Renamed because no sub-TL exists in the shipped architecture. |

Each ships the plan the controller consumes, installs it before `init`, and
asserts the controller's durable checkpoint rather than a root agent's config.

## Fixed in #1127

| Scenario | Finding | Action |
| --- | --- | --- |
| `recursive-crash-convergence/` | `scenario.py` wrote `root_agent_type = "codex"` into the fixture config. The scenario already drives the shipped `exomonad init` with a real `plan.json`, so the key was inert -- but it names a root Codex agent that no longer exists, and a reader would take it as a live one. | The line is removed. The scenario was plan-driven already; nothing else changed. |

## Out of scope, with rationale

| Scenario | Why it is out of scope |
| --- | --- |
| `orphan-pr-guard/` | The only remaining scenario that genuinely *drives* the retired model: it sets `root_agent_type = "codex"`, hands `init` a natural-language `initial_prompt`, and invokes `exomonad init --tl codex`. None of that can work against the shipped binary -- `rust/exomonad/tests/cli.rs` asserts `init --help` contains no `--tl`, and `write_tl_loop_plan` rejects a non-JSON `initial_prompt` -- so the scenario is already non-functional rather than merely stale. Migrating it is not a re-expression: its subject is an interactive TL calling `resume_pr` on an existing orphan pull request, and a `plan.json` declares *new* work, while `resume_pr` is the controller's review-repair path for a slice the controller itself owns. Re-expressing it means designing a different scenario, which belongs to a separate issue. It is opt-in and is not part of any gate, so nothing regresses while it waits. |
| `claude-only/`, `claude-teams-inbox/`, `tl-to-worker-messaging/`, `chainlink/`, `chainlink-close/`, `idle-shutdown/`, `tl-loop-shadow/`, `opencode-worker/` | Not Codex-root scenarios. Each sets an inert `root_agent_type = "claude"` and a natural-language `initial_prompt`, so the same retirement applies to them, but they are outside the Codex suite this issue owns. |
| `hook-rewrite/`, `opencode-tl/` | Not Codex-root scenarios. `hook-rewrite/` sets `root_agent_type = "opencode"` and `opencode-tl/` likewise; both still hand `init` a natural-language `initial_prompt`. Outside the Codex suite. |

The non-Codex rows are listed rather than omitted so the inventory is complete
across `tests/e2e/`. They belong with the broader TL-as-loop retirement work
tracked by epic #1121, not with the Codex scenario migration.

## What a reader should take from this

The Codex suite is plan-driven. `root_agent_type = "codex"` no longer appears in
any scenario that runs, and the one scenario that still tried to drive the
retired model is marked and explained rather than left to be discovered.
