# ExoMonad Failure Atlas synchronization plan

Status: proposed implementation plan based on the ExoMonad logging inventory and the
local Failure Atlas method at commit `36efd36d` (2026-08-07).

## Anti-patterns

These are design constraints, not optional polish:

- Do not export transcripts, prompt text, tool-input text, tool-result text, thinking
  traces, environment variables, absolute paths, raw URLs, branch names, or arbitrary
  custom payloads by default.
- Do not treat a redacted event bundle as automatically safe. Event fields such as
  commit messages, PR titles, review comments, error strings, branch names, and custom
  `log.emit_event` payloads can still identify a person or project.
- Do not make `exomonad init` silently scan and rewrite old logs. Import must be explicit,
  resumable, idempotent, and read-only with respect to the source logs.
- Do not append normalized records back into legacy `.exo/logs/*.jsonl` files. Preserve
  the original stream and write imported data to a separate analysis store.
- Do not use a file count as a session count. ExoMonad must distinguish a top-level
  human/orchestrator session, a swarm run, an agent invocation, and a provider-side
  transcript or process.
- Do not pool raw event databases across machines. Share aggregate statistics and a
  declared schema/version manifest; retain raw and sensitive evidence locally.
- Do not introduce a runtime logging mode. Required workflow telemetry must be captured
  automatically in every ExoMonad session; it must not depend on `--verbose`,
  `EXOMONAD_HOOK_TRACE`, `EXOMONAD_CHAINLINK_TRACE`, or a provider-specific debug
  switch.
- Do not remove useful diagnostic control when decoupling telemetry. `--verbose` may
  control human-readable tracing volume, but it must not control required structured
  telemetry; no analysis-grade event may depend on `--verbose` or trace environment
  variables.
- Do not make an external telemetry collector a logging dependency. Local EventLog
  and the normalized SQLite store must be sufficient for capture, import, and analysis.
- Do not treat detector output as ground truth. Preserve detector scores and evidence
  separately from incidents, adjudication, and aggregate findings.
- Do not claim that GROUP BY provider/runtime/harness demonstrates an architecture
  improvement. Those axes can be confounded with task type, human presence, model tier,
  or assignment policy.
- Do not publish a rate without a declared unit, denominator, completeness status, and
  source/provenance record.
- Do not treat fail-open warnings as acceptable measurement behavior. A failed write must
  become a durable sink-health signal and make the affected session partial or unknown.
- Do not hand-type derived metrics, incident counts, precision values, or experiment
  results into a report. Every published number must be regenerated from source data.
- Do not infer a successful delivery, completion, or failure solely from a missing log
  row. EventLog writes are fail-open and some delivery signals are transport-only.
- Do not make an external collector a prerequisite for local analysis. The export path
  must work entirely from local structured logs.
- Do not silently collapse events from Haskell, Rust, provider hooks, watcher
  observations, and SQLite state into one indistinguishable source.

## Research findings

### What the Failure Atlas actually uses

The local Failure Atlas is a standard-library Python 3.9 pipeline backed by SQLite:

```text
transcript roots
      |
      v
extract.py       -> data/atlas.db (sessions, events, files)
      |
      v
detectors.py     -> signals
      |
      v
incidents.py     -> incidents + data/incidents.jsonl
      |
      v
analyze.py       -> data/analysis.json + FINDINGS-draft.md
      |
      +--> optional local adjudication via context_window.py
```

The scripts are `extract.py`, `detectors.py`, `incidents.py`, and `analyze.py`. They
open a local SQLite database, run SQL/Python aggregation, and produce JSON/JSONL
artifacts. There is no evidence in the checked-in Failure Atlas pipeline of a hosted
log aggregation service, a required telemetry vendor, DuckDB, or a cross-machine raw
event warehouse. ExoMonad's EventLog comment mentions DuckDB/Kaizen as possible query
consumers, but the Failure Atlas implementation itself uses SQLite and Python.

The shareable output is `analysis.json`: counts, rates, co-occurrence, positions,
outcomes, model-tier splits, and pointer-only exemplars. Pooling installations means
pooling these aggregate files under a common schema, not pooling the databases or
transcripts. This is explicitly described as an OMOP-style “common schema, local data,
shared statistics” model.

### What is feasible for ExoMonad

Yes. ExoMonad is better positioned than a transcript-only harness for a privacy-first
export because it already records structured workflow events. A useful first version
does not need transcript parsing:

1. import `.exo/logs/*.jsonl`, `.exo/events/*.jsonl`, `init.jsonl`, `watcher.log`, and
   selected SQLite state into a local normalized database;
2. classify each source and preserve source offsets and hashes locally;
3. run mechanical detectors over structured events and timing/topology;
4. emit aggregate-only `analysis.json` compatible with the Failure Atlas sharing model;
5. keep any raw or sensitive evidence in a local, non-exported store for optional
   adjudication.

This supports multi-harness orchestration because provider, runtime, role, parent/child
identity, lifecycle, delivery, PR, CI, and hook events can be normalized even when the
underlying agent does not produce Claude-style transcript JSONL.

The first release should promise privacy at the compile boundary, not by mutating the
capture ledger. The local ledger and analysis/state stores may retain sensitive evidence
for reconstruction, but the shareable artifact is generated only from allowlisted
dimensions and aggregates; no raw `data` or tracing message fields cross that boundary.

## Symbols and layer contracts

The plan uses these symbols consistently:

| Symbol | Definition |
| --- | --- |
| `L1` | Raw immutable ledger: append-only, local, and allowed to contain sensitive evidence |
| `L2` | Derived analysis store at `.exo/analysis/atlas.db`; rebuildable from `L1` and mutable as a view |
| `L3` | Mutable operational state: `.exo/session.json`, `.exo/memory.db`, `.exo/inbox.db`, and optional `.exo/sink-health.json` fallback |
| `L4` | Shareable artifact: `analysis.json`, manifest, and privacy report compiled from aggregates or samples |
| `S` | Sensitive data: transactions, conversation, reasoning, payloads, paths, secrets, and identifying source details |
| `A` | Allowlist dimensions: coarse, non-identifying fields approved for export |
| `Pb` | Privacy boundary enforced when compiling `L4` from local layers |
| `seq` | `run_seq`, monotonic event sequence assigned per run by the canonical appender |

The frozen layer laws are:

- `S` is allowed in local `L1`, `L2`, or `L3`; it is never a permitted
  shareable output.
- `S` ∩ `L4` = ∅.
- `Pb` sits between local `L1`/`L2`/`L3` and `L4`; privacy is
  enforced at compile time.
