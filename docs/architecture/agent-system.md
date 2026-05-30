# Agent System Reference

Authoritative reference for ExoMonad's agent system: per-role tool matrix, per-role hook rules, per-role state machines, and the PR review convergence flow.

Sources of truth (read these if any diagram drifts):

- Roles: `.exo/roles/devswarm/{Root,TL,Dev,Reviewer,Worker}Role.hs`
- Phases: `.exo/roles/devswarm/{TLPhase,WorkerPhase}.hs`, `.exo/lib/{DevPhase,ReviewerPhase}.hs`
- Hook policy: `.exo/lib/HookPolicy.hs`, `.exo/lib/HttpDevHooks.hs`
- PR review handlers: `.exo/lib/PRReviewHandler.hs`, reviewer-side in `ReviewerRole.hs`
- Watcher (event source): `rust/exomonad-core/src/services/worktree_event_watcher.rs`
- Review policy knobs: `.exo/review-policy.toml`

---

## 1. Agent Triad and Roles

Five roles. Each agent is `worktree + context-window + actor`, born and torn down together.

| Role | Model | Spawns | Files PR | Merges PR | Lifecycle |
|------|-------|--------|----------|-----------|-----------|
| `root` | Opus | yes | no | yes | persistent (TL window) |
| `tl` | Opus | yes | yes | yes | per-subtree |
| `dev` | Codex / Gemini / OpenCode | no | yes | no | per-spec, exits at merge-ready |
| `reviewer` | Codex / Gemini | no | no | no | ephemeral per review round |
| `worker` | Codex / Gemini | no | no | no | ephemeral, same-worktree edits |

---

## 2. Tool Matrix — role × MCP tool

`x` = registered for that role (callable). Blank = not registered, calls return `tool not found`.

### ExoMonad orchestration tools

| Tool | root | tl | dev | reviewer | worker |
|------|:----:|:--:|:---:|:--------:|:------:|
| `fork_wave` | x | x | | | |
| `spawn_leaf` | x | x | | | |
| `spawn_codex` | x | x | | | |
| `spawn_worker` | x | x | | | |
| `spawn_reviewer` | x | x | | | |
| `close_worker_pane` | x | x | | | |
| `close_issue_and_cleanup` | x | x | | | |
| `cleanup_reviewer_leaf` | x | x | | | |
| `restart_review` | x | x | | | |
| `cleanup_orphan` | x | x | | | |
| `watcher_pr_state` | x | x | | | |
| `file_pr` | | x | x | | |
| `merge_pr` | x | x | | | |
| `notify_parent` | | x | x | | x |
| `send_tmux_message` / `send_mailbox_message` | x | x | x | | x |
| `session_status` | x | x | | | |
| `poll_workers` | x | x | | | |
| `task_list` / `task_get` / `task_update` | | | x | | x |
| `approve_pr` | | | | x | |
| `request_changes` | | | | x | |
| `post_review_comment` | | | | x | |

### Chainlink tools

| Chainlink tool | root | tl | dev | reviewer | worker |
|---------------|:----:|:--:|:---:|:--------:|:------:|
| `chainlink_issue_create` | x | x | | | |
| `chainlink_subissue_create` | x | x | x | | |
| `chainlink_subissue_close` | | | x | | |
| `chainlink_issue_list` | x | x | | | |
| `chainlink_issue_show` | x | x | x | | x |
| `chainlink_issue_update` | x | x | | | |
| `chainlink_issue_comment` | x | x | x | | x |
| `chainlink_issue_close` | x | x | | | |
| `chainlink_issue_block` | x | x | | | |
| `chainlink_issue_relate` | x | x | | | |
| `chainlink_issue_cascade` | x | x | | | |
| `chainlink_milestone_create` / `_list` | x | x | | | |
| `chainlink_session_start` | x | x | x | | x |
| `chainlink_session_work` | x | x | x | | x |
| `chainlink_session_status` | x | x | x | | |
| `chainlink_session_end` | x | x | x | | x |
| `chainlink_timer_start` / `_stop` / `_status` | x | x | | | |

Authority summary: **issue decomposition and lifecycle authority lives at the TL/root layer; dev and worker can read and comment but cannot create top-level issues, close them, or own timers.**

### Messaging inboxes

