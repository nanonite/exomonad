# Programming the TL for a new project

The tech lead is no longer a prompt. It is `tl_loop`, a bounded Python
controller that reads four required controller files and drives every dispatch,
review, merge, and escalation decision from durable state.

Programming the TL means authoring those files. This guide covers what each one
controls, the exact schemas, and worked examples for the two questions that come
up first: how review and CI gate a merge, and how to add a step that runs after
implementation lands.

Read [`docs/decisions/tl-as-loop.md`](../decisions/tl-as-loop.md) for why the
boundary sits here, and [`tl_loop/CLAUDE.md`](../../tl_loop/CLAUDE.md) for the
module-level contracts.

---

## The four required controller files

| File | Required | Owns |
|------|----------|------|
| `.exo/config.toml` | **yes** | Project and runtime configuration |
| `.exo/harness_policy.toml` | **yes** | Which harness/model each role may use, and the token ceilings |
| `.exo/review-policy.toml` | **yes** | Review rounds, timeouts, second-reviewer triggers |
| `.exo/harness_capability.toml` | **yes** | Difficulty ratings for every harness/model allowed by the policy |

The structured work plan lives separately at `.exo/tl-loop/plan.json`.

An externally blocked leaf may receive one bounded continuation clarification.
The clarification carries the prior and proposed plan revision plus a
SHA-256 invariant digest. It may change only the continuation task; paths,
dependencies, ownership, harness selection, verification, Definition of Done,
base, or timeout fields are authority-bearing and require an explicit human
gate. The validator compares the digest before a same-owner resume and records
only revision, digest, changed-field categories, and the gate decision in
telemetry; raw continuation prompts are never aggregate event fields.

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
task_timeout_seconds = 3600.0 # optional; zero disables the task ceiling

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
| `task_timeout_seconds` | Optional non-negative task ceiling; zero disables it |

Task timeout precedence is most-specific-wins: the slice's
`task_timeout_seconds` in `plan.json`, then the selected role's value here,
then project `tl_task_timeout_seconds` in `.exo/config.toml`, then the built-in
3600-second default. `null` and `0` explicitly disable enforcement. The
controller persists the resolved value and dispatch timestamp, and only the
heartbeat may enforce it; observational stall thresholds never kill a worker.

The checkpoint's `deadline_ledger` makes the accounting auditable across a
restart. `execution_seconds` excludes time after a slice enters recovery;
`recovery_wait_seconds` records that separate bounded wait, while
`execution_deadline_at`, `recovery_deadline_at`, and `run_deadline_at` identify
the three ceilings. A terminal invocation record is reconciled before a task
budget disposal in the same heartbeat, so elapsed time never overrides
authoritative lifecycle evidence.

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

Review timeout is never approval. The controller parks the run at the durable
`tl-timeout` gate. `reviewer_max_wait_seconds` controls when the timeout is detected,
and `reviewer_max_rounds` controls when repeated review attempts park the PR for a
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
`.exo/tl-loop/<parent>/<sub_tl>/run.json`. In an active run it executes in an
isolated controller process with its own owner branch and worktree coordinates;
it is not an interactive model session and it does not own the parent merge.
The direct parent remains the aggregate PR owner and the only process that
serializes integration.

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
`agent_type`, `worktree`, `agent_id`, positive `order`, and optional
`integration`. Missing `order` is backward-compatible shorthand for order 1;
zero and negative values are invalid.

```json
{
  "name": "auth",
  "order": 1,
  "integration": {
    "aggregate_pr_required": true,
    "base_revalidation_required": true,
    "merge_strategy": "merge"
  },
  "plan": { "leaves": [] }
}
```

The integration contract binds each aggregate candidate to its child PR,
head, patch digest, and original base. Code-review evidence must match the
candidate head and patch; integration evidence must match the current base,
integrated head, merge tree, and CI result. The lifecycle distinguishes
`CHILDREN_MERGED`, `CODE_REVIEWED`, `READY_FOR_INTEGRATION`,
`NEEDS_BASE_REVALIDATION`, `INTEGRATION_VALIDATED`, `MERGING`, `MERGED`, and
explicit repair, conflict, failure, and parked states. A changed base and a
changed head therefore take different recovery paths.

