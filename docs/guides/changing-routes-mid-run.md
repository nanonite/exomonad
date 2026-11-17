# Changing a run's harness, model, or effort mid-run

A run's route is the operator's configuration, not the child's request. A
mid-run change is a **routing transition**: the successor generation runs the
currently configured route and the old route is retained as audit evidence. It
is not an owner, scope, worktree, predecessor, epoch, or process-identity
conflict.

This guide covers the one supported route seam and every record that can accept
or refuse a configured change. It does not add a per-slice harness/model
selector; `plan.json` still cannot choose a route.

## The one seam

A slice's harness/model/effort is resolved in exactly one place:

| Step | Where | What it decides |
|------|-------|-----------------|
| harness | `tl_loop/select/agent_type.py:select_agent_type` | difficulty + capability + the role's `.exo/harness_policy.toml` allowlist + role/per-harness budget, narrowed by an optional plan request |
| model | `tl_loop/select/model.py:select_model` / `select_model_for_difficulty` (called from `_selected_spawn_model`) | catalog tier for the selected harness, or the configured/requested model |
| effort | `tl_loop/loop/driver.py:_dispatch_route_fields` | `config.worker_effort_level` plus the configuration/policy/manifest fingerprints |
| route proof | Rust `spawn_leaf_subtree` / `resume_pr` / `resume_blocked_leaf` | accepted role route (configuration fingerprint + dispatch-intent event) or the configured role route |
| transition row | Rust `configured_route_transition_id` / `record_configured_route_transition` | `agent.harness_switch` carrying old and new harness/model/effort, prior invocation/generation, worktree fingerprint |

`tl_loop/tests/test_route_seam.py` is a structural test over every
`select_agent_type`, `select_model`, `select_model_for_difficulty`, and
`_dispatch_route_fields` call site in `driver.py`. A new call site fails the
test, so a second resolution path cannot appear silently.

## Which configuration source is authoritative

The Rust spawn guard authorizes a harness when **either** source names it for
the role:

1. `config.toml`'s resolved role route (`spawn_agent_type` for worker/dev,
   `[reviewer]` for reviewer), or
2. the role's `allow` list in `.exo/harness_policy.toml`.

The `allow` list is not a wildcard: it is scoped per role (`tl`, `worker`,
`reviewer`). A harness allowlisted only for the reviewer is still an explicit
cross-harness request for a worker. A request outside both sources keeps the
human-approval rule: `[STUCK: harness-switch]` and `agent.stuck` unless the
operator sets `EXOMONAD_ALLOW_HARNESS_SWITCH=1`.

The Rust and Python definitions are pinned to one fixture,
`rust/exomonad-core/tests/fixtures/harness_policy_agreement.{toml,json}`.
`rust/exomonad-core/src/services/harness_policy.rs` and
`tl_loop/tests/test_harness_policy_agreement.py` both canonicalize
`harness/model` identifiers to the same entries, grammar, and route decisions.

`exomonad init` does not widen either source. Before it starts or continues a
controller, it checks that the worker and reviewer routes resolved from
`config.toml` (including CLI/environment overrides) agree with the same role's
`allow` list:

- the route's harness must appear in `roles.<role>.allow`;
- when the route names a model and every allow entry for that harness is
  model-qualified, the model must equal one of those entries; a bare
  `harness` entry allows any model of that harness.

A disagreement is a hard error naming the role, the resolved `harness/model`,
the config field and file, the allow list, and both files the operator can
change (`config.toml` field or `roles.<role>.allow` in
`harness_policy.toml`). There is no bypass flag, environment switch,
warning-and-continue, or automatic edit. A missing policy is left to the
controller's existing startup error; a malformed policy is parsed once by the
shared Rust definition. `root_agent_type` is legacy compatibility only: the
Python controller does not read it, and `roles.tl.allow` is validated by
`tl_loop` startup, so init does not check a TL route.

## Operator actions