- `L4` = compile_python(`L2`) and `L4` = aggregate(`A`) only.
- `L1` is strict append-only; `L2` is rebuildable and may be updated or deleted as a
  derived view; `L3` is mutable but every mutation emits a matching `L1` event; `L4`
  is regenerated and never hand-edited.

## Target architecture

| Flow | Contract |
| --- | --- |
| `L1` ledger -> `L2` importer/replayer | Replay immutable segments into normalized, rebuildable analysis rows |
| `L3` state -> `L1` | Every session, memory, inbox, and lifecycle mutation emits a corresponding ledger event |
| `L2` -> `L4` | Python compile step crosses `Pb`, selects `A`, and emits aggregate/sample artifacts only |
| `L4` -> share/pool statistics | Only `analysis.json`, manifest, and privacy report leave the machine |

The importer is a separate read path. Existing `.exo/logs` and `.exo/events` files may remain
append-only compatibility streams, but the canonical evidence record is segmented `L1`. Historical
files are never rewritten; they are replayed into `L2`.

## Immutable ledger (L1)

`L1` is the complete local evidence record, stored in dated segments such as
`.exo/ledger/segments/`. It is strict append-only: rows are never edited or deleted in place,
including sensitive rows. The canonical appender writes bounded events to
the current open segment and is the only component allowed to assign `seq`. A correction
is represented by a new `event.superseded` event containing the superseded `event_id`, replacement
reference, reason, and provenance; readers resolve the newest valid interpretation without
rewriting the original.

Closed segments are immutable. Rotate by closing a segment at a configured size or time
limit and opening a new dated segment. Reclaim storage only by dropping complete expired
segments under the retention policy; never remove an individual row or rewrite a closed
segment. Record each segment drop as a new ledger event in the current segment, with
retention and operator metadata. Local access controls must acknowledge that `L1` can
contain `S`; crypto-shredding is optional local retention tooling, not a core
ledger-correctness requirement.

A missing sequence value or detected segment gap is a measurement failure: mark the
affected session `partial` (or `unknown` when the health evidence is also unavailable).
The gap detector must distinguish an absent row from an unavailable or failed sink.

### Frozen ledger and sharing invariants

- I1: `L1` rows are never edited or deleted in place.
- I2: `L1` corrections use a new `event.superseded` event, not an edit.
- I3: `seq` is monotonic; a missing sequence marks the session `partial` or `unknown`.
- I4: `L2` = f(`L1`) and is fully rebuildable from the ledger.
- I5: every `L3` change has a matching `L1` event, so state is reconstructable.
- I6: `L4` = compile_python(`L2` projected to `A`), with `S` ∩ `L4` = ∅.
- I7: the `Pb` test is allowlist-first: select `A` columns and never serialize then redact.
- I8: every `L4` number carries source hash, query, code, detector/method, and experiment provenance.

## Identity and session model

Failure Atlas defines a session as a top-level human-started run, while Claude transcript
files can be sidechains. ExoMonad currently has a project `.exo/run_id` that persists
across server restarts and resets on `init --recreate`; that is useful for swarm
continuity but is not sufficient as the only analysis identity.

Add or derive these dimensions in the normalized store:

| Dimension | Meaning | Source/compatibility rule |
| --- | --- | --- |
| `project_id` | Stable non-secret project/workspace identity | Explicit configured ID or locally salted hash; never export absolute path |
| `session_id` | Top-level human/orchestrator session | New session on a real fresh `init`; preserve on attach/restart |
| `run_id` | Swarm continuity across server restarts | Existing `.exo/run_id`; mark exact or inferred |
| `agent_id` | Stable ExoMonad agent identity | Event envelope/identity files |
| `invocation_id` | One provider/process invocation | Lifecycle events and provider launch metadata |
| `generation` | Monotonic resume/retry generation within an agent/session | Assigned by the lifecycle store for each resume/retry; never derived from invocation ID; required for loop and escalation measurement |
| `parent_agent_id` | Agent topology edge | Spawn/resume/lifecycle payload or inferred local relation |
| `provider` | Claude, Codex, Gemini, OpenCode, Shoal, or process | Lifecycle/init configuration |
| `runtime` | Provider runtime/hook protocol | Hook/MCP/init source |
| `harness` | User-selected/custom harness identity | New explicit metadata field; unknown for legacy rows |
| `source_session_id` | Upstream provider session/transcript identity | Local-only by default; hash if needed for deduplication |
| `run_seq` | Global monotonic event order within a run | Assigned by the canonical writer; required for ordering and event denominators |

Every row should carry an identity confidence such as `exact`, `inferred`, or
`unknown`. A legacy row without a run ID must not be assigned the current run ID merely
because it was found under the current project.

## Authoritative session boundary state

The session boundary must have one concrete source of truth. Store the authoritative
state in `.exo/session.json`; the normalized `sessions` table mirrors it for analysis.
The file contains at least:

| Field | Meaning |
| --- | --- |
| `session_id` | Stable UUID for the top-level session |
| `created_at` | Creation timestamp |
| `started_by` | User, service, or command that created the session |
| `init_mode` | `fresh`, `attach`, or `recreate` |
| `attach_mode` | `new` or `existing` |
| `recreate_generation` | Monotonic recreation count |

Use a file lock during init, attach, and server start. A fresh init creates a new
session; attach/restart preserves the existing `session_id`; recreate creates a new
session and increments `recreate_generation`. Never derive `session_id` from a path or
from `.exo/run_id`; `run_id` identifies swarm continuity, not the session boundary.

## Privacy-preserving export contract

### Export profiles (not runtime logging modes)

Runtime session logging is always-on. These are export policies that control what leaves
the local machine, not modes that control whether ExoMonad records events:

| Mode | Intended use | Contents |
| --- | --- | --- |
| `aggregate` | Compile the shareable `L4` artifact | `analysis.json`, schema manifest, detector/version metadata, cohort counts, rates, and pointer-only samples |
| `events` | Internal local debugging or reproducibility | Allowlisted normalized events and pseudonymous IDs; never an `L4` shareable artifact |
| `local` | Machine-local analysis/adjudication | Full `L1`/`L2` evidence and source pointers; never treated as shareable |

The default and safest export profile is `aggregate`. `events` requires an explicit
warning and local destination; it is not a way to publish event rows. `local` describes
local evidence and should not produce a portable archive. None of these profiles may
disable live session logging.

### Always-on session instrumentation

Every ExoMonad session should automatically capture the structured events needed for
workflow reconstruction. The implementation should:

