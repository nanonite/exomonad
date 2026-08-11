# TL-loop event coverage

Audit date: 2026-08-11. This is the M2.7 audit required before the loop consumes
the ledger in shadow mode.

## Method

The interactive TL can receive a direct message, a WASM `handle_event` input,
an inbox message, or a parent notification. This audit calls a wakeup
“covered” only when the same condition has a durable L1 ledger row whose event
type is projected by M2.6. The bridge’s closed event set is
`tl_loop/events/envelope.py:30-64`; transient `pr_review`, `ci_status`,
`sibling_merged`, and `issue_closed` inputs, generic `event.dispatched` rows,
and direct inbox/tmux delivery do not count by themselves.

Every gap below has a filed Chainlink issue and blocks the next loop-driver
task, #678. The audit does not repair gaps.

## Notification vocabulary and direct wakeups

| wakeup | source | bridged kind | status (covered / gap) | notes |
|---|---|---|---|---|
| `[MERGE READY]` | `.exo/roles/devswarm/context/root.md:63-67`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:572-575,3324-3338` | `agent.notify_parent` for the repair handoff; no exact release envelope | gap — #709 | The exact release message is injected after the transient `pr_review` action at `rust/exomonad-core/src/services/worktree_event_watcher.rs:1998-2005`. |
| `[PR READY]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:605-607,681-687` | none | gap — #709 | Native TL delivery is derived from `approved`; no canonical row records that wakeup. |
| `[REVIEW TIMEOUT]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:609-612,3341-3375` | `agent.notify_parent` for the generic handoff | gap — #709 | The exact timeout variant is a transient `pr_review` input. |
| `[FIXES PUSHED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:615-626,2951-2962` | none | gap — #709 | The watcher emits only a transient `pr_review` input for this token. |
| `[COMMITS PUSHED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:628-640,2963-2971` | none | gap — #709 | The watcher emits only a transient `pr_review` input for this token. |
| `[CI Status]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:578-585,3282-3309` | `ci.status_changed` | covered | The canonical row preserves branch, status, message, reviews/comments, and verified `head_sha`. |
| `[CI BLOCKED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:587-596,2975-3024` | `agent.notify_parent` for a repair handoff; no exact token envelope | gap — #709 | The direct TL message is produced by transient `pr_review` handling. |
| `[CI TRIGGERED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:704-708,3113-3128` | none | gap — #709 | The trigger is a transient `pr_review` input. |
| `[STUCK: pr, rounds]` | `.exo/roles/devswarm/context/root.md:69-72`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:642-645,3148-3178` | `agent.notify_parent` for a generic handoff | gap — #709 | The exact stuck wakeup is not a canonical event; the generic handoff is insufficient for deterministic replay. |
| `[DEV NOT PUSHING]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:727-729` | none | gap — #709 | Native TL action only. |
| `[REVIEWER NOT RESPONDING]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:730-732` | none | gap — #709 | Native TL action only. |
| `[REVIEWER NEVER STARTED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:733-735` | none | gap — #709 | Native TL action only. |
| `[DEV FAILED]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:736-738` | none | gap — #709 | Native TL action only. |
| `[RATE LIMITED]` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:63-70,112,132`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:697-702` | none | gap — #709 | The guest accepts the variant, but no current watcher/poller producer records it. |
| `[REPAIR HANDOFF]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:560-568,2085-2101` | `agent.notify_parent` | covered | The parent notification ledger row preserves the handoff message and outcome context. |
| `[from: agent]` | `rust/exomonad-core/src/services/delivery.rs:215-235,744-805` | `agent.notify_parent` | covered | Success notifications are logged before EventQueue publication and delivery. |
| `[FAILED: agent]` | `rust/exomonad-core/src/services/delivery.rs:215-235,744-805` | `agent.notify_parent` | covered | Failure status and message are preserved in the canonical row. |
| `[STUCK: agent]` | `rust/exomonad-core/src/services/delivery.rs:182-205,215-235,744-805` | `agent.notify_parent` | covered | Rust accepts `stuck` as a parent-notification status and logs it. |
| `[Sibling Merged]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:599-603,2325-2389` | `agent.sibling_merged` | gap — #711 | A row exists, but it omits the target sibling and `sibling_pr_number` from the dispatched payload. |
| `[Sibling Merged]` from dormant poller | `rust/exomonad-core/src/services/github_poller.rs:643-705` | `agent.sibling_merged` | gap — #711 | The poller builds the full sibling payload for dispatch but logs only merged PR/branch/parent, with no target sibling or verified head. |
| `[ISSUE CLOSED: #id ...]` | `rust/exomonad-core/src/services/worktree_event_watcher.rs:662-667`; `rust/exomonad-core/src/services/orphan_reconciler.rs:199-223` | none | gap — #712 | The source is legacy `.exo/events/issue_closed.jsonl`, outside the M2.6 ledger segments. |
| unread-inbox poke | `rust/exomonad-core/src/services/worktree_event_watcher.rs:1444-1484` | none | gap — #712 | The watcher directly routes a tmux notification; it does not append a canonical event. |
| raw inbox message | `rust/exomonad-core/src/services/inbox_watcher.rs:44-138` | none | gap — #712 | New `TeamsMessage` entries are directly injected into tmux. |

## `PRReviewEvent` variants

The guest declares all variants at `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:25-97`
and parses them at lines 100-121. The rows below enumerate each variant,
including variants accepted by the guest but not currently produced by a live
Rust watcher.

| wakeup | source | bridged kind | status (covered / gap) | notes |
|---|---|---|---|---|
| `review_received` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:26-31,104`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3132-3204` | `copilot.review` | covered | Changes-requested review emits both the transient variant and the canonical review row. |
| `review_commented` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:32-37,105`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3208-3235` | none | gap — #709 | Commented review is dispatched only as `pr_review`. |
| `approved` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:38-40,106`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3041-3085` | none | gap — #709 | Approval is a transient `pr_review` event plus a parent handoff, not a distinct ledger row. |
| `timeout` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:41-44,107`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3341-3375` | none | gap — #709 | Timeout has no dedicated canonical envelope. |
| `fixes_pushed` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:45-49,108`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:2951-2962` | none | gap — #709 | The transient event includes `head_sha`, but no ledger row is emitted for the transition. |
| `commits_pushed` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:50-53,109`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:2963-2971` | none | gap — #709 | The transient event has no canonical counterpart. |
| `reviewer_approved` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:54-56,110`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3070-3078` | none | gap — #709 | The watcher emits `approved`, not a durable `reviewer_approved` envelope. |
| `reviewer_requested_changes` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:57-62,111`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3180-3193` | `copilot.review` | covered | The review row preserves the comments/reviews and verified head. |
| `rate_limited` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:63-66,112`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:697-702` | none | gap — #709 | Declared input with no current producer or ledger row. |
| `stuck` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:67-70,113`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3148-3178` | none | gap — #709 | The review-loop stuck signal is transient; `agent.stuck` is emitted only for a separate harness-switch/no-op condition. |
| `ci_triggered` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:71-75,114`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3113-3128` | none | gap — #709 | No canonical row records the manual-CI trigger decision. |
| `ci_blocked` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:76-80,115`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:2975-3024` | none | gap — #709 | The status-change row is not emitted for this review-blocking transition. |
| `merge_ready` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:81-85,116`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:3086-3112,3313-3338` | none | gap — #709 | The direct merge-ready wakeup is transient; `pr.merged` happens only after a later merge. |
| `dev_not_pushing` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:86-88,117`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:334-348` | none | gap — #709 | Review-stall classification is not projected to a canonical event. |
| `reviewer_not_responding` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:89-91,118`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:334-348` | none | gap — #709 | Review-stall classification is not projected to a canonical event. |
| `reviewer_never_started` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:92-94,119`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:334-348` | none | gap — #709 | Review-stall classification is not projected to a canonical event. |
| `dev_failed` | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs:95-97,120`; `rust/exomonad-core/src/services/worktree_event_watcher.rs:334-348` | none | gap — #709 | Review-stall classification is not projected to a canonical event. |

## GitHub poller transitions

`github_poller.rs` is currently a dormant compatibility path
(`rust/exomonad-core/src/services/github_poller.rs:1-6`), but its state machine
is still part of the supported wakeup contract and is audited here.

| wakeup | source | bridged kind | status (covered / gap) | notes |
|---|---|---|---|---|
| new SHA after changes requested → `fixes_pushed` | `rust/exomonad-core/src/services/github_poller.rs:137-174` | none | gap — #709 | Poller emits only transient `pr_review`. |
| new SHA outside review-response cycle → `commits_pushed` | `rust/exomonad-core/src/services/github_poller.rs:163-174` | none | gap — #709 | Poller emits only transient `pr_review`. |
| Copilot approval → `approved` | `rust/exomonad-core/src/services/github_poller.rs:181-193` | none | gap — #709 | No canonical approval envelope. |
| Copilot changes requested → `review_received` | `rust/exomonad-core/src/services/github_poller.rs:197-220` | `copilot.review` | covered | The poller emits the review row with the verified `head_sha`. |
| CI status transition | `rust/exomonad-core/src/services/github_poller.rs:221-237` | `ci.status_changed` | covered | The poller emits the CI row with the verified `head_sha`. |
| review timeout | `rust/exomonad-core/src/services/github_poller.rs:240-258` | none | gap — #709 | Timeout is only a transient `pr_review` input. |

## Parent-notification statuses

| wakeup | source | bridged kind | status (covered / gap) | notes |
|---|---|---|---|---|
| `notify_parent(status=success)` | `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Events.hs:52-67,130-168`; `rust/exomonad-core/src/services/delivery.rs:182-205,772-805` | `agent.completed` then `agent.notify_parent` | covered | The Haskell tool emits completion first; the shared Rust delivery path records the parent notification. The terminal completion row still lacks `head_sha` (#710). |
| `notify_parent(status=failure)` | `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Events.hs:52-67,130-168`; `rust/exomonad-core/src/services/delivery.rs:182-205,772-805` | `agent.completed` then `agent.notify_parent` | covered | Status/message are preserved. The terminal completion row still lacks `head_sha` (#710). |
| `notify_parent(status=stuck)` | `rust/exomonad-core/src/services/delivery.rs:182-205,772-805` | `agent.notify_parent` | covered | Rust accepts and logs `stuck`; the Haskell tool currently exposes only success/failure. The row still lacks `head_sha` (#710). |

## Existing canonical event cross-check

| wakeup | source | bridged kind | status (covered / gap) | notes |
|---|---|---|---|---|
| PR filed or updated | `rust/exomonad-core/src/handlers/file_pr.rs:149-179,206-215` | `pr.filed` / `pr.updated` | covered | The row includes PR metadata and `head_sha`. |
| verified PR published | `rust/exomonad-core/src/handlers/file_pr.rs:149-164` | `pr.published` | covered | Publication is durable and includes `head_sha`. |
| PR merged | `rust/exomonad-core/src/handlers/merge_pr.rs:104-121` | `pr.merged` | gap — #710 | The row has PR number/strategy but no verified `head_sha`. |
| PR merge failed | `rust/exomonad-core/src/handlers/merge_pr.rs:134-149` | `pr.merge_failed` | gap — #710 | The row has PR number/error but no verified `head_sha`. |
| Copilot review changed | `rust/exomonad-core/src/services/worktree_event_watcher.rs:2686-2741` | `copilot.review` | covered | #676 added `head_sha` to the active watcher emission. |
| CI status changed | `rust/exomonad-core/src/services/worktree_event_watcher.rs:3282-3309` | `ci.status_changed` | covered | #676 added `head_sha` to the active watcher emission. |
| guest completion | `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Events.hs:130-151` | `agent.completed` | gap — #710 | The completion row is canonical but has no verified `head_sha`. |
| harness-switch/no-op stuck | `rust/exomonad-core/src/handlers/events.rs:139-157,281-292` | `agent.stuck` | gap — #710 | The row is canonical but has no verified `head_sha`. |
| parent notification | `rust/exomonad-core/src/services/delivery.rs:772-791` | `agent.notify_parent` | gap — #710 | The row is canonical but has no verified `head_sha`. |
| sibling merge observation | `rust/exomonad-core/src/services/worktree_event_watcher.rs:2372-2389` | `agent.sibling_merged` | gap — #710, #711 | The row lacks both a verified `head_sha` and the dispatched sibling recipient context. |

## Mode decision

Coverage is not sufficient for M3 shadow mode (#678): #709-#712 are explicit
blockers, and the shadow loop would otherwise observe a partial projection and
could not deterministically replay several review decisions or direct inbox
wakeups.

Coverage is also not sufficient for M5 active mode. Active mode additionally
requires the reviewed head for terminal events, complete sibling targeting,
and a canonical representation for issue-close and inbox-triggered wakeups.
Resolve the filed blockers before enabling either mode.