Message delivery is serialized per recipient. Claude Code uses its native Teams inbox and InboxPoller. Codex, Gemini tmux fallback, OpenCode, and future runtimes without a native inbox route through ExoMonad's per-agent FIFO inbox with one consumer task per agent; see [cross-runtime-message-inbox.md](../decisions/cross-runtime-message-inbox.md).

---

## 3. Hook Rules — per role

### PreToolUse deny matrix

| Rule | root | tl | dev | reviewer | worker |
|------|:----:|:--:|:---:|:--------:|:------:|
| Deny `Edit` / `Write` / `MultiEdit` / `NotebookEdit` (redispatch nudge) | x | x | | | |
| Deny `Bash(gh …)` (force MCP tools) | x | x | x | x | x |
| Deny `Bash(sqlite3 .chainlink/…)` / direct `.chainlink/issues.db` access | x | x | x | x | x |
| Dev-specific HTTP-context rewriting | | | x | | |

The TL/root deny carries this exact redispatch message so the agent retries through `send_message` or a fresh `spawn_leaf` / `spawn_worker` instead of writing files itself.

### Other hooks

| Hook | root | tl | dev | reviewer | worker |
|------|------|----|-----|----------|--------|
| `SessionStart` | default (team register) | default | default | default | default |
| `PostToolUse` | team registration | team registration | http rewriting | none | none |
| `Stop` / `SubagentStop` | allow | `tlStopCheck` (blocks if children pending) | `DevPhase.canExit` | `reviewerStopCheck` (blocks if `ReviewerReviewing`) | `workerStopCheck` |
| `BeforeModel` / `AfterModel` | allow | allow | http rewriting | allow | allow |
| Event handlers | `prReviewEventHandlers` | `prReviewEventHandlers` | `prReviewEventHandlers` | `reviewerEventHandlers` | default |

```mermaid
flowchart LR
  call[Agent tool call] --> Pre[PreToolUse]
  Pre -->|gh in command| DenyGh[Deny: use MCP tools]
  Pre -->|sqlite3 .chainlink| DenyDb[Deny: use Chainlink MCP]
  Pre -->|Edit/Write and role in tl,root| DenyImpl[Deny: redispatch via spawn_leaf/spawn_worker/send_message]
  Pre -->|otherwise| Run[Run tool]
  Run --> Post[PostToolUse: team registration / http rewrite]
  Post --> Done[Result to agent]
  Stop[Stop hook] --> SM[Check role phase canExit]
  SM -->|MustBlock| Block[Block exit, inject reason]
  SM -->|ShouldNudge| Nudge[Allow + nudge]
  SM -->|Clean| Allow[Allow exit]
```

---

## 4. Per-role State Machines

State persisted in KV per `birth-branch`. Transitions fire from tool handlers and from event handlers.

### TLPhase

```mermaid
stateDiagram-v2
  [*] --> TLPlanning
  TLPlanning --> TLDispatching: (implicit on first spawn)
  TLDispatching --> TLWaiting: ChildSpawned
  TLWaiting --> TLWaiting: ChildSpawned\n(adds to map)
  TLWaiting --> TLAllMerged: ChildCompleted\n(last child)
  TLWaiting --> TLMerging: PRMerged
  TLMerging --> TLAllMerged: PRMerged\n(last child)
  TLAllMerged --> TLPRFiled: OwnPRFiled
  TLPRFiled --> TLDone: AllChildrenDone
  TLWaiting --> TLFailed: ChildFailed
  TLPRFiled --> [*]
  TLDone --> [*]
  TLFailed --> [*]

  note right of TLWaiting: canExit = ShouldNudge\n("N children still pending")
  note right of TLPRFiled: canExit = MustBlock\n("PR filed, awaiting parent merge")
```

### DevPhase

