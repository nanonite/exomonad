"""Independent pre-publication execution-recovery state machine."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Literal, cast


class RecoveryPhase(str, Enum):
    """Durable phases before a slice has published a PR."""

    DIAGNOSING = "diagnosing"
    WAITING_SIGNAL = "waiting_signal"
    REVALIDATING = "revalidating"
    RESUME_INTENDED = "resume_intended"
    RESUMING = "resuming"
    HUMAN_GATE = "human_gate"


@dataclass(frozen=True)
class RecoveryState:
    """Recovery identity and bounded progress for one implementation slice."""

    cause: str
    phase: RecoveryPhase
    recovery_round: int
    next_action: str
    owner_run_id: str
    entered_at: float
    slice_attempt: int
    owner_agent_id: str | None = None
    invocation_generation: int = 0
    plan_revision: int = 0
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.cause:
            raise ValueError("recovery cause must be non-empty")
        if not self.next_action:
            raise ValueError("recovery next_action must be non-empty")
        if not self.owner_run_id:
            raise ValueError("recovery owner_run_id must be non-empty")
        for name in (
            "recovery_round",
            "slice_attempt",
            "invocation_generation",
            "plan_revision",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"recovery {name} must be a non-negative integer")
        if self.entered_at < 0:
            raise ValueError("recovery entered_at must be non-negative")


class RecoveryTransitionError(ValueError):
    """Raised when a recovery phase transition is not legal."""


@dataclass(frozen=True)
class RecoveryIdentity:
    """Compare-and-set identity for one recovery owner and invocation."""

    run_id: str
    slice_id: str
    owner_agent_id: str
    invocation_id: str
    invocation_generation: int
    recovery_round: int
    branch: str
    worktree: str
    plan_revision: int = 0

    def __post_init__(self) -> None:
        for name in ("run_id", "slice_id", "owner_agent_id", "invocation_id", "branch", "worktree"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"recovery identity {name} must be non-empty")
        for name in ("invocation_generation", "recovery_round", "plan_revision"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"recovery identity {name} must be non-negative")


RecoveryIntentAction = Literal["probe", "resume_same_owner", "open_gate", "abandon"]
RecoveryIntentStatus = Literal["intended", "confirmed", "unknown", "reconciled"]


@dataclass(frozen=True)
class RecoveryIntent:
    """A journaled recovery effect with immutable owner expectations."""

    intent_id: str
    recovery_identity: RecoveryIdentity
    action: RecoveryIntentAction
    expected_worktree_fingerprint: str
    state: RecoveryIntentStatus = "intended"

    def __post_init__(self) -> None:
        if not self.intent_id.strip():
            raise ValueError("recovery intent_id must be non-empty")
        if not self.expected_worktree_fingerprint.strip():
            raise ValueError("recovery expected_worktree_fingerprint must be non-empty")


def assert_recovery_identity(expected: RecoveryIdentity, observed: RecoveryIdentity) -> None:
    """Fail closed when any owner or invocation fingerprint changed."""
    if expected != observed:
        raise RecoveryTransitionError(
            "recovery identity compare-and-set failed: owner, invocation, branch, "
            "worktree, generation, or recovery round changed"
        )


_LEGAL_TRANSITIONS: dict[RecoveryPhase, frozenset[RecoveryPhase]] = {
    RecoveryPhase.DIAGNOSING: frozenset(
        {
            RecoveryPhase.WAITING_SIGNAL,
            RecoveryPhase.REVALIDATING,
            RecoveryPhase.RESUME_INTENDED,
            RecoveryPhase.HUMAN_GATE,
        }
    ),
    RecoveryPhase.WAITING_SIGNAL: frozenset(
        {
            RecoveryPhase.REVALIDATING,
            RecoveryPhase.RESUME_INTENDED,
            RecoveryPhase.HUMAN_GATE,
        }
    ),
    RecoveryPhase.REVALIDATING: frozenset(
        {RecoveryPhase.RESUME_INTENDED, RecoveryPhase.HUMAN_GATE}
    ),
    RecoveryPhase.RESUME_INTENDED: frozenset({RecoveryPhase.RESUMING, RecoveryPhase.HUMAN_GATE}),
    RecoveryPhase.RESUMING: frozenset(
        {
            RecoveryPhase.WAITING_SIGNAL,
            RecoveryPhase.REVALIDATING,
            RecoveryPhase.HUMAN_GATE,
        }
    ),
    RecoveryPhase.HUMAN_GATE: frozenset({RecoveryPhase.RESUME_INTENDED}),
}


def begin_recovery(
    *,
    cause: str,
    owner_run_id: str,
    slice_attempt: int,
    owner_agent_id: str | None,
    invocation_generation: int = 0,
    plan_revision: int = 0,
    evidence: Mapping[str, object] | None = None,
    next_action: str = "diagnose",
    entered_at: float | None = None,
) -> RecoveryState:
    """Create the idempotent initial DIAGNOSING state."""
    return RecoveryState(
        cause=cause,
        phase=RecoveryPhase.DIAGNOSING,
        recovery_round=0,
        next_action=next_action,
        owner_run_id=owner_run_id,
        entered_at=time.time() if entered_at is None else entered_at,
        slice_attempt=slice_attempt,
        owner_agent_id=owner_agent_id,
        invocation_generation=invocation_generation,
        plan_revision=plan_revision,
        evidence=dict(evidence or {}),
    )


def transition_recovery(
    state: RecoveryState,
    phase: RecoveryPhase,
    *,
    next_action: str,
    entered_at: float | None = None,
    evidence: Mapping[str, object] | None = None,
) -> RecoveryState:
    """Apply one legal recovery transition without touching review phases."""
    if phase is state.phase:
        return replace(
            state,
            next_action=next_action,
            entered_at=time.time() if entered_at is None else entered_at,
            evidence=dict(evidence) if evidence is not None else state.evidence,
        )
    if phase not in _LEGAL_TRANSITIONS[state.phase]:
        raise RecoveryTransitionError(
            f"no recovery transition from {state.phase.value} to {phase.value}"
        )
    return replace(
        state,
        phase=phase,
        recovery_round=state.recovery_round + 1,
        next_action=next_action,
        entered_at=time.time() if entered_at is None else entered_at,
        evidence=dict(evidence) if evidence is not None else state.evidence,
    )


def decode_recovery(value: object) -> RecoveryState | None:
    """Decode an optional persisted recovery object after schema validation."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("recovery must be an object")
    return RecoveryState(
        cause=cast(str, value["cause"]),
        phase=RecoveryPhase(cast(str, value["phase"])),
        recovery_round=cast(int, value["recovery_round"]),
        next_action=cast(str, value["next_action"]),
        owner_run_id=cast(str, value["owner_run_id"]),
        entered_at=cast(float, value["entered_at"]),
        slice_attempt=cast(int, value["slice_attempt"]),
        owner_agent_id=cast(str | None, value.get("owner_agent_id")),
        invocation_generation=cast(int, value.get("invocation_generation", 0)),
        plan_revision=cast(int, value.get("plan_revision", 0)),
        evidence=dict(cast(Mapping[str, object], value.get("evidence", {}))),
    )


