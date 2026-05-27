# Cross-Runtime Message Inbox

**Status:** Accepted

**Date:** 2026-05-19

**Chainlink:** #314, #317, #318

## Context

The nemotron-port run exposed message pile-up in non-Claude runtimes: several messages were pasted into the same TL input box, producing concatenated `[from issue-N]` markers instead of one consumed message at a time.

The root cause is that the tmux injection lock serializes injection calls, not message consumption. It can prevent two writes from happening at the same instant, but it cannot prove the runtime has accepted, processed, and returned to an idle prompt before the next message arrives. Codex multi-line input mode made this worse by breaking the absence heuristic from #314: the pasted payload could disappear from the visible buffer without the runtime having actually consumed the submitted message.

This is a harness-agnosticism failure. Claude Code has a native Teams inbox and InboxPoller, so delivery is serialized by the harness. Codex, Gemini, OpenCode, and future tmux-backed runtimes need the same guarantee from ExoMonad instead of relying on runtime-specific input quirks.

## Decision

ExoMonad provides per-runtime inbox parity. For every runtime that lacks a native inbox plus poller, ExoMonad routes addressed messages through a per-agent FIFO inbox with a single consumer task. Claude Code remains on the Teams inbox path because it already provides native serialized delivery.

The inbox belongs to message delivery, not to agent phase state. Runtime-specific consumers may inject through tmux, ACP, or a future native API, but the ordering and in-flight guarantees are ExoMonad responsibilities for non-Claude runtimes.

## Invariants

1. Every message addressed to a non-Claude agent enters a per-agent FIFO before reaching the runtime.
2. The FIFO has a single consumer per agent; in-flight count is at most one per agent.
3. Consumption verification uses a per-runtime positive signal that the TUI rendered a response, not absence-of-payload heuristics.
4. Queue depth is observable and bounded, with explicit rejection at the cap. Messages are never silently dropped.

## Per-Runtime Table

| Runtime | Inbox source | Consumer | Notes |
|---------|--------------|----------|-------|
| Claude Code | Teams inbox (`~/.claude/teams/{team}/inboxes/`) | InboxPoller (native) | Already serialized; ExoMonad path unchanged |
| Codex | ExoMonad `AgentInbox` | ExoMonad consumer task | Tmux injection; multi-line input mode needs positive-signal verification |
| Gemini | ExoMonad `AgentInbox` fallback, or ACP prompt when connection is live | ExoMonad consumer task | ACP delivery is preferred when available and is separate from tmux fallback |
| OpenCode | ExoMonad `AgentInbox` | ExoMonad consumer task | Stub until OpenCode integration matures |

## Out Of Scope

- Persisting the inbox across ExoMonad restarts. In-memory queues are acceptable; senders use `notify_parent` retries.
- Cross-agent fanout. Broadcast-style sends use the same per-recipient inboxes underneath.
- Replacing Claude Teams delivery. Claude's native inbox is the reference behavior this ADR mirrors for other runtimes.

## Related

- [agent-lifecycle-invariants.md](agent-lifecycle-invariants.md) — peer ADR for spawn, worker, and leaf lifecycle invariants.
- [agent-identity-model.md](agent-identity-model.md) — birth-branch identity used as the stable inbox key.
- [teams-roadmap.md](teams-roadmap.md) and [claude-teams-integration.md](claude-teams-integration.md) — Claude's native Teams inbox path.
- Chainlink #314 — per-message Enter verification, complementary to this FIFO decision.
- Chainlink #317 — implementation of `AgentInbox`, single-consumer delivery, and per-runtime consumption verification.

## Implementation Tracking

| Chainlink | Priority | Covers |
|-----------|----------|--------|
| #314 | high | Per-message tmux Enter verification and injection hardening |
| #317 | high | Per-agent FIFO inbox, single-consumer task, queue depth caps, and positive consumption signals |
| #318 | medium | ADR and architecture references for the cross-runtime inbox invariant |