```mermaid
stateDiagram-v2
  [*] --> DevSpawned
  DevSpawned --> DevWorking
  DevWorking --> DevPRFiled: PRCreated
  DevPRFiled --> DevChangesRequested: ReviewReceivedEv\n(round 0)
  DevPRFiled --> DevApproved: ReviewApprovedEv
  DevChangesRequested --> DevUnderReview: FixesPushedEv\n(round=1)
  DevUnderReview --> DevUnderReview: CommitsPushedEv\n(round++)
  DevUnderReview --> DevApproved: ReviewApprovedEv
  DevUnderReview --> DevNeedsHumanDirection: ReviewReceivedEv\n(round >= 1)
  DevApproved --> DevDone: MergeReadyEv
  DevPRFiled --> DevDone: MergeReadyEv
  DevNeedsHumanDirection --> [*]: (escalated, stays alive)
  DevDone --> [*]
  DevFailed --> [*]

  note right of DevChangesRequested: canExit = MustBlock
  note right of DevPRFiled: canExit = MustBlock
  note right of DevUnderReview: canExit = MustBlock
  note right of DevApproved: canExit = MustBlock\n(awaits CI merge-ready)
  note right of DevNeedsHumanDirection: canExit = MustBlock
```

Round vocabulary is zero-based and tied to reviewer verdicts. Round 0 is the first reviewer verdict after the PR is filed. If that verdict requests changes, the dev fixes and pushes; `FixesPushedEv` moves the dev to `DevUnderReview` with `review_round=1`. A second `ReviewReceivedEv` in round 1 transitions to `DevNeedsHumanDirection`, and the handler notifies the TL with `[STUCK: PR #N]`. That is an in-band human-clarification signal, not a watcher health failure and not a Chainlink `review-stuck` issue.

### ReviewerPhase

```mermaid
stateDiagram-v2
  [*] --> ReviewerSpawned
  ReviewerSpawned --> ReviewerPosted: ReviewerApprovedEv
  ReviewerSpawned --> ReviewerPosted: ReviewerRequestedChangesEv
  ReviewerPosted --> ReviewerReviewing: ReviewerFixesPushedEv\nor ReviewerCommitsPushedEv
  ReviewerReviewing --> ReviewerPosted: ReviewerApprovedEv\nor ReviewerRequestedChangesEv
  ReviewerPosted --> ReviewerDone: ReviewerMergeReadyEv
  ReviewerPosted --> ReviewerDone: ReviewerTimedOutEv
  ReviewerPosted --> ReviewerDone: ReviewerStuckEv
  ReviewerDone --> [*]
  ReviewerFailed --> [*]

  note right of ReviewerReviewing: canExit = MustBlock\n("post a verdict before exiting")
```

### WorkerPhase

```mermaid
stateDiagram-v2
  [*] --> WorkerSpawned
  WorkerSpawned --> WorkerRunning: WorkerStarted
  WorkerRunning --> WorkerDone: WorkerCompleted
  WorkerRunning --> WorkerFailed: WorkerErrored
  WorkerDone --> [*]
  WorkerFailed --> [*]
```

Worker has no `canExit` guards — workers are ephemeral and may end at any time.

---

## 5. PR Review Convergence Flow

This is the loop the watcher + dev + reviewer + TL collectively run. The watcher is the only place that observes the world (filesystem, CI, time). Every other actor reacts to events the watcher dispatches.

### Sequence — happy path

```mermaid
sequenceDiagram
  autonumber
  participant TL
  participant Dev
  participant Watcher as Worktree Watcher
  participant Reviewer
  participant CI as Forgejo CI

  TL->>Dev: spawn_leaf(spec)
  Dev->>Dev: implement, commit
  Dev->>Dev: file_pr (opens Forgejo PR)
  Note over Dev: DevPhase: DevPRFiled
  Watcher->>Reviewer: spawn (ephemeral) via review-loop
  Reviewer->>Reviewer: read diff
  Reviewer->>Reviewer: approve_pr (submits Forgejo review)
  Note over Reviewer: ReviewerPhase: ReviewerPosted
  Watcher->>Dev: handle_event(ReviewApproved) -> NoAction
  Note over Dev: DevPhase: DevApproved (MustBlock)
  CI-->>Watcher: CIStatus = success
  Watcher->>Dev: handle_event(CIStatus, mergeReady=true) -> NotifyParentAction
  Watcher->>TL: deliver [from: dev] [MERGE READY] PR #N
  Note over Dev: DevPhase: DevDone
  TL->>TL: merge_pr
  Watcher->>Dev: PR branch gone -> dev exits cleanly
```

### Sequence — fixes-pushed loop

