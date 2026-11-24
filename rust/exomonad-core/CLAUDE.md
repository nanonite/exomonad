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
| `opencode_plugin.rs` | OpenCode TypeScript plugin package as compile-time constants (`OPENCODE_PLUGIN_TS`, `OPENCODE_PLUGIN_PKG_JSON`) and its shape tests |

## OpenCode plugin (`opencode_plugin.rs`)

The generated `.exo/opencode-plugin/index.ts` targets the OpenCode v2 plugin
API: its default export is `{ id: "exomonad", setup }`, and `setup(ctx)`
registers `ctx.tool.hook("execute.before")`, `ctx.tool.hook("execute.after")`,
and `ctx.event.subscribe(...)` for the terminal `session.execution.*` events.
Handlers spawn `exomonad hook <event> --runtime opencode`; deny is a thrown
error from the before hook, argument mutation is `Object.assign` from the
hook's `updatedInput`. `callHook` parses stdout even when `execFile` reports a
non-zero exit, because a deny exits 2.

`protocol/hook.rs` owns the single OpenCode adapter,
`normalize_opencode_hook_payload`. It fills the fields OpenCode omits
(`hook_event_name`, `tool_input`, `tool_response`, `tool_use_id`,
`session_id`), and maps provider tool ids from the closed
`OPENCODE_CANONICAL_BUILTINS` table onto the canonical OpenCode ids
`.exo/lib/HookPolicy.hs` matches (`shell`/`bash`/`run_command`→`shell`,
`edit`→`edit`, `patch`/`multiedit`→`patch`, `write`→`write`, and
`exomonad_<tool>`→`<tool>`). `opencode_unknown_tool_deny` fails **closed** on
PreToolUse: an id that is not in that table, an `exomonad_<tool>` id, or an MCP
`<server>_<tool>` id produces a deny envelope with exit 2 whose reason names
the exact id and `canonical_opencode_builtin`. Every role gets the deny; there
is no per-role allowlist and no env/flag bypass. PostToolUse, Stop, and
worker-exit only log `hook.opencode_unknown_tool` (warn) and never deny.

The code-mode `execute` tool maps to `execute` for the same reason. Verified
against v2.3.2: with `experimental.full_codemode: true`, the inner
`tools.<name>` calls (captured for `shell` and `write`) fire `execute.before`
again with their own provider id and `call_id`, so the role guardrails apply
inside the script and a deny blocks the inner call before it runs. The captured
sequence is `tests/fixtures/opencode_v2_codemode_hook_events.jsonl`, replayed by
`captured_codemode_inner_calls_normalize_to_guarded_ids`; the opt-in live probe
`codemode_inner_calls_are_hooked_individually_when_enabled` fails if that stops
being true. See `docs/decisions/opencode-hooks.md`.

The v1 `server` export is intentionally absent; v2.3.2 rejects it and v1.14.48
is not available to verify a dual-shape module. A real-CLI probe runs from
`tests/opencode_plugin_v2.rs` when `EXOMONAD_OPENCODE_V2_PROBE=1` and skips
otherwise.

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
| `SpawnLeafOptions` | Options for spawning the configured leaf agent |

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
| `HasSessionMemory` | `&SessionMemoryService` |
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

### Sink destinations and the worktree lifecycle lock

`services/sink_paths.rs` is the only place a telemetry, logging, ledger, or health sink decides where to write. `resolve_sink` is `async` and returns the candidate path only when it is a live Git worktree registered to the same repository (same common git dir, and the directory is a linked-worktree root); otherwise the project-owned directory is returned. Resolution happens at the write boundary, never at enqueue time, so a planned worktree that appeared or vanished in between can never be materialized by a sink write. The inbox `project_dir` carries the project root so a nested worktree can always fall back to a directory it owns.

`services/worktree_lifecycle.rs` holds the project-scoped advisory lock at `.exo/worktree-lifecycle.lock`, taken with `flock(2)` like the sink health, event log, and ledger writers:

| Region | Mode | Holder |
|---------|------|--------|
| Sink verify -> write | shared | `resolve_sink`, `run_inbox_consumer`, the injection closure, the duplicate-cache append |
| Worktree create/attach/reuse decision | exclusive | `AgentControlService::acquire_worktree_lifecycle` in `spawn.rs` |
| Residue classify -> quarantine rename | exclusive | `cleanup_unregistered_worktree_residue`, awaited by the spawn preflight |

Exclusive and shared are the same lock, so a cleanup pass drains in-flight sink writers before it classifies, and a leaf worktree cannot be created between a cleanup classification and its rename. Acquisition is bounded and every caller fails closed:

- A sink that cannot take the shared lock writes to the project-owned directory and never to a candidate worktree path (`tracing::warn!`).
- A cleanup pass that cannot take the exclusive lock skips the pass and leaves residue untouched (`tracing::warn!`).
- A spawn that cannot take the exclusive lock returns an error instead of creating, attaching, or reusing a worktree.

Every attempt is non-blocking, so a contended acquisition spends its whole timeout waiting. That wait must never park a runtime worker, so `LifecycleGuard::try_acquire` is async and yields with `tokio::time::sleep` between attempts. Every caller uses it — `resolve_sink`, `AgentControlService::acquire_worktree_lifecycle`, and `cleanup_unregistered_worktree_residue` — and there is no synchronous variant. The private `LifecycleAcquisition` is the single acquisition implementation, so the mode, the deadline, and the meaning of an error are decided in one place.

