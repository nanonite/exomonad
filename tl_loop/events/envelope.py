"""Typed read projection of the immutable Rust ledger event envelope."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias, cast

from tl_loop.select.classify import Difficulty

from .recovery import RecoveryDimensions, RecoveryTelemetryError
from .stall import ReviewStallClassification, classify_review_stall
from .types import BlockCause

LedgerEventInput: TypeAlias = Mapping[str, object]


class EnvelopeError(ValueError):
    """A ledger row cannot be projected into the TL envelope."""


class UnmappedEventType(EnvelopeError):
    """The ledger event type is outside the closed TL projection set."""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        super().__init__(f"unmapped ledger event_type: {event_type!r}")


class InvalidLedgerEvent(EnvelopeError):
    """A mapped ledger row has an invalid envelope field or payload."""


class EventKind(str, Enum):
    """Closed TL kinds, each mapped one-to-one onto an existing ledger type."""

    PR_FILED = "pr.filed"
    PR_UPDATED = "pr.updated"
    PR_PUBLISHED = "pr.published"
    PR_MERGED = "pr.merged"
    PR_MERGE_FAILED = "pr.merge_failed"
    PR_REVIEW = "pr.review"
    COPILOT_REVIEW = "copilot.review"
    CI_STATUS_CHANGED = "ci.status_changed"
    AGENT_SPAWNED = "agent.spawned"
    AGENT_COMPLETED = "agent.completed"
    AGENT_TASK_BLOCKED = "agent.task_blocked"
    AGENT_RECOVERY_STARTED = "agent.recovery.started"
    AGENT_RECOVERY_OUTCOME = "agent.recovery.outcome"
    AGENT_STUCK = "agent.stuck"
    AGENT_NOTIFY_PARENT = "agent.notify_parent"
    AGENT_SIBLING_MERGED = "agent.sibling_merged"
    ISSUE_CLOSED = "issue.closed"
    INBOX_MESSAGE = "inbox.message"
    INBOX_POKE = "inbox.poke"
    SLICE_ABANDONED = "tl.slice_abandoned"


EVENT_TYPE_BY_KIND: Mapping[EventKind, str] = MappingProxyType(
    {kind: kind.value for kind in EventKind}
)
KIND_BY_EVENT_TYPE: Mapping[str, EventKind] = MappingProxyType(
    {event_type: kind for kind, event_type in EVENT_TYPE_BY_KIND.items()}
)
MAPPED_EVENT_TYPES = frozenset(KIND_BY_EVENT_TYPE)


@dataclass(frozen=True)
class TaskBlocked:
    """Normalized task-blocked outcome with no raw evidence fields."""

    slice_id: str
    cause: BlockCause
    scope_attribution: str
    needs_human: bool
    retryable: bool
    recovery_action: str
    declared_difficulty: Difficulty
    matched_difficulty_rule: str
    attempt: int
    harness: str | None = None
    role: str | None = None
    invocation_id: str | None = None

    @property
    def attempt_bucket(self) -> str:
        if self.attempt <= 1:
            return "1"
        if self.attempt == 2:
            return "2"
        if self.attempt <= 4:
            return "3-4"
        return "5+"

    def aggregate_dimensions(self) -> dict[str, object]:
        """Return the allowlisted Failure Atlas dimensions only."""
        return {
            "outcome": "blocked",
            "slice_id": self.slice_id,
            "cause": self.cause.value,
            "scope_attribution": self.scope_attribution,
            "needs_human": self.needs_human,
            "retryable": self.retryable,
            "recovery_action": self.recovery_action,
            "declared_difficulty": self.declared_difficulty.value,
            "matched_difficulty_rule": self.matched_difficulty_rule,
            "attempt_bucket": self.attempt_bucket,
            "harness": self.harness,
            "role": self.role,
        }

    @classmethod
    def from_payload(
        cls,
        data: Mapping[str, object],
        *,
        envelope_slice_id: str | None,
        harness: str | None,
        role: str | None,
        invocation_id: str | None,
        event_type: str,
    ) -> TaskBlocked:
        payload = data.get("blocked", data)
        if not isinstance(payload, Mapping):
            raise InvalidLedgerEvent(f"{event_type!r}: blocked payload must be an object")
        slice_id = (
            _required_string(payload, "slice_id", event_type)
            if payload.get("slice_id") is not None
            else envelope_slice_id
        )
        if not slice_id:
            raise InvalidLedgerEvent(f"{event_type!r}: slice_id must be a non-empty string")
        if payload.get("outcome", "blocked") != "blocked":
            raise InvalidLedgerEvent(f"{event_type!r}: task-blocked outcome must be blocked")
        cause_value = payload.get("cause")
        try:
            cause = BlockCause(cause_value)
        except (TypeError, ValueError) as error:
            raise InvalidLedgerEvent(
                f"{event_type!r}: cause is outside the closed vocabulary"
            ) from error
        scope = _required_string(payload, "scope_attribution", event_type)
        recovery = _required_string(payload, "recovery_action", event_type)
        needs_human = payload.get("needs_human")
        retryable = payload.get("retryable")
        if type(needs_human) is not bool or type(retryable) is not bool:
            raise InvalidLedgerEvent(f"{event_type!r}: needs_human and retryable must be booleans")
        difficulty_value = payload.get("declared_difficulty")
        try:
            difficulty = Difficulty(difficulty_value)
        except (TypeError, ValueError) as error:
            raise InvalidLedgerEvent(f"{event_type!r}: declared_difficulty is invalid") from error
        rule = _required_string(payload, "matched_difficulty_rule", event_type)
        attempt = payload.get("attempt")
        if type(attempt) is not int or attempt <= 0:
            raise InvalidLedgerEvent(f"{event_type!r}: attempt must be a positive integer")
        return cls(
            slice_id=slice_id,
            cause=cause,
            scope_attribution=scope,
            needs_human=needs_human,
            retryable=retryable,
            recovery_action=recovery,
            declared_difficulty=difficulty,
            matched_difficulty_rule=rule,
            attempt=attempt,
            harness=harness,
            role=role,
            invocation_id=invocation_id,
        )


# These event types still have server-side emitters without verified PR context.
# This is a finding for M2.7 (#677), not permission for this projection to
# synthesize a SHA.
SERVER_EMIT_HEAD_SHA_GAPS = frozenset(
    {
        "agent.completed",
        "agent.stuck",
        "agent.notify_parent",
    }
)


@dataclass(frozen=True)
class EventEnvelope:
    """A typed, non-durable view over one immutable ledger row."""

    kind: EventKind
    event_type: str
    run_seq: int | None
    run_id: str | None
    agent_id: str | None
    slice_id: str | None
    session_id: str | None
    invocation_id: str | None
    generation: int | None
    harness: str | None
    role: str | None
    lifecycle_state: str
    observed_at: str
    pr_number: int | None
    head_sha: str | None
    review_kind: str | None
    notification: str | None
    review_state: str | None
    ci_status: str | None
    data: Mapping[str, object]
    parent_agent_id: str | None = None
    task_blocked: TaskBlocked | None = None
    recovery_dimensions: RecoveryDimensions | None = None

    @property
    def reviewed_head(self) -> str | None:
        """Compatibility name used by the run-state slice schema."""
        return self.head_sha

    @property
    def stall_classification(self) -> ReviewStallClassification | None:
        """Derive a stall class from raw watcher evidence at the TL boundary."""
        payload = dict(self.data)
        if self.review_kind is not None:
            payload.setdefault("kind", self.review_kind)
        if self.ci_status is not None:
            payload.setdefault("ci_status", self.ci_status)
        return classify_review_stall(payload)


def project(event: LedgerEventInput) -> EventEnvelope:
    """Project one Rust ``LedgerEvent`` JSON object into the TL view."""
    if not isinstance(event, Mapping):
        raise InvalidLedgerEvent("ledger event must be an object")
    event_type = _event_type(event)
    kind = KIND_BY_EVENT_TYPE.get(event_type)
    if kind is None:
        raise UnmappedEventType(event_type)
    data = event.get("data")
    if not isinstance(data, Mapping):
        raise InvalidLedgerEvent(f"{event_type!r}: data must be an object")
    return EventEnvelope(
        kind=kind,
        event_type=event_type,
        run_seq=_optional_int(event, "run_seq", event_type),
        run_id=_optional_string(event, "run_id", event_type),
        agent_id=_optional_string(event, "agent_id", event_type),
        slice_id=_optional_string(data, "slice_id", event_type),
        session_id=_optional_string(event, "session_id", event_type),
        invocation_id=_optional_string(event, "invocation_id", event_type),
        generation=_optional_int(event, "generation", event_type),
        harness=_optional_string(event, "harness", event_type),
        role=_optional_string(event, "role", event_type),
        lifecycle_state=_required_string(event, "lifecycle_state", event_type),
        observed_at=_required_string(event, "observed_at", event_type),
        pr_number=_optional_int(data, "pr_number", event_type),
        head_sha=_head_sha(data, event_type),
        review_kind=_optional_string(data, "kind", event_type),
        notification=_optional_string(data, "notification", event_type),
        review_state=_optional_string(data, "review_state", event_type),
        ci_status=_ci_status(data, event_type),
        parent_agent_id=_optional_string(event, "parent_agent_id", event_type)
        or _optional_string(data, "parent_agent_id", event_type),
        data=MappingProxyType(cast(dict[str, object], copy.deepcopy(dict(data)))),
        task_blocked=(
            TaskBlocked.from_payload(
                data,
                envelope_slice_id=_optional_string(data, "slice_id", event_type),
                harness=_optional_string(event, "harness", event_type),
                role=_optional_string(event, "role", event_type),
                invocation_id=_optional_string(event, "invocation_id", event_type),
                event_type=event_type,
            )
            if kind is EventKind.AGENT_TASK_BLOCKED
            else None
        ),
        recovery_dimensions=(
            _recovery_dimensions(data, generation=event.get("generation"))
            if kind is EventKind.AGENT_RECOVERY_OUTCOME
            else None
        ),
    )


def _recovery_dimensions(data: Mapping[str, object], *, generation: object) -> RecoveryDimensions:
    """Project one recovery outcome without exposing local evidence."""
    envelope_generation = generation if type(generation) is int else None
    try:
        return RecoveryDimensions.from_payload(data, envelope_generation=envelope_generation)
    except RecoveryTelemetryError as error:
        raise InvalidLedgerEvent(f"agent.recovery.outcome: {error}") from error


def project_ledger_event(event: LedgerEventInput) -> EventEnvelope:
    """Named alias for callers that want to make the source projection clear."""
    return project(event)


def _event_type(event: LedgerEventInput) -> str:
    type_value = event.get("type")
    event_type_value = event.get("event_type")
    if type_value is not None and event_type_value is not None and type_value != event_type_value:
        raise InvalidLedgerEvent("ledger event has conflicting type and event_type values")
    value = type_value if type_value is not None else event_type_value
    if not isinstance(value, str) or not value:
        raise InvalidLedgerEvent("ledger event type must be a non-empty string")
    return value


def _required_string(event: LedgerEventInput, key: str, event_type: str) -> str:
    value = event.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidLedgerEvent(f"{event_type!r}: {key} must be a non-empty string")
    return value


def _optional_string(event: LedgerEventInput, key: str, event_type: str) -> str | None:
    value = event.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidLedgerEvent(f"{event_type!r}: {key} must be null or a non-empty string")
    return value


def _optional_int(event: LedgerEventInput, key: str, event_type: str) -> int | None:
    value = event.get(key)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise InvalidLedgerEvent(f"{event_type!r}: {key} must be null or a non-negative integer")
    return value


def _ci_status(data: Mapping[str, object], event_type: str) -> str | None:
    key = "status" if event_type == EventKind.CI_STATUS_CHANGED.value else "ci_status"
    return _optional_string(data, key, event_type)


def _head_sha(data: Mapping[str, object], event_type: str) -> str | None:
    value = data.get("head_sha")
    if value is None:
        value = data.get("reviewed_head")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidLedgerEvent(f"{event_type!r}: head_sha must be null or a non-empty string")
    return value


__all__ = [
    "EVENT_TYPE_BY_KIND",
    "KIND_BY_EVENT_TYPE",
    "MAPPED_EVENT_TYPES",
    "SERVER_EMIT_HEAD_SHA_GAPS",
    "BlockCause",
    "EnvelopeError",
    "EventEnvelope",
    "EventKind",
    "InvalidLedgerEvent",
    "LedgerEventInput",
    "RecoveryDimensions",
    "TaskBlocked",
    "UnmappedEventType",
    "project",
    "project_ledger_event",
]
