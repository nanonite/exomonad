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

`.exo/tl-loop/plan.json` is the input for a new run. On the first durable
checkpoint, the controller canonicalizes it into the immutable
`run.json.plan_manifest`, which contains the complete recursive scope and node
declarations. That persisted manifest—not a later external file—is the
continuation authority. A continuation may omit `plan.json`; if an external
file is supplied, missing, changed, or reordered content must not silently
change the run. An active declaration changes only through the audited
`exomonad plan revise` transition. It advances changed scope revisions and
preserves dispatched ownership, parent scope, branch targets, and completed
history.

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

For a new run, `plan.json` is the only accepted form of a root specification. A
natural-language TL prompt is rejected — there is no interactive TL fallback.
After the first checkpoint, the persisted manifest is the root specification
for continuation. Editing the external plan alone changes no run authority;
use `exomonad plan revise` for a supported declaration change or
`exomonad plan reconcile` to repair evidence-backed drift.

---

## 1. `.exo/harness_policy.toml` — the allowlist and the budget

This is the human's veto over what the controller may spend and on what.

```toml
# Human-authored harness and budget boundaries for the TL loop.
#
# The codex entries name the model your Codex account can run — `exomonad new`
# fills them in from the top-level `model` in your Codex config, or from
# EXOMONAD_CODEX_MODEL. There is no built-in default: a model that no account
# supports provisions workers that cannot take a turn.

[roles.tl]
allow = ["codex/<your-model>"]
cost_rank = { "codex/<your-model>" = 1 }
token_budget = 120000
escalate_after_attempts = 1
task_timeout_seconds = 3600.0 # optional; zero disables the task ceiling

[roles.worker]
allow = ["codex/<your-model>", "claude/sonnet"]
cost_rank = { "codex/<your-model>" = 1, "claude/sonnet" = 2 }
token_budget = 120000
per_harness_budget = { "codex/<your-model>" = 80000, "claude/sonnet" = 40000 }
escalate_after_attempts = 1

[roles.reviewer]
allow = ["codex/<your-model>"]
cost_rank = { "codex/<your-model>" = 1 }
token_budget = 60000
escalate_after_attempts = 1
```

All three role tables — `tl`, `worker`, `reviewer` — must be present. Unknown
role names and unknown keys are rejected.

### The codex model has to be one your account can run

`codex/<model>` is not a label. The controller splits it
(`parse_harness_identifier`) and the model half becomes `model = ...` in the
generated child config, so the string names the model each worker asks for. A
model the account does not support fails on the first inference with a 400 from
the provider, not with anything a budget or allowlist check can see.

Which models those are depends entirely on the login, so nothing in ExoMonad
can answer it. `exomonad new` therefore does not pick a model: it reads the
top-level `model` from your Codex config — the same place a bare `codex` run
without `--model` gets its own — or takes `EXOMONAD_CODEX_MODEL`, and refuses to
scaffold at all when neither supplies one. The refusal names the model and the
account's own rejection rather than emitting a policy that fails later.

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

### The `roles.tl` judgment model has to resolve at startup

`roles.tl` also owns the one model judgment the review loop makes (see
section 2). The controller resolves that model before it does anything else, from
the first `roles.tl.allow` entry:

- a `harness/model` entry pins the model (`allow = ["claude/sonnet"]`); or
- a bare harness (`allow = ["claude"]`) defers to `.exo/model-catalog.json`.

If neither yields a model, the controller refuses to start, records the reason
as the run's exit reason, and names the file to fix. The bare harness name is
never used as a model id. For example, `allow = ["claude"]` with no catalog:

```text
roles.tl.allow entry 'claude' pins no model and .exo/model-catalog.json does not
exist. Pin a model in .exo/harness_policy.toml (for example
allow = ["claude/<model-id>"]) or add .exo/model-catalog.json.
```

The judgment is served by a tool-free subprocess of the harness's own CLI, so the
`tl` harness must be one of `claude`, `codex`, or `opencode`, and its binary must
be on `PATH`; otherwise startup is refused the same way. Each judgment is charged
to `roles.tl.token_budget`.

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

Session lifecycle is explicit: bare `exomonad init` means `--continue` and
preserves a non-terminal run; `--start` is only for a missing or terminal run;
and `--recreate --confirm-recreate` is the destructive replacement path. Use
`--recreate --recreate-dry-run` to inspect its plan without changing state.

### Revising an active plan

When an operator needs to add or update future work in a nonterminal run, edit
`.exo/tl-loop/plan.json` and inspect the candidate before applying it:

```bash
exomonad plan revise --dry-run
exomonad plan revise --apply --reason "Bind issue IDs to the remaining planned slices"
exomonad init --continue
```

The dry run reports changed nodes and scopes, proposed manifest revisions, and
any protected work that blocks the change. Apply requires an operator reason,
records an audit entry, and commits the plan snapshot, digest, and affected
RunStore manifests as a recoverable transition. The command does not start the
controller, dispatch work, or change Chainlink issues. After it succeeds, use
`exomonad init --continue` to resume the existing run.

Revisions can add or update future, undispatched work. They cannot rewrite the
identity or ownership of dispatched or completed nodes, remove nodes, or alter
their branch and dependency history. Chainlink issue IDs on future leaves are
optional tracking metadata; they do not gate completion or issue closure. If
the saved plan snapshot and current manifests have already drifted, revision
fails closed and requires a separately verified reconciliation path.

`--continue` remains a strict resume and never accepts an edited plan by itself.
`--recreate` remains the path for replacing a run, not applying a revision.

### Recovering plan drift

`exomonad plan reconcile` and `exomonad plan revise` are implemented CLI
commands. Reconciliation repairs drift in the persisted RunStore bindings from
the accepted `plan.snapshot`; revision then applies an operator-edited
`plan.json` candidate. Neither command starts the controller or dispatches
work.