The project-owned directory needs no lock: no lifecycle decision creates, removes, or quarantines it. A sink destination must stay in scope for the whole write, because dropping it releases the lock before the bytes land.

`cleanup_unregistered_worktree_residue` is awaited by the spawn preflight in `spawn.rs`, so a preflight that waits out `DECISION_TIMEOUT` for the lock cannot park a runtime worker. There is one classification and quarantine implementation behind it.

Residue cleanup quarantines a directory only when Git's worktree registry does not know it, no `.exo/agents/*/identity.json` claims it, and it contains nothing but known sink artifacts. Registered, dirty, identified, and ambiguous directories are refused, and `cleanup_unregistered_worktree_residue` returns `Result`: it is not a best-effort void. Each quarantined directory is preserved by rename into `.exo/worktrees-residue/`, and its manifest entry is written to a temporary file, fsynced, renamed over `manifest.jsonl`, and followed by an fsync of the quarantine directory, all inside the exclusive lock. A manifest failure leaves the quarantined directory in place and is surfaced to the caller, which logs it and continues. An existing manifest is read with `NotFound` as the only empty case: a manifest that cannot be read for any other reason keeps its bytes and stops the append, because rewriting it from an empty buffer would silently erase every prior quarantine record.

### Deterministic leaf branch attachment

A worktree-per-agent leaf always works on its deterministic birth branch, so
provisioning never invents a branch name. `provision_leaf_worktree` in
`spawn.rs` owns the whole decision, and cleanup is armed before its first
fallible creation:

| Leaf action | Condition | Proof required |
|-------------|-----------|----------------|
| Attach | the deterministic branch exists | `verify_attachable_branch` |
| Create from revision | no branch, an expected head was supplied | none beyond the revision |
| Create from base | no branch, no expected head | none beyond the base |

Both decision inputs are read by one function, `read_leaf_branch_state`, and the
order inside it is the contract: `ensure_branch_fetched` first, then
`branch_exists`. The fetch materializes the local branch from the remote
tracking ref when it is absent, so a branch that lives only on the remote reads
as *absent* if existence is inspected first — that misclassifies a preserved
branch as a create and lets it succeed only through the race recovery below,
instead of attaching it. `read_leaf_branch_state` is the single place this order
is expressed, and the caller passes its result straight into `LeafProvisioning`.

`verify_attachable_branch` runs only on that freshly fetched evidence, and it
classifies the branch as follows.

- Checked out at the deterministic leaf path: verified and reused. The
  provisioning result carries no rollback guard, so a later failure can never
  remove a worktree this attempt did not create.
- Checked out at any other live path, or registered at a path Git cannot
  resolve: refused with `worktree.branch_ownership_conflict`, naming both the
  registered owner and the deterministic leaf path.
- Held by nothing: the local head must be proven. Freshly observed remote
  evidence must not be behind or diverged. With an absent or uninspectable
  remote, at least one authoritative head is required; with none, attachment
  fails closed. Durable identity proves ownership and the deterministic branch,
  never a commit.
- A recorded head (`recorded_branch_head`) is read from
  `.exo/published-heads.json` for the latest **ledger-owned** publication this
  agent owns on that branch, otherwise from the owner `invocation.json` when it
  records the same branch. Because a publication head may be older than the
  work, it is proven by `head_coverage` ancestry — the local head must equal it
  or descend from it — so unique unpushed commits survive the reattach.

  Recorded-head evidence is deliberately narrow, and each exclusion is a
  fail-closed one:

  | Excluded | Why |
  |----------|-----|
  | A migrated `Legacy` publication | never verified at its filing boundary, so it cannot prove a commit head — the same rule the watcher applies to publication ownership |
  | A publication filed by another agent | it is not this branch's owner's record |
  | A head recorded against another branch | a SHA is only evidence for the branch it was recorded against |
  | Any head at all, when the publication registry is unreadable | the registry is authoritative, so a possibly stale `invocation.json` head must not substitute for it; the unreadable case ends the search instead of falling through |
  | An `invocation.json` that cannot be parsed | `read_invocation_conservatively` treats a malformed record as no record |

Every refusal is a typed `worktree.branch_ownership_conflict` whose message
names the deterministic branch, the missing or conflicting evidence, and the
operator action, because the branch is deterministic and no alternative slug
exists.

### Provisioning events and the machine codes a caller classifies

Provisioning records its own decisions in the ledger, because a caller that must
decide whether to retry cannot see the in-process outcome:

| Event | Written when | Carries |
|-------|--------------|---------|
| `agent.attach_decided` | once per provisioning, before the first fallible creation | `action` (`attach` / `create_from_revision` / `create_from_base`), `branch_exists`, `start_point`, `branch`, `worktree_path` |
| `agent.attach_completed` | once per provisioning that reached an outcome | the same `action` plus `created`, so a reuse is never reported as a creation |
| `agent.branch_ownership_conflict` | once per refused provisioning whose typed code is `worktree.branch_ownership_conflict` | `machine_code`, `branch`, `worktree_path` |

`provision_leaf_worktree` wraps the whole run, so a refusal emits the decision
and, when it is an ownership conflict, the conflict — and no completion. The
recorded events name the leaf they are about, so they are attributable without
a separate correlation table.

