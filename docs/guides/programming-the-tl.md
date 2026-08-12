# Programming the TL for a new project

The tech lead is no longer a prompt. It is `tl_loop`, a bounded Python
controller that reads three human-authored files and drives every dispatch,
review, merge, and escalation decision from durable state.

Programming the TL means authoring those files. This guide covers what each one
controls, the exact schemas, and worked examples for the two questions that come
up first: how review and CI gate a merge, and how to add a step that runs after
implementation lands.

Read [`docs/decisions/tl-as-loop.md`](../decisions/tl-as-loop.md) for why the
boundary sits here, and [`tl_loop/CLAUDE.md`](../../tl_loop/CLAUDE.md) for the
module-level contracts.

---

## The three files

| File | Required | Owns |
|------|----------|------|
| `.exo/harness_policy.toml` | **yes** | Which harness/model each role may use, and the token ceilings |
| `.exo/review-policy.toml` | no (defaults apply) | Review rounds, timeouts, second-reviewer triggers |
| `.exo/tl-loop/plan.json` | **yes** | The work itself: workers, leaves, recursive sub-TLs |

A missing or invalid `harness_policy.toml` is a startup error. The controller
never synthesizes a permissive default, and it never widens an allowlist or a
ceiling to make a run continue.

`plan.json` is the only accepted form of a root specification. A
natural-language TL prompt is rejected — there is no interactive TL fallback.

---

## 1. `.exo/harness_policy.toml` — the allowlist and the budget

This is the human's veto over what the controller may spend and on what.

```toml
# Human-authored harness and budget boundaries for the TL loop.

[roles.tl]
allow = ["codex/gpt-luna"]
cost_rank = { "codex/gpt-luna" = 1 }
token_budget = 120000
escalate_after_attempts = 1

[roles.worker]
allow = ["codex/gpt-luna", "claude/sonnet"]
cost_rank = { "codex/gpt-luna" = 1, "claude/sonnet" = 2 }
token_budget = 120000
per_harness_budget = { "codex/gpt-luna" = 80000, "claude/sonnet" = 40000 }
escalate_after_attempts = 1

[roles.reviewer]
allow = ["codex/gpt-luna"]
cost_rank = { "codex/gpt-luna" = 1 }
token_budget = 60000
escalate_after_attempts = 1
```

All three role tables — `tl`, `worker`, `reviewer` — must be present. Unknown
role names and unknown keys are rejected.

| Key | Rule |
|-----|------|
| `allow` | Non-empty, unique, ordered `harness/model` entries |
| `cost_rank` | One positive rank per allowed entry — exactly the same key set, no more, no less. Rank `1` is the baseline rate |
| `token_budget` | Positive per-role ceiling |
| `per_harness_budget` | Optional; every key must appear in `allow` |
| `escalate_after_attempts` | Positive NO-GO threshold before the slice parks |

### How the selector spends this

Before a spawn is written to run state, the selector estimates its cost and
reserves it against both the role and the harness counters:

```
ceil((base(difficulty) + 50*test_steps + 100*paths + 50*dependencies) * harness_rate)
```

with `base` = 100 (trivial), 500 (standard), 1000 (hard), and `harness_rate`
taken from `cost_rank`. The reservation is written in the same atomic mutation
that records the spawn, so two concurrent selectors cannot both consume the last
of a ceiling.

When a child completes, the reservation reconciles against authoritative usage
— Chainlink first, then harness-reported. If neither reports tokens, the charge
persists `actual = "unknown"` and conservatively applies the estimate. It never
claims the estimate was measured usage.

Running out of ceiling is not an error the controller works around. The selector
returns `SelectionFailure.OVER_BUDGET`, the slice parks with
`budget_exhausted`, and a human raises the ceiling or narrows the plan.

### Tuning it for a new project

Start narrow. One entry per role is a legitimate policy and the easiest to
reason about. Widen `roles.worker` only when you have a slice class the cheap
harness demonstrably fails, and set `per_harness_budget` so the expensive entry
cannot silently absorb the whole role ceiling.

---

## 2. `.exo/review-policy.toml` — the merge gates

This file is what people usually mean by "how do I make the reviewer check
PRs and CI." It is entirely declarative.

```toml
min_review_rounds = 1
reviewer_max_rounds = 5
reviewer_max_wait_seconds = 1200
reviewer_max_rate_limit_retries = 2
review_freshness_window_secs = 1200
external_review_threshold = 300
external_review_paths = [
    "proto/**",
    "rust/exomonad-core/src/handlers/**",
]
require_second_reviewer_complexity = false
complexity_line_threshold = 500
max_leaf_session_seconds = 3600
max_reviewer_session_seconds = 600
```

`exomonad init --reviewer-max-rounds N` overrides `reviewer_max_rounds` for one
session without editing the file. Precedence is: init override → policy file →
built-in default.