Use this order when a revision dry run reports `unversioned_plan_drift`:

```bash
exomonad plan reconcile --dry-run
exomonad plan reconcile --apply \
  --reason "Restore RunStore identity from the accepted snapshot" \
  --expected-fingerprint "<fingerprint from the dry run>"
exomonad plan revise --dry-run
exomonad plan revise --apply --reason "Bind issue IDs to future slices"
exomonad init --continue
```

Reconciliation is allowed only when its evidence report proves the proposed
restoration. It preserves completed work and receipts, and it does not change
`plan.json`, `plan.snapshot`, or its digest. If the report has blockers, keep
the run parked and resolve the missing evidence; do not copy a manifest or
rewrite a RunStore by hand. Revision then updates the candidate, snapshot,
digest, changed recursive manifests and runtime bindings, and audit in one
recoverable transaction. A changed manifest passed directly to a continuing TL
controller is refused with these command paths in the error.

For a protected completed declaration, reconciliation requires the exact
accepted snapshot baseline, dispatch identity, and confirmed dispatch record.
If any of those records are missing, conflicting, malformed, or stale, there
is no supported in-place override. Keep the original run and its candidate,
snapshot, checkpoints, and journals intact and parked. The safe recovery path
is to recover the original authoritative dispatch evidence from a trusted run
archive or backup without changing its run, scope, action key, status, or
result; then rerun `plan reconcile --dry-run` and apply only if the report
proves the restoration. If that evidence cannot be recovered, preserve the
parked run for a separately reviewed recovery decision. Do not use
`--continue`, `plan revise`, hand-edited RunStores, or `--recreate` as a
substitute: they cannot prove the old declaration while preserving completed
work.

For legacy spawn records with no leaf invocation ID, the ledger emitter must
match a separately proven scope controller. A root scope uses its canonical run
identity only when the manifest and checkpoint establish a root. A nested scope
uses the controller declared by the accepted plan, correlated with the parent's
durable sub-TL dispatch record and the server invocation registration for that
scope, branch, worktree, and invocation lineage. The nested checkpoint's
`parent_agent_id` identifies its parent controller; it is not the emitter of
the nested controller's leaf events. If a parent slice has moved to an aggregate
integration owner, that value alone does not establish the original controller.
The reconciliation report records the expected and observed emitter identities
and fingerprints the registration used by the proof. A blocked proof leaves the
run parked and requires fresh evidence before apply.

The parent sub-TL dispatch attempt counter is recorded as observed evidence, not
used as dispatch identity. A zero counter is compatible with reconciliation only
when the accepted declaration, parent identity, dispatch intent and invocation,
authoritative sequence, exact branch/worktree/base coordinates, unique recursive
child record, ownership, and host invocation registration all prove the same
dispatch. The audit retains the zero value and its proof condition; reconciliation
never increments or normalizes it. Positive counters do not replace missing proof,
and negative, boolean, or non-integer counters fail RunStore validation.

After `init --continue`, the controller may adopt a uniquely correlated
existing aggregate PR using current repository, owner, invocation, branch, PR,
head, base, and patch evidence. Adoption resumes review and CI tracking for that
head. It does not supply a reviewer verdict, CI result, merge receipt, or
ancestry proof. The ordered plan remains on that stage until its aggregate
integration contract is complete. Missing, stale, or ambiguous publication
evidence leaves the stage parked; do not create another PR or rerun completed
leaves to repair the binding.

### Plan identity writer inventory

This inventory separates initial authority capture and checkpoint migration
from active declaration changes:

| Writer | What it writes | Supported boundary |
| --- | --- | --- |
| `tl_loop/__main__.py` launcher and `rust/exomonad/src/init.rs` snapshot capture | The launcher sends the exact preflight-accepted bytes to Rust `record-plan-snapshot`; Rust records the initial `plan.snapshot` and SHA-256 under the plan-transition lock. Continuation verifies existing bytes. | Establishes a new or legacy baseline. A missing digest can be derived only from the exact saved snapshot. It does not approve an edited active plan. |
| `tl_loop/loop/driver.py` new-run creation | `run.json` manifest, slices, and recursive FSM in the initial checkpoint. | Initial declaration is installed together in one RunStore write. On continuation, a different external plan is refused. Proven legacy-manifest migration remains a compatibility path. |
| `RunStore.checkpoint` | FSM, slices, budgets, event offset, and optional declaration binding in one `run.json` checkpoint. | Routine checkpointing may retain or initially install a manifest, but it rejects a changed manifest identity. It cannot update the accepted snapshot and therefore is not a plan-edit path. |
| `RunStore.set_plan_manifest` | A single `run.json` manifest and its direct slice bindings. | Used for initial installation, verified legacy migration, and explicitly gated plan-authoring acceptance. Ordinary active revisions are rejected here because this method does not update the plan snapshot. |
| `apply_plan_reconciliation` | Eligible recursive `run.json` bindings and a reconciliation audit record. | Journaled restoration from the digest-verified accepted snapshot. It leaves the candidate plan and snapshot bytes unchanged. |
| `apply_plan_revision` | Candidate plan, snapshot, digest, affected recursive `run.json` manifests/slices/FSMs, and revision audit. | The supported path for active declaration changes; it locks participants, rechecks inputs, and commits or replays the transaction. |
| Startup transaction recovery | Replays an existing plan-revision or plan-reconciliation journal. | Completes an already-approved transition. It does not create a new revision or choose a new plan authority. |

The repository contains no separate operational script for changing these
identities. Tests and fixtures may write checkpoint files directly to construct
failure cases; those writes are not supported recovery operations.

### What actually gates a merge

The rule has two authorities and one integrity invariant:

```text
merge_allowed =
      reviewer_go(current_head_sha)  # binding reviewer findings
   && ci_success_or_neutral(current_head_sha)
   && current_head_sha == live_pr_head_sha
```