1. emit required lifecycle, topology, tool/effect, transport, review/CI, hook, timing,
   and state events under the normal server configuration;
2. remove the current dependency on `EXOMONAD_HOOK_TRACE` and
   `EXOMONAD_CHAINLINK_TRACE` for events required by the analysis contract;
3. keep `--verbose` available for human-readable tracing volume only; decouple it from
   required structured telemetry and deprecate any behavior where it enables
   analysis-grade events;
4. keep optional human-readable tracing narrowly scoped to developer diagnostics, never
   as the source of truth for session analysis; no required event may depend on
   `--verbose` or trace environment variables;
5. ensure the structured `L1` ledger sink is local and fail-visible, with a durable sink
   health/error signal when an event cannot be written and a fallback health path outside
   the failed event append;
6. record provider/runtime/harness/role/session/invocation identity automatically when
   available, using explicit `unknown` values rather than dropping the event;
7. keep the local `L1` segmented ledger and normalized `L2` store sufficient by design.

The desired operator experience is simply: start an ExoMonad session. Each session
emits the required structured telemetry when the writer is available. If the writer is
unavailable, durable sink-health state marks the session `partial` or `unknown`; it is
not silently treated as complete. There is no “logging enabled” choice or second
verbose run required to make a session measurable.

### Aggregate export schema

The shareable `analysis.json` should follow the Failure Atlas shape where useful:

```json
{
  "schema_version": 1,
  "producer": "exomonad",
  "generated_at": "2026-08-10T00:00:00Z",
  "method_revision": "failure-atlas-method-2026-08-07",
  "cohort": {
    "sessions": 0,
    "agents": 0,
    "invocations": 0,
    "events": 0,
    "incidents": 0,
    "time_start": null,
    "time_end": null
  },
  "dimensions": {
    "provider": {"claude": 0, "codex": 0},
    "runtime": {},
    "harness": {},
    "role": {},
    "topology": {}
  },
  "detectors": {},
  "incidents_by_mode": {},
  "cooccurrence": {},
  "outcomes": {},
  "latency": {},
  "measurement": {
    "primary_unit": "session",
    "secondary_units": ["invocation", "generation"],
    "denominators": {},
    "completeness": {},
    "contrast": {}
  },
  "provenance": {
    "source_manifest_hash": "...",
    "query_revision": "...",
    "detector_revision": "..."
  },
  "privacy": {
    "contains_transcripts": false,
    "contains_thinking": false,
    "contains_paths": false,
    "contains_raw_event_payloads": false,
    "contains_stable_source_ids": false
  }
}
```

The exact field set should be finalized as a versioned contract. Counts must include
denominators and cohort definitions. For example, “review-loop rate” must say whether
the denominator is sessions, PRs, agents, or events.

### L4 compile step contract

`L4` is compiled by a Failure Atlas-style Python builder from an allowlisted `L2` view
(or from `L1` replayed into `L2`). The compiler:

1. reads only columns in `A` through an explicit allowlisted view;
2. computes counts, rates, co-occurrence, latency buckets, and pointer-only exemplars;
3. draws a stratified sample only when exemplars are needed, storing pointers rather than text;
4. runs the denylist scanner after projection as a backstop;
5. emits `analysis.json`, the schema manifest, and the privacy report; and
6. fails the build if any class of `S` appears in `L4`.

### Sensitive-data policy

Sensitive data `S` is intentionally allowed in local `L1`, `L2`, and `L3` for
reconstruction and adjudication. The privacy boundary `Pb` is the compile step, not capture.
The aggregate exporter is allowlist-first: it selects only explicitly allowed columns from
normalized tables and never serializes raw rows before applying redaction. The denylist
scanner runs only after allowlist projection as a backstop.

Allowlist aggregate dimensions: event type, detector type, outcome class, status class,
coarse provider/runtime/harness/role, duration buckets, attempt buckets, topology
counts, and normalized error categories. Exclude by default:

- transcript text and tool input/result text;
- Haskell `custom` payloads and arbitrary `log.emit_event` payloads;
- `agent.notify_parent.message`, watcher comments/reviews, PR titles, and error text;
- absolute paths, repository URLs, remote names, branch names, commit messages, and
  raw SHAs;
- tokens, headers, environment values, usernames, email addresses, and hostnames;
- external span attributes not explicitly listed in the export profile;
- thinking/reasoning content, even if it is available locally.

For `events` mode, use keyed pseudonyms for project/session/agent/invocation IDs. Keep
the key local and never export it. Hashing without a secret is not sufficient because
IDs can be guessed or dictionary-attacked.

Every export should include a machine-readable privacy manifest and a validation report:
row count, field count, dropped-field counts, redaction ruleset version, and whether
any denylist detector found a secret-like value. Test fixtures must include branch names,
absolute paths, URLs, fake tokens, and arbitrary custom payloads. The exporter must
prove that those values cannot enter aggregate output through an unallowlisted column.

## Legacy import and coexistence plan

### Recommendation: dedicated command first

Add an explicit command family such as:

```text
exomonad logs import --source .exo/logs --source .exo/events
exomonad logs import --source /path/to/old/project/.exo --format auto
exomonad logs analyze --db .exo/analysis/atlas.db --out analysis.json
exomonad logs export --db .exo/analysis/atlas.db --mode aggregate --out analysis.json
```

Make import the primary interface because it is inspectable, can report exactly what it
will read, can be rerun safely, and does not make every `init` slower or surprising.

`exomonad init` may later provide an opt-in convenience flag such as
`--import-logs <path>` or `--import-legacy-logs`, but it should call the same importer
and print a dry-run summary before writing. A normal `init` should only initialize a
project and report that an analysis database is absent or stale.

### Source formats

The importer should recognize these formats independently:

1. current/legacy EventLog JSONL: `ts`, `id`, `type`, `agent_id`, `data`;
2. lifecycle EventLog JSONL under `.exo/events`;
3. `init.jsonl` records;
4. `watcher.log` human-readable diagnostics, with best-effort parsing and an explicit
   low-confidence source type;
5. `.exo/memory.db` session-memory rows;
6. `.exo/inbox.db` durable inbox rows;
7. custom `issue_closed.jsonl` rows.

Do not pretend that `sidecar.log` is a stable machine schema. It can be imported as a
diagnostic source with parser version and confidence, but structured EventLog records
should be preferred.

### Import database

Create a separate project-local store such as `.exo/analysis/atlas.db` with tables:

