"""Read-only operator projection over durable TL state and ledger events."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

from tl_loop.events.envelope import EventEnvelope
from tl_loop.events.reader import SequenceStatus

from .schema import BudgetCharge, BudgetLedger, RunState, SliceState, Verdict

RecentLimit: TypeAlias = int


@dataclass(frozen=True)
class HeadEvidence:
    """Bounded review and CI evidence for one observed PR head."""

    head_sha: str
    review_state: str | None
    review_kind: str | None
    review_verdict: str | None
    review_finding_count: int
    ci_status: str | None
    reviewer_attempt: int | None
    is_current: bool
    last_event_seq: int | None

    def to_document(self) -> dict[str, object]:
        """Return the body-free JSON representation."""
        return {
            "head_sha": self.head_sha,
            "review_state": self.review_state,
            "review_kind": self.review_kind,
            "review_verdict": self.review_verdict,
            "review_finding_count": self.review_finding_count,
            "ci_status": self.ci_status,
            "reviewer_attempt": self.reviewer_attempt,
            "is_current": self.is_current,
            "last_event_seq": self.last_event_seq,
        }


@dataclass(frozen=True)
class SliceReadModel:
    """Operator-safe slice state with per-head evidence summaries."""

    id: str
    status: str
    paths: tuple[str, ...]
    depends_on: tuple[str, ...]
    base_ref: str | None
    agent_type: str | None
    model: str | None
    branch: str | None
    worktree: str | None
    pr_number: int | None
    reviewed_head: str | None
    attempts: int
    repair_attempts: int
    verdict: str | None
    heads: tuple[HeadEvidence, ...]
    park_cause: str | None
    park_issue_id: int | None
    blocked_by: str | None
    stall_classification: str | None

    def to_document(self) -> dict[str, object]:
        """Return the body-free JSON representation."""
        return {
            "status": self.status,
            "paths": list(self.paths),
            "depends_on": list(self.depends_on),
            "base_ref": self.base_ref,
            "agent_type": self.agent_type,
            "model": self.model,
            "branch": self.branch,
            "worktree": self.worktree,
            "pr_number": self.pr_number,
            "reviewed_head": self.reviewed_head,
            "attempts": self.attempts,
            "repair_attempts": self.repair_attempts,
            "verdict": self.verdict,
            "heads": [head.to_document() for head in self.heads],
            "park_cause": self.park_cause,
            "park_issue_id": self.park_issue_id,
            "blocked_by": self.blocked_by,
            "stall_classification": self.stall_classification,
        }


@dataclass(frozen=True)
class BudgetChargeReadModel:
    """One bounded budget charge in the operator view."""

    slice_id: str
    attempt: int
    role: str
    harness: str
    estimated_tokens: int
    actual: int | str
    delta_tokens: int | None
    warning: bool
    reconciled: bool

    def to_document(self) -> dict[str, object]:
        """Return the JSON representation."""
        return {
            "slice_id": self.slice_id,
            "attempt": self.attempt,
            "role": self.role,
            "harness": self.harness,
            "estimated_tokens": self.estimated_tokens,
            "actual": self.actual,
            "delta_tokens": self.delta_tokens,
            "warning": self.warning,
            "reconciled": self.reconciled,
        }


@dataclass(frozen=True)
class BudgetReadModel:
    """Read-only budget counters and immutable charge history."""

    tokens: int
    wall_seconds: int
    role_spent: Mapping[str, int]
    harness_spent: Mapping[str, int]
    role_reserved: Mapping[str, int]
    harness_reserved: Mapping[str, int]
    charges: tuple[BudgetChargeReadModel, ...]

    def to_document(self) -> dict[str, object]:
        """Return the JSON representation."""
        return {
            "tokens": self.tokens,
            "wall_seconds": self.wall_seconds,
            "role_spent": dict(self.role_spent),
            "harness_spent": dict(self.harness_spent),
            "role_reserved": dict(self.role_reserved),
            "harness_reserved": dict(self.harness_reserved),
            "charges": [charge.to_document() for charge in self.charges],
        }


@dataclass(frozen=True)
class GateReadModel:
    """One named human gate."""

    name: str
    status: str

    def to_document(self) -> dict[str, str]:
        """Return the JSON representation."""
        return {"name": self.name, "status": self.status}


@dataclass(frozen=True)
class TransitionReadModel:
    """Allowlisted metadata for one recent ledger transition."""

    run_seq: int
    event_type: str
    observed_at: str
    lifecycle_state: str
    agent_id: str | None
    slice_id: str | None
    harness: str | None
    role: str | None
    pr_number: int | None
    head_sha: str | None
    review_kind: str | None
    review_state: str | None
    ci_status: str | None
    stall_classification: str | None

    def to_document(self) -> dict[str, object]:
        """Return the body-free JSON representation."""
        return {
            "run_seq": self.run_seq,
            "event_type": self.event_type,
            "observed_at": self.observed_at,
            "lifecycle_state": self.lifecycle_state,
            "agent_id": self.agent_id,
            "slice_id": self.slice_id,
            "harness": self.harness,
            "role": self.role,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "review_kind": self.review_kind,
            "review_state": self.review_state,
            "ci_status": self.ci_status,
            "stall_classification": self.stall_classification,
        }


@dataclass(frozen=True)
class ReadModel:
    """Immutable operator view built from one durable state cursor."""

    run_id: str
    revision: int
    phase: str
    waiting: tuple[str, ...]
    ledger_cursor: int
    ledger_sequence_status: str | None
    slices: Mapping[str, SliceReadModel]
    budgets: BudgetReadModel
    gates: tuple[GateReadModel, ...]
    park_causes: Mapping[str, str]
    recent_transitions: tuple[TransitionReadModel, ...]

    def to_document(self) -> dict[str, object]:
        """Return a stable JSON document without agent-authored bodies."""
        return {
            "run_id": self.run_id,
            "revision": self.revision,
            "phase": self.phase,
            "waiting": list(self.waiting),
            "ledger_cursor": self.ledger_cursor,
            "ledger_sequence_status": self.ledger_sequence_status,
            "slices": {
                slice_id: slice_model.to_document() for slice_id, slice_model in self.slices.items()
            },
            "budgets": self.budgets.to_document(),
            "gates": [gate.to_document() for gate in self.gates],
            "park_causes": dict(self.park_causes),
            "recent_transitions": [
                transition.to_document() for transition in self.recent_transitions
            ],
        }


def project_read_model(
    state: RunState,
    events: Iterable[EventEnvelope] = (),
    *,
    sequence_status: SequenceStatus | str | None = None,
    recent_transition_limit: RecentLimit = 20,
) -> ReadModel:
    """Project durable state and consumed ledger observations for operators.

    The persisted event cursor is the authority for the state snapshot. Events
    beyond that cursor are excluded so the model cannot present observations
    that the controller has not incorporated into run.json yet.
    """
    if type(recent_transition_limit) is not int or recent_transition_limit < 0:
        raise ValueError("recent_transition_limit must be a non-negative integer")
    event_list = _events_at_cursor(events, state.events.last_consumed_offset)
    event_index = _index_events(state, event_list)
    slices = {
        slice_id: _slice_model(slice_state, event_index.get(slice_id, {}))
        for slice_id, slice_state in sorted(state.slices.items())
    }
    park_causes = {
        slice_id: slice_model.park_cause
        for slice_id, slice_model in slices.items()
        if slice_model.park_cause is not None
    }
    status = _sequence_status_value(sequence_status)
    recent = (
        tuple(
            _transition(event)
            for event in event_list[-recent_transition_limit:]
            if event.run_seq is not None
        )
        if recent_transition_limit
        else ()
    )
    return ReadModel(
        run_id=state.run_id,
        revision=state.revision,
        phase=state.fsm.phase.value,
        waiting=tuple(state.fsm.waiting),
        ledger_cursor=state.events.last_consumed_offset,
        ledger_sequence_status=status,
        slices=MappingProxyType(slices),
        budgets=_budget_model(state.budgets),
        gates=tuple(GateReadModel(gate.name, gate.status.value) for gate in state.gates),
        park_causes=MappingProxyType(park_causes),
        recent_transitions=recent,
    )


def _events_at_cursor(events: Iterable[EventEnvelope], cursor: int) -> tuple[EventEnvelope, ...]:
    selected = [event for event in events if event.run_seq is not None and event.run_seq <= cursor]
    return tuple(sorted(selected, key=lambda event: event.run_seq or 0))


def _index_events(
    state: RunState, events: Iterable[EventEnvelope]
) -> dict[str, dict[str, EventEnvelope]]:
    indexed: dict[str, dict[str, EventEnvelope]] = {}
    for event in events:
        if event.head_sha is None:
            continue
        slice_id = _event_slice_id(state, event)
        if slice_id is None:
            continue
        by_head = indexed.setdefault(slice_id, {})
        previous = by_head.get(event.head_sha)
        if previous is None or (previous.run_seq or -1) <= (event.run_seq or -1):
            by_head[event.head_sha] = _merge_head_event(previous, event)
    return indexed


def _merge_head_event(previous: EventEnvelope | None, current: EventEnvelope) -> EventEnvelope:
    """Retain the latest value for each bounded evidence dimension."""
    if previous is None:
        return current
    return EventEnvelope(
        kind=current.kind,
        event_type=current.event_type,
        run_seq=current.run_seq,
        run_id=current.run_id,
        agent_id=current.agent_id or previous.agent_id,
        slice_id=current.slice_id or previous.slice_id,
        session_id=current.session_id or previous.session_id,
        invocation_id=current.invocation_id or previous.invocation_id,
        generation=current.generation,
        harness=current.harness or previous.harness,
        role=current.role or previous.role,
        lifecycle_state=current.lifecycle_state,
        observed_at=current.observed_at,
        pr_number=current.pr_number or previous.pr_number,
        head_sha=current.head_sha,
        review_kind=current.review_kind or previous.review_kind,
        notification=None,
        review_state=current.review_state or previous.review_state,
        ci_status=current.ci_status or previous.ci_status,
        data=MappingProxyType({}),
        parent_agent_id=current.parent_agent_id or previous.parent_agent_id,
    )


def _event_slice_id(state: RunState, event: EventEnvelope) -> str | None:
    if event.slice_id in state.slices:
        return event.slice_id
    if event.pr_number is None:
        return None
    matches = [
        slice_id
        for slice_id, slice_state in state.slices.items()
        if slice_state.pr_number == event.pr_number
    ]
    return matches[0] if len(matches) == 1 else None


def _slice_model(state: SliceState, events: Mapping[str, EventEnvelope]) -> SliceReadModel:
    heads = set(state.review_findings) | set(state.ci_state) | set(state.reviewer_attempt)
    if state.reviewed_head is not None:
        heads.add(state.reviewed_head)
    heads.update(events)
    head_models = tuple(
        _head_model(state, head_sha, events.get(head_sha)) for head_sha in sorted(heads)
    )
    return SliceReadModel(
        id=state.id,
        status=state.status.value,
        paths=tuple(state.paths),
        depends_on=tuple(state.depends_on),
        base_ref=state.base_ref,
        agent_type=state.agent_type,
        model=state.model,
        branch=state.branch,
        worktree=state.worktree,
        pr_number=state.pr_number,
        reviewed_head=state.reviewed_head,
        attempts=state.attempts,
        repair_attempts=state.repair_attempts,
        verdict=state.verdict.value if state.verdict is not None else None,
        heads=head_models,
        park_cause=state.park_cause.value if state.park_cause is not None else None,
        park_issue_id=state.park_issue_id,
        blocked_by=state.blocked_by,
        stall_classification=state.stall_classification,
    )


def _head_model(state: SliceState, head_sha: str, event: EventEnvelope | None) -> HeadEvidence:
    finding_count = len(state.review_findings.get(head_sha, ()))
    review_state = event.review_state if event is not None else None
    if review_state is None and state.verdict is not None and state.reviewed_head == head_sha:
        review_state = _review_state_for_verdict(state.verdict)
    if review_state is None and finding_count:
        review_state = "changes_requested"
    return HeadEvidence(
        head_sha=head_sha,
        review_state=review_state,
        review_kind=event.review_kind if event is not None else None,
        review_verdict=(
            state.verdict.value
            if state.verdict is not None and state.reviewed_head == head_sha
            else None
        ),
        review_finding_count=finding_count,
        ci_status=(
            state.ci_state.get(head_sha) or (event.ci_status if event is not None else None)
        ),
        reviewer_attempt=state.reviewer_attempt.get(head_sha),
        is_current=state.reviewed_head == head_sha,
        last_event_seq=event.run_seq if event is not None else None,
    )


def _review_state_for_verdict(verdict: Verdict) -> str:
    if verdict is Verdict.NO_GO:
        return "changes_requested"
    if verdict is Verdict.GO_WITH_NITS:
        return "approved_with_nits"
    return "approved"


def _budget_model(ledger: BudgetLedger) -> BudgetReadModel:
    return BudgetReadModel(
        tokens=ledger.tokens,
        wall_seconds=ledger.wall_seconds,
        role_spent=MappingProxyType(dict(ledger.role_spent)),
        harness_spent=MappingProxyType(dict(ledger.harness_spent)),
        role_reserved=MappingProxyType(dict(ledger.role_reserved)),
        harness_reserved=MappingProxyType(dict(ledger.harness_reserved)),
        charges=tuple(_charge_model(charge) for charge in ledger.charges),
    )


def _charge_model(charge: BudgetCharge) -> BudgetChargeReadModel:
    return BudgetChargeReadModel(
        slice_id=charge.slice_id,
        attempt=charge.attempt,
        role=charge.role,
        harness=charge.harness,
        estimated_tokens=charge.estimated_tokens,
        actual=charge.actual,
        delta_tokens=charge.delta_tokens,
        warning=charge.warning,
        reconciled=charge.reconciled,
    )


def _sequence_status_value(value: SequenceStatus | str | None) -> str | None:
    if value is None:
        return None
    return value.value if isinstance(value, Enum) else value


def _transition(event: EventEnvelope) -> TransitionReadModel:
    if event.run_seq is None:
        raise ValueError("recent transition requires a ledger sequence")
    classification = event.stall_classification
    return TransitionReadModel(
        run_seq=event.run_seq,
        event_type=event.event_type,
        observed_at=event.observed_at,
        lifecycle_state=event.lifecycle_state,
        agent_id=event.agent_id,
        slice_id=event.slice_id,
        harness=event.harness,
        role=event.role,
        pr_number=event.pr_number,
        head_sha=event.head_sha,
        review_kind=event.review_kind,
        review_state=event.review_state,
        ci_status=event.ci_status,
        stall_classification=classification.value if classification is not None else None,
    )


__all__ = [
    "BudgetChargeReadModel",
    "BudgetReadModel",
    "GateReadModel",
    "HeadEvidence",
    "ReadModel",
    "RecentLimit",
    "SliceReadModel",
    "TransitionReadModel",
    "project_read_model",
]