Every resource-creation refusal a controller may need to classify carries a
stable code, read from the typed `EffectError` variant and never recovered from
prose. `agent.spawn_failed` in `handlers/agent.rs` writes that `code` as its own
field beside the operator-facing `error` string; an untyped error records a null
`code` rather than a guess, and a `EffectError::Timeout` records
`dispatch.transport_timeout`. The codes a caller classifies are
`worktree.branch_exists` (a creation race that an attach recovers),
`worktree.lifecycle_lock_timeout` (the exclusive lock was busy, so the
create/attach decision never ran), `worktree.branch_ownership_conflict`
(terminal), and `worktree.pr_context_unavailable` (terminal). A lifecycle-lock
timeout is a transient refusal and carries its code from
`acquire_worktree_lifecycle`, so a caller does not have to infer it.

`dispatch.transport_timeout` is deliberately **not** a retryable code. The
`SPAWN_TIMEOUT` it derives from wraps the entire `spawn_leaf_subtree`, so it can
fire after the worktree, the identity record, and the tmux window already exist.
Its outcome is unproven, and a re-drive could launch a second actor on the same
deterministic branch; the caller holds the intent and waits for evidence
instead. Widening the typed code space therefore does not widen the retryable
set: retryability is a reviewed statement that a refusal proves no side effect
happened.

A refusal that cannot be recorded leaves the controller with nothing to
classify. `append_spawn_failed` therefore logs at `error!` with the child, the
intent, and the code both when there is no event log and when the append fails.
The refusal itself stays authoritative through the tool response; the ledger row
is the durable evidence, not the decision.

### The head set is proven on reuse too, not only on attach

`verify_leaf_head_evidence` is the single implementation, and it runs on both
decisions that admit a deterministic branch:

| Decision | Where it runs | What it proves |
|----------|---------------|----------------|
| Attach a preserved branch | `verify_attachable_branch`, after the fresh fetch | the whole set, plus at least one head when the remote is absent or uninspectable |
| Reuse the worktree that already holds the branch | `verify_existing_leaf_worktree`, at the preflight, the live-route return, and the reuse decision inside the exclusive lifecycle region | the whole set |

So an ordinary re-spawn of a live worktree — not only a resume — fails closed
when the branch no longer contains a head its owner recorded. Presence is not
required on reuse, only proof: a worktree with no recorded head is still
reusable, and a reuse never consults the remote, so it needs no fetch. The
reuse decision is the last check before the launch and arms no
`WorktreeRollback`, so a refusal there leaves the existing worktree registered
exactly as it was.

Two recorded-head refusals are distinct facts, and both apply to attach and
reuse alike:

| `head_coverage` | Meaning | Refusal |
|-----------------|---------|---------|
| `Covers` | the local head is the recorded commit or descends from it | accepted, so unique unpushed commits survive |
| `Diverged` | both commits are present, neither is an ancestor of the other | the branch was rewritten off the recorded work |
| `UnknownCommit` | this repository cannot resolve the recorded commit at all | the branch was rewritten, or the recorded commit was reclaimed by garbage collection |

`UnknownCommit` is **intended behavior**, not a gap: a recorded head nobody can
produce again means the history is gone, and continuing on that branch would
hand the leaf work descending from nothing its owner ever published. It carries
its own message naming the branch, the recorded head, its evidence source, the
observed head, both ways a head goes missing, and the operator action — so the
operator sees "fetch or restore this commit, or re-dispatch from the current
head" rather than a generic ownership conflict.

### Resume lineage and restored PR context

A resume takes the same verified attach path as a first spawn — there is no
second attach path, and `start_point` is only expected-head evidence. What makes
a resume safe is the head evidence it carries, and that evidence is a set
(`LeafHeadEvidence`), not a single value:

| Evidence | Source | Proof |
|----------|--------|-------|
| `expected` | the head the host resolved for the resume | exact equality |
| `prior_publication` | `recorded_branch_head` | ancestry |
| `resume_lineage` | `resolve_resume_lineage_head` | ancestry |

Every entry present is proven, not just the strongest one, on both the attach and
the reuse path, so a resume whose lineage head the branch no longer contains is
refused even when its expected head matches. Ancestry rather than equality for
the two recorded heads is what lets unique unpushed commits survive the
reattach. A refusal names the branch, the expected head, and the observed head.

`resolve_resume_lineage_head` runs before any worktree decision, including the
idempotent return, because a resume is authorized against exactly one prior
generation. It fails closed when the durable `invocation.json` is unreadable,
missing, or no longer the invocation the lineage names, and it reads a head only
from a record naming the deterministic branch. `Ok(None)` means the lineage
verified and recorded no head for this branch; only another authoritative record
can then prove it.

PR context is resolved, never created, and the first-spawn and expected-agent
resume paths share one composition function (`leaf_task`), so a re-spawn after
worktree loss and a `resume_pr` invocation restore the same context instead of
the resumed leaf starting blind. The head is read from git after the
provisioning decision and is what the PR is matched against, because a branch
name never identifies a PR. Only an open, unmerged PR on the deterministic
branch whose reported head SHA equals that verified head contributes context
(`pr_carries_verified_head`); a PR whose head has moved, a closed or merged one,
a PR the forge reports no head for, and no PR at all all yield none. Review
feedback is best effort — the PR identity is the context, and a failed review
listing must not cost the leaf its PR. A standalone repo owns no host PR, has no
verified head to match, and therefore gets no PR context.