**a. The reviewer's verdict on binding findings.** The reviewer supplies a
`GO`/`GO-WITH-NITS`/`NO-GO` verdict and structured findings for the exact head it
inspected. The controller records that verdict bound to the head and performs
`merge_pr` mechanically once CI agrees; no model re-judges it. The controller
cannot invent a verdict without reviewer evidence or ignore an unresolved
blocking finding.

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
`GO-WITH-NITS` remains mergeable, and the controller stores every nit in durable
per-head review findings for follow-up. It does not require an external
Chainlink issue writer, because the run checkpoint is the authoritative home
for review evidence.

### The repair path when review says NO-GO

Review convergence has an inner loop and an outer loop, and only the outer loop
uses a model.

**Inner loop: worker <-> reviewer, no model.** While a slice's completed rounds
are below `reviewer_max_rounds`, a reviewer NO-GO, and a pre-merge CI failure on
the reviewed head, go to the same owner through `resume_pr`, with the reviewer's
findings (or the CI failure) verbatim as the handoff's ROOT CAUSE. The slice's own
paths and test plan fill the remaining sections. `resume_pr` resumes the same
owner, worktree, branch, and PR; the controller never creates a new branch, a
`-2` suffix, or a second agent. A round is one completed reviewer verdict or one
first CI failure on a head. This loop works with nothing configured beyond
`harness_policy.toml`.

**Outer loop: review stuck, one `roles.tl` judgment.** When the round count
reaches `reviewer_max_rounds` without convergence, the controller makes one
`roles.tl` judgment whose result is exactly one of:

- `guide`: a seven-section handoff (ROOT CAUSE, PROPOSED SOLUTION, READ FIRST,
  STEPS, VERIFY, BOUNDARY, DONE CRITERIA) validated against the slice's owned
  paths and dispatched through `resume_pr` to the same owner with a fresh round
  budget;
- `escalate`: the slice parks with `review_rounds_exhausted` and the named human
  gate opens.

Interventions per slice are bounded by `roles.tl.escalate_after_attempts`; once
they are used up the gate opens without another model call. Each judgment emits
`tl.review_stuck_judged` and is charged to `roles.tl.token_budget`. If the
judgment cannot be made (backend failure, malformed or out-of-boundary guide,
exhausted budget) the slice parks as `review_stuck` with the error in its
`dispatch_error`; nothing falls back.

**Parent integration failures use one `roles.tl` judgment.** CI failures on a
persisted aggregate PR after child merges and conflicts while merging that
aggregate into its parent both enter `integration_failure`. `delegate` carries
a complete handoff that is checked against the aggregate slice's owned paths
and resumes its existing PR owner; `escalate` opens a named human gate. The
candidate stores the intervention count, `roles.tl.escalate_after_attempts`
bounds calls, and `roles.tl.token_budget` records their cost. At the ceiling or
on a malformed/backend-failed judgment, the controller opens the gate without
falling back to a different repair path. Direct leaf CI failures still go
straight to that leaf's owner through the deterministic inner loop.

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
and `reviewer_max_rounds` controls when the deterministic review loop hands the
slice to the `roles.tl` review-stuck judgment, and so, at most
`escalate_after_attempts` judgments later, to a human.

---

## 3. `.exo/tl-loop/plan.json` — the work

The plan document is a closed-key JSON object. Unknown keys are rejected at
load, not ignored. This document declares the initial recursive work only.
After it is accepted, its canonical recursive manifest is persisted in
`run.json`; activation, evidence, effects, and repository lanes remain mutable
FSM state alongside that immutable declaration.

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
| `agent_type` | no | A policy-bound harness **request**. Omitted means the selector picks from the allowlist |
| `boundary` | no | **Becomes the slice's owned `paths`.** Overlapping boundaries between non-terminal slices are a schema error |
| `verify` | no | **Becomes the slice's `test_plan`** (falls back to `steps`) |
| `done_criteria` | no | Becomes TL-owned reviewer acceptance criteria |
| `context`, `read_first`, `steps` | no | Passed through to `spawn_leaf` as structured spec fields |
| `chainlink_issue_id` | yes on leaves; no on workers/sub-TLs | Binds a leaf to one positive Chainlink issue ID; use `null` for an unlinked leaf |

`boundary` and `verify` are doing double duty: they are both instructions to the
leaf and the controller's own ownership and test-plan records. Two leaves whose
boundaries overlap will fail path-ownership validation rather than race — write
disjoint boundaries or merge the slices.

The TL composes reviewer acceptance criteria from the run-state `test_plan`,
plan-level `verify` and `boundary`, owned paths, and `done_criteria`. The PR
body may document the work, but it is not the authoritative source for review
acceptance.

#### `agent_type` is a request, not an override

`agent_type` on a worker or leaf names the harness you want *policy-bound*.
It narrows the selector's candidate set to the harnesses
`.exo/harness_policy.toml` already approves for the role; it never widens that
approval, and the capability map and the role and per-harness token budgets
still apply to the narrowed set. A bare agent type (`codex`) selects among the
approved entries of that agent type, which keeps the model choice in policy
instead of in the plan; a model-qualified value (`codex/<your-model>`) must name
an allowed entry exactly.

The request is either honored or refused, never silently replaced:

| Situation | Slice parks with |
|-----------|------------------|
| Request names nothing the role's allowlist approves | `harness_request_not_allowed` |
| Approved harness is not rated for the classified difficulty | `no_capable_harness` |
| Approved harness has no budget left | `budget_exhausted` |
| Policy escalation already took the requested harness out of rotation | `harness_request_superseded` |

Each parks with a human-readable gate and names the request and the allowlist,
so a refusal is an operator decision rather than a silent downgrade to a
different harness.

