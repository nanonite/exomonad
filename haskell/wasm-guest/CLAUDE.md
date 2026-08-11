# WASM Guest Documentation

Documentation for the Haskell WASM guest plugin, which defines MCP tools and effects.

## Effects

The WASM guest utilizes a variety of effects to interact with the host system.
These are defined in `ExoMonad.Effects.*` and interpreted by the Rust host.

- `Git`: Git operations (branch, status, log, etc.)
- `GitHub`: GitHub API interactions (issues, PRs).
- **`Events`**: Inter-agent synchronization (wait/notify).
- **`Session`**: Session lifecycle management (register Claude session ID).
- `Log`: Logging to the host console.
- `FS`: File system access.
- `Agent`: Agent lifecycle management.
- `Process`: Ad-hoc process execution (run commands with args, env, timeout).
- `Memory`: Append-only session-memory ledger and continuation brief rendering.

## MCP Tools

The guest exports MCP tools that agents can call. These are defined in `ExoMonad.Guest.Tools.*`.

### Events Tools (`ExoMonad.Guest.Tools.Events`)

- **`notify_parent`**: Used by worker/subtree agents to send messages to their parent. Routes to parent via server — delivers as a native `<teammate-message>` through Claude Code's Teams inbox when a team is active, falls back to tmux STDIN injection otherwise. Agent messages get `[from: id]` prefix; failure messages get `[FAILED: id]` prefix. Available as a bare field in both TL and dev roles.
- **`send_tmux_message` / `send_mailbox_message`**: Tool for sending arbitrary messages between exomonad-spawned agents.

### Spawn Tools (`ExoMonad.Guest.Tools.Spawn`)

- **`fork_wave`**: Fork N parallel agents in isolated worktrees (branch + PR). Agent type defaults to the server's `--worker` setting; omit `agent_type` to use the default. Claude agents get context inheritance via `--fork-session`; OpenCode agents use headless serve/attach mode. Requires clean git state.
- **`spawn_leaf`**: Spawn a leaf agent in its own worktree+branch. Files PR when done. Agent type defaults to the server's worker setting; pass `agent_type` only for a specific supported runtime. Structured spec fields: steps, verify, boundary, context, read_first.
- **`spawn_codex`**: Spawn a Codex leaf agent in its own worktree+branch. Files PR when done. Uses the same structured fields as `spawn_leaf` but forces `agent_type = codex`.
- **`spawn_worker`**: Spawn an ephemeral worker in a tmux pane. No branch, no PR. Agent type defaults to the server's worker setting; pass `agent_type` only for a specific supported runtime.
- **`spawn_leaf_subtree`** (SDK core): Lower-level worktree/standalone spawn used by `spawn_leaf`.
- **`spawn_workers`** (SDK core): Lower-level batch inline pane spawn used by `spawn_worker`.
- **`replace_close_pr`**: With explicit human approval, replace an open or closed unmerged PR from its exact head SHA and original base branch. It does not close the old PR; reconcile that PR explicitly after verifying the replacement.

### Cleanup Tools

- **`cleanup_orphan`**: Legacy cleanup for a named dead agent. It refuses live tmux windows and is intended for the existing orphan path.
- **`cleanup_leaf`**: On-demand safety-checked cleanup for a named orphan or a `sweep=true` set. The host verifies dead tmux state, a clean worktree, exactly one matching PR, and a merged or closed-unmerged PR before using the shared disposal path. Use `dry_run=true` to inspect; it never force-cleans dirty or ambiguous targets.

### Task Tools (`ExoMonad.Guest.Tools.Tasks`)

- **`task_list`**: List tasks from the shared Claude Code task list. Optionally filter by status. Team name auto-resolved from TeamRegistry.
- **`task_get`**: Get a task by ID from the shared task list.
- **`task_update`**: Update task status, owner, or activeForm. Structural fields (subject, description, blocks, blockedBy) are never overwritten.

Available in dev and worker roles. Enables agents to coordinate via the same task list that Claude Code's native TaskCreate/TaskList/TaskUpdate tools use.

### Memory Tools (`ExoMonad.Guest.Tools.Memory`)