### What actually gates a merge

The rule has two authorities and one integrity invariant:

```text
merge_allowed =
      tl_adjudicated_go(current_head_sha)  # binding reviewer findings
   && ci_success_or_neutral(current_head_sha)
   && current_head_sha == live_pr_head_sha
```

**a. TL adjudication on binding reviewer findings.** The reviewer supplies
structured findings for the exact head it inspected. The TL's
`adjudicate_review` call turns those findings, the TL-owned acceptance criteria,
and the diff into `GO` or `NO-GO`. The TL cannot invent a verdict without
reviewer evidence or ignore an unresolved blocking finding.

**b. CI is the machine authority.** The current head must have CI status
`success` or `neutral`. If Forgejo Actions does not post a status, the CI
condition is not satisfied; check that `.forgejo/workflows/ci.yml` actually
runs.

**c. Head binding is an integrity invariant.** The controller binds every
review and CI observation to a head SHA. If the PR moved after review, the
previous decision is stale and cannot merge. This prevents approving one commit
and merging another.

### Optional policy

Projects may require a second reviewer for declared risk paths or large diffs
using `external_review_paths`, `external_review_threshold`, and the complexity
threshold. Those are optional policy gates, not universal approval layers. A
`GO-WITH-NITS` remains mergeable, and the TL stores every nit in durable
per-head review findings for follow-up. It does not require an external
Chainlink issue writer, because the run checkpoint is the authoritative home
for review evidence.

### The repair path when review says NO-GO

`compose_repair` calls `watcher_pr_state` first and requires the PR to be open,
unmerged, and identified by both head branch and SHA. It produces exactly seven
sections (ROOT CAUSE, PROPOSED SOLUTION, READ FIRST, STEPS, VERIFY, BOUNDARY,
DONE CRITERIA) and dispatches through `resume_pr` — the same owner, worktree,
branch, and PR. It never creates a new branch, a `-2` suffix, or a second agent.
After `escalate_after_attempts` NO-GOs the slice parks with `review_stuck` and
waits for a human gate.

### Making the gates stricter

To require a second review on your project's dangerous surfaces, add path globs:

```toml
external_review_paths = [
    "migrations/**",
    "src/billing/**",
    "infra/terraform/**",
]
external_review_threshold = 200
```

Review timeout is never approval. It parks the slice with a named gate;
`reviewer_max_wait_seconds` controls when the timeout is detected, and
`reviewer_max_rounds` controls when repeated review attempts park the PR for a
human.

---

## 3. `.exo/tl-loop/plan.json` — the work

The plan document is a closed-key JSON object. Unknown keys are rejected at
load, not ignored.

```json
{
  "run_id": "root",
  "budgets": { "tokens": 400000, "wall_seconds": 14400 },
  "plan": {
    "workers": [],
    "leaves": [],
    "sub_tls": []
  }
}
```

| Top-level key | Meaning |
|---------------|---------|
| `run_id` | Single path component; names `.exo/tl-loop/<run_id>/run.json`. Defaults to `root` |
| `budgets` | `{ "tokens": int, "wall_seconds": int }` seeded into the run ledger |
| `plan` | The `WorkPlan`. `workers`, `leaves`, and `sub_tls` may also be written at the top level and are lifted into `plan` |

Names must be unique across all three lists — a name is the agent identity and
the last dot-segment of its branch.

### `leaves` — PR-producing dev agents

The workhorse. One leaf owns one branch, one worktree, one PR.

```json
{
  "name": "token-refresh",
  "task": "Implement refresh-token rotation for the OAuth provider.",
  "agent_type": "codex",
  "context": "Sessions currently die at 60 minutes because refresh is unimplemented.",
  "read_first": [
    "src/auth/CLAUDE.md",
    "src/auth/session.rs",
    "src/auth/provider.rs"
  ],
  "steps": [
    "Add `refresh_token` and `refresh_expires_at` columns in migrations/0007_refresh.sql.",
    "Implement `Provider::refresh(&self, token: &RefreshToken)` in src/auth/provider.rs.",
    "Rotate on every use: issue a new refresh token and revoke the presented one.",
    "Return 401 with code `refresh_reused` when a revoked token is presented."
  ],
  "verify": [
    "cargo test -p auth refresh",
    "cargo clippy -p auth -- -D warnings"
  ],
  "done_criteria": [
    "Refresh rotation is covered by the auth tests.",
    "The revoked-token response is documented."
  ],
  "boundary": [
    "src/auth/**",
    "migrations/0007_refresh.sql"
  ]
}
```

