# ADR: Operator control plane for the programmatic TL

Date: 2026-08-12
Status: Accepted
Builds on: [tl-as-loop.md](tl-as-loop.md), [watcher-as-sensor.md](watcher-as-sensor.md), [agent-loop-and-steering.md](agent-loop-and-steering.md)
Reconciled: 2026-09-03 in #1058

## Context

Before M9, a human operator talked to an interactive TL session. That
conversation was also the query interface: you could ask what the run was
doing, why a PR was stuck, and what it planned next, and the TL would answer
from its own context.

M9 replaced that session with `tl_loop`. The architecture is durable,
replayable, and testable. The operator surface remains two commands:

```bash
python3 ~/.exo/tl_loop.pyz status --run-id root
python3 ~/.exo/tl_loop.pyz gate   --run-id root --name <gate> --approve
```

`status` prints the hierarchical read model; `gate` answers a gate you already
knew the name of. The read model answers "why is slice X still open" and "what
is this run waiting on" without granting mutation authority. It reports the
scope path, manifest revision, current order, next transition, child and
post-merge state, repository-lane coordinates, replay cursor, journal state,
and the specific blocking guard. It is a projection, so operators should
check its cursor and refresh it before acting on a gate.

M11 ([agent-loop-and-steering.md](agent-loop-and-steering.md)) covers the
opposite direction — durable TL→agent guidance with batches, leases, and
per-runtime adapters. That work is not duplicated here.

So the gap is exactly one direction: **human → TL**.

## Decision

### 1. A `/control` surface on the existing UDS, not a message broker

The controller exposes an operator API over the ExoMonad Unix socket that
already exists. No new listener, no broker, no second event log.

The human console is a **client** of `tl_loop`, never a peer. This follows
zuihitsu's rule directly:

> Transport and authority are orthogonal. Co-location is never an authority
> escalation and never a back door to state.

and its corollary that orchestration is server-side:

> Clients deliver and receive; the server owns scheduling, time, and memory.

`tl_loop` remains the single writer of run state. A console that could mutate
`run.json` directly would rebuild the split brain M9 removed.

### 2. The console may do exactly three things

1. **Read projections.** A hierarchical read model over run state and the
   ledger: scope path and role, manifest revision, current order, typed child
   lifecycle, per-head review/CI evidence, post-merge progress, repository
   lanes, replay cursor, budget ledger, gates, park causes, recent transitions,
   and next-transition guidance.
2. **Answer named gates.** Approve or reject a gate that already exists. The
   console cannot invent a gate name.
3. **Propose plan mutations.** A proposal is validated by the same closed-key
   `WorkPlan` validator that `plan.json` uses, and is **inert until the
   operator confirms it** — zuihitsu's `MergeProposed` pattern.

Nothing else. In particular the console cannot merge a PR, approve a review,
set a verdict, alter the FSM phase, widen `harness_policy.toml`, or raise a
budget ceiling. Those remain typed ledger events and durable gates.

### 3. Natural language is a fourth RLM judgment

Operator prose becomes structured intent through a bounded judgment alongside
`decompose`, `adjudicate_review`, and `compose_repair`:

```
interpret_operator_intent(utterance, read_model) -> Query | GateAnswer | PlanProposal | Unclear
```

Same contract as the existing three: closed output schema, explicit context
budget, bounded attempts, token charge, replay record, `tools=()`, no effect
client. `Unclear` is a first-class outcome — the model asks rather than
guessing, because a misread instruction here moves real work.

The model translates. It does not decide. Every intent it produces still
passes the same validation and authority checks as if it had been typed as a
CLI argument.

### 4. Verb-first topic vocabulary as an addressing layer

Adopt lanius's grammar as the naming scheme for guidance and observation:

```
{verb}/{category}/{noun}/{locators}
```

#### Topic grammar and encoding

The grammar is intentionally small and closed. In ABNF-like notation, a
serialized topic is:

```text
topic    = verb "/" category "/" noun *( "/" locator )
verb     = "in" / "obs" / "signal"
category = segment
noun     = segment
locator  = segment
segment  = 1*(UTF-8 character)
```

`category` and `noun` are required; a topic may have zero or more locators.
Each segment is non-empty and opaque to the generic parser. `/` is the level
separator and therefore is not valid inside a segment. Empty levels, a leading
or trailing `/`, `.` and `..` segments, NUL, and malformed UTF-8 are invalid.
The meaning and required number of locators belong to the contract for the
specific category, not to the generic grammar.

Before joining segments with `/`, the serializer must percent-encode these
three characters in every path-derived segment:

| Raw character | Serialized form | Reason |
|---|---|---|
| `+` | `%2B` | Prevents a future MQTT single-level wildcard |
| `#` | `%23` | Prevents a future MQTT multi-level wildcard and URI fragment ambiguity |
| `%` | `%25` | Makes the encoding unambiguous and round-trippable |

The hexadecimal digits are uppercase. Encoding is a single pass over the raw
segment, so a literal `%2B` becomes `%252B`; it must not be decoded as a plus.
All other characters, including Unicode characters, are preserved by this
contract. A raw `/` is rejected rather than encoded because it would conceal a
level boundary. A serialized topic must never contain an unescaped `+`, `#`,
or `%` in a segment.

Decoding happens after splitting on `/` and reverses only `%2B`, `%23`, and
`%25`. Other percent triplets and incomplete triplets are rejected. The
serializer and parser must satisfy `decode(encode(segment)) == segment` for
every valid segment.

The verb set is closed: `in`, `obs`, and `signal` are the only valid verbs.
There is deliberately no `out/` verb. The recipient's `in/` topic is the
durable copy; introducing `out/` would imply a second delivery vocabulary and
authority.

