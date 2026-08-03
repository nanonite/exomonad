# exomonad-core — Unified Library

ExoMonad core is the unified library providing the effect system framework, WASM hosting via Extism, and built-in effect handlers and services for git, GitHub, agent orchestration, and more. It defines the FFI boundary using protobuf.

## Module Structure

| Directory | Purpose |
|-----------|---------|
| `effects/` | EffectHandler trait, EffectRegistry, dispatch, error helpers |
| `handlers/` | Effect handler implementations (git, github, log, agent, fs, etc.) |
| `services/` | Business logic services (git, github, agent_control, event_queue, etc.) |
| `services/external/` | External API clients (anthropic, github/octocrab, ollama, otel) |
| `mcp/` | MCP types (ToolDefinition) and tools module |
| `protocol/` | Wire format types (hook, mcp, service) |
| `codex_config.rs` | Codex runtime config rendering: `.codex/config.toml`, MCP server entries, model field, developer instructions, extra MCP servers, and shell-native hook command JSON |

## Feature Flags

| Feature | Default | Description |
|---------|---------|-------------|
| `runtime` | Yes | Full runtime: WASM hosting, effect handlers, services |

Without `runtime`: only `ui_protocol` module available (agent event types, telemetry).

## Key Types

| Type | Purpose |
|------|---------|
| `EffectHandler` | Trait for implementing namespace-based effect handlers |
| `EffectRegistry` | Registry for dispatching effects by namespace |
| `EffectContext` | Identity context (agent name, birth branch, working dir) passed to all handlers |
| `EffectError` | Common error type for all effects with protobuf mapping |
| `PluginManager` | Manages WASM guest calls and host function dispatch via Extism |
| `RuntimeBuilder` | Fluent API for assembling handlers and loading WASM |
| `SpawnSubtreeOptions` | Options for spawning a Claude agent (permissions, etc.) |
| `SpawnLeafOptions` | Options for spawning a Gemini agent |

`SpawnResult.branch_name` is the actual dot-prefixed git branch created or
resumed by worktree spawns. Agent response conversion must use this value
instead of the raw branch-name suffix from the MCP request; shared-directory
worker results leave it empty because they do not create a git branch.

## Capability Traits (`Has*` Pattern)

Handlers and delivery functions are generic over a context `C` bounded by capability traits. Each consumer declares only the traits it needs — the bounds ARE the dependency graph.

**Traits** (defined in `services/mod.rs`, implemented on `Services`):

| Trait | Provides |
|-------|----------|
| `HasTeamRegistry` | `&TeamRegistry` |
| `HasAgentResolver` | `&AgentResolver` |
| `HasEventQueue` | `&EventQueue` |
| `HasEventLog` | `Option<&EventLog>` |
| `HasProjectDir` | `&Path` |
| `HasSupervisorRegistry` | `&SupervisorRegistry` |
| `HasClaudeSessionRegistry` | `&ClaudeSessionRegistry` |
| `HasMutexRegistry` | `&MutexRegistry` |
| `HasGitHubClient` | `Option<&Arc<GitHubClient>>` |
| `HasGitWorktreeService` | `&Arc<GitWorktreeService>` |

**Handler pattern** — each handler is `Handler<C>` with `Arc<C>`:
```rust
pub struct SessionHandler<C> { ctx: Arc<C> }
impl<C: HasClaudeSessionRegistry + HasTeamRegistry + HasSupervisorRegistry + 'static>
    EffectHandler for SessionHandler<C> { ... }
```

**Delivery functions** — `impl Trait` bounds:
```rust
pub async fn route_message(
    ctx: &(impl HasTeamRegistry + HasAgentResolver + HasInboxStore + HasProjectDir),
    address: &Address, from: &AgentName, content: &str, summary: &str,
) -> DeliveryOutcome
```

**Concrete wiring** — only `groups.rs` and `serve.rs` name `Services`:
```rust
// groups.rs — the bridge between generic handlers and concrete Services
pub fn orchestration_handlers(
    agent_control: Arc<AgentControlService<Services>>,
    services: Arc<Services>,
    ...
) -> Vec<Box<dyn EffectHandler>>
```

