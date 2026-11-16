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
`harness/model` identifiers to the same agent-type sets.

## Operator actions

| Goal | Change | Restart? | Approval flag? |
|------|--------|----------|---------------|
| configured model change | `worker_model` (or `[opencode] worker_model`) in `.exo/config.toml` | yes, the server and controller read it at startup | no |
| configured effort change | `worker_effort_level` in `.exo/config.toml` | yes | no |
| configured harness change (Codex -> OpenCode or back) | `spawn_agent_type` in `.exo/config.toml` **and** add the harness to `[roles.worker].allow` in `.exo/harness_policy.toml` (plus `cost_rank`, optional `per_harness_budget`) | yes | no |
| explicit non-configured harness request | do not edit config; the child asked for a harness outside both sources | no | only `EXOMONAD_ALLOW_HARNESS_SWITCH=1`, audited |
| policy-escalated switch after NO-GO | add the replacement harness to the same role's `allow`; the selector removes the failed harness after `escalate_after_attempts` | yes | no |
| reviewer harness change | `[reviewer]` route in `.exo/config.toml` and reviewer `allow` in `.exo/harness_policy.toml` | yes | no |

Changing only the allowlist is enough for the *guard*, but the selector will
only choose the new harness when it is allowed, capable, and inside the role and
per-harness budgets. Changing only `spawn_agent_type` is enough for the default
route, but the selector still cannot select a harness absent from `allow`.

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
| `escalate_after_attempts` | slice attempts + verdict decide when a harness leaves rotation | PASS; a configured change does not reset the counter, and a NO-GO on harness A selects another allowlisted harness | `test_no_go_escalates_only_after_attempt_threshold`, H4 test |
| role budget / `per_harness_budget` | a dispatch reserves against the role and the selected harness | PASS for a new dispatch attempt: harness B is charged and harness A's spend is kept. A same-slice `resume_pr`/`resume_blocked_leaf` continuation is not a new dispatch attempt and is **not** charged again | `test_harness_change_charges_the_new_harness_and_keeps_the_old_spend`, `test_harness_budget_is_charged_to_the_harness_that_actually_runs` |
| owner / scope / worktree / predecessor / epoch | exact identity binding | REFUSE on mismatch; the route change never relaxes them | `test_harness_change_does_not_weaken_identity_bindings`, `test_a_stale_controller_epoch_still_refuses_a_changed_harness_dispatch`, `test_a_stale_controller_epoch_is_still_rejected` |

Budget note: the controller charges a slice attempt when it dispatches a fresh
attempt (`apply_spawn_and_charge`). `resume_pr` and `resume_blocked_leaf`
continue the same attempt with a new invocation, so they do not add a second
reservation. That is an accounting limit, not a route refusal; if per-invocation
metering is needed, it belongs in the resume effect, not in the guard.

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
