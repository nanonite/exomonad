# ADR: Slice identity, attempt identity, and PR ownership

Date: 2026-08-21
Status: Proposed
Issue: #933

## Context

A TL plan slice is permanently bound to the first pull request it publishes.
Once `tunable-operator-body` filed PR #42, every subsequent run of that slice
re-adopted #42 — after the PR was closed, after its branch was deleted, and
after the checkpoint was archived. The only escape found in practice was
renaming the leaf in `plan.json`, which changes the identity of the planned
work in order to discard a dead delivery attempt.

### What the code actually does

`rust/exomonad-core/src/services/pr_registry.rs:214`:

```rust
pub fn resolve_pr_number_for_slice(heads: &[PublishedHead], slice_id: &str) -> Option<u64> {
    ...
    matching()
        .rfind(|head| head.provenance == PublicationProvenance::LedgerOwned)
        .or_else(|| matching().next_back())
        .map(|head| head.pr_number)
}
```

The lookup is unconditional. It has exactly one caller,
`rust/exomonad-core/src/handlers/agent.rs:1706`, inside `watcher_pr_state`,
where an empty `pr_number` is filled in from `slice_id`.

`PublishedHead` documents itself as the opposite of a lifecycle record:

> This is publication metadata for the existing issue-owned PR. It is not a
> second owner or a process lifecycle record; invocation fields only explain
> which attempt filed the publication when that metadata was available.

### The three findings that matter

**1. An attempt dimension already exists on both sides, unused for resolution.**
`PublishedHead` carries `invocation_id`, `invocation_trigger`, and
`invocation_runtime`. `SliceState` carries `attempts`, `repair_attempts`, and
`merge_attempts`. Neither participates in `resolve_pr_number_for_slice`. The
data model already distinguishes attempts; only the resolver collapses them.

**2. `resolve_pr_number_for_slice` is two questions wearing one signature.**
"What PRs has this slice ever published?" is a history question the registry is
designed to answer. "Which PR should the watcher poll for this slice right
now?" is a liveness question the registry explicitly disclaims. The single call
site asks the second question and receives an answer to the first.

**3. `PARKED` has no exit.** After #932 a closed-unmerged PR parks with
`PR_CLOSED_UNMERGED`, which is correct. But dispatch only consumes `PENDING`
slices (`tl_loop/loop/schedule.py:57`), and the only transitions that clear
`park_cause` (`driver.py:1412`, `:1847`, `:3690`) set `SPAWNED` or
`DISPATCHING` on authoritative `agent.spawned` correlation — dispatch
confirmation, not general un-parking. Answering a human gate resolves
action-journal entries only (`driver.py:1327-1339`); it never touches slice
status. A correctly abandoned attempt is therefore terminal for the work.

The gap is not error reporting. #932 reports this state accurately. The gap is
that accurate reporting has no continuation.

## Decision

Separate three identities that are currently conflated:

- **Slice identity** — the planned unit of work. Stable for the life of the
  plan. Never renamed to escape a delivery outcome.
- **Attempt identity** — one bounded effort to deliver a slice. Ordered,
  countable against the retry ceiling, and the unit that can be abandoned.
- **Publication** — the immutable record that an attempt filed a PR at a head.
  Append-only history; never mutated, never deleted.

A slice owns an ordered sequence of attempts. An attempt owns at most one PR.
At most one attempt is live at a time. Abandonment is an attempt-level event
that leaves both the slice and the publication record intact.

### Query split

Replace the single resolver with two explicitly named functions:

- `publication_history_for_slice(heads, slice_id) -> &[PublishedHead]` —
  unfiltered history, for evidence, audit, and recovery.
- `resolve_live_pr_for_slice(heads, slice_id, ...) -> Option<u64>` — the PR the
  watcher should poll, which excludes publications belonging to abandoned
  attempts and PRs in a terminal state.

`watcher_pr_state` calls the second. Nothing else changes call sites, because
there is only one caller today.

### Abandonment is derived, not stamped

Do not add `superseded`, `abandoned_at`, or `state` to `PublishedHead`. Doing so
would contradict the type's stated contract and put mutable lifecycle state in
an append-only evidence file. Instead:

- The **operator decision** to abandon is a durable ledger event carrying
  slice id, attempt/generation, PR number, head SHA, and cause.
- The **PR's terminal state** is read from the forge at query time, which
  `watcher_pr_state` already does.

`resolve_live_pr_for_slice` combines the two. The registry stays pure history,
satisfying the constraint that publication entries are never rewritten.

## Options considered

### Option A — State-filtered resolution only

Filter terminal PRs out of `resolve_live_pr_for_slice`; make no other change.