Status output keeps the two dimensions apart: `requested_harness` is your
declared request, `resolved_harness` is the qualified `agent_type/model`
policy actually dispatched, and `agent_type` is the protocol field sent on the
wire. A restart replays the request from `requested_harness`, so a replay can
never mistake a request for the route that ran.

### `workers` — ephemeral, no branch, no PR

```json
{ "name": "audit-deps", "task": "List every crate with a known advisory.", "agent_type": "codex" }
```

Workers accept `name`, `task`, optional `agent_type`, and optional
`chainlink_issue_id`. Workers edit in place or research; they do not produce a
PR. Use them for narrow work where a direct commit to the caller's worktree is
acceptable.

### `chainlink_issue_id` — bind one issue to one slice

Every leaf must include `chainlink_issue_id`: use a positive integer when the
leaf owns a Chainlink issue, or explicit `null` when it has no issue. Omitting
the field is a plan validation error, including for leaves nested under a
sub-TL. Workers and sub-TL nodes do not need synthetic issue IDs. For example:

```json
{
  "leaves": [
    {
      "name": "token-refresh",
      "task": "Implement token refresh.",
      "chainlink_issue_id": 1169
    }
  ]
}
```

Leaf issue bindings are unique across the recursive plan. An issue ID does not
flow from a sub-TL into leaves in its nested plan; each leaf declares its own
binding or explicit `null`.
Put an ID on a nested leaf when that leaf owns a separate issue. Use explicit
`null` for dynamic work that has no Chainlink task.

