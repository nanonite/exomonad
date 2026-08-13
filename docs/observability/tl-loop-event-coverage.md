# TL-loop event coverage

Audit date: 2026-08-12. This is the M2.7 audit required before the loop consumes
the ledger in shadow mode.

## Method

The interactive TL can receive a direct message, a WASM `handle_event` input,
an inbox message, or a parent notification. This audit calls a sensor wakeup
“covered” only when the same condition has a durable L1 ledger row whose event
type is projected by the current closed bridge set. Controller events and
guidance-queue lifecycle rows are also audited here, but remain distinct from
the TL input projection: controller events are best-effort effects written by
Rust, while queue rows are durable state transitions owned by the inbox
services. The bridge’s closed event set is
`tl_loop/events/envelope.py:30-68`; the lifecycle `agent.spawned` row is
projected so shadow mode can observe real fan-out; transient `pr_review`, `ci_status`,
`sibling_merged`, and `issue_closed` inputs are covered only when their
canonical ledger rows are present. Generic `event.dispatched` rows do not
count by themselves.

The audit identified the original gaps; #709 records transient review
wakeups, #710 preserves terminal head SHAs, #711 preserves sibling recipient
context, and #712 routes issue-close and inbox wakeups through the canonical
ledger.

## Notification vocabulary and direct wakeups