**Handlers unchanged** (no `Services`/`ctx` dependency): `GitHandler`, `FsHandler`, `ProcessHandler`, `CopilotHandler`, `KvHandler`, `GitHubHandler`.

## Delivery Integration

Message delivery is centralized in `services/delivery.rs`.

**Delivery priority**: Teams inbox for Claude Code agents, then HTTP-over-UDS (`.exo/agents/{name}/notify.sock`) for socket-backed agents, then tmux STDIN injection. Non-Claude runtimes record messages in the ExoMonad inbox before reaching tmux fallback.

## Delivery Pipeline (`services/delivery.rs`)

Delivery functions are generic over `C` via `impl Has*` bounds (no concrete `Services` type):

| Function | Bounds | Used by |
|----------|--------|---------|
| `route_message()` | `HasTeamRegistry + HasAgentResolver + HasInboxStore + HasProjectDir` | `send_message` effect |
| `deliver_to_agent()` | `HasTeamRegistry + HasAgentResolver + HasInboxStore + HasProjectDir` | Peer messaging, event handler `InjectMessage` |
| `notify_parent_delivery()` | `HasTeamRegistry + HasEventLog + HasEventQueue + HasInboxStore + HasProjectDir` | `notify_parent` effect, poller `NotifyParent` action |

**Worker pane delivery** (tmux fallback for workers): `routing.json` stores `pane_id` (e.g. `%42`) for direct tmux targeting. `inject_input` passes `pane_id` as the `target` argument.

Agent inbox queues maintain the invariant that a non-empty queue always has a consumer; the consumer exits only when the queue is empty. Failed tmux injection retries up to `MAX_DELIVERY_ATTEMPTS` with exponential backoff capped at `MAX_DELIVERY_BACKOFF`, then abandons the message with an ERROR log and the `agent_inbox.messages_abandoned` metric. Injection panics are isolated per attempt and retried instead of killing the consumer. Abandoned messages clear `pending` without marking the event `recent`, so the same event can be re-delivered on a later `enqueue`.

All messages are prefixed with `[from: id]` (or `[FAILED: id]` for failures). Event handler messages include structural tags inside the body (e.g. `[from: leaf-id] [PR READY] PR #5 approved...`).

**Rule**: Any code path that notifies a parent MUST use `notify_parent_delivery()`, never raw `deliver_to_agent()`. This ensures OTel span events, EventQueue publication, and consistent `[from:]`/`[FAILED:]` formatting.

`deliver_to_agent()` is correct for peer-to-peer messaging (send_message, event handler InjectMessage).

### Routing Resolution

Durable inbox writes canonicalize recipient keys at the single `record_inbox_delivery()` chokepoint. The delivery layer uses `AgentResolver` to resolve a caller-supplied bare slug such as `patch-step-over` to the recipient's suffixed `AgentName` such as `patch-step-over-opencode` before writing `to_agent`. Already-canonical agent names and dotted branch identities pass through unchanged; unresolved keys are recorded unchanged with a WARN and `[event] message.delivery` telemetry instead of silently orphaning without evidence.

**Reserved alias `parent`.** The literal recipient `parent` is not an agent name — it is a reserved alias meaning "the caller's parent". Its behavior is context-dependent by design:

- In `notify_parent`, an `override_recipient` of `Agent("parent")` is treated as the normal parent sentinel: it is rewritten to `Address::Supervisor` and resolved to the real parent via supervisor/structural routing before delivery. This guarantees no inbox row is ever written under `to_agent = "parent"`.
- In generic `send_message` / `route_message`, `Agent("parent")` is **rejected** (`DeliveryOutcome::Failed`, no durable row): peer messaging requires a concrete agent name. An agent reaches its parent via `notify_parent`, never by addressing the literal string `parent`.

This asymmetry is intentional — only the `notify_parent` relationship has a well-defined parent to resolve. Both paths fail loudly rather than orphan a message under the literal key.