```text
sources(
  source_id, path_or_label, source_kind, file_size, mtime, content_hash,
  parser_version, imported_at, status, rows_read, rows_rejected
)

segments(
  segment_id, source_id, opened_at, closed_at, seq_start, seq_end,
  content_hash, status, dropped_at
)

sessions(
  session_key, project_id, session_id, run_id, start_ts, end_ts,
  source_count, event_count, agent_count, provider_set, runtime_set,
  harness_set, identity_confidence
)

events(
  event_key, source_id, source_offset, event_time, observed_at, run_seq,
  session_id, run_id, agent_id, parent_agent_id, invocation_id, generation,
  role, provider, runtime, harness, event_type, outcome, duration_ms, attempt,
  issue_number, pr_number, head_sha_hash, payload_class, lifecycle_state,
  sink_status, identity_confidence, local_payload_json,
  superseded_by, supersession_reason
)

supersessions(
  event_key, superseded_by, reason, correction_source, corrected_at
)

signals(
  event_key, detector, score, evidence_class, detector_version
)

incidents(
  incident_id, session_id, start_ts, end_ts, primary_mode,
  detector_set, severity, adjudication_status, source_pointer
)

import_errors(source_id, source_offset, error_class, detail_hash)
```

`local_payload_json` and `source_pointer` stay local. The aggregate exporter must not
select them. `head_sha_hash` is optional and must use a local keyed hash if exported at
all; raw SHAs are not needed for aggregate comparison.

### Local payload retention

### L1 segment retention and local payload policy

`L1` evidence, including `local_payload_json`, is retained in immutable segment files rather
than deleted row-by-row. Define a configurable retention period, segment size/time
rotation limits, and a local access-control policy. Provide an explicit command such as
`exomonad logs drop-segments` with dry-run, segment fingerprints, retention reason, and an
auditable deletion summary.

The retention procedure is:

1. append `L1` events to dated segment files;
2. close a segment at its size or time limit;
3. never rewrite a closed segment;
4. reclaim space only by dropping whole expired segments; and
5. record each segment drop as an `L1` event in the current segment.

Optional crypto-shredding of a segment key may accelerate local privacy deletion, but it
is not required for ledger correctness. Aggregate export must never scan or serialize
`local_payload_json`.

### Idempotence and old/new interaction

The importer must:

1. fingerprint each source using canonical path label, size, mtime, and content hash;
2. skip an unchanged source on rerun;
3. reprocess a changed source into a new parser revision without duplicating logical
   events;
4. derive a stable legacy event key from source ID, byte/line offset, and canonical
   row content when the old UUID is absent or duplicated;
5. keep `raw_schema_version=0` for the current envelope, because old EventLog rows have
   no schema version;
6. preserve invalid JSON lines and parser errors in `import_errors`;
7. never modify or rename source files;
8. emit a manifest showing exactly which sources and rows were included.

New ExoMonad live evidence should append to segmented `L1` ledger files. Existing
`.exo/logs` and `.exo/events` streams may be retained as append-only compatibility
outputs, but they are not rewritten and are not the source of truth. Imported rows must
not be copied into those directories. `L2` remains the compatibility layer where legacy and
new rows are normalized together.

### Live schema evolution

For new EventLog rows, add fields compatibly to the existing envelope:

```json
{
  "schema_version": 1,
  "ts": "RFC3339 event time",
  "id": "uuid",
  "event_id": "uuid",
  "event_time": "source or guest timestamp",
  "observed_at": "host ingest timestamp",
  "run_seq": 12345,
  "type": "agent.spawned",
  "agent_id": "...",
  "run_id": "...",
  "session_id": "...",
  "invocation_id": "...",
  "generation": 0,
  "source": "rust",
  "lifecycle_state": "emitted",
  "data": {}
}
```

Readers must accept `schema_version` absent and treat it as legacy. Do not require
every old row to contain new identity fields. Emit provider/runtime/harness, role,
sequence, and lifecycle fields where known; use explicit null/unknown values rather
than guessed identity. The single `lifecycle_state` field replaces a separate
`authoritative` boolean. Its values are `observed`, `emitted`, `delivered`,
`consumed`, or `authoritative`.

`event_id` is the canonical identifier. `id` is the legacy compatibility alias; for new rows,
`id` == `event_id`. Readers prefer `event_id` when present. Legacy import may map a
unique legacy `id` to `event_id` while recording its confidence and source revision.

### Schema registry and emitter conformance

Maintain a versioned registry such as `docs/observability/event-registry.json` with each
event type, required envelope fields, payload classification, lifecycle-state values,
producer sources, and additive compatibility rules. CI must exercise every emitter
family: Rust handlers, Haskell effects, lifecycle, watcher, MCP, hooks, and custom
events. Unknown dynamic events must use a declared namespace and pass the same privacy
and envelope checks.

### Ordering, completeness, and writer integrity

The canonical appender is the only `run_seq` allocator. It may use a SQLite sequence,
a locked counter file under `.exo`, or a single writer task, but independent processes may
never allocate sequence values. Timestamps are split into `event_time` (the source/guest
time, honored when supplied) and `observed_at` (the host ingestion time). Sequence
is authoritative for ordering when clocks disagree.

Every sink must expose durable health data: accepted event count, rejected event count,
write-failure count, last successful sequence, and measurement status
(`complete`, `partial`, or `unknown`). A quantified cohort must include these
fields; otherwise its rates are censored or excluded rather than silently treating missing rows
as zero. A missing `run_seq` creates a detectable gap and marks the session partial.

Normal `L1` writes use one canonical appender and append-only segment files. Corrections
use `event.superseded`, never UPDATE or DELETE on a ledger row. `L2` transactions may update
or delete derived rows because the view is rebuildable. `L3` mutations must emit matching
`L1` state-change events.

If the event sink cannot record its own failure, write sink health to `.exo/sink-health.json`
(or the locked health fields in `.exo/session.json`) and mirror it into `L2` during
import. If both event and health sinks fail, the next startup marks the session
`unknown` rather than assuming completeness. Oversized rows must be bounded or stored
through local pointers; add concurrent-writer tests, including rows over 4 KB.
Correct the stale EventLog module comment and test the actual per-agent-file layout.

### Expected-event denominator contract

The expected-event denominator must be generated from a deterministic, versioned
transition contract rather than analyst judgment:

| Transition | Required events |
| --- | --- |
| spawn | `agent.spawned`, `agent.invocation.started` |
| finish | `agent.invocation.finished` |
| delivery attempt | `message.delivery` |
| consumed inbox item | `message.consumed` |
| PR observed | `watcher.pr_observation` |
| PR merged | `pr.merged` |