| Goal | Change | Restart? | Approval flag? |
|------|--------|----------|---------------|
| configured model change | `worker_model` (or `[opencode] worker_model`) in `.exo/config.toml` | yes, the server and controller read it at startup | no |
| configured effort change | `worker_effort_level` in `.exo/config.toml` | yes | no |
| configured harness change (Codex -> OpenCode or back) | `spawn_agent_type` in `.exo/config.toml` **and** add the harness to `[roles.worker].allow` in `.exo/harness_policy.toml` (plus `cost_rank`, optional `per_harness_budget`) | yes | no |
| explicit non-configured harness request | do not edit config; the child asked for a harness outside both sources | no | only `EXOMONAD_ALLOW_HARNESS_SWITCH=1`, audited |
| policy-escalated switch after NO-GO | add the replacement harness to the same role's `allow`; the selector removes the failed harness after `escalate_after_attempts` | yes | no |
| reviewer harness/model/effort change | `[reviewer]` in `.exo/config.toml` **and** the matching `roles.reviewer.allow` in `.exo/harness_policy.toml` (see [Reviewer](#reviewer)) | yes | no |

Changing only the allowlist is enough for the *guard*, but init now rejects a
configured route the allowlist does not authorize. Changing only
`spawn_agent_type` is enough for the default route, but the selector still
cannot select a harness absent from `allow`, and init rejects the
disagreement. Change both sources together; init catches a mismatch before the
controller starts.

## Guard, record, log, and budget audit

`PASS` means a configured harness/model/effort change is accepted as a routing
transition. `REFUSE` means the record enforces a different invariant and still
rejects a genuinely invalid run.

| Guard / record / log / budget | What it records or enforces | Configured change | Test |
|-------------------------------|-----------------------------|-------------------|------|
| Rust `enforce_harness_switch_policy` / `harness_switch_decision` | rejects an explicit harness outside config + role allowlist; approves only the flag | PASS for configured/allowlisted; REFUSE (with `[STUCK: harness-switch]`) for an unlisted explicit request | `allowlisted_worker_harness_is_not_refused_as_a_cross_harness_switch`, `unlisted_worker_harness_is_still_refused_with_stuck_guidance`, `explicit_unconfigured_cross_harness_request_uses_only_the_approval_seam` |
| tl_loop `authorize_harness_switch` | same allow-or-flag definition, canonicalized to agent types | PASS; agrees with Rust on the shared fixture | `test_harness_policy_agreement.py`, `test_declared_harness_switch_is_allowed_and_audited` |
| `resume_pr` configured-route transition | current configured route is the successor; records `agent.harness_switch` with old/new route | PASS both directions | `configured_codex_to_opencode_resume_pr_records_a_routing_transition`, `configured_opencode_to_codex_resume_pr_records_a_routing_transition` |
| `resume_blocked_leaf` / terminal-owner recovery | accepted role route or current configured route; journal, intent, checkpoint | PASS; replay dispatches nothing new | `test_resume_with_changed_configured_harness_dispatches_one_opencode_successor` |
| `invocation.json` | current invocation runtime, model, effort, generation, `prior_invocation_id`, recovery round | PASS; replaced by the successor route | the two configured `*_resume_pr` tests |
| `identity.json` | current owner route (`agent_type`/`model`/`effort`/`role_route`) | PASS; updated to the successor route. The prior route survives in the transition row and prior lifecycle rows, not in identity | the two configured `*_resume_pr` tests |
| `routing.json` | owner agent, order/invocation id, generation | PASS; it carries no harness/model/effort | `validate_resumed_owner` path in the H4 test |
| `dispatch_intent` | exact intent that authorized the launch | PASS; a changed route changes the intent, never reuses a predecessor intent | H4 test, `configured_*_resume_pr` tests |
| `.exo/published-heads.json` / publication ownership | PR, branch, head, slice, owner, invocation succession | PASS; route-independent. A missing or foreign publication is still REFUSED | configured `*_resume_pr` tests |
| ledger `agent.spawned` / `agent.resumed` / `agent.invocation.*` | actual launched route and lifecycle | PASS; successor rows carry the new route | configured `*_resume_pr` tests, H4 test |
| ledger `agent.harness_switch` | `transition_id`, scope, PR/head, prior invocation/generation, from/to harness/model/effort, worktree fingerprint, authorization source | PASS; exactly one row per transition, idempotent on replay | configured `*_resume_pr` tests, `configured_route_transition_audit_is_idempotent_and_conflict_checked` |
| ledger `agent.stuck` | human-guidance refusal for an unconfigured explicit request | PASS is silent; only the REFUSE path writes `kind=harness_switch_disallowed` | `unlisted_worker_harness_is_still_refused_with_stuck_guidance` |
| reviewer exact-head record | reviewer route and invocation are separate; the reviewed head binds the verdict | PASS; a worker route change does not alter the reviewer route or verdict binding | `worker_harness_change_does_not_change_the_reviewer_route` |
| `[reviewer]` route (`agent_type`, `model`, `effort_level`) | the next one-shot reviewer spawn resolves the currently configured reviewer route | PASS; the next spawn uses the new harness/model/effort and records them plus `config_sources`. The prior route's invocation is not rewritten when the harness changes identity, and remains durable lifecycle evidence when the same harness replaces its own record | `reviewer_route_change_codex_to_opencode_runs_new_route_and_keeps_prior_invocation`, `reviewer_route_change_opencode_to_codex_runs_new_route_and_keeps_prior_invocation`, `reviewer_model_only_change_updates_the_next_invocation_and_keeps_the_prior_lifecycle`, `reviewer_effort_only_change_updates_the_next_invocation` |
| reviewer internal identity and birth branch | `AgentIdentity::new(slugify(review_branch), configured reviewer runtime)` | PASS; a harness change gives the replacement reviewer a different internal name and birth branch (`review-pr-N-codex` vs `review-pr-N-opencode`) for the same exact head | `reviewer_harness_change_changes_internal_name_and_birth_branch`, `reviewer_harness_change_opencode_to_codex_changes_internal_name_and_birth_branch` |
| durable reviewer assignment lookup (`resolve_reviewer_invocation`) | selects the reviewer bound to an exact PR head from agent directories, compatibility event files, or the ledger | PASS; when the old and replacement reviewer both match the same PR/head, one pure rule picks a live invocation first, else the most recently started, else the highest generation, else the smallest agent id. The result does not depend on `read_dir`/file order, and another head or PR is never selected | `reviewer_harness_change_selects_the_replacement_in_both_candidate_orders`, `reviewer_harness_change_selects_the_replacement_in_both_directions`, `reviewer_harness_change_resolves_the_replacement_directory_in_both_creation_orders`, `reviewer_harness_change_opencode_to_codex_resolves_the_replacement_directory_in_both_creation_orders`, `a_live_old_reviewer_outranks_a_newer_terminal_reviewer`, `equal_ties_are_broken_by_agent_id_not_collection_order`, `directory_event_file_and_ledger_sources_select_the_same_reviewer`, `another_head_or_another_pull_request_is_never_selected` |
| Forgejo review attribution (`resolve_review_author`, `resolve_durable_review_author`) | shared reviewer account login + the durable reviewer invocation for the exact PR/head | PASS; after a harness change a review submitted by the one configured reviewer account is attributed to the replacement reviewer identity. A review from any other login, and an owner self-review, are still refused | `review_author_after_a_reviewer_harness_change_resolves_the_replacement`, `durable_review_author_after_a_reviewer_harness_change_is_the_replacement` |
| `Reviewer-Agent` / `Reviewer-Birth-Branch` PR-body metadata | current reviewer assignment written by `persist_reviewer_assignment` | PASS; the replacement assignment replaces the old markers (one assignment, not two). It is display/registry metadata only; exact-head authorization uses the durable invocation lookup above, not the PR body | `reviewer_harness_change_rewrites_the_pr_reviewer_assignment_metadata` |
| reviewer invocation dedup / `ReviewerAttempt` | per-PR/head attempt identity and live-invocation reconciliation | PASS; a mid-flight reviewer keeps its attempt and is neither duplicated nor killed by a route change, including when a replacement reviewer directory for the same head already exists. An exited old-route invocation is reconciled to `Failed`, after which the new route can review the same head | `test_route_change_does_not_duplicate_or_kill_a_midflight_reviewer`, `test_reconcile_reviewer_attempt_marks_exited_old_route_reviewer_failed`, `test_reviewer_attempt_is_current_is_bound_to_the_attempt_head_and_id` |
| persisted reviewer verdict (`GO` / `GO-WITH-NITS` / `NO-GO`) | verdict is bound to `reviewed_head` and durable review evidence | PASS; `derive_next_action` has no reviewer-route input, so a route change cannot invalidate, re-request, or re-run a verdict for the same exact head. A head change still clears the verdict and queues one reviewer for the new head | `test_persisted_verdict_is_configuration_independent_and_not_re_requested`, `test_no_reviewer_route_input_changes_the_same_state_decision`, `test_head_change_invalidates_the_verdict_and_queues_one_reviewer_for_the_new_head` |
| reviewer identity in the slice state machine | `ReviewerIdentityObserved` updates the reviewer identity for the same head | PASS; `slice_transition` accepts a verdict bound to a replacement reviewer identity for the same exact head. This is a pure state test; author attribution after a route change is proven by the Rust `resolve_review_author`/`resolve_durable_review_author` tests, not by this state test | `test_verdict_from_a_replacement_reviewer_identity_is_accepted_for_the_same_head` |
| `review_rounds`, `tl_intervention_attempts`, `reviewer_max_rounds` | durable review budget and review-stuck judgment | PASS; these are not reviewer-route inputs (a structural test fails if one is added). A route change is not a round event, does not reset or double count either counter, and the ceiling still parks `review_rounds_exhausted` | `test_review_rounds_and_interventions_count_once_per_verdict_not_per_configuration`, `test_review_stuck_still_fires_at_the_ceiling_independent_of_configuration`, `test_reviewer_route_is_not_an_input_of_the_run_state_machine` |
| init reviewer model/effort validation | the reviewer's configured harness selects the catalog validator | PASS; Codex validates `gpt-` and the live model catalog; OpenCode validates the #1358 committed table. Unknown model, unrecorded effort and unsupported effort each fail naming the table file | `reviewer_opencode_model_in_the_version_table_with_recorded_effort_passes`, `reviewer_opencode_unknown_model_names_the_model_and_table_file`, `reviewer_opencode_unrecorded_effort_names_the_model_and_table_file`, `reviewer_opencode_unsupported_effort_lists_the_recorded_variants`, `reviewer_codex_model_without_the_gpt_prefix_is_refused`, `changing_the_reviewer_harness_changes_which_validator_runs` |
| reviewer harness request surface | `spawn_reviewer` / `restart_review` effect arguments | PASS; neither carries harness/model/effort/agent-type, so there is no unguarded per-call reviewer harness override. A future field fails the structural test | `reviewer_effect_requests_cannot_override_the_configured_route` |
| reviewer failure escalation | selector removes a failed worker harness after `roles.<role>.escalate_after_attempts` | N/A; no selector call and no model/harness escalation exists for the reviewer role. The configured `[reviewer]` route is the base and is not widened by an escalation | `test_no_reviewer_harness_escalation_path_exists` |
| `escalate_after_attempts` | slice attempts + verdict decide when a harness leaves rotation | PASS; a configured change does not reset the counter, and a NO-GO on harness A selects another allowlisted harness | `test_no_go_escalates_only_after_attempt_threshold`, H4 test |
| role budget / `per_harness_budget` | a dispatch reserves against the role and the selected harness; a same-slice `resume_pr`/`resume_blocked_leaf` continuation reserves again, on the route it actually runs | PASS for a new dispatch attempt: harness B is charged and harness A's spend is kept. PASS for a continuation: the resume-time route is charged (harness B), A's earlier spend is untouched, and the same ledger reconcile settles it | `test_harness_change_charges_the_new_harness_and_keeps_the_old_spend`, `test_harness_budget_is_charged_to_the_harness_that_actually_runs`, `test_configured_harness_switch_blocked_resume_charges_the_new_harness`, `test_review_repair_continuation_charges_the_configured_new_harness` |
| owner / scope / worktree / predecessor / epoch | exact identity binding | REFUSE on mismatch; the route change never relaxes them | `test_harness_change_does_not_weaken_identity_bindings`, `test_a_stale_controller_epoch_still_refuses_a_changed_harness_dispatch`, `test_a_stale_controller_epoch_is_still_rejected` |

Budget note: a fresh dispatch reserves one charge with
`apply_spawn_and_charge`/`charge_spawn` against the role and the selected
harness. A same-slice `resume_pr` or `resume_blocked_leaf` continuation runs on
a route too, so it reserves one continuation charge through the sibling
`charge_continuation` in the same ledger, under a stable continuation identity
(the effect/action intent key) that is distinct from the attempt's dispatch
charge. `resume_pr` charges the configured role route resolved at resume time;
`resume_blocked_leaf` charges the accepted route the resume effect carries.
The earlier dispatch's spend and the original harness's counters are never
rewritten, and the reservation is settled by the same `reconcile` call a
dispatch charge uses: a measured actual moves the estimate to spend, missing
usage conserves the estimate with `actual="unknown"` and a warning, and a
resume that is refused before running releases the reservation with a zero
actual.

Both ceilings still apply. If the continuation would exceed the role ceiling or
the route harness's `per_harness_budget`, the resume is not issued: the slice is
parked with `park_cause=budget_exhausted` and the existing needs-human issue
path, with the refusal reason, continuation identity, harness, role, and
estimate in `park_audit`. The route is never silently retargeted to a cheaper
harness, and the old harness is never charged as a fallback. Same-harness
continuations are charged by the same rule because they are real invocations
that spend tokens; there is no route-change special case.

## Reviewer

The reviewer has its own route. A worker route change never selects a reviewer,
and the reviewer route is not selected by `tl_loop/select/*`: every reviewer
spawn (`spawn_reviewer_subtree*`, `spawn_reviewer_for_recovery*`, reached through
the `spawn_reviewer` effect) reads `configured_runtime_route("reviewer")` from
`[reviewer]` in `.exo/config.toml`.

1. **Next round.** A one-shot reviewer is spawned per exact head. If the
   operator changes the reviewer harness, model, or effort between rounds, the
   next one-shot spawn (a new head, or a replacement for the same head after the
   previous reviewer exited) runs the new route; `invocation.json` records the
   new `runtime`, `model`, `effort` and the reviewer `config_sources`. The old
   route's record is not rewritten when the harness (and therefore the reviewer
   internal name) changes. With the same harness, the reviewer keeps its identity
   and its new attempt replaces its own invocation record; the prior
   `invocation_id` survives in durable lifecycle evidence.
2. **Identity.** `AgentIdentity::new(slugify("review-pr-N"), reviewer harness)`
   makes the reviewer's internal name and birth branch harness-dependent:
   `review-pr-N-codex` vs `review-pr-N-opencode`. A harness change is therefore
   a new reviewer identity for the same PR/head. `persist_reviewer_assignment`
   rewrites `Reviewer-Agent`/`Reviewer-Birth-Branch` in the PR body, so the body
   carries one current assignment. The body is display/registry metadata only.
3. **Durable assignment and attribution.** After a harness change two `Review`
   invocations can match the same PR/head. `resolve_reviewer_invocation` collects
   all matches from agent directories, compatibility event files and the ledger,
   then applies one rule: a live invocation first, otherwise the most recently
   started, otherwise the highest generation, otherwise the smallest agent id.
   The answer therefore never depends on `read_dir`/file order. Forgejo review
   attribution (`resolve_review_author`, `resolve_durable_review_author`) uses
   that durable assignment after proving the review author is the one configured
   reviewer service account, so a review submitted after the change is
   attributed to the replacement reviewer. The owner self-review refusal and the
   exact PR/head binding are unchanged.
4. **Verdict and counters.** A persisted `GO`, `GO-WITH-NITS`, or `NO-GO` for an
   exact head is not invalidated by a reviewer route change, is not re-requested,
   and does not require a reviewer on the new route. Only a head change
   invalidates it and queues one reviewer for the new head. These are structural
   properties: `derive_next_action` and `slice_transition` have no reviewer-route
   input, and a structural test fails if one is added. A route change is not a
   review round: `review_rounds` and `tl_intervention_attempts` neither reset
   nor double count, and the `reviewer_max_rounds` review-stuck judgment still
   fires at the ceiling.
5. **In-flight reviewer.** Reconciliation and watcher attempts are keyed by
   PR/head/attempt, not by harness. A reviewer that is `Claimed` or `Running`
   when the route changes keeps its attempt, is not duplicated, and is not killed
   by the change, even if a replacement reviewer directory for the same head is
   already present. Once its invocation has exited, reconciliation marks the
   attempt `Failed` and the new route may review the same head.
6. **Init.** `init_model_validation_targets` validates the reviewer's currently
   configured harness. Switching Codex to OpenCode changes the validator from
   the Codex `gpt-`/live-catalog rule to the #1358 committed OpenCode table, and
   an unknown model, an unrecorded effort, or an unsupported effort fails init
   naming `rust/exomonad/data/opencode/<version>.json`. Separately, init's
   configured harness-policy check requires `[reviewer].agent_type` and
   `[reviewer].model` to agree with `roles.reviewer.allow` before the controller
   starts, exactly as it does for the worker. The reviewer spawn itself still
   reads only `[reviewer]`; the allow list is the operator's boundary that init
   keeps in step.
7. **No reviewer escalation.** The selector's
   `roles.<role>.escalate_after_attempts` removes a failed *worker* harness from
   rotation. No code path calls the selector for the reviewer, so there is no
   reviewer "luna to sol" model/harness escalation. A configured reviewer change
   is the base route; escalation never widens `roles.reviewer.allow`.
8. **No explicit reviewer harness request.** `spawn_reviewer` and
   `restart_review` accept only PR/head/criteria/force (restart: PR only). The
   WASM guest and the Python effect client expose the same arguments. There is
   no per-call reviewer harness override to guard; a test fails if one is added.

### What the operator changes for a reviewer route change

| Goal | Change | Restart? | Approval flag? |
|------|--------|----------|---------------|
| reviewer harness | `[reviewer].agent_type` in `.exo/config.toml` **and** add the harness to `[roles.reviewer].allow` in `.exo/harness_policy.toml` | yes, the server/controller read `[reviewer]` at startup | no |
| reviewer model | `[reviewer].model` (or the reviewer model selected by `exomonad init --reviewer-model`) | yes | no |
| reviewer effort | `[reviewer].effort_level` (or `exomonad init --reviewer-effort`) | yes | no |
| reviewer must stay on an allowlisted harness | keep `[roles.reviewer].allow` consistent with `[reviewer].agent_type` | yes | no |

The reviewer spawn itself is driven by `[reviewer]`; `roles.reviewer.allow` is
part of the single "configured route" definition shared with the harness-switch
guard and the selector. Keep the two in step so the configured reviewer route
cannot drift outside the operator's allowlist. There is no approval flag for a
configured reviewer route change, and no way for a TL or plan to request another
reviewer harness.

## What per-slice route prescription would require

Today a plan request is a constrained preference over the role allowlist, not a
prescription. Adding "this slice must run on OpenCode/&lt;model&gt;" would need:

1. `tl_loop/state/schema.py` + `tl_loop/state/plan_manifest.py`: a closed
   per-slice route field and manifest digest.
2. `tl_loop/select/agent_type.py`: accept the prescription only when it names a
   role allowlist entry; otherwise refuse the slice, never substitute.
3. `tl_loop/loop/driver.py:_dispatch_route_fields`: bind the prescribed route
   to the same fingerprints and transition row.
4. Rust `enforce_harness_switch_policy`: treat a prescribed allowlisted route
   as configured (the accepted role route already carries the proof), while an
   unlisted prescription still requires the operator flag.
5. Rust `resume_pr` / `resume_blocked_leaf`: compare the prescription with the
   prior invocation and record the transition, exactly as a configured change
   does now.

The guard would not change again: it already answers "is this harness
configured for this role?" from `config.toml` + the role allowlist.