For example, these are canonical serialized topics:

```text
in/agent/codex%2Breview%231%25/steering
signal/park/root/ci%23failed
```

They decode respectively to an agent identifier of `codex+review#1%` and a
park cause of `ci#failed`.

| Verb | Contract | Rides on |
|---|---|---|
| `in/` | Addressed, at-least-once, durable; the recipient's mailbox is the single durable copy | M11 guidance queue (`enqueue_batch`) |
| `obs/` | Telemetry; droppable; persistence opt-in | Existing ledger event vocabulary |
| `signal/` | Algedonic; never coalesced or queued behind anything | Park causes and human gates |

Concretely:

```
in/agent/<agent_id>/steering        -> enqueue_batch(agent_id, class=steering, …)
in/agent/<agent_id>/follow_up       -> enqueue_batch(agent_id, class=follow_up, …)
obs/event/pr.review/<run_id>/<slice_id> -> ledger view of the `pr.review` event
signal/park/<run_id>/<cause>        -> park cause reaching the operator
```

`signal/park/<run_id>/<cause>` accepts only the controller's closed park-cause
set (`retries_exhausted`, `budget_exhausted`, `no_capable_harness`,
`schedule_deadlock`, `review_stuck`, `review_rounds_exhausted`,
`harness_switch_requested`, and `stall_detected`). It is an immediate view of
durable TL state: it is neither
an `in/` queue item nor a request to create, answer, or coalesce a named gate.
The existing controller and `/control` gate route remain the only authorities
for parking and gate mutation.

**This is addressing and presentation, not a second authority.** Two hard
constraints:

- It does **not** rename or replace the ledger event registry. `pr.filed`,
  `ci.status_changed`, `agent.stuck` and the rest keep their identities;
  `obs/` topics are a *view* over them.
- It does **not** introduce a transport. `in/` resolves to the M11 queue's
  existing operations. A topic is a way to name a destination, not a new way
  to reach it.

The value is that the delivery contract becomes decidable from segment one,
for both humans and code, and that a future MQTT listener — if the multi-host
condition ever arrives — is a boundary adapter over an existing grammar
rather than a redesign.

## Relationship to the MQTT rejection in `tl-as-loop.md`

This ADR does **not** reopen that decision, and the distinction is load-bearing.

`tl-as-loop.md` rejected a broker as the **coordination substrate**: the loop
reads the durable ledger and does not add "MQTT, a second event log, or an
inbox database" for PR, CI, review, or merge state. It set the revisit
condition as agents moving to multiple hosts or containers that cannot share
the UDS socket, filesystem state, and ledger segments.

**That condition is not met, and this ADR does not claim it is.** No broker is
proposed. The ledger remains the only workflow event source. What is added is
an operator *control plane* over the socket that already exists, and a naming
grammar over mechanisms that already exist.

If a broker is ever proposed, it must come back through `tl-as-loop.md` on the
multi-host trigger, and it must preserve event identity, ordering, replay, and
human-gate semantics rather than replace them.

## Authority matrix

| Actor | Read state | Answer gate | Propose plan | Merge / approve / set verdict |
|---|:---:|:---:|:---:|:---:|
| Operator console | yes | yes | yes, inert until confirmed | **no** |
| `interpret_operator_intent` | yes (read model only) | no — emits intent | no — emits proposal | **no** |
| `tl_loop` controller | yes | applies | validates and applies | yes, after gates |
| Agents (dev/worker/reviewer) | no | no | no | no |

A free-form message never carries workflow authority. This is the same rule
`agent-loop-and-steering.md` states for guidance, applied to the operator
channel.

## Rejected alternatives

**An MQTT broker now.** Contradicts `tl-as-loop.md` on an unmet trigger, and
the local single-host case is precisely what that ADR said a broker would not
improve. The grammar is worth adopting; the transport is not yet.

**Let the console write `run.json` directly.** Reintroduces a second writer to
durable state. The whole point of M9 was removing the second writer.

**Rebuild an interactive TL agent as the query surface.** That is the thing M9
deleted. A conversational *client* is not a conversational *coordinator*; the
distinction is that this one holds no authority and owns no state.

**Route operator chat through the agent inbox.** The inbox is a delivery
surface for agents. Operator control needs different authority, different
durability, and different failure semantics — and the inbox explicitly cannot
carry merge authority.

**A second event log for console history.** Console reads project from the
ledger. Its proposals become durable gate/plan records, not a parallel log.

## Consequences

Positive:

- Hierarchical diagnostics explain blocked recursive work without restoring an
  interactive coordinator or creating a second state authority.
- Escalation reaches the operator instead of waiting to be polled: a park
  cause becomes a `signal/` the console surfaces.
- The authority boundary is explicit and enforced server-side, so a console
  bug or a model misread cannot merge anything.
- The topic grammar is adopted cheaply and keeps a future transport decision
  open without pre-paying for it.

Costs and limits:

- A read model is a projection and can lag or be wrong; it must carry the
  ledger cursor it was built from so staleness is visible.
  `opencode-feature-factory`'s `OPERATING.md` has a section titled *"Signals
  that lie"* — that is the correct paranoia for any console over orchestrator
  state.
- `interpret_operator_intent` adds a model call on an interactive path, with
  its own latency and token charge.
- The grammar overlaps conceptually with the existing event registry; the
  "view, not rename" constraint has to be enforced in review or it will drift
  into a second vocabulary.
- Natural-language operator input is an injection surface. Intent is
  structured and validated before it touches state, which is the mitigation,
  but the read model must never render agent-authored text as if it were
  operator instruction.