A contract version records which prerequisite transition makes each event expected.
Missing required events are gaps, not zeros; legacy rows use `unknown` when the source
cannot establish that a transition occurred. Denominator generation, contract version,
and excluded/unknown transitions must be stored beside every rate.

Maintain the machine-readable contract at
`docs/observability/expected-events.v1.json`. Each rule defines the prerequisite event,
required event, allowed delay/window, applicable source, legacy-confidence rule, and
denominator effect. CI fixtures must exercise each rule and reject subjective expected
counts.

## Multi-harness aggregation model

The common unit is not a Claude transcript. It is a normalized orchestration event and
its position in an agent/session graph.

### Event classes

Start with mechanical, cross-harness classes:

| Class | Examples |
| --- | --- |
| lifecycle | invocation started/finished, guidance delivery, worker exit |
| topology | agent spawned/resumed, parent notification, sibling merge, stuck |
| tool/effect | tool call result, process failure, WASM effect failure, MCP request |
| transport | send, delivery attempt, deduplication, abandonment, inbox read |
| review/CI | PR filed/updated/published/merged, review, CI status, dispatch |
| hook | hook received, stop decision, model/tool hook outcome |
| timing | span duration, poll cycle, retry/backoff, queue depth |
| state | memory append, inbox state, issue closed, session summary |

This lets Claude, Codex, Gemini, OpenCode, Shoal, custom harnesses, and Python loops
contribute comparable rows without pretending their transcripts have the same format.

### Cross-agent graph

Build a graph keyed by `session_id`, `run_id`, `agent_id`, `invocation_id`, and
`parent_agent_id`. Derive:

- fan-out and fan-in counts;
- spawn-to-invocation-start latency;
- invocation-to-finish and finish-to-parent-notification latency;
- delivery attempts, duplicate suppression, unread and abandoned messages;
- PR/review/CI loops across agents;
- orphaned children and invocations without finish/handoff;
- harness/provider/runtime transitions within one swarm.

Do not merge agents by display name alone. The current EventLog file name is based on a
sanitized agent ID, so the importer must treat the envelope ID and lifecycle metadata as
the identity source and file name as only a source hint.

### Failure Atlas-compatible detectors

Port the Failure Atlas discipline, not its Claude-specific assumptions:

1. extract and normalize raw local sources into a versioned event store;
2. run high-recall detectors over normalized events;
3. store one signal row per event/detector with detector version, score, evidence class,
   source pointer, and completeness context;
4. cluster signals into incidents within a declared event/time window, separately by
   session and optionally across a parent/child graph;
5. adjudicate a deterministic, stratified sample per detector locally; publish precision
   with Wilson 95% intervals and label/judge revisions;
6. analyze only after signal, incident, and adjudication artifacts exist, preserving
   denominators and cohort definitions;
7. use confirmed, avoidable incidents to propose countermeasures, then evaluate them in a
   new preregistered contrast rather than claiming the countermeasure worked immediately.

Initial ExoMonad detectors should include:

- `delivery_loss` — sent/delivered/consumed/abandoned mismatch;
- `error_loop` — repeated failed tool/effect/process events;
- `review_loop` — repeated review/CI cycles without merge or completion;
- `orphaned_invocation` — started invocation without finish/handoff;
- `fanout_pressure` — abnormal child count or queue pressure;
- `harness_switch_friction` — requested/disallowed switch or repeated runtime failure;
- `latency_regression` — bucketed stage duration above cohort baseline;
- `session_abandonment` — terminal stop/exit with no completion or handoff;
- `duplicate_semantic_event` — same logical transition emitted by multiple sources;
- `event_sink_gap` — expected lifecycle edge absent while a process/transport signal exists.

These detectors can work without transcript content. Text-based detectors such as
frustration or honesty challenges should remain a separate, opt-in local adapter for
providers that expose transcript data; they should not be part of the shareable ExoMonad
aggregate contract.

## Measurement protocol

### Units and estimands

Use the top-level human/orchestrator `session` as the primary unit for architecture
claims. Use `invocation` and real monotonic `generation` as secondary units for runtime
and retry questions. Use events only to define event-density or completeness
denominators. Never mix session, invocation, generation, PR, and event denominators in
one rate.

### Preregistered controlled contrasts

Before collecting treatment data, create an experiment manifest containing:

- the architecture change, implementation revision, harness/provider configuration, and
  experiment arm;
- one primary outcome, its direction, its unit, its denominator, and its target effect;
- secondary outcomes, exclusions, missingness rules, detector/method revisions, and
  analysis queries;
- task allocation, sample-size or stopping rule, baseline window, and treatment window;
- allowed covariates and a rule for handling incomplete or sink-partial sessions.

A baseline snapshot is required before each architecture change. Prefer randomized arm
assignment at session start. When randomization is unsafe, use blocked or matched
contrasts on task type, initial complexity, human-presence condition, model tier,
repository class, and topology shape. A shadow-mode treatment is acceptable when it is
isolated from outcomes, but it still requires the same preregistered cohort and metrics. Keep task mix and human-presence policy fixed
across arms. Provider/runtime/harness GROUP BY tables are descriptive slices, not causal
comparisons, unless the manifest explicitly controls the assignment mechanism.

### Named outcomes

The first measurement contract should define:

- session completion: completed eligible sessions / eligible sessions;
- delivery reliability: consumed required deliveries / attempted required deliveries;
- error-loop incidence: sessions or invocations with a confirmed error loop / eligible
  sessions or invocations;
- review-loop incidence: PRs with a confirmed review loop / eligible PRs;
- abandonment: abandoned eligible sessions / eligible sessions;
- stage latency: median and p95 spawn-to-start, delivery, handoff, and finish latency;
- event completeness: accepted canonical events / expected canonical events, with sink
  health and partial-session status reported beside it.

Report absolute effect, relative effect, denominator, and uncertainty. Use Wilson intervals
for proportions and a declared bootstrap or quantile method for latency. A GROUP BY
without an identified contrast is not evidence of improvement.

### Signal, incident, and adjudication stages

Mechanical detectors are high-recall candidate generators. Incidents are clusters of
signals. Adjudication is the local validity check that estimates detector precision and
agent-fault/avoidability labels. Semantic detectors must not be treated as trustworthy
without this step; low precision is an expected result, not a pipeline failure.

Record the judge model, prompt revision, label schema, sampling seed, and adjudication
method. Use at least two independent judges or coders for published precision when
possible; single-judge precision must be labeled provisional and must not be presented
as an unbiased truth estimate.

### Provenance and anti-fabrication