Child branches use `{parent}.{name}`, the child PR targets the parent branch as
its `base_ref`, and recursion depth is bounded by `max_depth` — exceeding it
parks the slice with `schedule_deadlock`.

**Ordered sub-TLs execute in numbered stages.** The order value is scoped to
one parent's direct sub-TL siblings:

- Missing order is legacy shorthand for order 1.
- Explicit orders must be present on every sibling and be contiguous, starting
  at 1; mixing explicit and missing orders is rejected.
- Different orders are sequential. The next numeric order cannot start until
  every sub-TL in the current order has completed and its aggregate result is
  integrated.
- Sub-TLs with the same order run concurrently, bounded by
  max_parallel_slices. Each child may itself contain multiple parallel leaves.
- The parent serializes aggregate integration in stable sub-TL ID order. A
  plan does not author merge priority. After an earlier same-order aggregate
  merge advances the base, each later candidate is revalidated against that
  base without changing its reviewed head.
- A child that ends in TLFailed blocks all higher numeric orders and leaves the
  parent failed or parked; it is never silently skipped.
- A child with an authoritative pre-publication recovery checkpoint is
  projected as `recovering` or `human_gate`, not `TLFailed`. Same-order
  siblings continue, while higher orders remain pending. The projection keeps
  the owning run, complete child path, blocked slice, recovery round, and next
  probe time; only the nearest child TL may resume that checkpoint.

The parent's top-level execution is therefore: dispatch any top-level
workers/leaves, run order 1, integrate it, then advance through the remaining
ordered stages. A sub-TL has its own checkpoint and branch coordinates, so a
restart resumes the stage rather than spawning a duplicate child.

### Ordered plan examples

The following examples are complete plan.json documents. They use disjoint
boundaries and real verification commands so they can be passed to preflight.

One parallel group:

~~~json
{
  "run_id": "root",
  "budgets": { "tokens": 200000, "wall_seconds": 7200 },
  "plan": {
    "sub_tls": [
      {
        "name": "auth",
        "order": 1,
        "plan": {
          "leaves": [
            {
              "name": "tokens",
              "task": "Implement access-token rotation.",
              "boundary": ["src/auth/tokens/**"],
              "verify": ["cargo test -p auth tokens"]
            }
          ]
        }
      },
      {
        "name": "billing",
        "order": 1,
        "plan": {
          "leaves": [
            {
              "name": "invoices",
              "task": "Implement invoice persistence.",
              "boundary": ["src/billing/invoices/**"],
              "verify": ["cargo test -p billing invoices"]
            }
          ]
        }
      }
    ]
  }
}
~~~

Multiple same-order sub-TLs followed by an integration stage:

~~~json
{
  "run_id": "root",
  "budgets": { "tokens": 300000, "wall_seconds": 10800 },
  "plan": {
    "sub_tls": [
      {
        "name": "parser",
        "order": 1,
        "plan": {
          "leaves": [
            {
              "name": "grammar",
              "task": "Add the parser grammar.",
              "boundary": ["src/parser/grammar/**"],
              "verify": ["cargo test -p parser grammar"]
            }
          ]
        }
      },
      {
        "name": "schema",
        "order": 1,
        "plan": {
          "leaves": [
            {
              "name": "types",
              "task": "Add parsed AST types.",
              "boundary": ["src/parser/types/**"],
              "verify": ["cargo test -p parser types"]
            }
          ]
        }
      },
      {
        "name": "integration",
        "order": 2,
        "plan": {
          "leaves": [
            {
              "name": "adapter",
              "task": "Integrate the parser and schema changes.",
              "boundary": ["src/integration/parser/**"],
              "verify": ["cargo test -p integration parser"]
            }
          ]
        }
      }
    ]
  }
}
~~~

Nested orders reset at each recursive boundary:

