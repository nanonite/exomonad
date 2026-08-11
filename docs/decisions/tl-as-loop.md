# ADR: Programmatic TL controller

Date: 2026-08-11
Status: Accepted
Issue: #707

## Context

The baseline ExoMonad TL was an interactive harness session. A prompt in
`.exo/roles/devswarm/context/tl.md` asked that session to decompose work,
spawn agents, wait for messages, merge pull requests, and decide when the
tree was complete. The Haskell TL role and stop hook supplied additional
coordination nudges. This made the control loop narrative, difficult to
replay, and coupled to a particular model session.

The M0--M8 work moved the coordination contract into `tl_loop/`. The Rust
runtime still owns effects, worktrees, watchers, and process lifecycle. The
Haskell WASM role still owns tool schemas, effect dispatch, and event-handler
decisions. The missing decision was which layer is the TL and how the older
plans and prompts relate to it.

## Decision

`tl_loop` is the only TL coordinator. Its controller is a bounded, resumable
program that:

1. loads a closed-key `WorkPlan` and durable run checkpoint;
2. classifies slices and selects an allowed harness within human-authored
   budgets;
3. dispatches leaves, workers, and recursive sub-TLs through the existing
   ExoMonad effect surface;
4. consumes the existing immutable ledger through a typed projection and
   bounded in-process queue;
5. applies the explicit FSM, review/head-SHA gates, repair handoffs, and merge
   decisions; and
6. parks bounded failures or waits for an explicitly named human gate.

The human-facing tmux TL window is an operator surface for controller logs and
gate commands. It does not launch Claude, Codex, OpenCode, or a second
interactive coordinator. Agent harnesses remain bounded implementation and
review processes. Their branches, worktrees, PRs, Chainlink issues, and
reviewer lifecycle are unchanged.

The conceptual hylomorphism remains useful: a plan and checkpoint are the
unfold boundary, while event-driven review, merge, and upward PR effects are
the fold boundary. The controller makes those transitions executable and
testable; the model supplies only bounded structured judgments
(`decompose`, `adjudicate_review`, and `compose_repair`).

## Boundary choice: A — Haskell as the RPC surface

The controller calls the existing Rust UDS/MCP runtime. Haskell WASM remains
the single source of truth for MCP schemas, role permissions, effect
construction, and event-handler decisions. Rust remains the I/O runtime for
git, Forgejo, worktrees, processes, watchers, files, and sockets. Python does
not call providers directly, define a second MCP schema, or execute effects
outside that boundary.

This preserves the existing typed boundary while making the coordinator a
small standard-library Python program that can be unit-tested, replayed, and
resumed. The rejected alternatives were a Rust coordinator (which would
duplicate controller policy in the I/O runtime), a Python implementation of
MCP and provider effects (which would bypass the typed DSL), and an
interactive Claude/Codex/OpenCode TL (which would restore the untestable
prompt-driven coordinator).

## Inbox, ledger, and the rejected message broker

The loop reads the durable append-only ledger and projects it into an
in-process queue. The inbox remains the delivery surface for worker and human
messages; it is not the structured event source for PR, CI, review, or merge
state. The loop therefore does not add MQTT, a second event log, or an inbox
database.

This is appropriate for the current single-host topology: local agent
processes share a filesystem and a UDS socket, and the existing ledger already
provides durability, ordering, replay, and sequence-status checks. A broker
would add another ordering and delivery contract without improving the local
case.

Revisit this decision only if agents move to multiple hosts or containers that
cannot share the UDS socket, filesystem state, and ledger segments. At that
point a broker may be justified, but it must preserve event identity,
ordering, replay, and human-gate semantics rather than replace them with
best-effort inbox delivery.

## Borrowed patterns and explicit boundaries

The following source repositories were available in the workspace when this
decision was made. ExoMonad borrows the named pattern, not the repository's
runtime or control authority.

### Prime Agent

Source: [PrimeIntellect-ai/prime-agent](https://github.com/PrimeIntellect-ai/prime-agent)
(`packages/agent/src/agent-loop.ts`, model resolution, continual harness,
goals, and heartbeats).

Borrowed: a programmatic loop, narrow model calls, durable context/harness
state, bounded autonomous progress, and explicit goals/heartbeats. ExoMonad
keeps its own closed schemas, FSM, Chainlink accounting, and Rust/Haskell
effect boundary. It rejects allowing the model to rewrite the base controller
or using a persistent model REPL as the system of record.

### OpenCode Feature Factory

Source: [jasoncarreira/opencode-feature-factory](https://github.com/jasoncarreira/opencode-feature-factory)
(`packages/feature-factory/core/atomic-write.js`, `write-core.js`,
`run-lock.js`, and the state schema/transition modules).

Borrowed: one protected atomic state-write path, closed-key state, slice
ownership (`paths`, `depends_on`, `base_ref`, and `test_plan`), bounded
parallelism/retries, and reviewed-head binding. ExoMonad rejects importing a
second feature-factory pipeline or making OpenCode the coordinator; harnesses
are adapters selected by the controller's policy, not competing control
planes.

### Lanius

Source: [tkellogg/lanius](https://github.com/tkellogg/lanius)
(`src/events.rs`, context/control-plane documentation, and explicit inbox
answers).

Borrowed: an event-driven local control plane, shared observability, worker
dispatch, and explicit human gates. ExoMonad rejects making agents coordinate
through an unconstrained peer-messaging mesh, replacing the existing ledger
with a new daemon database, or allowing a free-form message to approve a
merge.

### Zuihitsu

Source: [philpax/zuihitsu](https://github.com/philpax/zuihitsu)
(`docs/events-and-storage.md`, event/replay implementation, and context
budgeting notes).

Borrowed: the append-only event log as the source of truth, deterministic
replay/materialized state, and the conservative context budget floor
`floor(context_length * 0.8)`. ExoMonad rejects copying Zuihitsu's knowledge
graph or platform-agent surface; the TL loop owns only orchestration state and
keeps model/provider calls outside the event log.

## Consequences

- The TL is deterministic around bounded judgments and can be tested from
  recorded events without a live model conversation.
- Run state, budgets, checkpoints, gate approvals, and selections are durable
  under `.exo/tl-loop/<run_id>/`; the immutable ledger remains read-only to the
  loop.
- Human intervention is explicit and resumable through
  `python3 -m tl_loop gate --run-id <id> --name <gate> --approve|--reject`.
- `tl.md` remains because it is useful decompose-prompt vocabulary, but it is
  not an agent protocol. The old merge-reviewer teammate plan is subsumed:
  the controller is the merge queue.
- A future distributed deployment must reopen the inbox/broker decision and
  prove that its transport preserves the same durable semantics.