`check_inbox` resolves a bare agent key through the `AgentResolver` slug table before exact-name fallback. This lets a root context whose runtime identity is `root` drain mail stored under the canonical suffixed agent name such as `root-claude`, preventing unread poke loops.

## Agent Resume and Liveness Contract

- A successful resume of an already-live PR owner refreshes `.exo/agents/{agent}/last_activity_at` without rewriting `routing.json` or identity metadata.
- `last_activity_at` is lifecycle/resume metadata and must not be reported as `last_check_inbox_at`; the latter is reserved for an explicit `check_inbox` drain.
- Timeout reconciliation uses the newest activity marker before considering historical `spawned_at` age, then re-verifies the stable routing window/pane ID through tmux before any kill. Missing or unverifiable routing is handled conservatively.
- Agent listing treats a valid routing target as the liveness source of truth, using
  invocation routing as a legacy fallback and display-name scans only for entries
  without persisted routing metadata.
- `routing.json` is a retained last-known routing snapshot. It is written for a new
  process attempt and remains readable after that attempt exits; `exited_at` and a
  terminal `invocation.json` status are the retirement markers, so retained IDs are
  history and never by themselves authorize delivery or liveness.
- Status surfaces report retained routing as `RETIRED` with the last-known window or
  pane ID and exit code. An agent with neither routing snapshot nor invocation routing
  reports `NO-ROUTING-RECORDED`, which is distinct from a retired target.

### Ownership versus invocation

The canonical owner remains one Chainlink issue → one agent identity → one
worktree → one branch/PR. A process attempt is metadata inside that existing
identity, not a second workflow owner and not a stacked-PR abstraction.

.exo/agents/{agent}/invocation.json stores exactly the current generation:

    {
      "invocation_id": "uuid",
      "runtime": "codex",
      "trigger": "spawn",
      "routing": {"window_id": "@42"},
      "started_at": 1730000000,
      "ended_at": null,
      "status": "running",
      "exit_code": null,
      "pr_number": 580,
      "head_sha": "abc123"
    }

runtime, trigger, routing, started_at, and status are required. ended_at,
exit_code, pr_number, and head_sha are optional. Starting resume_pr or a
SHA-scoped reviewer replaces only this record and records the exact current
tmux window/pane. Finishing must supply the invocation ID (or matching
routing) and cannot change a newer record. Missing or malformed records are
legacy metadata: status and cleanup paths log the condition and avoid
destructive reconciliation. A finished invocation is dormant ownership; an
open PR, review, or CI work unit remains pending and does not dispose the
issue-owned worktree.

### One-shot coding invocations

Each coding dev or reviewer process handles one assignment: receive the task,
perform it, publish the authoritative result, and exit cleanly. One-shot means
one assignment per process, not non-interactive execution. While the exact
invocation is alive, its durable inbox and tmux guidance remain available only
through the validated current routing pane; stale targets are rejected and are
never redirected to the root pane.

After a dev publishes a PR or a reviewer submits its exact-SHA verdict, the
process exits. PR, review, and CI watcher state remain authoritative. If more
dev work is needed later, `resume_pr` starts a fresh invocation in the same
issue-owned worktree, branch, and PR, with pending inbox guidance visible at
startup; it does not create a new owner.

## Forgejo Watcher and GitHub Poller State Machines

### Authoritative PR publication

`file_pr` may publish a `PublishedHead` only after the successful Forgejo
create/update response confirms the PR number, exact head and base branches,
and a non-empty head SHA. The publication is durably deduplicated by PR,
branch, and SHA in `.exo/published-heads.json`. A repeated PR+SHA publication
is harmless; a different SHA is retained as a new review-cycle candidate.

The Forgejo watcher consumes a publication only when its PR number, branches,
and SHA match the current Forgejo PR response. Missing, stale, or unconfirmed
heads are ignored, so they cannot spawn a reviewer or advance a review verdict.
The existing issue → agent identity → worktree → branch/PR ownership remains
unchanged. Invocation ID/runtime/trigger fields are optional context attached
to the publication, never a second owner model.