| Field | Required | Effect |
|-------|----------|--------|
| `name` | yes | Agent name and branch segment |
| `task` | yes | The assignment, passed verbatim after any continuation context |
| `agent_type` | no | Explicit harness override; otherwise the selector picks from the allowlist |
| `boundary` | no | **Becomes the slice's owned `paths`.** Overlapping boundaries between non-terminal slices are a schema error |
| `verify` | no | **Becomes the slice's `test_plan`** (falls back to `steps`) |
| `done_criteria` | no | Becomes TL-owned reviewer acceptance criteria |
| `context`, `read_first`, `steps` | no | Passed through to `spawn_leaf` as structured spec fields |

`boundary` and `verify` are doing double duty: they are both instructions to the
leaf and the controller's own ownership and test-plan records. Two leaves whose
boundaries overlap will fail path-ownership validation rather than race — write
disjoint boundaries or merge the slices.

The TL composes reviewer acceptance criteria from the run-state `test_plan`,
plan-level `verify` and `boundary`, owned paths, and `done_criteria`. The PR
body may document the work, but it is not the authoritative source for review
acceptance.

### `workers` — ephemeral, no branch, no PR

```json
{ "name": "audit-deps", "task": "List every crate with a known advisory.", "agent_type": "codex" }
```

Only `name`, `task`, and optional `agent_type`. Workers edit in place or
research; they do not produce a PR. Use them for narrow work where a direct
commit to the caller's worktree is acceptable.

### `sub_tls` — recursion, and the only way to order work

A sub-TL is a nested `tl_run` with its own checkpoint at
`.exo/tl-loop/<parent>/<sub_tl>/run.json`. It is not another agent session.

```json
{
  "name": "auth",
  "plan": {
    "leaves": [
      { "name": "token-refresh", "task": "..." },
      { "name": "session-store", "task": "..." }
    ]
  }
}
```

Accepted keys: `name`, `plan` (or inline `workers`/`leaves`/`sub_tls`),
`agent_type`, `worktree`, `agent_id`.

Child branches use `{parent}.{name}`, the child PR targets the parent branch as
its `base_ref`, and recursion depth is bounded by `max_depth` — exceeding it
parks the slice with `schedule_deadlock`.

**Sub-TLs run sequentially and each blocks until terminal.** The parent's
execution order is:

1. dispatch all top-level `workers` and `leaves` (in parallel),
2. run each `sub_tls` entry to completion, one at a time, in list order,
3. enter the event loop for the top-level children.

A sub-TL that ends in `TLFailed` fails the parent immediately.

---

## Worked example: a documentation step after the code merges

You want implementation to land, then documentation to be written against what
actually shipped.

### The mechanism

Top-level leaves in one plan are dispatched **in parallel with no ordering**. A
plan-level `depends_on` field does not exist — see the gap note below. Ordering
comes from `sub_tls`, which run sequentially to terminal.

So: put every stage in its own sub-TL, and put nothing at the top level.

```json
{
  "run_id": "root",
  "budgets": { "tokens": 600000, "wall_seconds": 21600 },
  "plan": {
    "sub_tls": [
      {
        "name": "impl",
        "plan": {
          "leaves": [
            {
              "name": "token-refresh",
              "task": "Implement refresh-token rotation.",
              "boundary": ["src/auth/**"],
              "verify": ["cargo test -p auth refresh"]
            },
            {
              "name": "session-store",
              "task": "Move session state into the shared store.",
              "boundary": ["src/session/**"],
              "verify": ["cargo test -p session"]
            }
          ]
        }
      },
      {
        "name": "docs",
        "plan": {
          "leaves": [
            {
              "name": "auth-docs",
              "task": "Document the auth changes that just merged into this branch.",
              "context": "The refresh-rotation and session-store work is already merged into your base branch. Read the merged diff, not a spec.",
              "read_first": ["src/auth/provider.rs", "src/session/store.rs"],
              "steps": [
                "Run `git log --oneline <base>..HEAD` and read every merged commit.",
                "Update docs/auth.md to describe refresh rotation, including the `refresh_reused` 401.",
                "Update docs/architecture/sessions.md for the new shared store.",
                "Add a CHANGELOG.md entry under Unreleased."
              ],
              "verify": ["just docs-lint"],
              "boundary": ["docs/**", "CHANGELOG.md"]
            }
          ]
        }
      }
    ]
  }
}
```

The `docs` sub-TL does not start until `impl` reaches `TLDone`, which means
every implementation PR has been reviewed, CI-checked, and merged into the
parent branch. The docs leaf then reads real merged code rather than a spec that
may have drifted during review.

Its `boundary` is disjoint from every implementation boundary, so the docs PR
cannot collide with the code it describes, and it goes through the same review
and CI gates as any other PR.

### The reactive alternative