| wakeup | source | bridged kind | status (covered / gap) | notes |
|---|---|---|---|---|
| `[PR READY]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:605-607,681-687` | `pr.review` | covered | The `approved` wakeup retains the native TL notification text. |
| `[REVIEW TIMEOUT]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs` raw observation; `tl_loop/events/stall.py` | `pr.review` | covered | The watcher records elapsed time and review evidence; the TL derives the timeout stall class. |
| `[FIXES PUSHED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:615-626,2951-2962` | `pr.review` | covered | The canonical row retains the transient payload and verified head. |
| `[COMMITS PUSHED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:628-640,2963-2971` | `pr.review` | covered | The canonical row retains the transient payload and verified head. |
| `[CI Status]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:578-585,3282-3309` | `ci.status_changed` | covered | The canonical row preserves branch, status, message, reviews/comments, and verified `head_sha`. |
| `[CI BLOCKED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs` raw observation; `tl_loop/events/stall.py` | `pr.review` projection | covered | The watcher retains CI evidence; the TL derives `ci_failed` from a failed CI-blocked observation. |
| `[CI TRIGGERED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:704-708,3113-3128` | `pr.review` | covered | The trigger is now retained with branch and verified head context. |
| `[STUCK: pr, rounds]` | `.exo/roles/devswarm/context/root.md:69-72`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:642-645,3148-3178` | `pr.review` plus `agent.notify_parent` handoff | covered | Max-round and review-loop stuck wakeups retain rounds and notification text. |
| `[DEV NOT PUSHING]` | `tl_loop/events/stall.py` from raw `pr.review` evidence | `pr.review` projection | covered | The TL derives and persists this class when changes remain unaddressed. |
| `[REVIEWER NOT RESPONDING]` | `tl_loop/events/stall.py` from raw `pr.review` evidence | `pr.review` projection | covered | The TL derives and persists this class after addressed changes with no new response. |
| `[REVIEWER NEVER STARTED]` | `tl_loop/events/stall.py` from raw `pr.review` evidence | `pr.review` projection | covered | The TL derives and persists this class when a registered reviewer has no Forgejo review. |
| `[DEV FAILED]` | `tl_loop/events/stall.py` review-stall vocabulary | `pr.review` projection | gap | No active raw watcher observation maps to this legacy guest variant. |
| `[RATE LIMITED]` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:63-70,112,132`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:697-702` | `pr.review` when produced | covered | The guest accepts the variant, but no current watcher/poller producer exists; there is therefore no live row to replay. |
| `[REPAIR HANDOFF]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:560-568,2085-2101` | `agent.notify_parent` | covered | The parent notification ledger row preserves the handoff message and outcome context. |
| `[from: agent]` | `rust/exomonad-core/src/services/delivery.rs:215-235,744-805` | `agent.notify_parent` | covered | Success notifications are logged before EventQueue publication and delivery. |
| `[FAILED: agent]` | `rust/exomonad-core/src/services/delivery.rs:215-235,744-805` | `agent.notify_parent` | covered | Failure status and message are preserved in the canonical row. |
| `[STUCK: agent]` | `rust/exomonad-core/src/services/delivery.rs:182-205,215-235,744-805` | `agent.notify_parent` | covered | Rust accepts `stuck` as a parent-notification status and logs it. |
| `[Sibling Merged]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:599-603,2419-2495` | `agent.sibling_merged` | covered | One canonical row is recorded per recipient with the recipient branch/PR and the complete dispatched payload. |
| `[Sibling Merged]` from dormant poller | `rust/exomonad-core/src/services/github_poller.rs:643-705` | `agent.sibling_merged` | covered | The dormant path records the same per-recipient payload and verified merged head before dispatch. |
| `[ISSUE CLOSED: #id ...]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:662-667`; `rust/exomonad-core/src/services/orphan_reconciler.rs:15-246` | `issue.closed` | covered | The reconciler appends the immutable ledger row before disposing the worktree; the old sidecar writer is gone. |
| unread-inbox poke | `rust/exomonad-core/src/services/worktree_event_watcher.rs:1547-1610` | `inbox.poke` | covered | The watcher records recipient, unread count, newest message, exact notification, transport, and delivery outcome before inbox bookkeeping. |
| raw inbox message | `rust/exomonad-core/src/services/inbox_watcher.rs:44-164` | `inbox.message` | covered | Each TeamsMessage is copied into the canonical ledger before exact tmux injection; the bridge tails only ledger segments. |

## `PRReviewEvent` variants

The guest declares all variants at `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:25-97`
and parses them at lines 100-121. The rows below enumerate each variant,
including variants accepted by the guest but not currently produced by a live
Rust watcher.

| wakeup | source | bridged kind | status (covered / gap) | notes |
|---|---|---|---|---|
| `review_received` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:26-31,104`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3132-3204` | `pr.review` and `copilot.review` | covered | Changes-requested review emits both the transient variant and canonical review evidence. |
| `review_commented` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:32-37,105`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3208-3235` | `pr.review` | covered | Commented review retains its comments and review kind. |
| `approved` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:38-40,106`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3041-3085` | `pr.review` | covered | Approval retains the transient payload and native notification. |
| `timeout` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:41-44,107`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3341-3375` | `pr.review` | covered | Timeout now has a canonical envelope with elapsed minutes. |
| `fixes_pushed` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:45-49,108`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:2951-2962` | `pr.review` | covered | The transient event and verified head are retained. |
| `commits_pushed` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:50-53,109`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:2963-2971` | `pr.review` | covered | The transient event and verified head are retained. |
| `reviewer_approved` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:54-56,110`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3070-3078` | `pr.review` | covered | The active producer’s `approved` kind is the canonical alias for this guest variant. |
| `reviewer_requested_changes` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:57-62,111`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3180-3193` | `pr.review` and `copilot.review` | covered | The review rows preserve the comments/reviews and verified head. |
| `rate_limited` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:63-66,112`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:697-702` | `pr.review` when produced | covered | Declared input has no current producer; future producers use the canonical review path. |
| `stuck` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:67-70,113`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3148-3178` | `pr.review` | covered | Review-loop stuck signals now retain rounds and notification text. |
| `ci_triggered` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:71-75,114`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3113-3128` | `pr.review` | covered | The manual-CI trigger decision is retained. |
| `ci_blocked` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:76-80,115`; `rust/exomonad-core/src/services/worktree_event_watcher.rs` raw observation | `pr.review` projection | covered | The review-blocking transition is retained with CI status and projected to `ci_failed` when CI fails. |
| `merge_ready` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:116,136`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3237-3246` | `pr.review` | retired compatibility input | The guest parser retains the legacy shape, but the watcher’s native fallback explicitly ignores it. Readiness is resolved at the merge boundary from current-head review and CI observations; no watcher merge decision is counted as sensor coverage. |
| `dev_not_pushing` | `tl_loop/events/stall.py` from raw `stuck` evidence | `pr.review` projection | covered | Classification is owned by the TL projection. |
| `reviewer_not_responding` | `tl_loop/events/stall.py` from raw `timeout` evidence | `pr.review` projection | covered | Classification is owned by the TL projection. |
| `reviewer_never_started` | `tl_loop/events/stall.py` from raw `timeout` evidence | `pr.review` projection | covered | Classification is owned by the TL projection. |
| `ci_failed` | `tl_loop/events/stall.py` from raw `ci_blocked` evidence | `pr.review` projection | covered | Classification is owned by the TL projection. |
| `dev_failed` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:95-97,120`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:334-348` | `pr.review` | covered | The canonical review path accepts the guest failure kind; no active watcher producer currently classifies this exact variant. |

## GitHub poller transitions

`github_poller.rs` is currently a dormant compatibility path
(`rust/exomonad-core/src/services/github_poller.rs:1-6`), but its state machine
is still part of the supported wakeup contract and is audited here.

| wakeup | source | bridged kind | status (covered / gap) | notes |
|---|---|---|---|---|
| new SHA after changes requested → `fixes_pushed` | `rust/exomonad-core/src/services/github_poller.rs:137-174` | `pr.review` | covered | Poller retains the transient payload and verified head. |
| new SHA outside review-response cycle → `commits_pushed` | `rust/exomonad-core/src/services/github_poller.rs:163-174` | `pr.review` | covered | Poller retains the transient payload and verified head. |
| Copilot approval → `approved` | `rust/exomonad-core/src/services/github_poller.rs:181-193` | `pr.review` | covered | Poller records the approval transition. |
| Copilot changes requested → `review_received` | `rust/exomonad-core/src/services/github_poller.rs:197-220` | `pr.review` and `copilot.review` | covered | The poller emits the transition row with the verified `head_sha`. |
| CI status transition | `rust/exomonad-core/src/services/github_poller.rs:221-237` | `ci.status_changed` | covered | The poller emits the CI row with the verified `head_sha`. |
| review timeout | `rust/exomonad-core/src/services/github_poller.rs:240-258` | `pr.review` | covered | Poller records the timeout transition and elapsed minutes. |

## Parent-notification statuses

| wakeup | source | bridged kind | status (covered / gap) | notes |
|---|---|---|---|---|
| `notify_parent(status=success)` | `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Events.hs:52-67,130-168`; `rust/exomonad-core/src/services/delivery.rs:182-805` | `agent.completed` then `agent.notify_parent` | covered | The Haskell tool emits completion first; the shared Rust delivery path records the parent notification. Both rows retain an explicit no-verified-PR-context finding. |
| `notify_parent(status=failure)` | `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Events.hs:52-67,130-168`; `rust/exomonad-core/src/services/delivery.rs:182-805` | `agent.completed` then `agent.notify_parent` | covered | Status/message are preserved, and the missing SHA is explicit rather than synthesized. |
| `notify_parent(status=stuck)` | `rust/exomonad-core/src/services/delivery.rs:182-805` | `agent.notify_parent` | covered | Rust accepts and logs `stuck`; the Haskell tool currently exposes only success/failure. The row records the same explicit finding when no PR context is available. |

## Controller event coverage

The controller declares eight `tl.*` aggregate event types. Each is accepted by
the Rust TL handler and written through the existing ledger writer; the Python
controller remains the caller and does not open a second ledger writer. Emission
is best-effort, so a failed write must not change the controller state transition.

| event | producer | status | notes |
|---|---|---|---|
| `tl.phase_changed` | `tl_loop/loop/driver.py:1531-1542`; `rust/exomonad-core/src/handlers/tl.rs:14-30` | covered | Phase transitions carry only bounded phase tags and run identity. |
| `tl.slice_status_changed` | `tl_loop/loop/escalate.py:267-285`; `rust/exomonad-core/src/handlers/tl.rs:14-30` | covered | Status transitions are emitted from durable slice state changes. |
| `tl.slice_parked` | `tl_loop/loop/escalate.py:286-293`; `tl_loop/loop/driver.py:747-756` | covered | Park cause and attempt count are bounded dimensions; no body is emitted. |
| `tl.gate_opened` | `tl_loop/loop/driver.py:599-608`; `rust/exomonad-core/src/handlers/tl.rs:14-30` | covered | Timeout parking opens the named gate before the terminal failed phase. |
| `tl.gate_answered` | `tl_loop/__main__.py:320-340` | covered | CLI/control answers carry gate name, decision, and source. |
| `tl.merge_decided` | `tl_loop/loop/driver.py:1109-1118` | covered | The decision carries a PR number and bounded head-SHA hash, not the merge body. |
| `tl.judgment` | `tl_loop/rlm/call.py:324-355`; `tl_loop/rlm/store.py:114-123` | covered | RLM calls project model, attempt, outcome, tokens, latency, replay, and bounded redacted result. |
| `tl.plan_proposed` | `tl_loop/__main__.py:242-256` | covered | Proposal outcome is recorded without the plan document or rejection prose beyond the bounded reason. |

## Guidance-queue coverage

The durable guidance path is a queue lifecycle, not a second transport. Batch and
item identities are committed before delivery; consumption and abandonment remain
observable even when the compatibility notification path is used.

| lifecycle | producer | event(s) | status | notes |
|---|---|---|---|---|
| durable enqueue | `rust/exomonad-core/src/services/delivery.rs:811-824`; `rust/exomonad-core/src/services/inbox_store.rs` | `inbox.state_changed` | covered | The batch identity, queue class, and pending state are durable before transport. |
| transport delivery | `rust/exomonad-core/src/services/delivery.rs:160-180,730-750` | `message.delivery` | covered | Delivery outcome is recorded for the durable batch and transport attempt. |
| inbox consumption | `rust/exomonad-core/src/services/inbox_store.rs:246-262` | `message.consumed` | covered | The consumer identity and batch correlation are retained before the read is acknowledged. |
| delivery abandonment | `rust/exomonad-core/src/services/delivery.rs:1068-1085` | `agent_inbox.messages_abandoned` | covered | Exhausted retries become an explicit terminal outcome rather than disappearing. |
| identity duplicate suppression | `rust/exomonad-core/src/services/agent_inbox.rs:181-201` | `agent_inbox.duplicates_dropped` | covered | Identity-based idempotency is recorded at the durable inbox boundary. |
| unread poke bookkeeping | `rust/exomonad-core/src/services/inbox_store.rs:268-400`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:1547-1610` | `inbox.state_changed`, `inbox.poke` | covered | Poke metadata and delivery outcome are separate from message consumption. |
| lifecycle guidance summary | `docs/exomonad-session-logging.md:228-230` | `agent.guidance.delivery` | explicit finding | The registry declares this event, but no current producer was found in the active tree; current queue coverage relies on the canonical durable delivery/consumption/abandonment rows. |

## Existing canonical event cross-check

| wakeup | source | bridged kind | status (covered / gap) | notes |
|---|---|---|---|---|
| child fan-out | `rust/exomonad-core/src/handlers/agent.rs:936-955,1112-1131,1744-1758`; `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Spawn.hs:616-630` | `agent.spawned` | covered for shadow mode | The projection preserves the child identity, branch, and agent type needed to start a real shadow trajectory; duplicate Rust/guest rows remain observable. |
| PR filed or updated | `rust/exomonad-core/src/handlers/file_pr.rs:149-179,206-215` | `pr.filed` / `pr.updated` | covered | The row includes PR metadata and `head_sha`. |
| verified PR published | `rust/exomonad-core/src/handlers/file_pr.rs:149-164` | `pr.published` | covered | Publication is durable and includes `head_sha`. |
| PR merged | `rust/exomonad-core/src/handlers/merge_pr.rs:104-129`; `rust/exomonad-core/src/services/merge_pr.rs:44-49`; `haskell/wasm-guest/src/ExoMonad/Guest/Tools/MergePR.hs:443-470` | `pr.merged` | covered | Rust and the Haskell tool receive the same verified Forgejo head SHA through the merge effect response; a missing source is explicitly annotated. |
| PR merge failed | `rust/exomonad-core/src/handlers/merge_pr.rs:134-149`; `rust/exomonad-core/src/services/merge_pr.rs:44-49` | `pr.merge_failed` | covered | The failure row retains the head SHA from the same verified PR lookup. |
| Copilot review changed | `rust/exomonad-core/src/services/worktree_event_watcher.rs:2686-2741` | `copilot.review` | covered | #676 added `head_sha` to the active watcher emission. |
| CI status changed | `rust/exomonad-core/src/services/worktree_event_watcher.rs:3282-3309` | `ci.status_changed` | covered | #676 added `head_sha` to the active watcher emission. |
| guest completion | `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Events.hs:130-153` | `agent.completed` | explicit finding | This generic tool event has no verified PR context; it records `head_sha: null` and `head_sha_finding` for interpretation. |
| harness-switch/no-op stuck | `rust/exomonad-core/src/handlers/events.rs:139-159,281-294` | `agent.stuck` | explicit finding | These policy events have no verified PR context; they record the null SHA and finding. |
| parent notification | `rust/exomonad-core/src/services/delivery.rs:772-799` | `agent.notify_parent` | explicit finding | Generic notifications retain a null SHA and finding unless a verified watcher handoff supplies one. |
| sibling merge observation | `rust/exomonad-core/src/services/event_log.rs:42-72`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:2419-2495` | `agent.sibling_merged` | covered | The row carries the removed sibling’s verified head, recipient branch/PR, and full wakeup payload. |

## Mode decision

Coverage is sufficient for M3 shadow mode (#678): direct inbox and issue-close
wakeups have projected canonical ledger rows, controller aggregate events have
Rust ledger handlers, and the guidance queue has explicit enqueue, delivery,
consumption, abandonment, and duplicate outcomes. The retired `merge_ready`
compatibility input is not treated as a watcher decision or a sensor producer.

Coverage is not sufficient for M5 active mode. Active mode still requires the
separate policy and control-plane gates defined by the milestone.