Inbox and exact-pane tmux delivery are guidance channels. Injection success,
process exit, and local push events do not advance watcher state; publication,
Forgejo review verdicts, and CI observations remain the state-machine inputs.

`worktree_event_watcher.rs` is the active Forgejo-backed PR/review/CI watcher. It rebuilds PR registry state from Forgejo each cycle and persists only watcher bookkeeping such as review rounds and stuck flags. `github_poller.rs` is currently hibernated: it has zero active call sites. Keep its review-loop semantics in parity with `worktree_event_watcher` so future GitHub Actions integration can re-enable it as a thin transport shim.

`GitHubPoller<C>` is generic over capability traits. Single-phase init: `GitHubPoller::new(ctx)` — no `with_services()`. Background tokio task polling GitHub every 60s. Tracks per-PR state in `HashMap<PRNumber, PRState>`.

### PR Lifecycle States

```
ForgejoReviewVerdict::None ──(Forgejo review approves)──→ ForgejoReviewVerdict::Approved
       │                                         │
       │                                    sends [PR READY] to parent
       │
       ├──(Forgejo review requests changes)──→ ForgejoReviewVerdict::ChangesRequested
       │                                         │
       │                                    stop hook blocks exit
       │                                         │
       │                              (agent pushes, SHA changes)
       │                                         │
       │                              fires [FIXES PUSHED] to parent
       │                              sets addressed_changes = true
       │                                    reset → None
       │
       └──(timeout, no review)──→ timeout
              │                      sends [REVIEW TIMEOUT] to parent
              │
              15 min (initial) / 5 min (after addressing changes)
```

**Copilot review lifecycle:** The first review is automatic (triggered on PR creation). Subsequent reviews after pushing fixes are NOT — automatic re-review is not guaranteed. The `FixesPushed` event fills this gap: when the poller detects a SHA change on a PR that was `ChangesRequested`, it fires `fixes_pushed` immediately and uses a shorter 5-minute fallback timeout.

**Reviewer auto-spawn lifecycle:** The Forgejo watcher attempts reviewer spawn on first PR sighting and on every poll where `reviewer_spawned=false`, `reviewer_disposed=false`, and review rounds remain below policy. Failed spawn attempts keep `reviewer_spawned=false` so the next poll retries even when the head SHA is unchanged. A missing `reviewer_spawner` is a WARN because silent skips strand PRs without review.

**Reviewer completion routing:** Reviewer agents submit Forgejo reviews directly and then exit. They do not call `notify_parent`; the watcher observes Forgejo verdicts and routes review events to the PR-owning dev leaf and TL. The watcher never injects review events back into the exited reviewer pane. When another review round is needed, the watcher explicitly spawns a fresh reviewer through its `reviewer_spawned`/`reviewer_disposed` state machine.

### Event Dispatch Flow

1. Poller detects state change (new comments, approval, timeout, merge)
2. Calls `call_handle_event()` → WASM `handle_event` FFI
3. Haskell `dispatchEvent` routes to role's `EventHandlerConfig` handler
4. Handler returns `EventAction` (InjectMessage, NotifyParentAction, NoAction)
5. Poller acts on the action via `handle_event_action()`

### Stale Notification Guard

Once the parent has been notified (via `[PR READY]` approval or `[REVIEW TIMEOUT]`), `compute_pr_actions` suppresses all further events for that PR. Late Copilot reviews, CI status changes, and new commits are silently dropped — the TL has already been told to merge, so any further notifications are stale and confusing.

### Merge Detection

When a tracked PR's branch disappears from the open PR list, it was merged/closed. The poller:
- Fires `sibling_merged` WASM event on sibling agents (same parent branch, open PRs) via `call_handle_event`
- Emits `agent.sibling_merged` OTel span event
- Removes the PRState from tracking

## Related Documentation

- [Root CLAUDE.md](../../CLAUDE.md)
- [Handlers CLAUDE.md](src/handlers/CLAUDE.md)
- [Haskell WASM guest](../../haskell/wasm-guest/CLAUDE.md)
