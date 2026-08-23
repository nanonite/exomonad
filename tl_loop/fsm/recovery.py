"""Independent pre-publication execution-recovery state machine."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import cast


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


__all__ = [
    "RecoveryPhase",
    "RecoveryState",
    "RecoveryTransitionError",
    "begin_recovery",
    "decode_recovery",
    "encode_recovery",
    "transition_recovery",
]
