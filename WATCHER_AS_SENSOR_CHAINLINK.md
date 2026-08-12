# Chainlink decomposition — Watcher as sensor

Work breakdown for [WATCHER_AS_SENSOR_PLAN.md](WATCHER_AS_SENSOR_PLAN.md),
shaped for `chainlink_milestone_create` / `chainlink_issue_create` /
`chainlink_subissue_create`.

**Created 2026-08-12** via the Chainlink CLI (server was down, so the MCP tools
were unreachable; CLI use was explicitly authorised for this).

Hierarchy: **milestone** groups epics → **issue** is the epic → **subissue** is
the task. Every epic carries `## Acceptance Criteria` because the reviewer
contract in `.exo/roles/devswarm/context/reviewer.md` treats that literal
heading as authoritative — and Decision 2 of the plan makes the TL responsible
for supplying it.

## Live IDs

| Item | ID | Milestone | Blocked by |
|---|---|---|---|
| M9: Watcher as sensor | milestone 25 | — | — |
| M10: Agent loop ownership | milestone 26 | — | — |
| E1 Documentation reconciliation | **#718** | 25 | — (ready) |
| E2 Reviewer→TL evidence channel | #719 | 25 | #718 |
| E3 Review workflow into `tl_loop` | #720 | 25 | #719 |
| E4 Watcher → pure sensor | #721 | 25 | #720 |
| E5 TL MCP tool removal | #722 | 25 | #718 |
| E6 Agent loop ownership | **#723** | 26 | — (ready) |

Tasks: E1 `#724-728` · E2 `#729-733` · E3 `#734-740` · E4 `#741-745` ·
E5 `#746-751` · E6 `#752-756`.

**Sequencing is enforced at both levels.** Epics are chained with
`chainlink issue block`, and tasks are chained within each epic *and across
epic boundaries* — the first task of an epic is blocked by the last task of the
epic it depends on. This matters because `chainlink issue ready` does **not**
inherit a parent epic's blocked state: without the cross-epic task blockers,
E2.1/E3.1/E4.1/E5.1 all showed as workable while their epics were blocked.

Verify at any time with:

```bash
chainlink issue ready      # should show only the current unblocked frontier
chainlink issue blocked    # full blocker graph
```

At creation the frontier was exactly `#718`, `#724` (E1.1), `#723`, `#752`
(E6.1).

---

## Milestone 1 — `watcher-as-sensor`

> One workflow brain (Python TL), one sensor (Rust watcher), one I/O runtime
> (Rust). Removes the split-brain where both the watcher and `tl_loop` decide
> when a PR is ready.

Epics run in order. E2→E3 is a hard dependency; E4 must not start until E3 is
proven in a live run or both components will spawn reviewers.

---

### E1 — Reconcile review-gate documentation with the TL-as-loop architecture

`template=refactor` `priority=high` `labels=docs,architecture`

Phases 0 and 1 of the plan. Pure documentation; no code. Removes
timeout-as-approval language and the four-layer gate framing that produced the
split brain.

This is a documentation defect, **not** a live vulnerability —
`tl_loop/loop/review.py::verify_review` already requires a verdict and a
matching head, so no Python bypass exists.

**Tasks (subissues):**

1. **Revise the watcher-as-sensor ADR.** `docs/decisions/watcher-as-sensor.md`
   currently says to demote `adjudicate_review` out of the approval path.
   Decision 1 supersedes that: the TL adjudicates on *binding* reviewer
   findings. Add Decision 2 (TL-owned acceptance criteria) and the same-leaf
   repair rule. Record messaging as out of scope, pointing at Milestone 2.
   Status → Accepted.
2. **Fix `CLAUDE.md`.** Replace "or an allowed review timeout with passing CI,
   permits `merge_pr`" with "Review timeout parks the slice."
3. **Fix `docs/guides/programming-the-tl.md`.** Replace "Four independent checks
   must all hold" with the canonical rule. Move second-reviewer rules under an
   explicit "Optional policy" heading. Drop the "forbid the timeout-merge
   escape" paragraph — it presumes an escape that does not exist.