The sub-TL that owns the slice closes its explicitly bound issue mechanically
after merge adoption and parent-branch sync. The root only orchestrates child
handoff and persisted plan order; it never substitutes a parent ID or closes a
child issue. An unlinked slice has no issue to close, so the CHANGELOG would
otherwise miss its work: after parent-branch sync the controller makes one
`roles.tl` `changelog_coverage` judgment over the `[Unreleased]` section, the
merged paths, and the PR title and body. `captured` writes nothing; `add_entry`
inserts one line under the judged Keep a Changelog section, commits it on the
parent branch, and pushes it with the same compare-and-swap lease as a linked
slice. A missing CHANGELOG, a failed judgment, or a failed write or push becomes
a typed `changelog_outcome` (`no_changelog`, `judgment_failed`,
`entry_write_failed`, `entry_push_failed`) and the slice still completes; a
failed write or push first returns the local parent to its synchronized head.
Each judgment is charged to `roles.tl.token_budget` and emits
`tl.unlinked_changelog_resolved`. The `review_stuck` judgment does not
create issues or epics and never edits the live `plan.json`.

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
      { "name": "token-refresh", "task": "...", "chainlink_issue_id": null },
      { "name": "session-store", "task": "...", "chainlink_issue_id": null }
    ]
  }
}
```

Accepted keys: `name`, `plan` (or inline `workers`/`leaves`/`sub_tls`),
`worktree`, `agent_id`, positive `order`, optional `integration`, and optional
`chainlink_issue_id`. Missing `order` is backward-compatible shorthand for
order 1; zero and negative values are invalid.

A sub-TL has **no** `agent_type`. It is a nested controller process, not a
model session, so a harness request on one was never a truthful field. Declare
the request on the workers and leaves of its nested plan instead. A plan that
still sets `sub_tls[].agent_type` is rejected as an unknown key rather than
silently ignored.

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

### Parent worktree handoff

Before a pending ordered sub-TL is provisioned or dispatched, its parent
controller prepares the authenticated source worktree that the spawn path
uses. The current policy is explicit commit-all: `git add -A` semantics stage
tracked additions, modifications, deletions, and nonignored untracked files
in that one parent worktree. This includes staged and unstaged changes and
changes to tracked files even when their paths match ignore rules. Ignored
untracked files remain ignored. A clean worktree is a no-op and creates no
empty commit.

The runtime records the parent scope, child declaration, source branch, HEAD,
and worktree fingerprint in the durable handoff action. Its receipt reports
`committed_paths` as hexadecimal encoded relative paths included in this
handoff commit, and `remaining_dirty_paths` as paths still dirty afterward.
The deprecated `changed_paths` field aliases `remaining_dirty_paths`; it never
identifies which files the commit captured. The commit uses the
repository's configured Git identity and hooks. A conflict, unsafe in-progress
Git operation, ownership mismatch, hook refusal, active mutation, or changed
worktree blocks the child handoff without discarding the edits. Continuation
reconciles an interrupted commit against the same handoff identity before it
can dispatch. Local handoff commits do not imply that a remote push occurred;
publication and push receipts remain separate requirements of the existing
integration flow.

This first version commits all eligible changes. It does not ask a model to
select files or infer which edits belong to the next child. A narrower,
smarter scope policy can replace commit-all in a later change without
changing the ownership and replay checks.

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
              "chainlink_issue_id": null,
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
              "chainlink_issue_id": null,
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
              "chainlink_issue_id": null,
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
              "chainlink_issue_id": null,
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
              "chainlink_issue_id": null,
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
                    "chainlink_issue_id": null,
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
                    "chainlink_issue_id": null,
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
              "chainlink_issue_id": null,
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
              "chainlink_issue_id": null,
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
              "chainlink_issue_id": null,
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

A direct-leaf dependency chain, with no stage boundary:

~~~json
{
  "run_id": "root",
  "budgets": { "tokens": 200000, "wall_seconds": 7200 },
  "plan": {
    "leaves": [
      {
        "name": "rotation",
        "task": "Implement access-token rotation.",
        "chainlink_issue_id": null,
        "boundary": ["src/auth/rotation.rs"],
        "verify": ["cargo test -p auth rotation"]
      },
      {
        "name": "rotation-docs",
        "task": "Document the merged rotation behavior.",
        "chainlink_issue_id": null,
        "boundary": ["docs/auth/**"],
        "depends_on": ["rotation"],
        "verify": ["just docs-lint"]
      }
    ]
  }
}
~~~

### Recursive integration and recovery

Each completed child publishes one aggregate candidate owned by the direct
parent. Leaf review evidence is bound to each leaf PR's exact head and patch
digest before it merges into the sub-TL branch. The parent verifies completed
child receipts for the aggregate, then checks integration against the current
parent base, aggregate head, merge tree, and CI result:

~~~text
child_completion_allowed =
    every_leaf_receipt.reviewed_head matches its merged head
 && aggregate_publication matches the candidate PR, head, base, and patch

integration_allowed =
    child_completion_allowed
 && integration.base_sha == live_parent_base
 && integration.head_sha == candidate.head_sha
 && integration.merge_tree_sha == live_merge_tree
 && integration.ci_status in {"success", "neutral"}
~~~

Leaf reviewers adjudicate each leaf PR before it merges into its sub-TL branch.
The parent owns aggregate integration but does not run an aggregate reviewer
phase. An integration failure uses the bounded `roles.tl` judgment described
above. Recovery keeps the same aggregate owner and never creates a replacement
PR.

The integration lifecycle shown in `status` and `/control` is:

| State | Meaning | Next legal transition |
|---|---|---|
| RUNNING | Children are still executing | Wait for child completion |
| CHILDREN_MERGED | The child fold completed | Open or reuse the aggregate PR |
| AGGREGATE_PR_OPEN | Aggregate PR and owner are persisted | Verify completed leaf receipts and aggregate publication |
| CODE_REVIEWED | Legacy aggregate-review state retained for compatibility | Reconcile child receipts; do not wait for new aggregate approval |
| READY_FOR_INTEGRATION | Child receipts and exact aggregate publication are verified | Validate base/head/tree/CI evidence |
| NEEDS_BASE_REVALIDATION | The parent base advanced; the aggregate head remains valid | Refresh integration evidence and CI |
| INTEGRATION_VALIDATED | Base, head, tree, patch, and CI all match the snapshot | Persist `MERGING` and call `merge_pr` |
| MERGING | Merge request was durably started | Reconcile live PR state after restart |
| MERGED | The aggregate result is recorded | Advance to the next numeric order |
| REPAIRING_AGGREGATE | Same owner is repairing the aggregate head | Revalidate the repaired aggregate against child receipts |
| INTEGRATION_CONFLICT | The candidate cannot merge cleanly | `resume_pr` the same aggregate owner or open the gate |
| FAILED | The child or integration failed terminally | Inspect the failure and answer any named gate |
| PARKED | A ceiling or human decision stopped progress | Answer the named gate or revise the plan |

Repair and revalidation have separate ceilings. Exhausting either opens a
named gate (tl-integration-conflict or tl-integration-revalidation).
Restart reads the persisted stage, owner, PR, head, base, evidence, and
attempt counters; it never treats a missing event as permission to duplicate a
spawn, PR, review, repair, or merge.

A terminal rejected `merge_pr` result is authoritative history: the merge did
not happen. Startup reconciliation retires that attempt only on exact evidence
(the persisted action's intent id resolves to a rejected `merge_pr` entry for
the same slice, a fresh authoritative watcher observation resolves the same
PR/head, and it proves `merged=false`). It clears the stale in-flight action,
returns the candidate to `READY_FOR_INTEGRATION` through the typed
`merge_not_confirmed` edge, advances the merge attempt counter so a new
approved attempt has a distinct action identity, and appends a
`tl.merge_attempt_retired` disposition. The original rejected entry is never
rewritten. An unavailable observation holds; a contradictory one opens
`tl-merge-retirement-<digest>`. A rejected entry whose PR is authoritatively
`merged=true` at the same head is adopted as the merge, without a second merge.

Retiring a rejected aggregate merge does not generally release a parked slice.
The compatibility recovery for a historical worker-timeout park is narrower:
the saved dispatch boundary must be `aggregate_pr_open`, the persisted
publication/action/candidate must agree on the PR and exact head, and the
authoritative watcher must confirm that same PR is still open at the published
head and branches. Only then does the immutable retirement disposition record
the released `task_budget_exceeded` park and return the stage to `IN_REVIEW`.
Manual parks and timeout parks without that aggregate evidence retain their
status and cause.

#### Recovery/live boundary and work timers

Continuation is a two-phase invocation. During recovery the controller folds
persisted evidence (journal outcomes, watcher observations, ordered/checkpoint
projections), retires proven-rejected attempts, and validates
cursor/ordered-stage/candidate consistency; it performs no live scheduling or
work-timer enforcement. While replayable quarantine evidence is pending it
records a `tl.recovery_boundary` audit event, defers finalization/convergence,
and arms no timer. A recovered projection that disagrees (missing manifest,
negative cursor, ordered plan without stages, candidate or stage member with no
slice) opens `tl-recovery-consistency` and holds with the specific diagnostic.
Only then does live operation resume.

Work timeouts are scoped to the current invocation and to proven active work:

~~~text
eligible = recovery_complete and work_is_active and clock_epoch == current_invocation
budget_charged = current_invocation_active_elapsed >= slice_budget   (monotonic)
~~~

Between-invocation downtime and replay/catch-up consume zero budget: each
invocation builds a fresh monotonic `ActiveWorkClock` whose start table is
empty until live operation observes proven active work, and the historical
`dispatch_started_at` is never subtracted from the current time. Only a slice
executing producer/repair work in this invocation is timed; a completed
producer awaiting parent aggregate integration is never timed, so aggregate
integration cannot be expired by a worker budget. Historical timestamps and the
`deadline_ledger` remain audit facts and are not rewritten.

#### Ordered-child recovery gates

A terminal ordered child whose controller stopped before authoritative
resolution is recovered by `--continue` only when the durable proof holds:
matching plan manifest, child branch and worktree, parent ownership, an
accepted dispatch intent, an exit diagnostic bound to that exact checkpoint
revision, and a retryable exit reason. Any failed proof keeps `tl_failed` and
opens `tl-ordered-child-recovery-<child>`.

Only an `approved` answer releases that gate, and only when the exit-reason
classification is the single unproven fact. `pending` and `rejected` both
leave the run gated, and neither is written back to `pending`, so a decline
survives every later restart rather than being re-asked:

```bash
python3 ~/.exo/tl_loop.pyz gate --project-root . --run-id root \
  --name tl-ordered-child-recovery-<child> --approve
