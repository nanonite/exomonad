# Agent Loop Ownership by Harness

**Status:** Assessment for #723; final adoption ADR deferred to #756.

## Question

Could ExoMonad intercept and own the active response loop for every supported
harness—Claude Code, Codex, and OpenCode—so that durable steering and
follow-up queues are consumed in one common loop?

The hard constraint is harness agnosticism. A mechanism that works only for
Claude Code, or only by relying on tmux input behavior, is not a common loop
owner.

## Existing boundary

The current architecture gives each layer a different authority:

| Layer | ExoMonad can own | Harness still owns |
|---|---|---|
| Launch | Worktree, configuration, role, process, and tmux placement | Runtime process internals after launch |
| Policy hooks | ExoMonad WASM decisions for tool and lifecycle hooks | Whether and when the runtime calls a hook |
| Message delivery | Durable inbox record, routing, FIFO serialization, retries, and ledger evidence | Reading the native inbox or accepting injected input |
| Active turn | Workflow intent and queued guidance | Model request, tool-call batch, context append, and next response |
| Completion | PR/review/CI/merge FSM and explicit gates | Runtime stop/idle behavior and final context state |

This is consistent with the programmatic TL decision: `tl_loop` is the
workflow coordinator, while Claude, Codex, and OpenCode remain bounded
implementation or review harnesses. Owning the workflow loop is not the same
as owning a runtime's model-response loop.

## Per-harness assessment

### Claude Code

Claude Code owns its response loop and its native Teams `InboxPoller`. The
ExoMonad path can:

- register the Claude session and team;
- append addressed messages to the native Teams inbox;
- observe the on-disk inbox and use tmux fallback for ExoMonad-owned
  recipients; and
- receive lifecycle and tool-hook events through the existing hook surface.

The Teams file is a producer-facing mailbox. A successful append does not give
ExoMonad a callback at the next assistant-turn boundary, nor does the current
hook contract expose a portable operation that appends an arbitrary user
message to Claude's active context. Replacing Claude Code with a separate
headless model client would be a new harness, not loop ownership of the
existing Claude agent.

**Verdict:** Do not take ownership of the Claude response loop. Use Teams as
the native delivery adapter and treat native read/acceptance evidence as a
harness-owned boundary.

### Codex

ExoMonad launches Codex through `codex exec` or `codex fork`, writes project
`.codex/config.toml`, and installs command hooks for `PreToolUse`,
`PostToolUse`, and `Stop`. Those hooks are synchronous observations and policy
points. They can allow, deny, or rewrite a tool call, and a stop hook can
return a continuation decision, but they do not provide a common API for
inserting a queued steering message into the next model context.

The current non-Claude path therefore routes through the ExoMonad FIFO and
tmux. Tmux can serialize submitted input, but it cannot prove that Codex has
accepted the message as a new turn or has returned to an idle prompt. The
existing cross-runtime inbox decision explicitly rejects absence-of-payload
heuristics for this reason.

ExoMonad could build a Codex-specific outer driver that starts Codex for one
turn, persists context, and starts the next turn itself. That would require a
stable transcript/context protocol, new interruption and tool-result
semantics, and a different launch contract from the current interactive
agent. It would also need a separate implementation for Claude and OpenCode,
so it cannot be the E6 common solution.

**Verdict:** Do not own the current Codex response loop. Keep shell hooks for
policy/observation and use a queued delivery adapter with positive runtime
acceptance evidence. A one-turn Codex driver may be a future opt-in harness,
not a change to the common contract.

### OpenCode

ExoMonad writes `opencode.json` and a TypeScript plugin into the agent
worktree. The plugin runs inside OpenCode and forwards lifecycle events to the
ExoMonad hook command over the existing UDS path. The current plugin can:

- inspect and rewrite tool arguments before execution;
- deny a tool by throwing from `tool.execute.before`;
- observe tool results after execution; and
- observe `session.stopped` and route it to the stop or worker-exit hook.

Those callbacks are valuable control points, but they are not a queue API or a
contract for appending a user message to the next model turn. The plugin's
failure fallback is deliberately fail-open for hook transport, and its
successful hook response only proves that the plugin handled the hook call.
It does not prove that a separately delivered message was consumed by the
OpenCode session.