4. **Fix `docs/architecture/agent-system.md` + `.html`.** "The TL runs the
   workflow; the watcher only observes world state." Deprecate `MergeReady` in
   the event-vocabulary table. Rewrite the "Controller-side merge gates" table
   added in `5626ab86`. Both files change together.
5. **Declare the target event contract** in
   `docs/decisions/tl-loop-event-bridge.md`: `pr.filed`,
   `pr.updated`/`pr.head_changed`, `pr.review`, `ci.status_changed`,
   `pr.merged`, `pr.merge_failed`. State that `merge_ready` is derived state,
   not a source event.

**## Acceptance Criteria**

- `grep -rn 'REVIEW TIMEOUT\|review timeout' *.md docs/` returns no
  merge-permitting language.
- No document describes a four-layer merge gate.
- `docs/decisions/watcher-as-sensor.md` status is `Accepted` and its
  adjudication section matches Decision 1.
- `just validate-observability-contracts` passes.
- `.md` and `.html` views of `agent-system` agree.

---

### E2 — Reviewer→TL evidence channel with head-SHA binding

`template=feature` `priority=high` `labels=haskell,wasm,reviewer,tl-loop`
depends on: E1

The reviewer tools already exist in `.exo/roles/devswarm/ReviewerRole.hs`
(`approve_pr` :270, `request_changes` :293, `post_review_comment` :319) and
already call `applyEvent @ReviewerPhase`. Two gaps: the verdict carries no head
SHA, and it reaches the TL only via the watcher re-observing Forgejo.

All tool definitions stay in Haskell WASM. **Do not add a Rust MCP tool.**

**Tasks (subissues):**

1. **Add `head_sha` and structured `findings` to the reviewer tool args.**
   Findings carry severity, path, and rationale. Schemas in `ReviewerRole.hs`;
   arg records follow the existing `genericToolSchemaWith` pattern.
2. **Emit a canonical `pr.review` ledger event** carrying verdict, head SHA, and
   findings, through the existing effect surface. Add the effect type in
   Haskell and the handler in `rust/exomonad-core/src/handlers/` only if no
   existing effect covers it.
3. **Add `spawn_reviewer` to `tl_loop/client/effects.py`**, taking PR number,
   head SHA, and TL-supplied acceptance criteria.
4. **Compose acceptance criteria in the TL** from `SliceState.test_plan` and the
   plan's `verify` / `boundary` / DONE CRITERIA. Keep `reviewer.md`'s
   `## Acceptance Criteria` heading as the reviewer-side format — this closes
   the provenance gap where the dev-leaf authored its own pass condition.
5. **Extend role tests** in `.exo/roles/devswarm/test/Main.hs` (existing
   reviewer coverage at :395, :411, :736) for the new args and the ledger event.

**## Acceptance Criteria**

- A reviewer verdict for SHA A submitted while the live PR head is SHA B is
  rejected at the TL and does not merge.
- A `pr.review` ledger event carries verdict, head SHA, and findings.
- Acceptance criteria reaching the reviewer originate from TL run state, not
  from the PR body written by the dev-leaf.
- No Rust MCP tool schema was added.
- `just role-hook-tests` and `just wasm-all` pass.

---

### E3 — Move review workflow into `tl_loop`

`template=feature` `priority=high` `labels=tl-loop,python,fsm`
depends on: E2

Additive. The watcher keeps emitting legacy events so both paths can be diffed
before anything is deleted.

**Tasks (subissues):**

1. **Per-head review state in `tl_loop/state/schema.py`** — `review_findings`,
   `ci_state`, `reviewer_attempt` keyed by head SHA, plus `repair_attempts`.
   Closed keys. **`SCHEMA_VERSION` is 1 and `_version` rejects anything else —
   ship a migration or in-flight runs lose resume.**
2. **`pr.filed` and `pr.updated`/head-change transitions** in
   `tl_loop/fsm/transition.py` and `tl_loop/loop/driver.py`. A head change
   clears prior review and CI state for that slice.