**A PR lookup that could not be answered is not "no PR exists".**
`resolve_existing_pull_request` returns a typed `PullRequestContext` that keeps
`NoQualifyingPr` (the forge answered, nothing qualifies) apart from
`LookupFailed` (the forge could not be asked, carrying the reason), and
`leaf_task` decides between them per path:

| Outcome | Expected-agent resume | First spawn |
|---------|-----------------------|-------------|
| `Resolved` | context appended | context appended |
| `NoQualifyingPr` | no context | no context |
| `LookupFailed` | refused with `worktree.pr_context_unavailable` | `warn!` with branch and error, then no context |

The resume must refuse: a leaf that cannot see the PR it owns receives a task
that says nothing about it, which is how a second PR gets filed, and a forge
outage must not look like a branch with no PR. The refusal propagates with `?`
at the call site, before any tmux launch and while the `WorktreeRollback` guard
is armed, so the worktree this spawn created is removed. A first spawn owns no
pull request and cannot be blind to one, so it proceeds with the loss recorded —
a documented, logged first-spawn behavior rather than a silent fallback.

`LookupFailed` covers every way the question could not be asked: an
unresolvable repository, a forge API error, **and an unconfigured forge client**.
A missing client is a misconfiguration, not an answer — the query was never made
— so a resume refuses it rather than starting blind, and a first spawn records
it. Only an *unverified head* (a standalone repo, which owns no host pull
request) is `NoQualifyingPr` before any query happens. Review and inline-comment
listing failures stay non-fatal and each `warn!`s with the PR number and the
error, so no failure in this path is silent.

The quarantine manifest records `source_kind: "unregistered_worktree_residue"` and `forensic_only: true` for every entry. Quarantined `.exo/ledger/segments` and `.exo/events` describe a worktree that no longer exists, so `exomonad logs import` excludes any source under `.exo/worktrees-residue/` by default and reports the count as `excluded_quarantined_sources`. `exomonad logs import --include-quarantined` is the explicit operator opt-in. The exclusion is a path predicate, so it holds even when a manifest entry is missing after a crash.

### Session memory ledger

`SessionMemoryService` is the append-only SQLite ledger for durable semantic
session facts. It lives at `.exo/memory.db`, uses the same `Mutex<Connection>`
and migration lifecycle as `InboxStore`, and exposes typed append, filtered list,
and latest-by-kind reads. `MemoryKind` is a closed enum; unknown stored values
fail during decoding, while append validation rejects invalid summaries, detail
sizes, importance values, and predecessor references. There are no update or
delete APIs; tests may use the test-only `clear_all` helper.

### Continuation adapters

`services/continuation/adapters.rs` gathers typed state for the continuation
brief. `ChainlinkAdapter` shells out through `chainlink --json` with
`CHAINLINK_DB` set to the current project's `.chainlink/issues.db`; it never
opens Chainlink's database directly. `InboxAdapter` reads unread counts and
last-check timestamps without calling notification-draining APIs.

`AgentAdapter` obtains the discovered agent list from `AgentControlService` and
uses the shared `resolve_agent_liveness` predicate from `AgentHandler`, so
tmux presence, retirement markers, and delivery-target requirements stay in one
place. `ForgejoAdapter` returns an explicit unavailable section when Forgejo is
not configured or a source call fails. Every adapter uses `SectionData`, whose
only outcomes are `Available` and `Unavailable { reason }`; an empty successful
source is still `Available(vec![])`.

`services/continuation/renderer.rs` is the pure deterministic markdown renderer
for root/TL and child continuation briefs. It keeps the fixed section order,
sorts every source collection explicitly, renders unavailable sources with their
reasons, scopes child feedback by agent and issue, and enforces the 4096-byte
cap by dropping the lowest-importance, oldest ledger rows first. `render_tl`
and `render_child` do not perform I/O or model calls; Wave 3 owns their
SessionStart and task-injection call sites.

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

## Runtime protocol tool coverage

The OpenCode and Codex runtime protocol text is embedded in
`services/agent_control/spawn.rs`, but its tool coverage is verified against the
compiled Haskell role configuration. The WASM integration test calls
`handle_list_tools` for `dev`, `worker`, and `reviewer` and checks every returned
tool name against each corresponding runtime protocol. Do not add a second Rust
tool manifest: update the Haskell role configuration and the runtime protocol
together, then run `just test-cargo-all` after rebuilding WASM.

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
      "head_sha": "abc123",
      "model": "gpt-5.6-luna",
      "effort": "xhigh"
    }

runtime, trigger, routing, started_at, and status are required. ended_at,
exit_code, pr_number, head_sha, model, and effort are optional. model and
effort describe the current process attempt. identity.json holds the owner's
current route: a controller-routed spawn and a resume of a named owner both
write the route they run with, so a configured route change updates it. The
prior route survives in the `configured_runtime_route_transition` row and the
prior invocation's lifecycle rows; a re-spawn with no routed or named owner
keeps the identity record's existing model and effort. See
[docs/guides/changing-routes-mid-run.md](../../docs/guides/changing-routes-mid-run.md).