Every aggregate number must carry a source-manifest hash, database content hash, query
revision, code revision, detector/method revisions, cohort/contrast manifest, and
generation timestamp. Outputs are generated artifacts, never hand-edited reports.
Regeneration CI must fail on unexplained changes and must preserve the query/source path
for every published number.

## MVP implementation cut

The full plan is intentionally staged. Do not implement all phases at once; each MVP
must produce a usable artifact before the next dependency is started.

| MVP | Deliverable | Work packages | Exit gate |
| --- | --- | --- | --- |
| MVP-A | `L1` ledger format, rebuildable `L2` store, and legacy importer | WP1, WP7, WP8 | Old and current sources replay without mutation; schema, parser, and rebuild tests pass |
| MVP-B | Live envelope fields, authoritative session state, `seq`, sink health, and `L3` mirrors | WP2, WP4, WP6 | New sessions have identity, ordering, health, and complete/partial/unknown records |
| MVP-C | Corrections, segment retention, durable delivery/inbox outcomes, and expected-event contracts | WP3, WP5 | Corrections are events, segment tests pass, and denominator counts reconcile |
| MVP-D | Python `L4` compiler, sample-only export, privacy, and provenance | WP9, WP10, WP11 | `analysis.json` contains only `A` and passes privacy/provenance tests |
| MVP-E | Detectors, incidents, adjudication, and controlled contrasts | WP12, WP13 | Signal-to-incident-to-adjudication output is reproducible before effect claims |

The dependency order is MVP-A -> MVP-B -> MVP-C -> MVP-D -> MVP-E. Architecture-effect
claims remain gated until the MVP that supplies their measurement prerequisites is
complete; logging plumbing alone is not an effect-measurement release.

## Work package decomposition

Each work package has a disjoint primary write scope where practical and can be assigned
independently after its dependencies are accepted:

| Work package | Scope | Depends on |
| --- | --- | --- |
| WP1 | `L1` immutable ledger format and segment writer | none |
| WP2 | `seq` allocator owned by the canonical appender | WP1 |
| WP3 | `event.superseded` correction events and reader resolution rule | WP1, WP2 |
| WP4 | Sink health, fallback health path, and gap detection | WP2 |
| WP5 | `L1` segment rotation, retention, and drop events | WP1 |
| WP6 | `L3` state changes mirrored as `L1` events | WP1, WP3 |
| WP7 | `L2` importer/replayer builds a view from `L1` | WP1, WP3 |
| WP8 | `L2` idempotence, reprocess, and `import_errors` | WP7 |
| WP9 | Python `L4` compiler: allowlist-first aggregate and sample | WP7 |
| WP10 | Privacy report, denylist backstop, and sensitive fixtures | WP9 |
| WP11 | Provenance stamping on `L4` | WP9 |
| WP12 | Detectors, incidents, and adjudication over `L2` | WP7 |
| WP13 | Measurement contract and preregistered contrast gate | WP9, WP12 |

WP1-WP6 own capture, WP7-WP11 own import/compile, and WP12-WP13 own analysis.
The first implementation wave starts WP1, then starts WP2 and WP7 as soon as the
`L1` interface is frozen; disjoint scopes may proceed in parallel. Do not publish
architecture-effect claims before MVP-E and the measurement-ready gate.

## Phased implementation plan

### Phase 0 — contract, experiment preregistration, and fixtures

1. Freeze the common envelope, `L1`/`L2`/`L3`/`L4` contracts, schema and
   expected-event registries, identity vocabulary, event taxonomy, export privacy profiles,
   and `analysis.json` compatibility version.
2. Define the primary/secondary units, estimands, denominators, missingness rules, and
   controlled-contrast protocol before implementation claims are made.
3. Define session/run/invocation boundaries for fresh init, attach, restart, resume, and
   `init --recreate`; make real monotonic generation a prerequisite. Make
   `.exo/session.json` authoritative, lock it during lifecycle transitions, and mirror
   its state into the analysis store.
4. Freeze `docs/observability/expected-events.v1.json` and the rule that expected counts are
   generated from observed prerequisites, never selected subjectively.
5. Create synthetic fixtures representing Claude plus at least two other harnesses,
   segment rotation, supersession, segment gaps, `L3` state changes, and a failed
   health fallback, in addition to multiple agents, resumed invocation, delivery retry,
   duplicate suppression, abandonment, review loop, partial sink failure, concurrent
   writers, and an old legacy log.
6. Register baseline and treatment manifests with task-mix, human-presence, model-tier,
   and topology confound controls.
7. Record invariants: no raw payload in aggregate output, stable deduplication, source
   immutability, deterministic aggregate output for a fixed input, unknown identity
   preservation, sequence monotonicity, and denominator conservation.
8. Add a hard “measurement-ready” gate: no architecture-effect claim is published until
   generation, durable delivery rows, sink health, sequence/timestamp, provenance, and
   conformance tests pass.

### Phase 1 — capture ledger, normalization store, and legacy importer

1. Implement WP1: define the strict `L1` segment format, bounded event envelope, segment
   manifest, and canonical append API; never edit or delete a ledger row in place.
2. Implement WP2 and WP4: make the canonical appender own `seq`, write fallback sink health,
   and detect sequence/segment gaps as `partial` or `unknown`.
3. Implement WP7: add `.exo/analysis/atlas.db` migrations and parameterized inserts for
   the rebuildable `L2` view, including sequence, timestamp, lifecycle, sink-health,
   completeness, and supersession tables.
4. Implement source discovery and parser/replayer adapters for `L1` segments, legacy
   EventLog/lifecycle/init JSONL, watcher logs, memory DB, inbox DB, and issue-closed JSONL.
5. Add `exomonad logs import` with explicit source paths and `--format auto`; preserve
   source immutability, manifests, parser versions, dry-run, resume, idempotence, and
   `import_errors`.
6. Implement WP8 reprocessing and rebuild checks. `L2` may update or delete derived rows,
   but a clean rebuild from the same `L1` input must produce the same view.
7. Bound event rows, preserve oversized local evidence by segment/pointer, and add
   concurrent-writer, over-4KB-row, malformed-line, segment-gap, and denominator tests.
8. Correct the stale EventLog `.exo/events.jsonl` comment and test actual compatibility
   layouts under `.exo/logs/{agent_id}.jsonl` and `.exo/events/{agent_id}.jsonl`.

### Phase 2 — new live metadata and measurement prerequisites

1. Make required structured telemetry unconditional for every session. Keep `--verbose`
   available for human-readable tracing volume only; no required structured event may
   depend on `--verbose` or trace environment variables.