3. **Reviewer spawn from the head-change transition.** Attempt claiming moves
   out of the watcher's `claim_reviewer_attempt` into durable run state.
   **Keep behind a flag until E4 removes the watcher's spawn** — otherwise both
   fire.
4. **Route findings into `adjudicate_review`**; route NO-GO and CI failure into
   `compose_repair` → `resume_pr`. Same owner, same branch, same worktree —
   already enforced in `tl_loop/rlm/repair.py`. Never `spawn_leaf`.
5. **Narrow `verify_review`** to `adjudicated_go(head) && ci_ok(head) &&
   head == live_head`. Make extra-review policy an explicit optional predicate
   rather than an inline veto.
6. **Decide the fate of `GO-WITH-NITS`.** It currently gates on nits being
   written to the owning Chainlink issue. Real behavior — give it a home or
   remove it deliberately. Do not leave it half-wired.
7. **Timeout parks with a named gate.** No merge path from a timeout.

**## Acceptance Criteria**

- A replay fixture where SHA A is approved and SHA B is then pushed does not
  merge.
- A review timeout parks the slice with a named gate and never merges.
- A NO-GO resumes the same owner's worktree and branch; no second branch is
  created.
- Resume works against a `run.json` written before the schema change.
- `GO-WITH-NITS` is either fully wired or fully removed, with a note saying
  which and why.
- `just e2e-tl-loop-active` passes.

---

### E4 — Reduce the watcher to a pure sensor

`template=refactor` `priority=medium` `labels=rust,watcher`
depends on: E3 proven in a live run

`rust/exomonad-core/src/services/worktree_event_watcher.rs` is 7,808 lines.

**Tasks (subissues):**

1. **Remove reviewer-spawn decision logic** —
   `should_spawn_reviewer_for_new_head` (:211), `claim_reviewer_attempt` (:225),
   and the `spawn_reviewer_for_pr` call sites (:1005, :2026). Keep the spawn
   effect itself; the TL calls it now.
2. **Remove authoritative merge readiness** — `ci_mergeable_at` (:329, :469) and
   `merge_ready_notified` (:2682, :2699). Emit `merge_ready` as a compatibility
   event behind a temporary flag, then delete the flag in the same epic.
3. **Remove repair-handoff composition** — `parent_repair_handoff_message`
   (:562), `deliver_parent_repair_handoff` (:2306), and the seven
   `parent_handoff_fingerprint` sites.
4. **Decide where reviewer disposal lives.** `dispose_reviewers_for_pr` (:2029)
   is resource cleanup, but currently fires on a watcher-side semantic judgment
   about terminal review state.
5. **Confirm the timeout path** does not permit merge anywhere in Rust.

**Keep:** PR/head/review/CI observation, canonical ledger emission,
`watcher_pr_state` as a read effect.

**## Acceptance Criteria**

- The watcher spawns no reviewer and composes no repair handoff.
- `grep -rn 'merge_ready' rust/` returns only deleted or compatibility-flagged
  paths.
- Every removed behavior has a Python test that now covers it.
- Watcher line count drops substantially from 7,808.
- `just test` passes.

---

### E5 — Remove TL-specific MCP tools built for an interactive coordinator

`template=refactor` `priority=medium` `labels=haskell,wasm,tools,cleanup`

| Tool | Action |
|---|---|
| `fork_wave` | **Remove.** A sub-TL is a nested `tl_run` |
| `has_pending_work` | **Remove.** Now a controller phase predicate |
| `shutdown_server` | Remove from role registration; keep as an operator CLI path |
| `check_inbox` | Keep for human/worker delivery; remove from root/tl registration |
| `poll_workers` | Keep for the heartbeat; drop the idle-protocol framing |
| `merge_pr` | Keep; confirm registration matches the authority matrix |

**Tasks (subissues):**