```

Approval is a judgement, not evidence. A gate raised because the recorded branch
is not the declared owner's, publication evidence needs integration
reconciliation, a merge is in flight, or the action journal holds an
unreconciled effect stays gated however it is answered — those require
re-decomposition, not a yes. So does a gate whose failed child cannot be
uniquely identified, because reopening that scope would relaunch its siblings.

An approved recovery relaunches the child with a fresh invocation identity when
its recorded controller has exited. Reusing the old identity would resolve the
pane that already exited and fail delivery instead of recovering. Branch,
worktree, controller identity, PR, head, and publication ownership are preserved:
the child is reopened, never duplicated.

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

### One-shot review orchestration

The controller owns one review path from observation to effect:

```text
watcher facts -> ledger projection -> reducer -> policy intent/wait
             -> EffectJournal -> host effect -> authoritative reconciliation
```

The watcher is a sensor. It never spawns a reviewer, adjudicates a verdict,
repairs a leaf, or merges a PR. A reviewer is one-shot for an exact head and
must persist its terminal GO/NO-GO evidence before exiting. The reducer then
derives either a compare-guarded `merge_pr` (GO + same-head CI success/neutral)
or one same-owner `resume_pr` carrying the findings verbatim (NO-GO/comments or
same-head CI failure) while the slice is below `reviewer_max_rounds`. A new
head clears prior verdict and CI evidence before a new reviewer is derived.

Action intent is journaled before dispatch and keyed by run, slice, operation,
PR, and head. Restart reconciliation reuses that key and the persisted owner;
it never rewinds the ledger, edits `plan.json`, messages an exited process, or
creates a sibling branch. `tl.action_queued`, `tl.action_started`,
`tl.action_reconciled`, and `tl.wait_reason_changed` expose the decision and
the exact missing predicate. Treat those records, rather than pane liveness or
free-form notifications, as the acceptance evidence.

---

## Worked example: a documentation step after the code merges

You want implementation to land, then documentation to be written against what
actually shipped.

### The mechanism

Two ordering mechanisms exist, and they answer different questions.

Numeric `order` on direct sibling `sub_tls` entries is **staged**: use order 1
for implementation and order 2 for documentation. Same-order sub-TLs are
concurrent; different orders wait for all child completion and serialized
aggregate integration before advancing. A stage boundary is a barrier across
every slice it contains.

`depends_on` between sibling leaves of the **same** scope is a **finer edge**: it
names one prerequisite and releases only the dependent. Use it when two leaves
must land in a known order without paying for a stage boundary.

An edge is a dispatch precondition, not a preference. A dependent leaf is not
spawned until its prerequisite reaches `merged` in the base branch the dependent
is dispatched against, and the controller refuses the dispatch rather than
reordering it.

Rules an edge follows:

- **Only leaves.** An edge may not name a worker or a sub-TL, because neither
  merges into a dependent's base. `order` remains the sub-TL contract.
- **Same scope only.** An edge names a direct sibling leaf of the same `plan`.
  Each sub-TL resolves its own leaves' edges inside its own nested plan.
- **No inference.** Edges come only from an explicit `depends_on`. Overlapping
  `boundary` globs and list position are never read as ordering.
- **Declared once, enforced always.** The edges are part of the immutable plan
  manifest and each slice record, so they survive a restart. A revision may
  rewire a leaf that has not dispatched yet and may not touch one that has.
- **A cycle, a self edge, a repeated target, or an unknown name is rejected**
  when the plan is validated, before anything dispatches.

See "Ordered plan examples" for a complete plan that uses an edge.

The example below uses `sub_tls` because the docs leaf must wait for *all*
implementation work, which is what a stage expresses.

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
              "chainlink_issue_id": null,
              "boundary": ["src/auth/**"],
              "verify": ["cargo test -p auth refresh"]
            },
            {
              "name": "session-store",
              "task": "Move session state into the shared store.",
              "chainlink_issue_id": null,
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
              "chainlink_issue_id": null,
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

### When a prerequisite cannot merge

A dependent whose prerequisite can no longer merge is blocked, not left waiting.
`failed` and `blocked` are terminal, so the dependent's edge can never be
satisfied; the controller records the slice as `blocked` with `blocked_by` naming
the prerequisite that ended it, emits `tl.dependency_blocked`, and carries that
forward through the dependent's own dependents.

A `parked` or `dispatch_failed` prerequisite is different. Those are operator
gates: the operator may still answer, so the dependent stays `pending` and waits
rather than being blocked on a question that has not been asked yet.

---

## Letting the model propose the plan

`plan.json` is authority: a human writes it, and the controller dispatches from
it. Plan authoring is a separate, narrower input — a **request** — that the
model may answer with a proposal. It is deliberately not a plan document.

```python
from tl_loop.plan_validation import validate_plan_document
from tl_loop.rlm import (
    PlanAuthoringInput,
    acceptance_gate_name,
    author_plan,
    install_accepted_plan,
    open_acceptance_gate,
    resolve_authoring_model_choice,
)
from tl_loop.select.policy import load_policy
from tl_loop.state.store import RunStore