2. Add additive `schema_version`, `source`, `event_id`, `session_id`,
   `run_id`, `invocation_id`, `generation`, `run_seq`,
   `event_time`, `observed_at`, provider/runtime/harness/role, and one
   `lifecycle_state` field to new `L1` rows; set `id` equal to `event_id`.
3. Preserve compatibility with readers that only understand `ts`, `id`, `type`,
   `agent_id`, and `data`; readers prefer `event_id` when present.
4. Make generation a real monotonic counter for each agent/session resume or retry;
   invocation ID remains unique per process and is never used as generation.
5. Mirror `message.delivery`, `agent_inbox.duplicates_dropped`,
   `agent_inbox.messages_abandoned`, inbox notification/read, and parent
   notification outcomes into durable `L1`/`L2` rows, not only optional telemetry.
6. Use one canonical appender to allocate `seq`; no independent process may assign
   `run_seq`. Honor guest/source timestamps while retaining host observation time.
7. Add durable sink-health and write-failure counters. On event-write failure, update
   `.exo/sink-health.json` (or locked `.exo/session.json` health fields) and mirror it
   into `L2`. If both sinks fail, mark the next startup `unknown`; exclude
   unmeasurable rows from silently becoming denominator zeros.
8. Emit a matching `L1` state-change event for every `L3` mutation, including
   session, memory, inbox, lifecycle, and attach/restart state.
9. Add the schema registry, `docs/observability/expected-events.v1.json`, and emitter-conformance
   tests for Rust, Haskell, watcher, lifecycle, MCP, and custom-event emitters.
10. Gate architecture-effect analysis until all preceding prerequisites are green.

### Phase 3 — privacy-safe export and aggregation

1. Add `exomonad logs analyze` to populate detector/signal/incident tables in `L2`
   locally; it may rebuild or replace derived rows.
2. Add `exomonad logs export --mode aggregate` as the Python `L4` compiler. It reads
   only an allowlisted `L2` view, emits `analysis.json`, manifest, method revision,
   cohort/contrast definitions, completeness status, pointer-only samples, and privacy report.
3. Keep `--mode events` local-only for explicit internal debugging. It is never `L4`,
   never uploaded, and must not read `local_payload_json` unless the destination is
   explicitly local and access-controlled.
4. Add output validation tests that fail if any class of `S` appears in `L4`. Select
   `A` columns before serialization; run the denylist scanner only after projection, with
   fixtures for branches, paths, URLs, fake tokens, and custom payloads.
5. Make aggregate files mergeable by schema version and cohort, not by raw session ID.
6. Include source-manifest, query, code, detector, method, and experiment provenance in
   every generated artifact; prohibit hand-edited metric outputs.
7. Exclude or explicitly label partial/unknown sessions and preserve accepted-event,
   expected-event, and sink-failure denominators beside each rate.
8. Document a manual upload/share step; do not add network upload to the first release.

### Phase 4 — signals, incidents, adjudication, and countermeasures

1. Implement the initial mechanical detectors above with versioned evidence classes and
   high-recall behavior.
2. Implement clustering with explicit event/time windows and cross-agent graph rules;
   preserve every detector signal and derive primary mode only for presentation.
3. Emit local incident JSONL with pointers only; keep local adjudication inputs separate.
4. Draw deterministic stratified adjudication samples per detector; record judge model,
   prompt revision, label schema, sampling seed, labels, and coder revisions; compute
   precision with Wilson 95% intervals. Use two independent judges where possible and
   mark single-judge precision provisional.
5. Keep semantic detectors local until their precision, agent-fault, and avoidability
   labels are measured; never promote a lexical signal directly to a causal finding.
6. Add cohort slices for provider, runtime, harness, role, topology shape, and architecture
   version as descriptive views only; use the preregistered contrast for effects.
7. Turn confirmed avoidable clusters into explicit countermeasure candidates, then measure
   them in a new baseline/treatment contrast with the same protocol.

### Phase 5 — always-on session startup and opt-in legacy import convenience

1. Have normal `exomonad init` start the required structured logging contract
   automatically and report writer availability and sink health. `--verbose` may still
   adjust human-readable diagnostics, but it must not gate structured telemetry.
2. Have normal `exomonad init` report analysis-store status without importing.
3. Add an explicit `--import-logs` convenience path that invokes the dedicated importer
   in dry-run then apply mode.
4. Ensure attaching to an existing tmux session does not create a duplicate session or
   re-import unchanged sources.
5. Show a concise summary: sources discovered, rows imported, rows skipped, errors,
   inferred identities, and privacy status.

### Phase 6 — compatibility with open-science

1. Add a documented adapter from ExoMonad aggregate output to the Failure Atlas display
   schema, or keep ExoMonad output a declared extension if dimensions differ.
2. Run the Failure Atlas synthetic smoke-test philosophy against multi-harness fixtures.
3. Compare aggregate results only through preregistered controlled contrasts with fixed
   task mix, human-presence policy, model-tier controls, and explicit denominators; treat
   provider/runtime/harness GROUP BY tables as descriptive.
4. Publish only aggregate artifacts and method/version metadata; retain source databases,
   event bundles, adjudication windows, and thinking traces locally.
5. Record discrepancies between the ExoMonad event taxonomy and the Failure Atlas
   detector/incident taxonomy rather than forcing a lossy one-to-one mapping.

### Optional cross-session collector aggregation

A collector such as OTel is not ExoMonad's logging sink. It is only an optional
aggregation bridge when several ExoMonad sessions already export compatible spans. If
used, the bridge should:

- carry the normalized `session_id`, `run_id`, `agent_id`, and `invocation_id` as
  correlation attributes;
- supplement, never replace, local EventLog/SQLite aggregation;
- preserve source, provider, runtime, harness, and identity-confidence fields;
- support cross-session latency, delivery, topology, and outcome comparisons; and
- leave the complete local workflow functional when no collector exists.

## Verification plan

### Append-only ledger correctness

- V1: attempt to edit an `L1` row and confirm the writer rejects it; closed segments remain
  byte-identical.
- V2: attempt to drop one `L1` row and confirm only `event.superseded` can correct an
  interpretation.
- V3: remove or hide one `L1` segment during a run and confirm the `seq` gap marks the
  session `partial` (or `unknown` when health evidence is unavailable).
- V4: rebuild `L2` from `L1` twice and confirm byte-equivalent normalized views.
- V5: replay `L3` state-change events from `L1` and confirm session, memory, and inbox
  state can be reconstructed.
- V6: compile `L4` from an `S`-laden `L1`/`L2` fixture and confirm
  `L4` contains no `S` classes.