1. **Remove `fork_wave`** — 13 code files:
   `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Spawn.hs`, `Records/Spawn.hs`,
   `Prompt.hs`, `.exo/roles/devswarm/{TLRole,RootRole}.hs`, `test/Main.hs`,
   `rust/exomonad-core/src/protocol/mcp.rs`,
   `services/agent_control/spawn.rs`, `rust/exomonad/src/init.rs`,
   `rust/exomonad-core/tests/wasm_integration.rs`,
   `tl_loop/client/effects.py`, `tl_loop/tests/test_{driver,effects}.py`.
2. **Repoint or delete `tests/e2e/subtl-recursive-fork-wave/`** and the
   `fork_wave` references across ~14 other e2e test docs.
3. **Remove `has_pending_work` and `shutdown_server`** from role registration.
4. **Drop `check_inbox` from root/tl registration**; keep it for
   worker/reviewer/human delivery.
5. **Rewrite `.exo/roles/devswarm/context/chainlink-tl.md`** (132 lines) — it
   still opens with interactive-TL framing, the same problem `root.md` had in
   `957a921e`.
6. **Update the role × tool matrix** in `docs/architecture/agent-system.md` §2
   and `docs/decisions/hylo-worktree-model.md` to match actual registration.

**## Acceptance Criteria**

- `grep -rn 'fork_wave' --include='*.hs' --include='*.rs' --include='*.py' .`
  returns nothing outside deleted-test history.
- The role × tool matrix in `agent-system.md` §2 matches actual registration.
- `chainlink-tl.md` contains no interactive-coordinator protocol.
- `just role-hook-tests` and `just test` pass.

---

## Milestone 2 — `agent-loop-ownership`

> Fix the flaky TL→agent messaging at its root rather than patching transports.

### E6 — Evaluate owning the agent loop for durable TL→agent steering

`template=research` `priority=medium` `labels=architecture,messaging,prime-agent`

prime-agent's steering works because prime-agent **owns** the agent loop:
`runLoop` (`packages/agent/src/agent-loop.ts:317-344`) polls
`getSteeringMessages` and pushes straight into `currentContext.messages` before
the next assistant response, with a second `getFollowUpMessages` channel drained
when the agent would otherwise stop (`agent.ts:185`, `PendingMessageQueue`,
`one-at-a-time` mode).

ExoMonad does not own the loop — Claude Code, Codex, and OpenCode do. Our only
injection points are the Teams inbox, HTTP-over-UDS, and tmux paste-buffer.
That is the root cause of the messaging flakiness, and no amount of transport
hardening fixes it.

**Tasks (subissues):**

1. **Document the current injection paths and their failure modes** —
   `rust/exomonad-core/src/services/{delivery,inbox_watcher,state_mirror}.rs`.
   Measure actual drop/duplication rates from ledger evidence.
2. **Study prime-agent's queue model** — `PendingMessageQueue`, the
   steering/follow-up split, `one-at-a-time` vs. drain-all, and abort handling.
3. **Assess owning the loop per harness.** Claude Code and Codex own their
   loops; what would ExoMonad have to intercept, and is it viable per runtime?
   This is the harness-agnosticism constraint — a Claude-only fix is not a fix.
4. **Design a durable per-agent steering queue** drained at harness turn
   boundaries with acknowledgement, as the fallback if owning the loop is not
   viable.
5. **Write an ADR** with the recommendation and rejected alternatives.

**## Acceptance Criteria**

- The failure modes of the three current transports are documented with
  evidence, not assertion.
- The recommendation states explicitly whether ExoMonad should own the agent
  loop, per harness.
- Any proposed mechanism works across Claude, Codex, and OpenCode — no
  single-harness fix.
- An ADR lands in `docs/decisions/`.
- Authoritative workflow state stays on the ledger regardless of outcome; the
  steering channel never carries merge authority.

---

## Execution order

```
#718 E1  →  #719 E2  →  #720 E3  →  #721 E4
  └────────────────────────────────→  #722 E5

#723 E6  (independent, milestone 26)
```

E2→E3→E4 is a hard chain. E5 needs only E1 (it is tool cleanup, not review
workflow) but is chained behind E1's last task so the documentation lands
first. E6 is independent and can run in parallel throughout.

Within every epic, tasks run strictly in listed order.
