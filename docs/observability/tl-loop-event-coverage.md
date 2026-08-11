# TL-loop event coverage

Audit date: 2026-08-11. This is the M2.7 audit required before the loop consumes
the ledger in shadow mode.

## Method

The interactive TL can receive a direct message, a WASM `handle_event` input,
an inbox message, or a parent notification. This audit calls a wakeup
“covered” only when the same condition has a durable L1 ledger row whose event
type is projected by M2.6. The bridge’s closed event set is
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
| `[MERGE READY]` | `.exo/roles/devswarm/context/root.md:63-67`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:572-575,3324-3338` | `pr.review` plus `agent.notify_parent` repair handoff | covered | The canonical row preserves the exact release notification before injection. |
| `[PR READY]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:605-607,681-687` | `pr.review` | covered | The `approved` wakeup retains the native TL notification text. |
| `[REVIEW TIMEOUT]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:609-612,3341-3375` | `pr.review` plus `agent.notify_parent` handoff | covered | The active watcher now records a canonical timeout wakeup with elapsed minutes and notification text. |
| `[FIXES PUSHED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:615-626,2951-2962` | `pr.review` | covered | The canonical row retains the transient payload and verified head. |
| `[COMMITS PUSHED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:628-640,2963-2971` | `pr.review` | covered | The canonical row retains the transient payload and verified head. |
| `[CI Status]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:578-585,3282-3309` | `ci.status_changed` | covered | The canonical row preserves branch, status, message, reviews/comments, and verified `head_sha`. |
| `[CI BLOCKED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:587-596,2975-3024` | `pr.review` plus `agent.notify_parent` repair handoff | covered | The canonical row retains the exact native TL notification and CI diagnosis. |
| `[CI TRIGGERED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:704-708,3113-3128` | `pr.review` | covered | The trigger is now retained with branch and verified head context. |
| `[STUCK: pr, rounds]` | `.exo/roles/devswarm/context/root.md:69-72`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:642-645,3148-3178` | `pr.review` plus `agent.notify_parent` handoff | covered | Max-round and review-loop stuck wakeups retain rounds and notification text. |
| `[DEV NOT PUSHING]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:727-729` | `pr.review` | covered | Human-escalation diagnostics are projected as canonical review wakeups. |
| `[REVIEWER NOT RESPONDING]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:730-732` | `pr.review` | covered | Human-escalation diagnostics are projected as canonical review wakeups. |
| `[REVIEWER NEVER STARTED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:733-735` | `pr.review` | covered | Human-escalation diagnostics are projected as canonical review wakeups. |
| `[DEV FAILED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:736-738` | `pr.review` | covered | The canonical review path is ready for this guest variant; current watcher classification is recorded as a review stall. |
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
| `ci_blocked` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:76-80,115`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:2975-3024` | `pr.review` | covered | The review-blocking transition is retained with CI status. |
| `merge_ready` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:81-85,116`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3086-3112,3313-3338` | `pr.review` | covered | The direct merge-ready wakeup is retained before any later `pr.merged` event. |
| `dev_not_pushing` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:86-88,117`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:334-348` | `pr.review` | covered | Review-stall classification is projected to a canonical event. |
| `reviewer_not_responding` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:89-91,118`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:334-348` | `pr.review` | covered | Review-stall classification is projected to a canonical event. |
| `reviewer_never_started` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:92-94,119`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:334-348` | `pr.review` | covered | Review-stall classification is projected to a canonical event. |
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

Coverage is sufficient for M3 shadow mode (#678): all direct inbox and
issue-close wakeups in this audit now have projected canonical ledger rows.

Coverage is not sufficient for M5 active mode. Active mode still requires the
separate policy and control-plane gates defined by the milestone.