~~~json
{
  "run_id": "root",
  "budgets": { "tokens": 250000, "wall_seconds": 9000 },
  "plan": {
    "sub_tls": [
      {
        "name": "release",
        "order": 1,
        "plan": {
          "sub_tls": [
            {
              "name": "api",
              "order": 1,
              "plan": {
                "leaves": [
                  {
                    "name": "endpoint",
                    "task": "Add the release endpoint.",
                    "boundary": ["src/release/api/**"],
                    "verify": ["cargo test -p release endpoint"]
                  }
                ]
              }
            },
            {
              "name": "cli",
              "order": 2,
              "plan": {
                "leaves": [
                  {
                    "name": "command",
                    "task": "Expose the release endpoint in the CLI.",
                    "boundary": ["src/release/cli/**"],
                    "verify": ["cargo test -p release command"]
                  }
                ]
              }
            }
          ]
        }
      }
    ]
  }
}
~~~

Sequential dependent stages:

~~~json
{
  "run_id": "root",
  "budgets": { "tokens": 180000, "wall_seconds": 6000 },
  "plan": {
    "sub_tls": [
      {
        "name": "build",
        "order": 1,
        "plan": {
          "leaves": [
            {
              "name": "implementation",
              "task": "Implement the feature.",
              "boundary": ["src/feature/**"],
              "verify": ["cargo test -p feature"]
            }
          ]
        }
      },
      {
        "name": "verify",
        "order": 2,
        "plan": {
          "leaves": [
            {
              "name": "acceptance",
              "task": "Verify the merged feature behavior.",
              "boundary": ["tests/feature/**"],
              "verify": ["cargo test -p feature --test acceptance"]
            }
          ]
        }
      },
      {
        "name": "publish",
        "order": 3,
        "plan": {
          "leaves": [
            {
              "name": "documentation",
              "task": "Document the verified feature.",
              "boundary": ["docs/feature/**"],
              "verify": ["just docs-lint"]
            }
          ]
        }
      }
    ]
  }
}
~~~

### Recursive integration and recovery

Each completed child publishes one aggregate candidate owned by the direct
parent. Review evidence is bound to the candidate's exact PR head and patch
digest. Integration evidence is a separate check bound to the current parent
base, integrated head, merge tree, and CI result:

~~~text
review_allowed =
    reviewer_evidence.head_sha == candidate.head_sha
 && reviewer_evidence.patch_digest == candidate.patch_digest

integration_allowed =
    review_allowed
 && integration.base_sha == live_parent_base
 && integration.head_sha == candidate.head_sha
 && integration.merge_tree_sha == live_merge_tree
 && integration.ci_status in {"success", "neutral"}
~~~

The parent adjudicates the child result and owns the aggregate PR. A review
NO-GO uses compose_repair and resume_pr for that same aggregate owner, branch,
worktree, and PR. It does not spawn a worker, create a replacement PR, or
rebase a reviewed head solely because the parent base moved.

The integration lifecycle shown in `status` and `/control` is:

| State | Meaning | Next legal transition |
|---|---|---|
| RUNNING | Children are still executing | Wait for child completion |
| CHILDREN_MERGED | The child fold completed | Open or reuse the aggregate PR |
| AGGREGATE_PR_OPEN | The aggregate PR and owner are persisted | Collect aggregate review evidence |
| CODE_REVIEWED | The aggregate head has binding review evidence | Wait for CI and enter integration readiness |
| READY_FOR_INTEGRATION | Head-bound review and CI are acceptable | Validate base/head/tree/CI evidence |
| NEEDS_BASE_REVALIDATION | The parent base advanced; the reviewed head remains valid | Refresh integration evidence and CI |
| INTEGRATION_VALIDATED | Base, head, tree, patch, and CI all match the snapshot | Persist `MERGING` and call `merge_pr` |
| MERGING | Merge request was durably started | Reconcile live PR state after restart |
| MERGED | The aggregate result is recorded | Advance to the next numeric order |
| REPAIRING_AGGREGATE | Same owner is repairing the aggregate head | Re-review the repaired head |
| INTEGRATION_CONFLICT | The candidate cannot merge cleanly | `resume_pr` the same aggregate owner or open the gate |
| FAILED | The child or integration failed terminally | Inspect the failure and answer any named gate |
| PARKED | A ceiling or human decision stopped progress | Answer the named gate or revise the plan |