Every invocation also records its launch provenance. `build` is the build
identity the writing server injected through
`AgentControlService::with_build_identity` (the `exomonad` crate passes
`env!("EXOMONAD_BUILD_GIT_COMMIT")`). `exomonad-core` on its own has no such
package-private env, so its fallback is the explicit string `unknown`, never an
invented crate-version stamp. `launch_command` is the resolved agent command,
argv, and flags with every environment value and every credential flag value
redacted to `<redacted>`; `launch_command_source` says whether that text was the
`actual` captured command or a `reconstructed` rendering from the resolved
route, so evidence is never presented as an argv that did not launch. A launch
command is never persisted without its source marker. `config_sources` records
the `RouteValueSource` for each resolved route field separately (`harness`,
`model`, `effort`); one field's provenance never stands in for another's. Legacy
records without these fields decode with an empty build and absent provenance;
they are never rewritten merely by reading. Starting resume_pr or a
SHA-scoped reviewer replaces only this record and records the exact current
tmux window/pane. A reviewer respawned under a changed harness is a different
reviewer internal identity and writes its own agent directory, so the prior
route's record is left untouched; a same-harness reviewer attempt replaces its
own record and the prior invocation ID survives in durable lifecycle evidence.
Finishing must supply the invocation ID (or matching
routing) and cannot change a newer record. Missing or malformed records are
legacy metadata: status and cleanup paths log the condition and avoid
destructive reconciliation. A finished invocation is dormant ownership; an
open PR, review, or CI work unit remains pending and does not dispose the
issue-owned worktree. Spawned-child memory records and structured
agent.spawned/agent.resumed events carry the same bounded model, effort,
topology, branch, and spawn-type provenance. Prompts and secrets are never
copied into these provenance fields.

Measured per-invocation usage is additive on the same record and on the
`agent.invocation.finished` payload as `usage: { input_tokens, output_tokens,
total_tokens }`. It is written only from an authoritative source the harness
produced for this exact invocation. A one-shot Claude agent is launched with
`claude -p --output-format json` and its stdout is captured into the
invocation's own `.exo/agents/<agent>/usage.json`; the finish path reads that
file under a 1 MiB bound and a 500 ms timeout, sums `input_tokens` with
`cache_creation_input_tokens` and `cache_read_input_tokens` plus
`output_tokens`, consumes the file, and records the result. The one-shot pane
therefore carries no prose on stdout: the envelope is the invocation's usage
evidence and the workflow result remains the PR/review/CI outcome. A missing,
unreadable, partial, zero, oversized, or ambiguous source writes no `usage` and
logs why; it is never zero and never an estimate. Codex, OpenCode, Shoal,
`Process`, and interactive Claude companions have no established
per-invocation usage source, so their records and finished rows stay absent.
Old records and old finished rows without `usage` decode unchanged.

### Configured-route transitions at resume

Resume/continuation treats the currently configured harness/model/effort as the
route for the successor generation. The historical invocation's route is stored,
shown, and diffed as evidence, never used as a constraint. When the configured
route differs, `resume_pr` and `resume_blocked_leaf` record one
`configured_runtime_route_transition` row through
`configured_route_transition_id`/`record_configured_route_transition`, carrying
the operation, scope, prior invocation ID and generation, from/to harness, model,
and effort, and the worktree fingerprint. The successor's invocation record then
carries the new route. A change of model, effort, or harness is a routing
transition; it is never reported as an owner, scope, or worktree conflict. Owner,
scope, worktree, predecessor-invocation, epoch, and process-identity checks are
unchanged.

A blocked-leaf resume is multi-step and mutating, so it is cancellation-safe as
one unit. `resume_blocked_leaf` runs the durable work on its own `tokio::spawn`
task, so dropping the HTTP request future when a controller client disconnects
cannot stop it after the route transition. Before the first durable route
record it writes `.exo/agents/<owner>/blocked_resume_journal.json` (`pending`);
every exit path then moves that journal to `launched` or `failed`. A failure
after the transition writes one `agent.spawn_failed` row carrying
`operation: "resume_blocked_leaf"`, the intent, the transition id, and the typed
code. The server's recovery loop calls `reconcile_blocked_resume_journals` on
its first tick and every tick after: a pending journal left by a dead server is
re-driven through the same idempotent resume, so an already-launched successor
is adopted and a launch that never happened becomes a classified terminal
failure. A durable route transition with neither a launch row nor a terminal
failure row therefore cannot be left behind.

Every tool request has exactly one `tool.called` completion event in the
canonical ledger, emitted by the detached `execute_tool_call` task and naming
`dispatch_intent_id` (from `dispatch_intent_id`, `intent_id`, or
`role_route.dispatch_intent_id`). The event is written on success, on error, and
on an aborted task (`success=false`, `cancelled=true`); a dropped HTTP
connection does not suppress it.

### Repeated dispatch declarations under one logical resume

