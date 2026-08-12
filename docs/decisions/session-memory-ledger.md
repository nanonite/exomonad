# ADR: Session memory ledger and continuation brief

Date: 2026-08-12
Status: Accepted
Issue: #620

## Context

Continuity facts were available in several independent places but were not
composed when a root or TL session started again. Chainlink owns issue and
session state, the inbox owns notifications, the event log owns low-level
tool-call history, and the watcher observes Forgejo and agent liveness. Prompt
text alone could not reliably carry the original plan, active work, decisions,
blockers, or the next action across a context reset or resumed PR.

The system needs a bounded, inspectable representation of semantic session
facts that can be rendered at startup and attached to child work without an
LLM summarization step.

## Decision

ExoMonad uses an append-only session-memory ledger and a deterministic
continuation-brief renderer.

### Ledger

`SessionMemoryService` stores validated, typed records in `.exo/memory.db`.
Records are scoped to a run and use a closed `MemoryKind` set. The service
supports append, filtered list, and latest-by-kind reads. It has no update or
delete API; corrections are represented by a superseding record. Validation
keeps summaries, details, importance, metadata, and predecessor references
bounded and well formed.

Append-only storage preserves the history needed to explain how a brief was
formed, supports deterministic replay, and avoids making the continuation
path depend on mutable prompt state.

### Adapters

The continuation service gathers typed sections from explicit adapters:

- Chainlink is queried through `chainlink ... --json` with the project
  `CHAINLINK_DB` environment variable. ExoMonad does not read Chainlink's
  private SQLite schema, so Chainlink migrations cannot silently break the
  brief.
- Inbox state contributes metadata such as unread counts and timestamps. It
  does not drain messages while rendering a brief.
- Event, agent, and Forgejo adapters contribute available state or an explicit
  unavailable reason. A missing provider is represented as degraded context,
  not fabricated state.

The inbox is deliberately not the ledger: inbox reads are delivery operations
and may be drain-on-read, while continuation requires durable semantic history
and reproducible projections. The event log is likewise an observation source,
not a replacement for typed semantic facts.

### Renderer

`ContinuationBriefRenderer` is a pure function of structured inputs. It emits
the fixed `<exomonad-continuation-brief>` markdown sections in stable order,
sorts collections explicitly, scopes child feedback to its owner, renders
unavailable sources with their reasons, and enforces the context-size cap by
dropping the least important and oldest records first. It performs no I/O,
provider calls, or model summarization.

### Injection and tools

The root/TL SessionStart hook requests `memory.brief` and appends the result
after the existing TeamCreate instruction. Memory failures remain fail-open:
the session still receives the TeamCreate instruction. `spawn_leaf` and
`resume_pr` receive the relevant continuation prefix automatically. Explicit
`memory_append`, `memory_list`, and root/TL-only `continuation_brief` tools are
available for semantic facts and mid-session refreshes.

Automatic capture inside every tool handler is Phase 3 and remains deferred;
this decision covers the ledger, adapters, renderer, explicit tools, and
injection points only.

## Alternatives rejected

- Reading Chainlink's database directly would couple ExoMonad to a private
  schema and duplicate Chainlink's migration responsibility.
- Treating the inbox as session memory would lose history when notifications
  are drained and would mix delivery semantics with state projection.
- Rendering a model-generated summary would make startup nondeterministic,
  add a provider dependency to a critical path, and make tests unable to prove
  the exact context delivered to a TL.
- Capturing every tool call as a semantic record would reproduce raw event-log
  noise rather than preserve useful facts; automatic capture is deferred until
  its taxonomy and retention policy are defined.

## Consequences

Root and TL sessions receive the same bounded, reproducible continuity surface
after every restart or resume. Child prompts carry the relevant context
without requiring the child to discover the parent state manually. The ledger
adds a project-local SQLite file and explicit validation rules, while degraded
adapters remain visible instead of silently disappearing. Any future change to
record kinds, rendering order, or context limits must preserve deterministic
output and update the corresponding fixtures and tests.