Repair and revalidation have separate ceilings. Exhausting either opens a
named gate (tl-integration-conflict or tl-integration-revalidation).
Restart reads the persisted stage, owner, PR, head, base, evidence, and
attempt counters; it never treats a missing event as permission to duplicate a
spawn, PR, review, repair, or merge.

### Troubleshooting `status`

The status projection is intentionally body-free but contains enough evidence
to diagnose a stopped stage. Inspect these fields first:

```bash
python3 ~/.exo/tl_loop.pyz status --project-root . --run-id root
```

```json
{
  "current_order": 2,
  "ordered_stages": [
    {"order": 1, "sub_tls": [{"id": "auth", "lifecycle": "MERGED"}]},
    {"order": 2, "sub_tls": [{
      "id": "docs",
      "lifecycle": "NEEDS_BASE_REVALIDATION",
      "aggregate_pr_number": 42,
      "head_sha": "head-docs",
      "validated_base_sha": null,
      "integration_ci": "unknown",
      "owner_run_id": "docs",
      "owner_branch": "main.docs"
    }]}
  ],
  "integration": {"lifecycle": "NEEDS_BASE_REVALIDATION"},
  "next_transition": "revalidate_base_and_integration_ci"
}
```

`next_transition` is the operator diagnosis: `await_sub_tl_completion` means
the child checkpoint is still running, `await_review_or_ci` means the
aggregate PR is waiting for head-bound evidence, `validate_integration_evidence`
means the parent has a ready candidate, and `resume_pr:<owner>` means a
conflict repair belongs to the persisted aggregate owner. A pending
`tl-integration-revalidation` or `tl-integration-conflict` gate must be
answered explicitly; a restart or a new worker is not an answer.

For a real-git/tmux and Forgejo-backed acceptance run, use the project recipe
with a dedicated repository. The mock mode is hermetic and does not replace a
real Forgejo run:

```bash
EXOMONAD_FORGEJO_E2E_MOCK=1 just tl-loop-ordered-forgejo
# real run: set EXOMONAD_FORGEJO_E2E_URL/TOKEN/OWNER/REPO/GIT_REMOTE first
just tl-loop-ordered-forgejo
```

---

## Worked example: a documentation step after the code merges

You want implementation to land, then documentation to be written against what
actually shipped.

### The mechanism

Top-level leaves in one plan are dispatched **in parallel with no ordering**. A
plan-level `depends_on` field does not exist — see the gap note below. Ordering
comes from numeric `order` values on direct sibling `sub_tls` entries.

Use order 1 for implementation and order 2 for documentation. Same-order
sub-TLs are concurrent; different orders wait for all child completion and
serialized aggregate integration before advancing.

```json
{
  "run_id": "root",
  "budgets": { "tokens": 600000, "wall_seconds": 21600 },
  "plan": {
    "sub_tls": [
      {
        "name": "impl",
        "order": 1,
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
        "order": 2,
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
python3 ~/.exo/tl_loop.pyz run    --project-root . --plan .exo/tl-loop/plan.json --run-id root
python3 ~/.exo/tl_loop.pyz status --project-root . --run-id root
python3 ~/.exo/tl_loop.pyz gate   --project-root . --run-id root --name <gate> --approve
python3 ~/.exo/tl_loop.pyz gate   --project-root . --run-id root --name <gate> --reject
```

`run` flags: `--max-events` (default 256), `--task-timeout` (3600s; `0` disables),
`--poll-interval` (0.25s), `--wait-for-plan` (block until `plan.json` appears),
`--verbose`. `EXOMONAD_TL_LOOP_PROJECT_ROOT` and `EXOMONAD_TL_LOOP_RUN_ID` set
the defaults.

The focused ordered integration checks are:

~~~bash
just tl-loop-ordered-e2e
tl_loop/.venv/bin/python -m pytest -q tl_loop/tests/test_replay.py
cargo test -p exomonad-core --test tmux_liveness
~~~

The first command exercises the hermetic recursive lifecycle. The second
checks replay compatibility, and the third checks the real tmux liveness
boundary. Run the Forgejo-backed end-to-end target when CI credentials and its
server are available.