- V7: resolve every `L4` number to a source manifest, query, code/method revision, and
  reproducible source path.
- V8: drop an expired segment and confirm the drop is itself recorded as an `L1` event
  in the current segment and appears in the retention audit.

### Import correctness

- Import the same source twice; row and event counts must remain unchanged.
- Modify one source; only that source gets a new fingerprint/parser revision.
- Include malformed JSON and confirm import continues with an `import_errors` row.
- Import old EventLog rows lacking schema/version/run identity and confirm they remain
  `legacy`/`unknown`, not falsely assigned current identity.
- Confirm source files, `.exo/logs`, `.exo/events`, `.exo/memory.db`, and `.exo/inbox.db`
  timestamps/content are unchanged.

### Privacy correctness

- Run aggregate export against fixtures containing fake secrets, paths, URLs, branch
  names, review text, tool inputs, and custom payloads.
- Assert none occur in `analysis.json`, manifest, or privacy report except approved
  coarse categories.
- Assert thinking lengths may be counted locally but thinking content never exports.
- Verify event-mode warnings and redaction profiles are explicit and testable.

### Multi-harness correctness

- Replay equivalent workflows from Claude, Codex, Gemini, and a custom Python loop.
- Confirm the normalized graph retains provider/runtime/harness distinctions while
  producing comparable stage metrics.
- Confirm parent/child ordering, monotonic generations, durable delivery retries,
  deduplication, abandonment, and PR review loops are reconstructed from multiple files.

### Measurement completeness and writer integrity

- Force `L1` write failures and confirm the fallback health file or locked session state
  records the failure; if both fail, next startup marks the session `unknown` rather than
  silently reducing the denominator.
- Exercise fresh init, attach, restart, and recreate under the session-state lock; confirm
  the authoritative `.exo/session.json` fields and session IDs are stable or recreated
  according to contract.
- Replay every expected-event transition and confirm deterministic expected counts,
  explicit contract versions, and `unknown` status where legacy evidence cannot establish
  the prerequisite transition.
- Confirm `run_seq` is monotonic, `event_time` honors supplied guest timestamps, and
  `observed_at` remains available for ingest ordering.
- Run concurrent writers with oversized rows and confirm no interleaved or unparsable
  JSONL records.

### Detector validity

- Run signal -> incident -> adjudication -> aggregate as separate reproducible stages.
- Compute per-detector precision and Wilson intervals from deterministic samples.
- Confirm adjudication artifacts record judge model, prompt revision, label schema, and
  whether the estimate is single-judge provisional or independently replicated.
- Confirm semantic detectors cannot appear as validated findings without adjudication.

### Experiment validity

- Reject an effect report without a preregistration, baseline snapshot, controlled arm
  contrast, unit, denominator, task-mix/confound record, and provenance manifest.
- Verify descriptive GROUP BY slices cannot be labeled causal without a controlled design.

### Analysis reproducibility

- Fixed input plus fixed detector/method revisions produces byte-stable aggregate output
  after removing `generated_at` or placing it only in metadata.
- Every rate has an explicit unit, denominator, completeness status, and uncertainty
  method.
- Detector signals, incidents, adjudication, and aggregate summaries are separately
  queryable.
- Every derived metric carries source-manifest, query, code, detector, method, and
  experiment provenance; regeneration detects unexplained changes.
- Run the open-science Python smoke-test style against synthetic fixtures, and add Rust
  unit/integration tests for the importer and exporter.

## Done criteria

The plan is ready to implement when:

- the envelope, layer contracts, and session identity definitions are accepted;
- the schema registry and all emitter-conformance tests are green;
- every ExoMonad session attempts the required structured telemetry automatically;
  writer availability is recorded as `complete`, `partial`, or `unknown`, without a
  logging mode or trace environment variable;
- `--verbose` only controls human-readable diagnostic volume and never gates
  analysis-grade events;
- `.exo/session.json` is authoritative for session boundaries and is locked during
  lifecycle transitions;
- generation is a real monotonic counter, delivery/inbox outcomes are durable, and
  guest timestamps, run sequence, sink health, completeness status, and expected-event
  contract versions are available;
- `exomonad logs import` can ingest legacy and current sources without modifying them and
  rebuilds `L2` from immutable `L1` input;
- the MVP-A through MVP-E gates are explicit and implementation proceeds in that order;
- a local normalized SQLite store supports multiple agents and multiple harnesses;
- `L1` is strict append-only, corrections use `event.superseded`, and segment
  retention drops whole segments only;
- `L2` is rebuildable from `L1` and may be replaced as a derived view;
- `L4` is sample/aggregate-only, selects only `A`, and passes `S` ∩ `L4` = ∅;
- `exomonad logs export --mode aggregate` selects only allowlisted columns and produces
  no transcripts, thinking traces, local payloads, paths, stable source IDs, or secrets;
- the same import is idempotent and reports its provenance;
- signal, incident, adjudication, precision/interval, and aggregate stages are
  separately reproducible, with judge metadata and single-judge estimates marked
  provisional;
- baseline snapshots, preregistered controlled contrasts, explicit units/denominators,
  confound controls, and provenance are required before architecture-effect claims;
- baseline and custom-harness cohorts can be compared by provider/runtime/harness/role,
  latency, delivery, review loops, retries, abandonment, and completion;
- aggregate output is versioned and documented as compatible with the Failure Atlas
  share-statistics model;
- every `L3` mutation has a matching `L1` event and sink-health fallback is
  tested;
- optional `init` integration is explicit, dry-run capable, and does not duplicate
  sessions or imports.

## References

- [`docs/exomonad-session-logging.md`](exomonad-session-logging.md) — current ExoMonad
  logging/event inventory.
- [`Failure Atlas METHOD.md`](../../open-science/docs/experiments/failure-atlas/METHOD.md)
  — local method reference when viewed from the workspace; the canonical source is
  `/home/goya/agent-workspace/open-science/docs/experiments/failure-atlas/METHOD.md`.
- [`Failure Atlas pipeline README`](../../open-science/docs/experiments/failure-atlas/pipeline/README.md)
  — local SQLite/Python pipeline and privacy model.
- [`rust/exomonad-core/src/services/event_log.rs`](../rust/exomonad-core/src/services/event_log.rs)
  — current EventLog envelope and append behavior.
- [`rust/exomonad/src/serve.rs`](../rust/exomonad/src/serve.rs) — project run ID and
  live `.exo/logs` initialization.
- [`rust/exomonad/src/main.rs`](../rust/exomonad/src/main.rs) — current CLI surface.
