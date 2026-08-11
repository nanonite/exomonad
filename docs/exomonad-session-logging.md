# ExoMonad session logging and workflow event inventory

Status: repository-backed inventory, prepared for observability and Failure Atlas analysis.

## Current integrated observability contract

The inventory below includes historical compatibility sinks. The current
implementation always emits structured telemetry during normal ExoMonad
sessions; --verbose controls only additional human-readable tracing and never
controls analysis-grade events.

The authoritative local path is the append-only L1 ledger at
.exo/ledger/segments/*.jsonl. L2 is the rebuildable .exo/analysis/atlas.db
view, and .exo/session.json plus the memory/inbox databases are mutable L3
state whose changes are mirrored into L1. L4 is the only shareable boundary:

    exomonad logs export --mode aggregate

This produces allowlist-only analysis.json, manifest.json, and
privacy-report.json. Raw events, local payloads, transcripts, reasoning,
paths, secrets, and stable source identifiers never enter L4. OTel is optional
and may bridge compatible traces across sessions, but local L1/L2 capture does
not depend on a collector.

Legacy import is explicit and read-only when requested:

    exomonad logs import --source PATH --dry-run
    exomonad init --import-legacy PATH --import-legacy-dry-run

An apply import is idempotent and never edits the source. Use
exomonad logs measure with a preregistration to run the Failure Atlas
signal/incident/adjudication pipeline; provider, runtime, and harness
group-bys remain descriptive rather than causal.

This document describes what ExoMonad records while a session is running, where it is
recorded, which event names are currently emitted, and which fields are available for
cross-referencing orchestration workflows. It is an inventory of the current code, not
a proposal that every signal is already durable or complete.

The analysis is intended to work with the [Failure Atlas](https://github.com/NimbleCoOrg/open-science/tree/main/docs/experiments/failure-atlas)
method: keep raw signals, distinguish detector signals from incidents and adjudication,
and aggregate only after preserving provenance and caveats.

## Executive summary

ExoMonad currently has several partially overlapping telemetry planes:

| Plane | Typical location | Durability | Primary purpose |
| --- | --- | --- | --- |
| Rust `tracing` | `.exo/logs/sidecar.log`, `.exo/logs/mcp-stdio-*.log`, stderr | File-backed for sidecar/MCP processes | Operational diagnostics, spans, errors, timings |
| Standard `EventLog` JSONL | `.exo/logs/{agent_id}.jsonl` | Append-only while the process can write | Workflow events suitable for dashboards and joins |
| Lifecycle `EventLog` JSONL | `.exo/events/{agent_id}.jsonl` | Append-only while the process can write | Invocation and guidance lifecycle metadata |
| OpenTelemetry | Configured OTLP collector, commonly Tempo | Collector-dependent | Distributed traces and structured telemetry |
| Haskell log effects | Forwarded through the WASM host | Depends on handler/sink | Guest-runtime diagnostics and custom events |
| Watcher/init logs | `.exo/logs/watcher.log`, `.exo/logs/init.jsonl` | Append-only files | Startup and worktree watcher diagnostics |
| SQLite state | `.exo/memory.db`, `.exo/inbox.db` | Durable databases | Memory, unread/read state, delivery bookkeeping |
| In-memory queues/UI events | Process memory and tracing | Not durable by themselves | Wakeups, retries, terminal UI status |

The strongest current workflow rows are agent spawn/resume, PR publication/merge,
event dispatch, hook decisions, tool calls, and explicit guest `emit_event` calls. The
main analysis risks are that the planes use different schemas, `.exo/logs` and
`.exo/events` are separate, OTel-only signals are absent from JSONL, and some semantic
events are emitted by both Haskell and Rust with different payloads.

## Runtime path

```text
provider / hook / MCP request
              |
              v
     Rust server and WASM host
       |          |          |
       |          |          +--> tracing spans/events --> sidecar/MCP log, stderr, OTel
       |          +-------------> EventLog ------------> .exo/logs/{agent}.jsonl
       +------------------------> lifecycle EventLog --> .exo/events/{agent}.jsonl
                                      |
                                      +--> dashboard reads .exo/logs/*.jsonl

     Haskell guest effects -- log.info/... --> tracing
                          \-- log.emit_event --> EventLog + tracing

     watcher / delivery / inbox / queues --> tracing and selected EventLog rows
     session memory / inbox store -------> .exo/memory.db / .exo/inbox.db
```

## Artifact and sink map

### `.exo/logs/sidecar.log`

Created by `rust/exomonad/src/logging.rs` for the normal server and command processes.
It is a daily rolling file named `sidecar.log`. The file layer uses ANSI-disabled
`tracing_subscriber` output. The same initialization also adds stderr output, unless the
process is using MCP stdio mode. `RUST_LOG` controls filtering and an INFO directive is
installed by default.

Set `EXOMONAD_LOG_FORMAT=json` for JSON-formatted tracing output. Without it, the file
is human-readable text. This is not the same schema as `EventLog` JSONL; structured
tracing fields and span context may be present, but there is no universal event envelope.

### `.exo/logs/mcp-stdio-*.log`

`init_mcp_stdio` writes a non-rolling process-specific file named like:

```text
.exo/logs/mcp-stdio-{role}-{name}-{pid}.log
```

Role and name are sanitized for use in a filename. MCP stdio keeps stdout reserved for
JSON-RPC responses, so diagnostics go to the file and optional OTel exporter rather
than stderr. Each request also has an `mcp-stdio.request` span.

### `.exo/logs/{agent_id}.jsonl`

This is the standard structured workflow event stream. `EventLog::append` creates one
UUID, an RFC3339 UTC millisecond timestamp, and one line with this envelope:

```json
{
  "ts": "2026-08-09T12:34:56.789Z",
  "id": "event-uuid",
  "type": "agent.spawned",
  "agent_id": "child-agent",
  "data": {}
}
```

The file name is derived from `agent_id`; `/`, `\\`, and NUL are replaced with `_`.
Writes are serialized per `EventLog` instance and use append mode. Most write failures
are logged as warnings and do not abort the workflow, so a missing row is not proof
that an action did not happen.

The active server opens the log directory under the project directory. The dashboard
currently scans only JSONL files directly under `.exo/logs`, parses the standard
envelope, sorts by timestamp, and displays the result. It does not scan
`watcher.log`, nested files, `.exo/events`, or SQLite.

### `.exo/events/{agent_id}.jsonl`

Lifecycle telemetry uses the same `EventLog` envelope but a separate directory. The
invocation paths open an agent directory's `.exo/events`; guidance delivery opens the
project's `.exo/events`. This separation is important when correlating a full run:
both locations may contain agent-named files, but they are not automatically merged.

The `EventLog` module's old comment mentions `.exo/events.jsonl`; current callers pass a
directory and produce per-agent JSONL files.

### `.exo/logs/watcher.log`

The worktree watcher writes human-readable lines in the form:

```text
timestamp [watcher] message
```

It is useful for local diagnosis, but it is not the standard event envelope and is not
included by the dashboard. The watcher also emits structured `watcher.poll_cycle` and
`watcher.pr_observation` rows to the EventLog.

### `.exo/logs/init.jsonl`

Initialization appends one JSON object per invocation containing `timestamp_ms`, argv,
tmux session, resolved root/spawn/reviewer agent types, models, and effort. Token,
secret, password, and API-key-like argv values are redacted. `--verbose` additionally
sets `RUST_LOG=info`, `EXOMONAD_HOOK_TRACE=1`, `EXOMONAD_CHAINLINK_TRACE=1`, and
`EXOMONAD_VERBOSE=1`.

### OpenTelemetry

When an OTLP endpoint is configured, ExoMonad creates a batch Tokio exporter and adds a
resource containing `service.name` and `service.version`. It may also include
`swarm.run_id` from `EXOMONAD_SWARM_RUN_ID` and `agent.parent` from
`EXOMONAD_PARENT_AGENT`. Trace-context propagation is installed, and an incoming
`TRACEPARENT` is used as the parent for agent requests.

Without an OTLP endpoint, tracing still writes to the local layers but there is no
collector-side trace. OTel signal availability is therefore deployment-dependent.

## Logging mechanisms

### Rust `tracing`

This is the broad diagnostic mechanism. It records free-form messages, structured
fields, spans, and instrumentation timing. Important sources include:

- server startup, shutdown, configuration, WASM load/reload, Forgejo, and watcher state;
- `exomonad.serve`, `agent_request`, `mcp_stdio.request`, `host_function`, effect
  dispatch, plugin calls, and watcher poll spans;
- subprocess execution, including command, working directory, exit status, and
  completion timing (stdout/stderr are returned by the process API rather than logged
  wholesale);
- Chainlink command execution when `EXOMONAD_CHAINLINK_TRACE` is enabled;
- effect suspension/resumption, continuation errors, MCP parsing, and delivery retries;
- state-machine transitions and invalid transition warnings.

The tracing stream is open-ended. The event catalog below covers durable workflow rows;
it does not claim that every diagnostic message has a finite event name.

### Haskell log effects

The guest exposes `log.info`, `log.error`, `log.debug`, `log.warn`, and
`log.emit_event` in `ExoMonad.Effects.Log`. The host maps the four log levels to Rust
`tracing` with a `[wasm]` marker and structured fields represented as UTF-8 text.

`log.emit_event` additionally:

1. creates a UUID event ID;
2. uses the current host time for the durable row;
3. parses the JSON payload, or wraps invalid payload bytes as raw data;
4. writes the row using the current host agent name;
5. emits a tracing record with `otel.name`, `event_type`, `event_id`, and `payload`.

The request's optional timestamp is currently ignored by the host. Guest code can use
arbitrary event names, so the catalog must treat `event_type` as an extensible namespace.

### Explicit `EventLog` writes

Rust handlers call `EventLog::append` for high-value orchestration transitions. These
rows are the best local source for a workflow timeline, but the payload is event-specific
and the envelope does not currently carry all identity or trace fields needed for a
collision-proof cross-runtime join.

### Lifecycle telemetry

`LifecycleTelemetry` records provider and invocation metadata in `.exo/events`:

| Lifecycle kind | Event type | Key fields |
| --- | --- | --- |
| Invocation started | `agent.invocation.started` | source agent, provider, role, invocation ID, generation, trigger, issue, PR, head SHA, rollout mode, channel |
| Invocation finished | `agent.invocation.finished` | same identity fields plus outcome and status |
| Guidance delivered | `agent.guidance.delivery` | source agent, provider, role, invocation ID, generation, delivery outcome, channel, authoritative flag |

The current `generation` value mirrors the invocation ID rather than representing a
separate monotonic generation. The one-shot lifecycle mode can be enabled, shadowed, or
disabled; the default is enabled. Provider delivery contracts are:

| Provider | Delivery sequence |
| --- | --- |
| Claude | `teams_inbox` -> exact tmux -> durable inbox |
| Shoal | UDS -> exact tmux -> durable inbox |
| Codex, OpenCode | exact tmux -> durable inbox |
| External process | external process -> durable inbox |

Process exit, inbox delivery, tmux injection, and watcher/Forgejo observations are
guidance or lifecycle signals. They are not automatically authoritative workflow truth.

### OpenTelemetry-only delivery and inbox signals

Some important signals are currently emitted through tracing/OTel without a matching
standard EventLog row:

- `message.delivery`: delivery channel, method, outcome, detail, and attempt;
- `agent.message_sent`: address, method, and success;
- `agent_inbox.duplicates_dropped`: recipient, event type, and deduplication key;
- `agent_inbox.messages_abandoned`: recipient and retry count;
- `agent.guidance.delivery`: also has a lifecycle EventLog representation;
- delivery/inbox retry and backoff spans.

The in-memory inbox deduplicates event classes such as `MergeReady`, `ReviewApproved`,
`FixesPushed`, `CommitsPushed`, `ReviewTimeout`, `CIStatus`, `ReviewReceived`, and
`Stuck`; free-form messages are also supported. The queue has retry and abandonment
behavior, but its state is not a durable event ledger.

### SQLite state stores

`.exo/memory.db` is an append-oriented session-memory store. Its records include
`run_id`, `agent_id`, `birth_branch`, issue ID, memory kind, importance, summary, detail,
creation time, supersession, and metadata. Kinds include `original_plan`, `wave_plan`,
`spawned_child`, `child_handoff`, `blocker`, `decision`, `review_feedback`,
`fix_direction`, `merge_result`, `ci_result`, `next_action`, `human_clarification`,
`session_summary`, and `turn_end`.

`.exo/inbox.db` stores durable message state: message ID, sender, recipient, content,
summary, creation time, notification time, read time, and agent inbox metadata. This
records inbox state, not every attempted transport or tmux injection.

The in-memory event queue has `agent_message`, `timeout`, and `issue_closed` events,
monotonic IDs, and a maximum of 1,000 entries; it drops the oldest entry after the cap.
It is not durable and should not be treated as a complete session history.

## Durable structured event catalog

These are the named rows currently written to the standard EventLog or emitted as
explicit guest events. Payloads are not yet versioned; fields listed below are the
observed high-value fields, not a guaranteed schema contract.

### Agent and orchestration events

| Event type | Emitted when | Observed payload / fields |
| --- | --- | --- |
| `agent.spawned` | A worker, subtree, or leaf-subtree child is created | child agent, agent type, spawn type, branch, model, effort, topology; Haskell guest spawn additionally includes slug and task summary |
| `agent.resumed` | A dormant PR invocation is resumed | child agent, agent type, branch, `resume_pr` spawn type, PR number, head SHA, model, effort, topology |
| `agent.harness_switch` | A requested harness switch is allowed | operation, from/to harness, reason, approver/policy source, model, effort |
| `agent.stuck` | A disallowed harness switch or silent no-op handoff requires guidance | operation/kind, configured/requested harness, parent, status, reason, guidance requirement, retry policy, model/effort/policy |
| `agent.notify_parent` | A child sends completion/status to its parent | parent, status, message, source |
| `agent.sibling_merged` | A sibling merge is observed/notified | sibling/PR context, status, and related message fields |
| `issue.closed` | The orphan reconciler observes a closed issue before worktree disposal | issue ID, worktree slug, closer, source |
| `inbox.message` | A new Teams inbox message is observed before tmux injection | team, sender, recipient, exact text, summary, timestamp, read state, transport |
| `inbox.poke` | The watcher attempts an unread-inbox wakeup | recipient, unread count, newest message ID, exact notification, outcome, transport |
| `agent.completed` | Guest event before parent notification | status, message, PR number, tasks completed |
| `agent.stop_check` | Guest stop-check decision is recorded | branch and result |
| `custom` | Guest runtime emits an arbitrary custom event | arbitrary JSON payload |

`agent.spawned` and `pr.merged` can be emitted by both Rust and Haskell paths. Their
payload shapes differ, so consumers should use `event_id`, source context, and schema
inspection rather than assuming one payload shape.

### Branch, PR, review, and CI events

| Event type | Emitted when | Observed payload / fields |
| --- | --- | --- |
| `pr.filed` / `pr.updated` | A PR is filed or its known metadata is updated | PR number, URL, head/base branch, head SHA, created flag, title |
| `pr.published` | A PR publication is verified | PR number/URL/head, verification result, publication details, invocation ID |
| `pr.replaced` | A failed or stale PR invocation is replaced | Chainlink issue, old PR/leaf/head, source/base/new branch, worktree path, new agent |
| `pr.merged` | A PR is merged | PR number, merge strategy, fetch result; guest variant includes PR number and success |
| `pr.merge_failed` | Merge attempt fails | PR number and error |
| `copilot.review` | A worktree watcher sees a changed-request review | branch, status, message, comments, reviews, PR and SHA context |
| `ci.status_changed` | A watcher sees a changed CI state | branch, status (`pending`, `success`, `failure`, `neutral`, or `unknown`), message, comments, reviews |
| `event.dispatched` | A watcher/role event is routed | role, event type, action (`inject_message`, `notify_parent`, or `no_action`) |
| `event.dispatch_failed` | Routing an event fails | role, event type, error |
| `watcher.poll_cycle` | A worktree watcher poll completes | agent ID `watcher`, PR count |
| `watcher.pr_observation` | A watcher inspects a PR | PR number, review state, CI status, head SHA, changed-review round count |

The watcher maps changed-review status to `copilot.review`. CI states `pending`,
`success`, and `failure` map to `ci.status_changed`; `neutral` and `unknown` are
preserved as their raw status values. A legacy GitHub poller contains similar logic,
but the active server uses `WorktreeEventWatcher`.

The orphan reconciler records `issue.closed` in the canonical immutable ledger
before idempotent cleanup. The former `.exo/events/issue_closed.jsonl` sidecar is
not a live source; historical sidecar files remain untouched for interpretation.

### Hook events

`hook.stop` is a standard EventLog row for stop decisions. Its fields include the
incoming hook event type, decision (`allow` or `block`), and reason. Hook tracing can
also log receipt and dispatch diagnostics when `EXOMONAD_HOOK_TRACE=1`.

The protocol recognizes these hook event types:

```text
PreToolUse       PostToolUse       Notification       Stop
SubagentStart    SubagentStop      PreCompact         SessionStart
SessionEnd       PermissionRequest UserPromptSubmit   AfterAgent
BeforeTool       BeforeModel       AfterModel         WorkerExit
```

The server routes them as follows:

| Incoming hook type | Internal path | Durable `hook.stop` row? |
| --- | --- | --- |
| `Stop`, `AfterAgent`, `SubagentStop`, `SessionEnd` | Stop | Yes, when the stop path reaches the decision handler |
| `PreToolUse`, `BeforeTool`, `PostToolUse`, `SessionStart` | Tool-use | No generic row; diagnostics and tool events may be emitted |
| `BeforeModel`, `AfterModel` | Model hook | No generic row |
| `WorkerExit` | Worker-exit path | No `hook.stop` row |
| `Notification`, `SubagentStart`, `PreCompact`, `PermissionRequest`, `UserPromptSubmit` | Early pass-through | No generic row |

The Haskell runtime additionally logs hook parsing/receipt, stop-hook firing or
suspension, and worker exit diagnostics.

### Tool, effect, and transport events

| Event type or signal | Where | Observed fields |
| --- | --- | --- |
| `tool.called` | Standard EventLog and OTel | tool name, role, original arguments, duration in ms, success, error |
| `log.emit_event` event type | Standard EventLog and OTel | caller-selected type, event ID, JSON payload |
| `message.delivery` | OTel/tracing | sender, recipient, channel/method, outcome, detail, attempt |
| `agent.message_sent` | OTel/tracing | address, method, success |
| `agent_inbox.duplicates_dropped` | OTel/tracing | recipient, event type, deduplication scope/key |
| `agent_inbox.messages_abandoned` | OTel/tracing | recipient, attempts |

WASM effect dispatch is also traced with effect type, namespace, and agent. Host
function spans include the yielded effect. Plugin calls include function, agent, and
birth branch and log suspension/resumption rounds. Effect failures and process
failures are primarily diagnostic tracing records rather than normalized EventLog rows.

## OTel names and spans

The following names were found in `otel.name` fields or instrumentation spans:

| Name | Signal | Notes |
| --- | --- | --- |
| `agent.spawned` | Event/log | Also standard EventLog |
| `agent.message_sent` | Event/log | OTel-only transport observation |
| `agent.resumed` | Event/log | Currently standard EventLog handler row; not consistently named as `otel.name` |
| `pr.filed`, `pr.updated` | Event/log | Dynamic event name based on operation |
| `pr.merged`, `pr.merge_failed` | Event/log | Merge outcome |
| `hook.stop` | Event/log | Stop decision |
| `tool.called` | Event/log | Tool duration and outcome |
| `agent.notify_parent` | Event/log | Parent notification |
| `message.delivery` | Event/log | Delivery attempt and outcome |
| `agent_inbox.duplicates_dropped` | Event/log | Deduplication |
| `agent_inbox.messages_abandoned` | Event/log | Retry exhaustion |
| `agent.sibling_merged` | Event/log | Sibling merge notification |
| `issue.closed` | Event/log | Closed issue observed before worktree disposal |
| `inbox.message` | Event/log | Teams inbox message observed before tmux injection |
| `inbox.poke` | Event/log | Unread inbox wakeup attempt and delivery outcome |
| `event.dispatched`, `event.dispatch_failed` | Event/log | Role routing |
| `agent.guidance.delivery` | Event/log | Lifecycle and OTel |
| `copilot.review` | Event/log | Dynamic watcher mapping |
| `ci.status_changed` | Event/log | Dynamic watcher mapping |
| `exomonad.serve` | Span | Server lifetime |
| `agent_request` | Span | Agent ID, role, parent, run ID |
| `mcp_stdio.request` | Span | JSON-RPC method, ID, role, agent |
| `host_function` | Span | WASM host function/effect yield |
| `worktree_event_watcher.poll_cycle` | Span | Active watcher poll |
| `github_poller.poll_cycle` | Span | Legacy poller path |

Trace fields such as `trace_id`, `span_id`, and `parent_span_id` are valuable joins when
present, but are not currently copied into the standard EventLog envelope.

## Failure Atlas cross-reference

The Failure Atlas separates raw extracted signals, clustered incidents, human or model
adjudication, and aggregate analysis. ExoMonad should preserve that separation. A
single `agent.stuck` row is a signal; it is not by itself an adjudicated failure.

### Proposed normalized analysis row

Materialize a view over all raw sources with at least:

```text
event_id, timestamp, run_id, source, event_type,
agent_id, birth_branch, role, provider, runtime, harness,
invocation_id, generation, issue_number, pr_number, head_sha,
trace_id, span_id, parent_span_id,
outcome, status, duration_ms, attempt,
authoritative, payload, raw_source, raw_offset
```

Until the envelope is expanded, the minimum practical join is `run_id + agent_id +
timestamp window`, supplemented by PR number, branch, issue number, and trace ID when
available. That join is heuristic and must be marked as such.

### Detector candidates

| Failure Atlas-style detector | ExoMonad signals |
| --- | --- |
| Tool/effect error loop | repeated failed `tool.called`, process failures, effect dispatch errors, `pr.merge_failed`, `event.dispatch_failed` |
| Delivery loss | `agent.notify_parent`, `message.delivery`, `agent.message_sent`, `agent.guidance.delivery`, inbox notification/read state, abandoned messages |
| Review loop | `copilot.review`, review/CI events, repeated review rounds, `agent.stuck`, `review_feedback` memory, review timeout |
| Fan-out or orphaning | `agent.spawned` without matching invocation/finish/handoff, missing dispatch, orphan cleanup, excessive child count |
| Harness/runtime mismatch | `agent.harness_switch`, `agent.stuck`, provider/runtime/harness fields, hook/MCP/WASM errors |
| Abandonment | `agent.invocation.started` without finish, stop/worker exit without handoff, unread durable inbox message |
| Latency regression | OTel span duration, `tool.called.duration_ms`, watcher cycle duration, delivery attempts/backoff, queue depth |
| User interruption | stop-hook decisions, worker exit, session-end hook, process exit outcome |

### Example analysis queries

These examples assume a future normalized table named `exomonad_events`.

```sql
-- Invocations that never reached a finish event.
SELECT run_id, agent_id, invocation_id, MIN(timestamp) AS started_at
FROM exomonad_events
WHERE event_type = 'agent.invocation.started'
  AND NOT EXISTS (
    SELECT 1 FROM exomonad_events finished
    WHERE finished.event_type = 'agent.invocation.finished'
      AND finished.invocation_id = exomonad_events.invocation_id
  )
GROUP BY run_id, agent_id, invocation_id;

-- Review-related activity by architecture/runtime.
SELECT provider, runtime, harness, COUNT(*) AS review_events,
       COUNT(DISTINCT pr_number) AS prs
FROM exomonad_events
WHERE event_type IN ('copilot.review', 'ci.status_changed', 'agent.stuck')
GROUP BY provider, runtime, harness;

-- Delivery attempts that ended in abandonment.
SELECT recipient, COUNT(*) AS abandoned
FROM exomonad_events
WHERE event_type = 'agent_inbox.messages_abandoned'
GROUP BY recipient;
```

For the current files, a first-pass extractor should ingest:

1. standard JSONL under `.exo/logs/`;
2. lifecycle JSONL under `.exo/events/`;
3. OTel export data;
4. `.exo/logs/init.jsonl` and `watcher.log` as diagnostic sources;
5. `.exo/memory.db` and `.exo/inbox.db` as state snapshots or related tables;
6. historical sidecar files, if present, as immutable legacy evidence rather than a live event source.

Raw files should be retained alongside parsed rows so detector changes can be replayed.
Do not interpret absent rows as negative evidence without recording whether the relevant
sink was enabled, whether an EventLog write failed, and whether the process was alive.

## Current gaps affecting monitoring resolution

1. **Identity is incomplete in standard JSONL.** Rows lack a consistent `run_id`, role,
   provider/runtime, harness, birth branch, invocation ID, and trace/span IDs.
2. **There are multiple sinks and schemas.** `.exo/logs`, `.exo/events`, watcher text,
   init JSONL, SQLite, and OTel need an explicit ingestion map; the canonical ledger is
   the source for issue-close and inbox wakeups.
3. **Semantic duplicates are not normalized.** Haskell and Rust can both emit
   `agent.spawned` and `pr.merged` with different payloads.
4. **Important delivery signals are OTel-only.** Message sends, delivery attempts,
   deduplication, and abandonment are not reliably visible in local EventLog JSONL.
5. **Coverage is selective.** Not every hook, MCP request, effect, subprocess, queue
   transition, or provider tool action produces a durable event row.
6. **Writes are fail-open.** EventLog write failure produces a warning, so event absence
   and event negative evidence are ambiguous.
7. **In-memory state disappears.** Queue drops, retries, wakeups, and process-local
   deduplication cannot be reconstructed after restart from their current source alone.
8. **Lifecycle generation is not independent.** It currently mirrors invocation ID,
   limiting analysis of repeated generations.
9. **Guest timestamps are not honored.** `log.emit_event` uses host time even when the
   guest supplies a timestamp.
10. **The dashboard has a narrow view.** It reads direct `.exo/logs/*.jsonl` only and
    omits lifecycle, OTel, watcher text, and database state.

## Recommended canonical envelope

Keep the existing raw streams, then add a versioned envelope at ingestion or emission:

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "timestamp": "RFC3339 UTC",
  "run_id": "swarm-run-id",
  "trace_id": "optional",
  "span_id": "optional",
  "parent_span_id": "optional",
  "source": "rust|haskell|watcher|lifecycle|otel|sqlite|provider",
  "event_type": "agent.spawned",
  "agent_id": "agent",
  "birth_branch": "branch-at-creation",
  "role": "worker",
  "provider": "claude|codex|opencode|process",
  "runtime": "optional-runtime-name",
  "harness": "optional-harness-name",
  "invocation_id": "optional",
  "generation": 0,
  "issue_number": 0,
  "pr_number": 0,
  "head_sha": "optional",
  "outcome": "success|failure|pending|blocked|unknown",
  "duration_ms": 0,
  "attempt": 0,
  "authoritative": false,
  "data": {}
}
```

For migration, add fields without removing the current `ts`, `id`, `type`, `agent_id`,
and `data` envelope. Record `source` and a deterministic `dedup_key`; preserve raw
payloads. Distinguish at least these states:

```text
observed      provider/Forgejo/CI observation
emitted       ExoMonad produced a signal
delivered     signal reached a transport or inbox
consumed      recipient read/processed it
authoritative watcher/Forgejo or lifecycle source is considered workflow truth
```

This makes architecture comparisons possible: custom harnesses and Python execution
loops can be measured for spawn-to-start latency, tool/effect latency, delivery loss,
review-loop length, retry count, abandonment, and successful completion while retaining
the distinction between a missing observation and a failed workflow.

## Source map

- [`rust/exomonad/src/logging.rs`](../rust/exomonad/src/logging.rs) — tracing, file,
  stderr, MCP stdio, and OTel initialization.
- [`rust/exomonad-core/src/services/event_log.rs`](../rust/exomonad-core/src/services/event_log.rs)
  — standard JSONL envelope and append behavior.
- [`rust/exomonad-core/src/handlers/log.rs`](../rust/exomonad-core/src/handlers/log.rs)
  — Rust handlers for log effects and guest events.
- [`haskell/wasm-guest/src/ExoMonad/Effects/Log.hs`](../haskell/wasm-guest/src/ExoMonad/Effects/Log.hs)
  — guest logging effect declarations.
- [`rust/exomonad/src/serve.rs`](../rust/exomonad/src/serve.rs) — run ID, hook routing,
  server initialization, and active watcher startup.
- [`rust/exomonad/src/mcp_stdio.rs`](../rust/exomonad/src/mcp_stdio.rs) — MCP request
  spans and stdio diagnostics.
- [`rust/exomonad-core/src/services/worktree_event_watcher.rs`](../rust/exomonad-core/src/services/worktree_event_watcher.rs)
  — watcher observations and dispatch events.
- [`rust/exomonad-core/src/services/delivery.rs`](../rust/exomonad-core/src/services/delivery.rs)
  — message/guidance delivery telemetry.
- [`rust/exomonad-core/src/services/lifecycle.rs`](../rust/exomonad-core/src/services/lifecycle.rs)
  — invocation and guidance lifecycle events.
- [`rust/exomonad-core/src/services/session_memory.rs`](../rust/exomonad-core/src/services/session_memory.rs)
  — session-memory SQLite schema and kinds.
- [`rust/exomonad-core/src/services/inbox_store.rs`](../rust/exomonad-core/src/services/inbox_store.rs)
  — durable inbox state.
- [`rust/exomonad-core/src/protocol/mod.rs`](../rust/exomonad-core/src/protocol/mod.rs)
  — hook event protocol.
- [`CLAUDE.md#tempo-observability`](../CLAUDE.md#tempo-observability) — local Tempo/
  observability configuration.