`status` prints the phase, waiting slices, gates, per-slice status and PR
number, and the consumed event offset.
The output also includes the current numeric order, grouped same-order
sub-TLs, integration evidence, and a next_transition describing the next
legal operator action. The /control run read model exposes the same fields
with schema_version: 2; older checkpoints are projected with empty ordered
stages and unknown integration evidence.

### Slice statuses

`pending` → `ready` → `spawned` → `in_review` → (`repairing` →) `merged`.
Terminal alternatives: `failed`, `parked`, `blocked`.

For an ordered run, inspect these fields first:

| Status output | Diagnose |
|---|---|
| current_order and ordered_stages | Which numeric stage is active and which sibling sub-TLs are running together |
| integration.lifecycle = READY_FOR_INTEGRATION | Review and CI are acceptable for the candidate head; the controller is checking base-bound evidence |
| integration.lifecycle = NEEDS_BASE_REVALIDATION | The base moved; wait for refreshed integration CI, not a new review or rebase |
| integration.lifecycle = INTEGRATION_CONFLICT | The aggregate PR needs same-owner resume_pr repair |
| next_transition | The controller's next legal action, such as answer_gate:<name>, revalidate_base_and_integration_ci, or resume_pr:<owner> |

tl-integration-revalidation means repeated base movement exceeded the
configured revalidation ceiling. tl-integration-conflict means conflict
repair exceeded its ceiling or requires human direction. Answering a gate is
the only operator mutation; do not manually mark a stage merged.

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
| `task_budget_exceeded` | A declared slice execution ceiling elapsed | Inspect the deadline ledger and adjust the task budget or plan |

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

## 4. The operator control plane

`status` and `gate` are the CLI floor. The server also exposes a `/control`
route group for a richer operator surface — a read model over run state plus
the two mutations you are allowed to make.

See [`docs/decisions/operator-control-plane.md`](../decisions/operator-control-plane.md)
for why the boundary sits here.