```mermaid
sequenceDiagram
  autonumber
  participant Dev
  participant Watcher
  participant Reviewer
  participant TL

  Reviewer->>Reviewer: request_changes
  Watcher->>Dev: handle_event(ReviewReceived) -> InjectMessage(comments)
  Note over Dev: DevPhase: DevChangesRequested
  Dev->>Dev: fix, commit, push (SHA changes)
  Watcher->>Dev: handle_event(FixesPushed) -> NoAction
  Note over Dev: DevPhase: DevUnderReview round=1
  Watcher->>Reviewer: handle_event(FixesPushed) -> InjectMessage
  Note over Reviewer: ReviewerPhase: ReviewerReviewing
  Reviewer->>Reviewer: approve_pr OR request_changes again
  alt approved
    Watcher->>Dev: ReviewApproved -> DevApproved
  else changes requested round 2
    Watcher->>Dev: ReviewReceived -> DevNeedsHumanDirection
    Watcher->>TL: [STUCK] PR #N requires human direction
  end
```

### Event vocabulary (Rust watcher -> WASM handler)

These are the `PRReviewEvent` constructors the watcher emits. Each role's `prReviewEventHandlers` decides what to do with them.

| Event | Watcher trigger | Dev/TL handler | Reviewer handler |
|-------|-----------------|----------------|------------------|
| `ReviewReceived` | new Forgejo review comments | log + `ReviewReceivedEv` + inject comments | log + `ReviewerRequestedChangesEv` + inject |
| `ReviewApproved` | review state = approved | `ReviewApprovedEv` -> `DevApproved` | `ReviewerApprovedEv` -> `ReviewerPosted` |
| `ReviewerApproved` | reviewer agent set verdict approved | same as above | same as above |
| `ReviewerRequestedChanges` | reviewer wrote requested-changes verdict | `ReviewReceivedEv` (one fix round) | `ReviewerRequestedChangesEv` |
| `FixesPushed` | SHA change after `changes_requested` | `FixesPushedEv` -> round++ | inject `[FIXES PUSHED]` to re-review |
| `CommitsPushed` | SHA change outside the changes-requested window | `CommitsPushedEv` -> round++ | `ReviewerCommitsPushedEv` |
| `ReviewTimeout` | no reviewer response within `reviewer_max_wait_seconds` | log only | `ReviewerTimedOutEv` -> Done |
| `MergeReady` | reviewer approval AND CI success/neutral both seen | `MergeReadyEv` -> Dev sends `[MERGE READY]` to TL | `ReviewerMergeReadyEv` -> Done |
| `Stuck` | rounds exceed `reviewer_max_rounds` | inject "stay alive, wait for TL" | `ReviewerStuckEv` -> Done |
| `RateLimited` | rate-limit hit | log only | log only |
| `DevNotPushing` / `ReviewerNotResponding` / `ReviewerNeverStarted` / `ReviewDevFailed` | health probes | log only (escalated by watcher to chainlink `review-stuck`) | n/a |

### CI gating — why `MergeReady` requires both

```mermaid
flowchart TD
  A[Watcher tick] --> B{review approved?}
  B -- no --> Z[no merge_ready]
  B -- yes --> C{CI status in success or neutral?}
  C -- no --> Z
  C -- yes --> D{first time both true?}
  D -- no --> Z
  D -- yes --> E[fire MergeReady event]
  E --> F[Dev: NotifyParentAction]
  F --> G[TL receives 'from: dev MERGE READY']
```

Without Forgejo Actions producing a CI status, `ci_mergeable_at` stays `None` and `MergeReady` never fires even with reviewer approval.

---

## 6. Watcher Escalation Outputs

Beyond per-PR events, the watcher escalates terminal failure modes to **chainlink `review-stuck` issues** rather than re-trying. The TL surface treats those as human-clarification inputs (do not auto-close, do not respawn the dev leaf).

| Watcher signal | Outcome |
|----------------|---------|
| `dev_not_pushing` | open chainlink `review-stuck` issue |
| `reviewer_not_responding` | open chainlink `review-stuck` issue |
| `reviewer_never_started` | open chainlink `review-stuck` issue |
| `dev_failed` | open chainlink `review-stuck` issue |
| `Stuck` (rounds exceeded) | inject "wait for TL", dev moves to `DevNeedsHumanDirection` |

---

## 7. Generated HTML View

A standalone single-file view of every diagram in this doc renders in any browser:

- `docs/architecture/agent-system.html`

Open it directly (no server needed). Update both files together when role behavior changes.