If you want documentation triggered by a merge rather than scheduled after a
stage, that is an event handler, not a plan. The watcher emits merge events
(`agent.sibling_merged` in the ledger projection; `sibling_merged` in the WASM
event vocabulary) and a handler in `.exo/lib/` returns an `EventAction`. See
`haskell/wasm-guest/src/ExoMonad/Guest/Events.hs` for the event types and
`CLAUDE.md` § Event Handler Call for the dispatch path.

Prefer the sub-TL form when the documentation is part of the planned work.
Reach for the handler only when the trigger is genuinely external to the plan.

### Known gap: no plan-level `depends_on`

`RunState` slices carry `depends_on`, the schema validates it for cycles and
unknown IDs, and `tl_loop/loop/schedule.py` schedules on it. But
`WorkPlan.LeafTask` has no `depends_on` field and `_initial_slice_record` in
`tl_loop/loop/driver.py` hard-codes `"depends_on": []`, so a hand-authored
`plan.json` cannot express a DAG. Only `decompose`-produced `SliceSpec` records
carry dependencies.

Until that is plumbed through, `sub_tls` is the supported way to express
ordering, at the cost of a stage boundary where a finer dependency edge would
do.

---

## Running and steering it

```bash
# The TL window runs this automatically; these are for direct/bounded use.
python3 -m tl_loop run    --project-root . --plan .exo/tl-loop/plan.json --run-id root
python3 -m tl_loop status --project-root . --run-id root
python3 -m tl_loop gate   --project-root . --run-id root --name <gate> --approve
python3 -m tl_loop gate   --project-root . --run-id root --name <gate> --reject
```

`run` flags: `--max-events` (default 256), `--idle-timeout` (30s),
`--poll-interval` (0.25s), `--wait-for-plan` (block until `plan.json` appears),
`--verbose`. `EXOMONAD_TL_LOOP_PROJECT_ROOT` and `EXOMONAD_TL_LOOP_RUN_ID` set
the defaults.

`status` prints the phase, waiting slices, gates, per-slice status and PR
number, and the consumed event offset.

### Slice statuses

`pending` → `ready` → `spawned` → `in_review` → (`repairing` →) `merged`.
Terminal alternatives: `failed`, `parked`, `blocked`.

### Park causes, and what each one asks of you

| Cause | What happened | What you do |
|-------|---------------|-------------|
| `retries_exhausted` | NO-GO past `escalate_after_attempts` | Read the reviews; re-plan the slice or approve a gate |
| `budget_exhausted` | Role or harness ceiling reached | Raise the ceiling in `harness_policy.toml` or narrow the plan |
| `no_capable_harness` | No allowed entry meets the capability requirement | Widen `allow` deliberately |
| `schedule_deadlock` | Nothing dispatchable, or `max_depth` hit | Fix the plan structure |
| `review_stuck` | Review rounds exceeded without convergence | Human reads the PR |
| `harness_switch_requested` | The configured harness could not proceed | Approve explicitly; set `EXOMONAD_ALLOW_HARNESS_SWITCH=1` |
| `stall_detected` | Dead pane or no progress past the heartbeat threshold | Investigate the worker |

The controller does not retry past a ceiling, coax a model into continuing, or
pick a different harness on its own. Every one of these becomes a durable,
named, auditable gate.

### Long-running waves

Optional `goals` in run state carry an objective, deadline, and completion
predicate. When the event queue is idle, `HeartbeatConfig` re-observes liveness
via `poll_workers` and PR state via `watcher_pr_state`. Reconciliation is
idempotent and never invents ledger sequence numbers or budget charges. tmux
scraping is not a liveness source.

---

## Checklist for a new project

1. `exomonad new` — creates `.exo/config.toml`, `.gitignore`, CI scaffold, rules.
2. Write `.exo/harness_policy.toml`. Start with one allowed entry per role.
3. Set Forgejo credentials — `forgejo_url`, `forgejo_token`,
   `forgejo_webhook_secret` in `.exo/config.toml`. Without a working CI status,
   the canonical merge rule cannot pass.
4. Adjust `.exo/review-policy.toml` if the defaults are wrong for your risk
   surface — mainly `external_review_paths`.
5. Write `.exo/tl-loop/plan.json`. Disjoint `boundary` globs, real commands in
   `verify`.
6. `exomonad init` — starts the server and the controller in the TL window.
7. `python3 -m tl_loop status` to watch; `python3 -m tl_loop gate` to answer.

## Anti-patterns

- **Do not** put a natural-language brief in `plan.json`. There is no
  interactive TL to read it; the load fails.
- **Do not** give two leaves overlapping `boundary` globs. Path-ownership
  validation rejects the state.
- **Do not** write `verify: ["run the tests"]`. It becomes the slice's
  `test_plan`; it must be an exact command.
- **Do not** widen `allow` or raise `token_budget` to clear a park without
  deciding that is the right call. The park is the design working.
- **Do not** expect top-level `leaves` to run in order. They do not.