- **`memory_append`**: Appends a validated semantic fact. The ledger is append-only and accepts the closed session-memory kind set.
- **`memory_list`**: Lists current-run records with optional kind, issue, importance, and limit filters.
- **`continuation_brief`**: Requests the deterministic root/TL continuation brief. It is withheld from dev and worker roles.

The guest performs argument parsing and yields `memory.*` effects; Rust owns the
ledger, adapters, and renderer. No guest tool performs filesystem, database,
Chainlink, or Forgejo I/O.

### Defining MCP Tools (`ExoMonad.Guest.Tool.Class`)

MCP tools are defined by implementing the `MCPTool` typeclass for a specific type.

```haskell
class (FromJSON (Args t), ToJSON (Result t)) => MCPTool t where
  type Args t :: Type
  type Result t :: Type
  toolName :: Text
  toolDescription :: Text
  toolSchema :: Aeson.Object  -- JSON Schema as an Object (Aeson.KeyMap Value)
  handleCall :: Args t -> Eff es (Result t)
```

Tool schemas are typically derived using `genericToolSchema` from `ExoMonad.Guest.Tool.Schema`.

### Permissions (`ExoMonad.Guest.Types.Permissions`)

Claude-only permissions system using the `ClaudePermissions` DSL.

- **`ToolPattern`**: DSL for defining tool allow/deny patterns (e.g., `bash`, `gh`, `read_file`).
- **`ClaudePermissions`**: Record of allowed/denied tools and paths.
- **`renderPermissions`**: Renders to Claude Code's native `settings.local.json` format.

### Prompt Builder (`ExoMonad.Guest.Prompt`)

Pure Haskell prompt assembly for worker/leaf agents. Replaces the former template effect round-trip (Haskell → proto → Rust disk I/O → proto → Haskell) with direct string composition.

- Builder monoid: `task`, `boundary`, `steps`, `context`, `verify`, `doneCriteria`, `readFirst`, `raw`
- Inline profiles: `tlProfile`, `leafProfile`, `workerProfile`, `researchProfile`, `generalProfile`, `rustProfile`, `haskellProfile`

### SDK/Role Split

The SDK (`wasm-guest`) exports **core I/O functions** and **shared descriptions/schemas**. Role code (`.exo/roles/devswarm/`) defines **MCPTool instances** that call the core and apply role-specific state transitions.

| SDK Module | Exports | Used by |
|-----------|---------|---------|
| `Tools.FilePR` | `filePRCore`, `filePRDescription`, `filePRSchema`, `FilePRArgs`, `FilePROutput` | `DevFilePR`, `TLFilePR` |
| `Tools.Events` | `notifyParentCore`, `shutdownCore`, descriptions/schemas, `MCPTool SendTmuxMessage / SendMailboxMessage` | `DevNotifyParent`, `TLNotifyParent`, `WorkerNotifyParent` |
| `Tools.MergePR` | `mergePRCore`, `mergePRRender`, description/schema, `extractAgentName` | `TLMergePR` |
| `Tools.Spawn` | `forkWaveCore`, `spawnWorkerToolCore`, `spawnLeafSubtreeCore`, `spawnWorkersCore`, descriptions/schemas, render functions | `TLForkWave`, `TLSpawnLeaf`, `TLSpawnWorker`, `RootForkWave`, `RootSpawnLeaf`, `RootSpawnWorker` |
| `Tools.SpawnCodex` | `handleSpawnCodex`, `spawnCodexDescription`, `spawnCodexSchema`, `SpawnCodex` | `TLSpawnCodex`, `RootSpawnCodex` |
| `Tools.Tasks` | `taskListCore`, `taskGetCore`, `taskUpdateCore`, descriptions/schemas | `DevTaskList`, `DevTaskGet`, `DevTaskUpdate`, `WorkerTaskList`, `WorkerTaskGet`, `WorkerTaskUpdate` |
| `Tools.Memory` | `memoryAppendCore`, `memoryListCore`, `continuationBriefCore`, schemas and descriptions | `RootRole`, `TLRole`, `DevRole`, `WorkerRole` (brief only root/TL) |

