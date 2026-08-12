# Prime-agent Queue Model Study

**Status:** Research input for #723; not the final ExoMonad adoption decision.

## Scope and source

This note studies the queue and loop boundary in the local prime-agent source
at `/home/goya/agent-workspace/prime-agent`. The inspected implementation is
in `packages/agent/src/agent.ts` and `packages/agent/src/agent-loop.ts`, with
API and behavior notes in `packages/agent/src/types.ts`, the package README,
and the agent and loop tests. The observations below describe the source as
inspected on 2026-08-12.

## Mental model

Prime-agent owns the run loop and keeps two independent pending-message
queues:

- **Steering** messages alter the current run after the active assistant turn
  has finished, including all tool calls. They are injected before the next
  assistant response.
- **Follow-up** messages are considered only at the point where the agent
  would otherwise stop. They run after steering has been checked and found
  empty, before an optional continuation is requested.

The queue stores batches rather than only individual messages. Enqueuing an
array copies that array into one batch. The default `one-at-a-time` mode drains
one complete batch per poll; `all` flattens and drains every queued batch.
This preserves the caller's message grouping in the default mode and makes
batch size an explicit scheduling choice.

The public `prompt` and `continue` methods reject while a run is active. Active
work is extended through `steer` or `followUp`, which prevents a second loop
from competing with the current loop. `continue` first drains steering and
then follow-up queues when it is called while the transcript cannot otherwise
continue.

## Loop sequence

The implementation's scheduling order is:

1. Poll steering at the start of a run.
2. Inject pending messages into the current context and request the next
   assistant response.
3. Execute the current assistant's complete tool-call batch.
4. After `turn_end`, run the hard `shouldStopAfterTurn` check. A positive
   result ends the run without polling either queue.
5. Otherwise poll steering. Non-empty steering becomes the next pending turn;
   an empty result permits the stop-before-turn check.
6. When there are no tool calls and no steering, poll follow-up. Non-empty
   follow-up becomes pending work and returns to the inner loop.
7. Only when follow-up is empty may the loop request continuation messages.
   Empty continuation ends the run.

In compact form:

```text
assistant turn + all tools
        |
        v
hard stop? --yes--> agent_end
        |
        no
        v
steering batch? --yes--> next assistant turn
        |
        no
        v
follow-up batch? --yes--> next assistant turn
        |
        no
        v
continuation? --yes--> next assistant turn; --no--> agent_end
```

This ordering is a loop property, not a transport property. In particular,
steering does not cancel or skip tool calls already issued by the current
assistant turn.

## Queue comparison

| Property | Steering | Follow-up |
|---|---|---|
| When read | At run start and after each completed turn/tool batch | Only when no tool calls or steering remain and the agent would stop |
| Purpose | Modify an active run at its next loop boundary | Supply work after natural completion would otherwise occur |
| Ordering | Before follow-up and continuation | After steering, before continuation |
| Default drain | One complete batch | One complete batch |
| Active-run API | `steer(...)` | `followUp(...)` |

The `all` mode is available for both queues, but it changes the boundary by
flattening all pending batches into one poll result. It should therefore be
selected only when coalescing queued messages is intentional.

## Abort and cleanup

Each active run owns an `AbortController`. The signal is passed through the
LLM stream, tool execution, queue polling, stop predicates, continuation
provider, and event lifecycle. A polling operation is raced against the signal;
when the signal is already aborted, the poll returns no messages, and when it
aborts during an operation, the loop receives an abort error while the
underlying promise is caught to prevent an unhandled rejection.

Abort handling has four useful properties:

1. A queue is not drained by a poll that observes an already-aborted signal.
2. An abort during a turn or tool operation ends the loop and emits
   `agent_end`; an in-flight operation that ignores abort cannot hold the
   lifecycle promise forever.
3. A provider or listener failure is converted into a synthetic assistant
   failure message and an `agent_end` event. An abort is distinguished from a
   normal error through `stopReason: "aborted"`.
4. Runtime state is cleared only after awaited lifecycle listeners complete.
   `waitForIdle` therefore observes an actually idle agent, rather than merely
   the point at which no further loop event will be emitted.

The distinction between `shouldStopAfterTurn` and `shouldStopBeforeTurn` is
important. The former is a hard post-turn stop that skips queue polls. The
latter is checked at loop boundaries, while a non-empty steering result from a
poll still gets its turn even if the stop condition changes during polling.

## Lessons for ExoMonad

The portable idea is to own queue scheduling at the harness loop boundary.
For each agent, ExoMonad can model:

- explicit steering and deferred follow-up classes;
- atomic message batches with a stable batch identity;
- one active consumer per invocation;
- abort-aware dequeue, with messages retained until a positive consumption
  acknowledgement;
- deterministic precedence of current-turn completion, steering, follow-up,
  and continuation;
- replay identity containing message or batch ID plus run, session,
  invocation, and generation information.

This complements the transport evidence in
[agent-steering-transport-evidence.md](agent-steering-transport-evidence.md).
The durable SQLite inbox and ledger should remain the recovery and authority
boundary regardless of whether a harness can accept a message. A successful
write to Teams, UDS, or tmux is not proof that the target runtime accepted the
next turn.

The current ExoMonad inbox already has a single-consumer FIFO, a short
deduplication window, retry handling, and durable records. It does not yet
provide all of the prime-agent semantics: the queue is not a durable
per-agent scheduling abstraction, transport injection is not runtime
consumption acknowledgement, and the current delivery path does not encode a
steering-versus-follow-up class. Those gaps belong in the follow-on design
work, not in this study note.

## Boundaries and non-goals

Prime-agent is an in-process agent implementation. Its in-memory queue is not
restart-safe, and its loop can directly append queued messages to the next
LLM context. ExoMonad cannot assume either property for Claude, Codex, or
OpenCode. The harness must express intent and observe evidence through the
runtime-specific boundary; it must not pretend that a successful injection
is a merge decision or that it can skip tool calls already owned by a target
runtime.

The `shouldStopAfterTurn` behavior also should not be copied as a transport
rule. It is meaningful only to the loop that owns the active assistant turn.
ExoMonad can record a stop or cancellation intent, but the target harness must
decide how and when that intent is accepted.

## Questions carried into #754–#756

- Which loop boundary can each supported harness expose reliably?
- Which queue state, batch identity, and acknowledgement survive a process
  restart?
- How should cancellation retain, requeue, or abandon a steering batch?
- Which messages may be coalesced, and which must remain atomic?
- What evidence proves runtime acceptance separately from transport delivery?

## Conclusion

Prime-agent's key contribution is the separation of queue classes and the
explicit loop boundary that consumes them. It informs ExoMonad's E6 design,
but does not decide whether ExoMonad should own the active loop for any
harness. That recommendation requires the per-harness assessment in #754 and
the durable queue design in #755 before the final ADR in #756.