OpenCode's plugin could eventually implement a native session adapter if the
runtime exposes a documented, durable prompt/continue API. That would be
runtime-specific and would still need equivalent adapters for Claude and
Codex. The current OpenCode integration is not such an adapter.

**Verdict:** Do not own the current OpenCode response loop. Keep the plugin as
a tool/lifecycle hook bridge and route steering through the durable inbox
adapter until a positive native acceptance API exists.

## Common feasibility result

The three runtimes share enough surface for policy hooks and process
supervision, but not enough surface for loop ownership:

| Required common operation | Claude Code | Codex | OpenCode | Common result |
|---|---|---|---|---|
| Observe tool/lifecycle events | Hooks and Teams/runtime events | Command hooks | Plugin hooks | Yes, through adapters |
| Deny or alter a tool call | Hook policy | Pre-tool hook | Plugin throw/arg mutation | Yes, with runtime adapters |
| Queue durable guidance | Teams/inbox fallback | ExoMonad FIFO/tmux | ExoMonad FIFO/tmux | Yes, as delivery |
| Append guidance to the next model context | Native runtime-owned poll | No common hook contract | No current plugin contract | No |
| Prove runtime accepted a turn | Native poll/read evidence | Positive TUI/runtime signal required | Native signal not implemented | Not common today |
| Cancel and requeue at a turn boundary | Runtime-owned | Runtime-owned | Runtime-owned | No common authority |

Therefore ExoMonad cannot safely claim ownership of the active model loop
without either weakening the guarantee to best-effort tmux delivery or
replacing all three runtimes with new adapters. Both choices violate the
current cross-runtime inbox invariant or the harness-agnosticism constraint.

## Recommended boundary for #755 and #756

Keep the active response loop inside each runtime. Put the durable queue one
layer before that loop and give it a runtime adapter with explicit evidence:

1. Record an immutable message or batch identity in SQLite and the ledger.
2. Classify it as steering or follow-up without assigning merge authority.
3. Select the runtime adapter from the agent identity and runtime.
4. Deliver one atomic batch at a time, retaining it until the adapter reports
   positive acceptance or an explicit terminal abandonment.
5. Let the runtime consume the message at its own loop boundary.
6. Record transport delivery, runtime acceptance, and workflow effects as
   separate events.

For Claude, the adapter observes the native Teams path. For Codex and
OpenCode, the adapter owns FIFO serialization and runtime-specific positive
acceptance checks. A tmux write remains a transport attempt, never an
acceptance acknowledgement. The controller consumes the ledger and makes
review, CI, merge, retry, and human-gate decisions independently of all three
delivery paths.

This gives prime-agent's useful ordering semantics—steering before follow-up
and explicit one-batch consumption—without pretending that ExoMonad can
append to a context it does not own. The final ADR should specify the durable
record and adapter protocol; it should not promise a universal outer model
loop.

## Rejected alternatives

### Make Claude the universal loop owner

This would make Claude's Teams and InboxPoller semantics the de facto
protocol. Codex and OpenCode would still require tmux or a compatibility
bridge, so the result would be Claude-specific orchestration with weaker
semantics for the other runtimes.

### Make tmux the universal loop owner

Tmux controls keystroke delivery, not model context, tool completion, or
runtime acceptance. The ledger evidence in #752 already records retries,
duplicates, and abandonment that can occur after a transport write. Tmux is a
transport adapter, not a response-loop boundary.

### Replace every runtime with an ExoMonad model client

That would provide a common loop only by removing the supported harnesses'
own context, tools, hooks, and lifecycle semantics. It is a new product and
authority model outside E6.

## Conclusion

ExoMonad should own the durable workflow and message-scheduling boundary, not
the active Claude Code, Codex, or OpenCode response loop. This is the only
recommendation that preserves harness agnosticism, the ledger authority
boundary, and runtime-owned tool/context semantics. #755 should design the
durable queue and adapter evidence contract; #756 should ratify this boundary
alongside the transport findings from #752.