### Routes

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/control/runs/{run_id}` | Run read model: phase, slices, budgets, gates, park causes |
| `GET` | `/control/runs/{run_id}/slices/{slice_id}` | One slice, with per-head review and CI evidence |
| `GET` | `/control/runs/{run_id}/transitions` | Recent FSM transitions |
| `POST` | `/control/runs/{run_id}/gates/{gate_name}` | Answer an **existing** named gate |
| `POST` | `/control/runs/{run_id}/plan/proposals` | Propose a plan mutation — inert until confirmed |

### Authority is enforced server-side

Two separate credentials, and they are mutually exclusive per request
(`rust/exomonad/src/control.rs`):

```bash
export EXOMONAD_CONTROL_TOKEN=...   # operator console  -> X-Exomonad-Control-Token
export EXOMONAD_AGENT_TOKEN=...     # agent effect calls -> X-Exomonad-Agent-Token
```

A request carrying the agent header is refused on `/control`, and a request
carrying the control header is refused on `/agents`. Presenting both is refused
everywhere. Co-location grants nothing — a process on the same host still needs
the right credential.

**The console can do exactly three things:** read projections, answer a gate
that already exists, and propose a plan mutation that stays inert until
confirmed. It cannot merge a PR, approve a review, set a verdict, alter the FSM
phase, widen `harness_policy.toml`, or raise a budget ceiling. Those stay with
the controller.

A plan proposal is validated by the same closed-key `WorkPlan` validator that
`plan.json` uses, so a malformed proposal is rejected at the boundary rather
than half-applied.

### Read models are projections, and can be stale

Every read model carries the ledger cursor it was built from. Check it before
acting on what you see — a gate answer decided from a stale projection is the
failure mode this field exists to prevent.

### Natural language

`tl_loop.rlm.intent.interpret_operator_intent` translates operator prose into
one of `Query`, `GateAnswer`, `PlanProposal`, or `Unclear`. It is a bounded
judgment like `decompose` and `adjudicate_review`: closed output schema,
`tools=()`, no effect client, no filesystem capability.

The model **translates; it does not decide**. Its output passes the same
validation and authority checks as the equivalent CLI argument, and `Unclear`
is a first-class result — it asks rather than guessing, because a misread
instruction moves real work.

Agent-authored text in the read model is tagged `observation_only` and is never
presented to the judgment as instruction. That provenance envelope is the
prompt-injection boundary.

---

## 5. Measuring a run

`status` answers "what is happening now". The ledger answers "what happened
across runs" — and since M13 that includes the controller's own decisions, not
just agent and PR activity.

### Controller events

Declared `tl.*` event types land in the ledger alongside agent events, all
carrying identities and bounded dimensions only — never utterances, diffs,
repair prose, or plan documents:

| Event | Fires when |
|---|---|
| `tl.phase_changed` | Durable FSM transition |
| `tl.slice_status_changed` | Slice status transition |
| `tl.slice_parked` | A slice parks, with which of the seven causes |
| `tl.gate_opened` | A named gate becomes pending |
| `tl.gate_answered` | You approve or reject — `source` distinguishes `cli` from `control` |
| `tl.merge_decided` | The controller decides to merge or not |
| `tl.judgment` | An RLM judgment completes, with attempt and outcome |
| `tl.plan_proposed` | A `/control` plan proposal is accepted or rejected |
| `tl.stage_started` / `tl.stage_completed` | An ordered numeric stage starts or finishes |
| `tl.aggregate_pr_opened` | An aggregate PR is created for a child sub-TL |
| `tl.integration_validated` / `tl.integration_revalidated` | Base-bound integration evidence is accepted or refreshed |
| `tl.integration_base_invalidated` | The integration base changed and evidence must be refreshed |
| `tl.integration_conflict` | Aggregate integration needs conflict repair |
| `tl.integration_repair_requested` | Same-owner repair was requested |
| `tl.merge_reconciled` | Restart observed that a persisted merge already completed |

Emission is **fail-open**. If the ledger write fails it is logged and the run
continues — observability can never stall a run or change a merge decision.

### Reading it back

```bash
exomonad logs import --source .exo/ledger/segments --dry-run   # inspect first
exomonad logs import --source .exo/ledger/segments             # -> .exo/analysis/atlas.db
exomonad logs measure --output .exo/analysis/measurement       # detectors + gate
exomonad logs export  --mode aggregate --output .exo/analysis/export   # L4, shareable
```

Import is idempotent and read-only with respect to the source files. Only the
aggregate export is shareable — L1–L3 stays local.

### What this makes answerable

Questions that were anecdotes before, and are now numbers:

- **How long do my gates sit unanswered?** `gate_opened_requires_answer` has no
  delay bound, so the interval is measured rather than assumed.
- **Which park cause dominates?** If `budget_exhausted` leads, your ceilings are
  wrong; if `review_stuck` leads, your specs are.
- **Are judgments retrying?** `tl.judgment` carries attempt and outcome, so a
  decompose that habitually needs three attempts is visible.
- **Did a merge decision reach an outcome?** `merge_decision_requires_pr_outcome`
  correlates on `pr_number`, so a decision that vanished is a defect rather than
  silence.

The denominator rules in
[`docs/observability/expected-events.v1.json`](../observability/expected-events.v1.json)
are what make a *missing* event detectable. Without them, a quiet run and a
broken run look identical.

### Trusting what you see

Check `.exo/sink-health.json` before trusting a denominator — it records
accepted/rejected counts and the last successful sequence. Health from another
session is `unknown` and cannot classify the current one.

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
7. `python3 ~/.exo/tl_loop.pyz status` to watch; `python3 ~/.exo/tl_loop.pyz gate` to answer.
8. Optional: `export EXOMONAD_CONTROL_TOKEN=...` if you want the `/control`
   read model and gate/proposal routes rather than the CLI alone.
9. After a run, `exomonad logs import --source .exo/ledger/segments` and
   `exomonad logs measure` to see where the run actually spent its time.

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


### Required controller files

A project must provide four files under `.exo/`: `config.toml`, `harness_policy.toml`, `review-policy.toml`, and `harness_capability.toml`. The policy is the authoritative allowlist and budget boundary; the capability map records a difficulty rating for every `harness/model` entry allowed by any policy role. It may not be used to widen that allowlist.

`harness_capability.toml` has this shape:

```toml
[capabilities]
"codex/gpt-luna" = "standard"
```

Run `python3 ~/.exo/tl_loop.pyz preflight --project-root .` to validate all four files before starting the controller.