`SendTmuxMessage / SendMailboxMessage` is the only tool with an `MCPTool` instance in the SDK (no state transitions needed).

### PR acceptance criteria contract

Every new or updated `file_pr` body carries the issue's Definition of Done under the literal `## Acceptance Criteria` heading, with bullets copied verbatim. `resume_pr` keeps the structured `done_criteria` handoff and instructs the resumed owner to preserve or update that same heading on the next `file_pr` call. Reviewers treat the heading as authoritative and request changes for missing or unsatisfied bullets without inventing criteria.

### Roles

| Role | Tools | State Machine | Spawned by |
|------|-------|---------------|------------|
| **root** | `RootForkWave`, `RootSpawnLeaf`, `RootSpawnCodex`, `RootSpawnWorker`, `RootMergePR`, `SendTmuxMessage / SendMailboxMessage` | `TLPhase` (tracks children via `ChildSpawned`/`ChildCompleted`) | `exomonad init` (human-facing TL) |
| **tl** | `TLForkWave`, `TLSpawnLeaf`, `TLSpawnCodex`, `TLSpawnWorker`, `TLMergePR`, `TLFilePR`, `TLNotifyParent`, `SendTmuxMessage / SendMailboxMessage` | None — Python `tl_loop` owns state; no TL phase is persisted in WASM | `fork_wave` |
| **dev** | `DevFilePR`, `DevNotifyParent`, `SendTmuxMessage / SendMailboxMessage`, `DevTaskList`, `DevTaskGet`, `DevTaskUpdate` | `DevPhase` (one assignment per process; watcher owns later PR/review/CI state) | `spawn_leaf` (worktree) |
| **worker** | `WorkerNotifyParent`, `SendTmuxMessage / SendMailboxMessage`, `WorkerTaskList`, `WorkerTaskGet`, `WorkerTaskUpdate` | None (ephemeral, parent controls exit) | `spawn_worker` |
| **testrunner** | `Instruct`, `TestrunnerNotifyParent` | None (allow-all hooks) | Companion config |

## Hooks

The guest handles hooks invoked by Claude Code:
- **`onSessionStart`**: Captures Claude session ID and yields `SessionRegister` effect.
- **`onPreToolUse`**: Validates tool calls (stops restricted tools).
- **`onPostToolUse`**: Logs tool usage.
- **`onSubagentStop`**: Validates child agent exit status.
- **`onStop`**: Role-specific lifecycle hook. Worker and reviewer checks may gate exit; root and programmatic TL allow exit, while the Python loop owns TL terminal decisions.

### State Machine (`ExoMonad.Guest.StateMachine`)

Generic `StateMachine` typeclass for agent lifecycle phases. Users define sum types + transitions, and the framework handles KV persistence, logging, and stop-hook integration for roles that opt into it.

```haskell
class (ToJSON phase, FromJSON phase, Typeable phase, Show phase) => StateMachine phase event where
  transition :: phase -> event -> TransitionResult phase  -- pure
  canExit    :: phase -> StopCheckResult
  machineName :: Text  -- scopes KV key: "phase-{name}"
```

**Framework functions:**
- `getPhase` — read current phase from KV
- `applyEvent defaultPhase event` — read phase, apply transition, persist + log
- `checkExit defaultPhase` — read phase, return `StopCheckResult`

**Phase types** live in `.exo/roles/devswarm/`:
- `DevPhase.hs` — dev agent phases + events + `StateMachine` instance
- `TLPhase.hs` — Haskell golden-parity source for the Python TL FSM; also retained by the legacy root role
- `WorkerPhase.hs` — worker agent phases + events + instance

**KV key scoping:** Each persisted machine writes to `"phase-{machineName}"` (e.g., `"phase-dev"` or `"phase-worker"`). The programmatic `tl` role does not write a `phase-tl` key; `TLPhase.hs` remains available for root compatibility and golden parity.

**Usage from tool/event handlers:**
```haskell
import ExoMonad.Guest.StateMachine (applyEvent)
import DevPhase (DevPhase(..), DevEvent(..))

-- In a tool handler:
void $ applyEvent @DevPhase @DevEvent DevSpawned (PRCreated prNum url branch)
```

