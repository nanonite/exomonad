# Stable Teams with Dedicated Merge Queue Reviewer

## Context

Today, every PR notification (`[PR READY]`, `[FIXES PUSHED]`, `[REVIEW TIMEOUT]`) routes to the TL's Teams inbox. With 5+ parallel agents, the TL spends most of its time reacting to PR events instead of planning and spawning. The TL is Opus — every merge-review cycle burns expensive tokens on mechanical work.

**Goal:** Each Claude TL holds a stable team with a dedicated **merge-reviewer teammate** (Claude haiku) that receives PR notifications *instead of* the TL. The reviewer autonomously merges when conditions are met and notifies the TL only on success or failure.

## Architecture

```
TL (Opus) ── stable team ──┬── merge-reviewer (Haiku teammate)
                            │     receives: [PR READY], [FIXES PUSHED], [REVIEW TIMEOUT]
                            │     actions: calls merge_pr, notifies TL of results
                            │     persists: across wave boundaries
                            │
                            ├── wave 1 agents (spawned, file PRs, complete)
                            └── wave 2 agents (spawned after wave 1 merges)
```

The merge-reviewer is a **native Claude Code teammate** spawned via `Task` tool with `team_name` and `model: haiku`. It shares the TL's MCP connection, so it can call `merge_pr` directly. No synthetic members, no inbox watcher — real teammate, real inbox delivery.

## Key Design Decisions

### Message Routing Split

Two independent code paths already exist — route them to different targets:

| Source | Code path | Current target | New target |
|--------|-----------|----------------|------------|
| Agent calls `notify_parent` tool | `EventHandler::notify_parent()` | TL inbox | **TL inbox** (unchanged) |
| Future GitHub Actions poller `NotifyParent` action | `GitHubPoller::handle_event_action()` | TL inbox | **Reviewer inbox** (if registered) |

The split is natural: these are separate call sites. The current active implementation is the worktree event watcher; `github_poller.rs` is hibernated and should mirror watcher semantics until GitHub Actions integration is re-enabled. Poller `NotifyParent` actions are always PR-related (they come from PR review event handlers). Agent `notify_parent` calls are status/completion messages.

### Sub-Agent Identity

Sub-agents (like the merge-reviewer) use a **colon-separated identity** distinct from the dot-separated worktree hierarchy:

- Worktree children: `main.feature-a` (dot = branch hierarchy)
- Sub-agents: `main.feature-a:merge-reviewer` (colon = sub-agent of parent)

This keeps the worktree branch namespace clean. A nested TL's reviewer would be `main.foo.bar:merge-reviewer`. Rust representation: `AgentIdentity { birth_branch: String, sub_agent: Option<String> }` or similar structural type.

### Auto-Merge Behavior

The reviewer merges autonomously when conditions are met:
- `[PR READY]` (Copilot approved + CI green) → merge immediately
- `[REVIEW TIMEOUT]` (no review after timeout, CI green) → merge with `force=true`
- `[FIXES PUSHED]` (agent addressed Copilot comments, CI green) → merge

Reviewer notifies TL after each merge: "Merged PR #42 for agent-a. Build clean." On failure or conflict, escalates to TL.

### Fallback

If no reviewer is registered, poller falls back to existing behavior: PR events go to TL inbox. Graceful degradation, no breaking change.

## Open Questions

### Spawn Event Routing

When the TL spawns work agents (`fork_wave`/`spawn_gemini`), the reviewer needs to know about expected PRs. Undecided whether this should be:
- **Server-side**: New event type fired automatically when spawn completes, auto-delivered to reviewer inbox
- **TL-initiated**: TL explicitly messages reviewer after each wave ("Spawned wave 1: agent-a, agent-b, agent-c")
- **Both**: Server fires structural event, TL adds commentary

### Inbox Routing Mechanism

Need to experiment with Claude Code's on-disk inbox format before committing to an approach. Specifically: when a teammate is spawned via `Task` tool, what inbox files exist? How does `SendMessage` write to them? Can exomonad write to a teammate's inbox directly?

**Experiment needed:** Spawn a Claude haiku teammate, send it a message via `SendMessage`, inspect `~/.claude/teams/{name}/` on-disk state.

## Implementation Sketch

### 1. Extend `TeamInfo` with reviewer routing

**File**: `rust/claude-teams-bridge/src/registry.rs`

Add `merge_reviewer_inbox: Option<String>` to `TeamInfo`. Add `set_merge_reviewer` / `clear_merge_reviewer` methods to `TeamRegistry`.

### 2. New session effects

**Proto**: `proto/effects/session.proto`

```protobuf
message RegisterMergeReviewerRequest {
    string reviewer_inbox_name = 1;
}
message RegisterMergeReviewerResponse {
    bool success = 1;
}
```

**Rust**: `rust/exomonad-core/src/handlers/session.rs` — new dispatch arm.
**Haskell**: `haskell/wasm-guest/src/ExoMonad/Guest/Effects/Session.hs` — new effect constructor.

### 3. New MCP tool: `register_merge_reviewer`

**File**: New tool in `haskell/wasm-guest/src/ExoMonad/Guest/Tools/`

Takes `{ reviewer_name: String }`, calls `SessionRegisterMergeReviewer` effect. Available to `root` and `tl` roles.

### 4. Route poller `NotifyParent` to reviewer

**File**: `rust/exomonad-core/src/services/github_poller.rs` (future GitHub Actions path; currently hibernated)

In `handle_event_action` for `NotifyParent`: check `team_info.merge_reviewer_inbox`. If set, write to reviewer's inbox. If not, existing behavior.

### 5. Sub-agent identity type

**File**: `rust/exomonad-core/src/effects/mod.rs` (or new file)

Extend `EffectContext` or introduce `AgentIdentity` struct with `birth_branch` + `sub_agent: Option<String>`. Colon-separated serialization: `main.foo:reviewer`.

### 6. Documentation

Update `CLAUDE.md`: add `register_merge_reviewer` to tools table, add merge-reviewer to TL Praxis section, document stable team pattern.

## Files Touched

| File | Change |
|------|--------|
| `rust/claude-teams-bridge/src/registry.rs` | `merge_reviewer_inbox` field + setter methods |
| `proto/effects/session.proto` | Register/deregister merge reviewer messages |
| `rust/exomonad-proto/proto/effects/session.proto` | Same |
| `rust/exomonad-core/src/handlers/session.rs` | Handle new session effects |
| `rust/exomonad-core/src/services/github_poller.rs` | Route `NotifyParent` to reviewer |
| `haskell/wasm-guest/src/ExoMonad/Guest/Effects/Session.hs` | New effect constructors |
| `haskell/wasm-guest/src/ExoMonad/Guest/Tools/` | New tool |
| `.exo/roles/devswarm/RootRole.hs` | Register new tool |
| `.exo/roles/devswarm/TLRole.hs` | Register new tool |
| `CLAUDE.md` | Document pattern |

## Not in Scope (v2)

- **Dedicated `reviewer` WASM role** with restricted tool set (only merge_pr, not fork_wave/spawn_gemini)
- **Auto-respawn** of reviewer if it dies
- **Build verification** after merge (reviewer runs cargo check post-merge)
- **Wave completion tracking** (reviewer detects "all PRs in wave N merged" and notifies TL)