def encode_recovery(value: RecoveryState) -> dict[str, object]:
    """Encode recovery state into the closed JSON checkpoint shape."""
    return {
        "cause": value.cause,
        "phase": value.phase.value,
        "recovery_round": value.recovery_round,
        "next_action": value.next_action,
        "owner_run_id": value.owner_run_id,
        "entered_at": value.entered_at,
        "slice_attempt": value.slice_attempt,
        "owner_agent_id": value.owner_agent_id,
        "invocation_generation": value.invocation_generation,
        "plan_revision": value.plan_revision,
        "evidence": dict(value.evidence),
    }


def encode_recovery_identity(value: RecoveryIdentity) -> dict[str, object]:
    """Encode immutable compare-and-set identity for a journal entry."""
    return {
        "run_id": value.run_id,
        "slice_id": value.slice_id,
        "owner_agent_id": value.owner_agent_id,
        "invocation_id": value.invocation_id,
        "invocation_generation": value.invocation_generation,
        "recovery_round": value.recovery_round,
        "branch": value.branch,
        "worktree": value.worktree,
        "plan_revision": value.plan_revision,
    }


def decode_recovery_identity(value: object) -> RecoveryIdentity:
    """Decode a validated journal identity."""
    if not isinstance(value, Mapping):
        raise TypeError("recovery identity must be an object")
    return RecoveryIdentity(
        run_id=cast(str, value["run_id"]),
        slice_id=cast(str, value["slice_id"]),
        owner_agent_id=cast(str, value["owner_agent_id"]),
        invocation_id=cast(str, value["invocation_id"]),
        invocation_generation=cast(int, value["invocation_generation"]),
        recovery_round=cast(int, value["recovery_round"]),
        branch=cast(str, value["branch"]),
        worktree=cast(str, value["worktree"]),
        plan_revision=cast(int, value.get("plan_revision", 0)),
    )


def encode_recovery_intent(value: RecoveryIntent) -> dict[str, object]:
    """Encode a journaled recovery intent."""
    return {
        "intent_id": value.intent_id,
        "recovery_identity": encode_recovery_identity(value.recovery_identity),
        "action": value.action,
        "expected_worktree_fingerprint": value.expected_worktree_fingerprint,
        "state": value.state,
    }


def decode_recovery_intent(value: object) -> RecoveryIntent:
    """Decode a persisted recovery intent."""
    if not isinstance(value, Mapping):
        raise TypeError("recovery intent must be an object")
    action = value.get("action")
    state = value.get("state", "intended")
    if action not in {"probe", "resume_same_owner", "open_gate", "abandon"}:
        raise ValueError(f"unknown recovery intent action {action!r}")
    if state not in {"intended", "confirmed", "unknown", "reconciled"}:
        raise ValueError(f"unknown recovery intent state {state!r}")
    return RecoveryIntent(
        intent_id=cast(str, value["intent_id"]),
        recovery_identity=decode_recovery_identity(value["recovery_identity"]),
        action=cast(RecoveryIntentAction, action),
        expected_worktree_fingerprint=cast(str, value["expected_worktree_fingerprint"]),
        state=cast(RecoveryIntentStatus, state),
    )


__all__ = [
    "RecoveryIdentity",
    "RecoveryIntent",
    "RecoveryIntentAction",
    "RecoveryIntentStatus",
    "RecoveryPhase",
    "RecoveryState",
    "RecoveryTransitionError",
    "assert_recovery_identity",
    "begin_recovery",
    "decode_recovery",
    "decode_recovery_identity",
    "decode_recovery_intent",
    "encode_recovery",
    "encode_recovery_identity",
    "encode_recovery_intent",
    "transition_recovery",
]