# The authoring request. It rejects `leaves`, `workers`, `sub_tls`, and
# `plan` outright: a proposal must never be mistaken for a declaration.
request = PlanAuthoringInput.from_mapping(
    {
        "task": "Split the refresh-rotation work into owned slices",
        "read_first": ["rust/exomonad-core/src/handlers/auth.rs"],
        "constraints": ["keep the harness policy decisions out of the plan"],
        "base_ref": "main",
    }
)

# Open the existing run checkpoint before authoring so roles.tl spend is charged
# to the same durable budget used by review-stuck judgments.
store = RunStore("root", ".exo/tl-loop")

# The model is resolved from harness_policy.toml, never from the request.
choice = resolve_authoring_model_choice(load_policy(), backend=backend)

proposal = author_plan(request, model_choice=choice, run_id="root", run_store=store)

open_acceptance_gate(store, proposal)
print(acceptance_gate_name(proposal))
```

What the boundary guarantees:

| Property | How |
|---|---|
| Tools empty | The judgment is the existing stateless `decompose` call; `RlmRequest.tools == ()` and no `EffectClient` is constructed |
| Bounded | At most three attempts; a schema, ownership, or cycle violation is fed back as `validation_feedback` and the final failure raises `DecompositionParked` |
| Policy-owned model | Harness and model come from the role's allowlist and the spend is charged against the same role budget; a model-qualified allowlist entry pins the model |
| Validated | Each `SliceSpec` becomes a leaf, then the whole document passes the same `validate_plan_document` closed-key validator `plan.json` uses, and `build_plan_manifest` produces the declaration |
| Inert | The proposal is recorded durably with its bounded audit, and becomes authority only through the acceptance gate |

The RLM gains nothing. It cannot read the filesystem, run a shell, touch git,
spawn, call MCP, or dispatch an effect, and the authoring request deliberately
withholds the harness request and budgets so the judgment cannot reason about
authority it does not hold.

### Acceptance is a human gate bound to one digest

The gate name is derived from the proposal's manifest digest, so approving one
proposal never approves a different one.

```python
from tl_loop.state.schema import GateStatus

open_acceptance_gate(store, proposal)          # PENDING, durable
store.answer_gate(acceptance_gate_name(proposal), GateStatus.APPROVED)
state = install_accepted_plan(store, proposal)  # now the manifest
```

Before approval, `require_acceptance` and `install_accepted_plan` both raise
`PlanAcceptanceRequired`; a rejection raises `PlanRejected` and is terminal.
Because the gate lives in run state, a restart before acceptance still has no
authority and a restart after acceptance keeps exactly the accepted digest.
Installation goes through `set_plan_manifest` with dispatched nodes protected,
so a continuation cannot rewrite ownership that already ran.

### The operator surface

`plan-authoring` reads the durable record, so a second process finishes the
flow without re-running the judgment:

```bash
# What was proposed, what it needs, and the bounded audit record.
python3 ~/.exo/tl_loop.pyz plan-authoring --project-root . --run-id root

# Answer the named gate, then install the recorded proposal.
python3 ~/.exo/tl_loop.pyz gate --project-root . --run-id root \
  --name plan-acceptance-<digest prefix> --approve