`handlers/dispatch_authorization.rs` resolves the `tl.dispatch_intended`
declaration for a caller's full authorization tuple (caller, scope, role,
harness, agent type, model, effort, configuration/policy/manifest/route
fingerprints). It is used by `resume_blocked_leaf`, which passes the owner
invocation as durable no-effect evidence. A reauthorized retry keeps the stable
`blocked-resume:` intent id, so the historical declaration and the new one share
an id. The resolver groups historical declarations by route tuple plus their
observation fields (`started_at`, `attempt`, `dispatch_generation`,
`controller_epoch`) and binds each distinct observation to its own host-recorded
`tool.called` row for `resume_blocked_leaf` carrying that observation's exact
route tuple, `success=false`, the exact configuration-not-accepted error, no
result, and a sequence inside that observation's window. Equivalent repeated
observations may share one refusal; one refusal after two distinct observations
cannot release both. A missing or malformed observation field is never
equivalent evidence: observations only collapse when every one of those fields
is present and equal on both sides. A missing launch row, a non-live
predecessor, or a generic failure is not enough. The caller-supplied
`human_approved` flag is not proof: the authoritative controller checkpoint must
show exactly one current recovery gate for the slice, still `approved`, owned by
the same `run_id` and `controller_epoch` that emitted the selected declaration,
and it must own the declared scope. Any pending/reopened gate, rejected gate,
additional same-slice recovery gate, stale epoch, foreign run or missing
checkpoint holds before launch. `tl.gate_answered` rows are corroboration only:
the controller persists the checkpoint before its best-effort emission, so a
real approved checkpoint with no matching event (the #1511 shape) is accepted
without fabricating history, while a ledger rejection that contradicts the
checkpoint fails closed. Once a launch consumes and clears the gate, an exact
repeat or response-loss adoption reuses the host's durable
`tl.dispatch_authorization_resolved` row for the same intent, selected
declaration, superseded declarations and refusal proof; a pending or rejected
current gate still blocks, and a changed declaration set must prove a fresh
current gate.
It then proves from the ledger and owner registry that the older declaration did
not launch (the predecessor is still current and no `agent.spawned` row exists
for the intent), or that the current invocation is the single linked successor
whose stored `role_route` is this authorization. Equivalent repeated
observations collapse; missing, unrelated, wrong-declaration or contradictory
refusal proof, conflicting observations, later competing declarations,
wrong-caller declarations, live or unknown owner outcomes, and prior launch rows
hold before launch. Diagnostics
name the ledger sequences and safe identity fields. One idempotent
`tl.dispatch_authorization_resolved` audit row records an accepted supersession;
historical events and journals are only appended to, never rewritten. The wire
contract is unchanged: no proto or Haskell type changes, because the full route
tuple and the stable intent id already cross the boundary. Tests live in the
module; the compiled acceptance is `just e2e-dispatch-collision`, and
`just e2e-dispatch-interruptions` runs its four interruption boundaries in
separate fixtures. Both pause the real `serve` process at the authorization,
request, launch and adoption boundaries through the inert-unless-configured
`src/e2e_interrupt.rs` hook, kill the controller and host at that window, and
prove each restart reconciles exactly once. The marker is checked for its exact
boundary, dispatch scope, paused-host pid and live `exomonad serve` command.

A response-loss adoption is read-only on the ledger: it re-validates the
already-launched successor instead of appending a second `agent.spawned`. The
adoption path therefore records one durable, idempotent
`.exo/agents/<owner>/adoption.json` receipt (`invocation_id`, `intent_id`,
`prior_invocation_id`, `adopted_at`) before the boundary, so a restart and the
compiled acceptance can prove adoption crossed durably before the response. A
repeat adoption of the same invocation and intent keeps the first receipt, and
a new invocation generation retires it. A different dispatch intent adopting
the same successor is not an error: it is appended to the receipt's
`subsequent_intents` list with its own `adopted_at`, so the first intent's flat
fields and bytes are preserved and every intent stays readable in adoption
order. A receipt owned by another `invocation_id` remains an ownership
conflict. The compiled acceptance additionally
requires exactly one launch row older than the marker and an `intended`
controller journal action with no result, which proves the response was not yet
returned.

The accepted configuration itself is one canonical `sha256:v2:` identity over
each role's effective harness/model/effort, computed by the single
`exomonad::config::Config::routing_configuration_fingerprint`. Source provenance
is a separate audited summary (`routing_configuration_provenance`, exported
through `/health`), never part of the identity. `AgentControlService` stores the
identity it accepted at server start; `AgentHandler::accepted_role_route` still
compares every caller route byte-for-byte with that identity, then revalidates
role/scope, top-level route agreement, dispatch-intent history, policy, manifest,
and durable intent. An absent identity, an old `sha256:v1:` value, a forged or
cross-scope identity, and a changed value disguised as the same configuration
all fail before launch. `init` compares its own resolved identity with the
server's `/health` identity before releasing a controller and holds early when
they differ.

A cross-runtime change is configured when it comes from `config.toml`'s resolved
role route or from the role's `allow` list in `.exo/harness_policy.toml`; both
sources are consulted by `crate::services::harness_policy` and are pinned to the
Python selector by `tests/fixtures/harness_policy_agreement.{toml,json}`. A
configured cross-runtime change therefore does not require
`EXOMONAD_ALLOW_HARNESS_SWITCH`. An explicit request outside both sources still
emits `[STUCK: harness-switch]`/`agent.stuck` and is approved only by that flag.
`exomonad init` additionally requires the resolved worker and reviewer routes
to agree with the same role's allow list before it starts a controller; the
guard still accepts either source for an already-running dispatch. The host
check in `handlers/agent.rs` is unchanged: `accepted_role_route` still compares
the caller's `configuration_fingerprint` byte-for-byte with the identity the
server accepted and then validates policy, scope, manifest, role and the
durable `tl.dispatch_intended` event. init cannot satisfy that check by
asserting a value; it must read the server's `/health` identity, and its
proof-carrying preflight makes releasing a controller without that read a
compile error. See
[docs/guides/changing-routes-mid-run.md](../../../docs/guides/changing-routes-mid-run.md).

The `resume_pr` response's `InvocationHandoff` also carries the launched
`runtime`, `model`, and `effort` (proto fields 3, 11, and 12). The controller
uses them to meter the continuation on the exact route this host launched
instead of inferring it from the Python allowlist ordering. They are launch
evidence only: the handoff never widens the allowlist and does not replace the
accepted role-route receipt used by blocked-leaf recovery.

The reviewer is the same rule with a role-specific route. Reviewer spawns read
`configured_runtime_route("reviewer")` (`[reviewer]` in `config.toml`); they do
not pass through the worker harness-switch guard because there is no caller
harness argument to guard. A configured reviewer harness/model/effort change
therefore applies at the next one-shot reviewer spawn. Because the reviewer
identity embeds the harness, a harness change produces a new internal name and
birth branch for the same PR/head and rewrites the PR body's reviewer
assignment; the old route's invocation record is not rewritten. Two `Review`
invocations can then match the same PR/head, so `resolve_reviewer_invocation`
selects one with a single pure rule over all candidates (live first, then most
recently started, then highest generation, then smallest agent id) instead of
returning whichever directory the filesystem listed first. The watcher's
`resolve_review_author` and the handler's `resolve_durable_review_author` both
use that rule, so Forgejo review attribution after a harness change follows the
replacement reviewer. The watcher's dedup/reconciliation is keyed by
PR/head/attempt and is unaffected by the route change. No reviewer harness/model
escalation exists: the selector's `escalate_after_attempts` is never invoked for
the reviewer role. The `roles.reviewer.allow` list is part of the shared
configured-route definition and should be kept in step with `[reviewer]`; see
[docs/guides/changing-routes-mid-run.md](../../../docs/guides/changing-routes-mid-run.md#reviewer).

### Ordered sub-TL invocation provisioning

`provision_ordered_sub_tl` validates the plan-owned identity, branch, worktree,
invocation history, and publication evidence before resolving exactly one
current `TL` delivery target. A missing or ambiguous target fails closed; the
service never guesses a pane or creates a second routing writer. In
`interactive` mode, first provisioning uses `spawn`; an exact matching live
controller is reused without changing its invocation; a dormant terminal
controller with no publication uses the explicit `controller_restart` trigger
and records the prior invocation as recovery lineage; and published work uses
`resume_pr` only when exact PR, head, owner, branch, slice, and invocation
succession are corroborated. Existing publication evidence cannot fall back
to a controller restart. Conflicting ownership, a live invocation bound to
another route, or an unresolved prior outcome is held without replacement.
The confirmed invocation ID is returned for the parent checkpoint to bind.

A saved `Running` status is not proof of live execution. When the recorded
delivery target is absent, or when a fresh tmux server recycles the same numeric
`@window`/`%pane` IDs, `reconcile_controller_binding` compares the committed
controller ownership with the exact pending startup target recorded by init:
controller epoch, tmux server PID/start ticks, pane PID/start ticks, root run
scope, and the optional startup fence. A proven-ended historical lifetime is
terminalized under the invocation mutation lock with its routing target
preserved as evidence, and one `controller_restart` successor is planned.
`ControllerRestart` successors record the controller epoch that created them so
a retry inside the same startup window reuses the successor instead of
reconciling it. The epoch comes from the exact current controller target; an
absent target records no epoch and logs why, while an unreadable, malformed, or
incomplete target fails provisioning closed with `LifecycleBlocked`, creates no
successor, and leaves the prior invocation untouched. Silently omitting the
epoch for an unreadable target would make a same-window retry reconcile the
successor as a stale lifetime. A verified live matching owner is reused; a
wrong owner, a partial ownership record, a target from another root scope, or
unavailable inspection is held with a distinct diagnostic and no replacement.
Absent delivery alone never authorizes duplicating a separately running worker.

Shared TL delivery is explicit. Several ledger-owned sibling controllers may
share the exact root controller pane; a single window/pane ownership tag does
not make one sibling the exclusive owner of the others. Provisioning accepts a
scoped parent or a declared sibling only when both identities belong to the
accepted root declaration and the delivery target is exactly the recorded
controller target. A pending startup target without its `controller-startup.json`
fence is a partial handoff and is never selected. Shutdown therefore
distinguishes four outcomes: wrong owner (conflict, no kill), absent target
(proven ended, scoped interruption), unknown inspection (unverified hold), and
partial shutdown (bounded `blocked` intent with the remaining blocker).

A delayed exit monitor from a superseded predecessor carries its invocation ID
as well as its routing snapshot into
`finish_invocation_and_tombstone_for_invocation`; a recycled numeric target can
therefore never let the predecessor retire the successor generation.
`finish_invocation_after_superseded_target` also converges when an exit monitor
already retired the exact observed generation: a terminal record with the same
invocation ID and generation is the same crash boundary, not a conflicting
successor, and it must not block reprovisioning with `LifecycleBlocked`.

### Scope handoff commit leases

`prepare_scope_handoff` holds the shared-refs and index mutation leases across
inspection, staging, and commit. A different owner still fails fast after the
two-second conflict window. When the durable lease record names the same
operation identity, the second caller is a replay of one handoff and waits for
the in-flight owner (bounded by the Git command timeout) instead of returning a
spurious `Git mutation readiness is unavailable`. Once the owner releases the
leases, the replay adopts the existing handoff commit, so concurrent replays
create exactly one commit.

During `init --continue`, ordered sub-TL recovery uses the same provisioning
decision as live execution. The accepted plan declaration must match the
durable child identity; an existing invocation is classified as live reuse,
dormant controller restart, or publication-backed PR resume from durable
evidence. After the current `TL` delivery target and server are ready, init
calls `/agents/tl/{parent}/provision-sub-tl` and verifies the confirmed
invocation and routing records before the controller starts. Other agents
retain the publication-backed continuation classification.

### Harness selection guardrail

A coding spawn inherits the configured worker harness. Retrying or calling
resume_pr stays on that harness and the existing owner identity. The configured
harness is the resolved role route from `config.toml`, extended by the role's
`allow` list in `.exo/harness_policy.toml`; a harness named by either source is
a configured route and needs no approval. An explicit cross-harness request
outside both sources is rejected with agent.stuck guidance unless the human has
set EXOMONAD_ALLOW_HARNESS_SWITCH=1; approved switches are audited with from/to
harness, reason, policy source, model, and effort. `AgentControlService::new`
reads the allowlist once when `exomonad serve` builds the service;
`configured_harness_for_role` consults that snapshot, so editing the file while
the server runs has no effect until a restart. See
[docs/guides/changing-routes-mid-run.md](../../docs/guides/changing-routes-mid-run.md).
No-op, no-commit, or no-PR failure handoffs also emit bounded agent.stuck
guidance for the parent to steer or escalate.

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

### Verified cleanup provenance recovery

Cleanup may recover a missing agent identity only for a plain, unregistered
directory directly below .exo/worktrees that contains only nested .exo runtime
state. The append-only ledger, published-head registry, local and
configured-remote refs, and exactly one matching merged or closed-unmerged PR
must agree on the agent, branch, base, and head SHA. The Forgejo body must also
carry exact `Authoring-Agent` and `Birth-Branch` metadata, and a finished
invocation record must bind the publication. Recovery is cleanup-only evidence
and never re-registers the resolver identity. A real Git worktree, dirty
residual contents, missing or conflicting evidence, a protected branch, or an
ambiguous PR remains refused. The configured server tmux session is queried
when recovery is planned and immediately before mutation; a matching window or
an unavailable session keeps the candidate from being cleaned. Recovered
cleanup records every evidence source in its plan and durable receipt and uses
the same liveness, branch, and exact-lease checks as ordinary cleanup.
Remote deletion without complete verified branch evidence is an explicit
refusal in both dry-run and apply receipts. Resumed receipts retain a
`resumed_cleanup` audit action, and retry options re-normalize local branch
actions so a later `preserve_unique_commits` request cannot delete the last
local ref.

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

**Reviewer lifecycle:** The TL's `spawn_reviewer` effect starts reviewers for the reviewed head. The Forgejo watcher observes verdicts and CI; the host orphan reconciler disposes reviewer resources after terminal invocation. The watcher does not decide when to spawn, retry, or dispose reviewers.

**Reviewer completion routing:** Reviewer agents submit Forgejo reviews directly and then exit. They do not call `notify_parent`; the watcher observes Forgejo verdicts and routes review events to the live PR-owning dev leaf. For `review_received`, `review_commented`, and changes-requested review events, it dispatches the same event to the owning parent TL, whose `tlPrReviewHandler` constructs the TL-facing review guidance. The watcher never injects review events back into the exited reviewer pane or composes `[REPAIR HANDOFF]` messages. Approved, merge-ready, `ci_blocked`, `stuck`, and timeout facts remain observable for the TL/controller to adjudicate. When another review round is needed, the TL/controller explicitly spawns a fresh reviewer for the next head or review round; the host orphan reconciler cleans up terminal reviewer resources.

### Event Dispatch Flow

1. Poller detects state change (new comments, approval, timeout, merge)
2. Calls `call_handle_event()` → WASM `handle_event` FFI
3. Haskell `dispatchEvent` routes to role's `EventHandlerConfig` handler
4. Handler returns `EventAction` (InjectMessage, NotifyParentAction, NoAction)
5. Poller acts on the action via `handle_event_action()`

### Stale Notification Guard

Once the parent has been notified (via `[PR READY]` approval or `[REVIEW TIMEOUT]`), `compute_pr_actions` suppresses all further events for that PR. Late Copilot reviews, CI status changes, and new commits are silently dropped — the timeout is already parked for controller adjudication, so any further notifications are stale and confusing.

### Merge Detection

When a tracked PR's branch disappears from the open PR list, it was merged/closed. The poller:
- Fires `sibling_merged` WASM event on sibling agents (same parent branch, open PRs) via `call_handle_event`
- Emits `agent.sibling_merged` OTel span event
- Removes the PRState from tracking

### Publication lookup integration coverage

`just test-wasm-integration` runs the WASM tool registry tests and generates a
temporary JSON result from the production `AgentHandler` reading an isolated
`.exo/published-heads.json` history. Forgejo PR observations are served by a
local mock server; publication selection and typed response construction run
in production code. The same generated tool envelope is passed to the Python
aggregate lookup consumer before the recipe succeeds. Never describe the
mocked `MockAgentHandler` path as coverage for publication-history selection.

## Related Documentation

- [Root CLAUDE.md](../../CLAUDE.md)
- [Handlers CLAUDE.md](src/handlers/CLAUDE.md)
- [Haskell WASM guest](../../haskell/wasm-guest/CLAUDE.md)