### Stop Hook State Machine (`ExoMonad.Guest.Effects.StopHook`)

| PR State | Decision | Agent can exit? |
|----------|----------|----------------|
| `changes_requested` | **MustBlock** | No — must address review comments |
| Has comments (not changes_requested) | ShouldNudge | Yes, with nudge |
| No reviews yet | ShouldNudge | Yes — "system will auto-notify your parent" |
| Approved | Clean | Yes |
| No PR, uncommitted work | ShouldNudge | Yes, with nudge |
| No PR filed for branch | ShouldNudge | Yes, with nudge to file PR |
| No PR, clean, no commits | ShouldNudge | Yes, with nudge to file PR |
| On main/master | Allow | Yes |

Coding invocations are one assignment per process, not non-interactive. A live
dev or reviewer continues to consume durable inbox guidance through the
validated tmux pane for that exact invocation; stale targets are rejected and
never redirected to the root pane. After the dev publishes its PR or the
reviewer submits its verdict, the process exits without waiting for
merge-ready. `resume_pr` starts the next invocation in the same owner
worktree/branch/PR and exposes pending guidance at startup.

## Event Handlers

Third dispatch category alongside tools and hooks. Reactive to world events (GitHub poller, timers).

### Architecture

```
GitHub poller (Rust, 60s interval)
  → detects state change (new comments, approval, timeout, merge)
  → calls WASM handle_event({ role, event_type, payload })
  → Haskell dispatchEvent routes to EventHandlerConfig handler
  → handler returns EventAction
  → Rust acts on action (InjectMessage → deliver to agent, NotifyParent → notify_parent_delivery)
```

### Types (`ExoMonad.Guest.Events`)

| Type | Purpose |
|------|---------|
| `EventHandlerConfig` | Per-role handler config: `onPRReview`, `onCIStatus`, `onTimeout`, `onSiblingMerged` |
| `EventAction` | Handler return: `InjectMessage Text`, `NotifyParentAction Text Int`, `NoAction` |
| `PRReviewEvent` | `ReviewReceived` (comments), `ReviewApproved`, `ReviewTimeout`, `FixesPushed` (CI status) |
| `SiblingMergedEvent` | `mergedBranch`, `parentBranch`, `siblingPRNumber` |
| `EventInput` | Top-level wrapper with `event_type` discriminator for dispatch |

### PR Review Handler (`.exo/lib/PRReviewHandler.hs`)

| Event | Action | Effect |
|-------|--------|--------|
| `ReviewReceived`, `ReviewCommented`, `ReviewerRequestedChanges` | `NoAction` for dev, `InjectMessage` for TL | The dev records review state where applicable but does not notify the parent directly. The Rust watcher emits the durable, SHA-scoped parent handoff; the TL diagnoses it and resumes the existing PR owner. |
| `ReviewApproved`, `ReviewerApproved`, `ReviewTimeout`, `CIBlocked`, `MergeReady` | `NoAction` for dev, `InjectMessage` for TL | The Rust watcher owns the authoritative parent handoff, keyed to the verified PR head and review outcome. Forgejo verdicts, verified head, and CI remain the state-machine inputs. |
| `FixesPushed`, `CommitsPushed` | `NoAction` for dev, `InjectMessage` for TL | The Rust watcher records the new verified head and starts the next review cycle; delivery is not a workflow transition. |
| `CITriggered` and `Stuck` | `InjectMessage` for dev and TL | Runtime guidance remains available to the live exact invocation; it does not itself advance watcher state. |
| `SiblingMerged` | `InjectMessage` | Injects rebase instructions when a sibling branch is merged |

### Wiring

- **Dispatch**: `handle_event` FFI export in `Main.hs`, routes `{ role, event_type, payload }` JSON to the role's `EventHandlerConfig`
- **Config**: Dev and TL roles use `prReviewEventHandlers`, Worker uses `defaultEventHandlers` (all NoAction)
- **Extensibility**: Add new event types to `EventInput` + new handlers to `EventHandlerConfig`. The poller fires events, WASM decides actions.