python3 ~/.exo/tl_loop.pyz plan-authoring --project-root . --run-id root --install
```

`--install` without an approved gate exits non-zero and dispatches nothing.

### What is persisted

Only the validated plan identity and a bounded judgment audit record, in
`.exo/tl-loop/<run_id>/plan-authoring.json`:

```json
{
  "schema_version": 1,
  "run_id": "root",
  "plan_digest": "<manifest sha256>",
  "gate_name": "plan-acceptance-<digest prefix>",
  "status": "pending",
  "owned_branch": "main",
  "document": { "run_id": "root", "plan": { "leaves": [] } },
  "audit": {
    "judgment": "decompose",
    "model": "<the model that made the judgment>",
    "attempts": 2,
    "tokens": 418,
    "failures": 1,
    "violations": ["output.slices[0]: required key is missing"]
  }
}
```

The record is a sidecar, not run state: it grants no authority on its own, and
the accepted identity reaches run state only through `set_plan_manifest`. On
read, the stored document is re-validated and re-hashed against the recorded
digest — the digest the gate is named after — so a hand-edited body cannot
borrow an approval. The audit carries only scalar dimensions and truncated
validation reasons; the model's prose, the request, and the plan body never
enter it.

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

For continuation, first inspect the persisted checkpoint with `status` and
prefer running without `--plan`; the controller reconstructs the complete
recursive work from `run.json.plan_manifest`. Do not restore or hand-edit
`run.json` or use a changed external plan to bypass a gate. Inspect
`scope_path`, `manifest_revision`, `current_order`, `next_transition`, action
and journal identities, post-merge phase, and repository-lane coordinates when
diagnosing a blocked run.

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

### Recovering a slice already in review

If a controller was started before reviewer spawning was enabled, a slice can
remain `in_review` with an open PR but no reviewer claim. Do not edit the
checkpoint or create a reviewer manually. Rebuild/install the TL controller,
then restart the same run so startup reconciliation can claim the exact PR
head idempotently:

~~~bash
just tl-loop-archive
cp tl_loop.pyz ~/.exo/tl_loop.pyz
python3 scripts/check_tl_loop_archive.py ~/.exo/tl_loop.pyz --source "$PWD/tl_loop"
python3 -m tl_loop status --project-root /path/to/project --run-id root
python3 -m tl_loop run --project-root /path/to/project --run-id root
~~~

Every archive carries a source fingerprint containing the Git commit and a
deterministic hash of the archived `tl_loop/` tree. The status output includes
`controller_fingerprint`; a `stale` status means the running archive is not the
same source as the checkout. After source edits, run `just install-all-dev`, then
repeat the check before restarting the run:

~~~bash
python3 scripts/check_tl_loop_archive.py ~/.exo/tl_loop.pyz --source "$PWD/tl_loop"
~~~

Controller preflight rejects the mismatch before any leaf or reviewer is
spawned. The Rust launcher also checks that the installed server and embedded
controller were built from the checkout revision; --recreate resets state but
never updates either artifact. Run just install-all-dev to repair a mismatch.

The run must retain its existing state root and ledger identity. Verify that
the slice's `reviewer_attempt` contains the PR head and that exactly one
reviewer spawn event exists before continuing the normal review/CI flow.

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
| `review_rounds_exhausted` | Review-round ceiling reached and the `roles.tl` judgment escalated, or `roles.tl.escalate_after_attempts` guides were already spent | Human reads the PR and answers the named gate |
| `review_stuck` | The `roles.tl` review-stuck judgment failed (backend error, unusable guide, exhausted budget) or a repair dispatch failed | Read the PR and the slice's `dispatch_error`; fix `roles.tl` or the plan and resume |
| `harness_switch_requested` | The configured harness could not proceed | Approve explicitly; set `EXOMONAD_ALLOW_HARNESS_SWITCH=1` |
| `stall_detected` | Dead pane or no progress past the heartbeat threshold | Investigate the worker |
| `task_budget_exceeded` | A declared wall-clock slice ceiling elapsed | Use the authenticated per-slice reset only after inspecting the parked task; it restarts that task clock and adjusts its token reservation without changing global ceilings |

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
| `POST` | `/control/runs/{run_id}/children/{child_run_id}/slices/{slice_id}/task-budget` | Reset one nested slice with an existing PR parked for `task_budget_exceeded`; compare-and-set both run revisions and record the operator calibration event |

The reset body carries `expected_parent_revision`, `expected_child_revision`,
`expected_attempt`, and `expected_charge_attempt` from the latest read model. It
also carries the current and selected task timeout, token estimate, a
`task_type` label, an `operator_reason`, an `idempotency_key`, and optional
`observed_tokens`. Equal timeout or token values are valid: resetting a stale
clock does not imply the task consumed more tokens.

### A gate name is one percent-encoded path segment

A per-slice gate name embeds its slice id (`tl-dispatch-failed-<slice>`,
`tl-dispatch-ownership-conflict-<slice>`, `tl-post-merge-<slice>`), and a
slice id may contain `/`. Since `/` separates levels on the gate route, a gate
name is carried as exactly one percent-encoded segment
(`exomonad::control_gate_name`):

- The RFC 3986 unreserved set `A-Z a-z 0-9 - . _ ~` passes through unchanged.
- Every other byte becomes an uppercase `%XX` triplet, so `/` becomes `%2F`.
- The router decodes the segment back to the raw name, which is the same string
  the CLI passes to `gate --name`.

```bash
# gate: tl-dispatch-failed-feat/auth
curl --unix-socket .exo/server.sock -H "X-Exomonad-Control-Credential: $EXOMONAD_CONTROL_TOKEN" \
     -H "Content-Type: application/json" -X POST \
     http://localhost/control/runs/root/gates/tl-dispatch-failed-feat%2Fauth \
     --data '{"decision":"approve"}'
```

An ordinary gate name is unchanged by the encoding, so existing operators and
consoles keep working. The encoding is injective: a gate literally named
`tl-dispatch-failed-feat%2Fauth` is `tl-dispatch-failed-feat%252Fauth` on the
wire, so two gates never answer each other. `.` and `..` are refused in any
encoding; a `/` inside a name is data, not a traversal, because a gate name is
matched against recorded gates and is never used as a path. A `run_id` is
different — it is a real directory under `.exo/tl-loop/`, so it stays a single
path component.

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

**The console can read projections, answer an existing named gate, propose an
inert plan mutation, and reset the budget of one parked slice. A reset requires
expected parent and child revisions, the current attempt and budgets, a task
class, an operator reason, and an idempotency key. It updates only that slice’s
wall-clock limit and token reservation, canonically reopens its existing scope,
and writes `operator.task_budget_reset` to the event ledger. The reset itself creates no PR. If the durable review already requires repair, the controller can resume the same PR owner; the reset key gives that authorized retry a distinct action-journal identity. The optional
`observed_tokens` value is recorded separately from the new estimate. The
endpoint cannot raise a global budget ceiling, merge a PR, approve a review, set
a verdict, or widen `harness_policy.toml`.

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
judgment like `decompose` and the `review_stuck` guide/escalate judgment: closed output schema,
`tools=()`, no effect client, no filesystem capability.

The model **translates; it does not decide**. Its output passes the same
validation and authority checks as the equivalent CLI argument, and `Unclear`
is a first-class result — it asks rather than guessing, because a misread
instruction moves real work.

Agent-authored text in the read model is tagged `observation_only` and is never
presented to the judgment as instruction. That provenance envelope is the
prompt-injection boundary.

The same boundary holds for plan authoring: the model may *propose* a typed
plan through bounded judgment, but only a validated and explicitly accepted
proposal becomes execution authority. See
[Letting the model propose the plan](#letting-the-model-propose-the-plan).

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
| `tl.aggregate_publication_gate_superseded` | An exact, verified aggregate merge resolves its obsolete publication gate |
| `tl.integration_validated` / `tl.integration_revalidated` | Base-bound integration evidence is accepted or refreshed |
| `tl.integration_base_invalidated` | The integration base changed and evidence must be refreshed |
| `tl.unlinked_changelog_resolved` | An unlinked merged slice completed with its CHANGELOG decision and outcome |
| `tl.integration_failure_judged` | One roles.tl judgment chose `delegate` or `escalate` for an aggregate CI failure or parent merge conflict |
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
"codex/<your-model>" = "standard"
```

Every key is the *same* string the policy allowlist uses. A map keyed on a
different model is the same failure as a missing key: preflight rejects it with
`missing capability entry for codex/<model>` before a run starts.

Run `python3 ~/.exo/tl_loop.pyz preflight --project-root .` to validate all four files before starting the controller.