Small, and it stops a closed PR being re-adopted. But it does not give the
slice a way back to `PENDING`, so the work still does not resume. It also
silently changes the meaning of a shared function rather than naming the two
questions. Necessary, not sufficient.

### Option B — Attempt-scoped ownership (recommended)

Option A plus an explicit attempt dimension: an abandonment ledger event, an
attempt/generation on the slice, and a controller transition from an abandoned
attempt to a fresh dispatchable one that consumes the retry ceiling.

Reuses `invocation_id` on `PublishedHead` and `attempts` on `SliceState`, both
of which already exist. Keeps the registry append-only. Costs a new durable
event type, a new transition, and an operator verb.

### Option C — Separate live-ownership store

Introduce a `slice_pr_ownership` record as the authoritative live binding, and
demote `published-heads.json` to pure archive.

Cleanest conceptually, but it adds a second durable store that must be kept
consistent with the ledger and the registry, and it duplicates state that
Option B derives. The registry's existing `invocation_*` fields make the extra
store redundant. Rejected as over-engineering for the observed problem.

### Option D — Rename the slice (status quo workaround)

Rejected. It changes the identity of planned work to discard a delivery
attempt, breaks `depends_on` edges and cross-slice references, splits a single
work item's history across two identities, and scales into the `-2` / `-retry`
auto-suffix pattern that `.exo/roles/devswarm/context/root.md` forbids.

## Consequences

- `resolve_pr_number_for_slice` is removed in favour of two named functions.
  There is one caller, so the blast radius is small.
- A closed or abandoned PR stops being re-adopted, which removes the
  `--recreate` dead end observed in the beast workspace.
- `PARKED` gains a documented exit for the abandonment case only. Other park
  causes keep their current terminal semantics.
- Abandonment consumes an attempt, so it cannot be used to bypass the retry
  ceiling. This must be explicit rather than incidental.
- Recursion is unaffected by construction: sub-TLs run through the same
  `tl_run` -> `run_tl_loop` -> reconciliation path, so any behaviour defined
  here applies to nested slices without a second implementation.
- Existing `published-heads.json` files remain valid. Entries lacking
  `invocation_id` are treated as a single implicit attempt, consistent with how
  `PublicationProvenance::Legacy` is already handled.

## Resolved decisions

These were open during research and are now decided (#933).

### Abandonment is operator-only

The controller never abandons an attempt on its own, including on
`PR_CLOSED_UNMERGED`. A human closing a PR is a decision, and automatic
abandonment would erase the distinction between that decision and a fault.
The controller parks with the existing auditable cause and waits. Abandonment
is an explicit operator verb that emits the durable abandonment event.

### A fresh attempt starts from the slice spec

Re-dispatch after abandonment builds from the plan's spec for that slice, not
from the abandoned attempt's branch. The abandoned work is discarded, not
inherited. This keeps a retry deterministic and reproducible from the plan
alone, and avoids silently carrying forward the defects that caused the
abandonment.

### The abandoned branch and worktree are disposed

Abandonment disposes the attempt's worktree and branch through the existing
resource-disposal path shared with `cleanup_leaf` and the orphan reconciler.
It does not introduce a second teardown path. The publication record in
`published-heads.json` remains as the durable evidence that the attempt
existed and at which head; the working artifacts do not.

### `watcher_pr_state` strictly reflects Forgejo

`watcher_pr_state` is a sensor over the forge. Its response describes what
Forgejo says about a pull request, and nothing else. It must not decide, or
report, that a slice has not published a PR.

The current behaviour violates this. When `slice_id` resolves to nothing,
`handlers/agent.rs:1706` returns:

```
no published PR found for slice_id '<id>' in the publication registry
```

That is a controller-domain statement about plan state, emitted by a forge
sensor, and it is indistinguishable from a genuine Forgejo failure. It is also
the same string whether the slice never published, published and was
abandoned, or published under a different identity.

Therefore the slice-to-PR resolution moves out of the observation call:

- A dedicated effect resolves a slice to its live PR, returning a typed
  absence rather than an error when there is none. The controller distinguishes
  "never published", "all attempts abandoned", and "published, live PR is N".
- `watcher_pr_state` takes a concrete `pr_number`. It stops accepting
  `slice_id`, and stops reading the publication registry.
- `tl_loop` owns the meaning of absence. A slice with no live PR is a
  controller state, reported by the controller, with its own diagnosis.

The controller must not read `.exo/published-heads.json` from disk to achieve
this. Registry access stays behind an effect, consistent with the boundary that
Rust executes I/O and the controller consumes typed results.
