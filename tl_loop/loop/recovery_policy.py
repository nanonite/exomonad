"""Closed recovery policies and replay-safe probe decisions.

Recovery decisions are driven by typed watcher evidence and durable state.  This
module deliberately does not classify free-form agent text: a cause must come
from :class:`BlockCause` and a recovery signal must carry an authoritative
event sequence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Literal

from tl_loop.events.envelope import BlockCause
from tl_loop.fsm.recovery import RecoveryPhase, RecoveryState, transition_recovery

ProbeKind = Literal["base_ci", "dependency", "runtime_health", "none"]


class RecoveryPolicyError(ValueError):
    """A recovery policy or probe result violates the closed contract."""


class RecoveryAction(str, Enum):
    """The only actions a probe decision may request."""

    WAIT = "wait"
    PROBE = "probe"
    RESUME = "resume_same_owner"
    HUMAN_GATE = "human_gate"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class RecoveryPolicy:
    """Finite policy for one typed blocked-task cause."""

    cause: BlockCause
    max_rounds: int
    max_wait_seconds: float
    probe_kind: ProbeKind
    automatic_resume: bool
    immediate_human_gate: bool
    backoff_seconds: tuple[float, ...]

    def __post_init__(self) -> None:
        if type(self.max_rounds) is not int or self.max_rounds < 0:
            raise RecoveryPolicyError("max_rounds must be a non-negative integer")
        if type(self.max_wait_seconds) is not float or self.max_wait_seconds < 0:
            raise RecoveryPolicyError("max_wait_seconds must be a non-negative float")
        if self.probe_kind not in {"base_ci", "dependency", "runtime_health", "none"}:
            raise RecoveryPolicyError(f"unsupported probe kind {self.probe_kind!r}")
        if not self.backoff_seconds and self.probe_kind != "none":
            raise RecoveryPolicyError("automatic probes require a non-empty backoff")
        if any(delay < 0 for delay in self.backoff_seconds):
            raise RecoveryPolicyError("backoff values must be non-negative")
        if tuple(sorted(self.backoff_seconds)) != self.backoff_seconds:
            raise RecoveryPolicyError("backoff values must be monotonic")
        if self.automatic_resume and self.immediate_human_gate:
            raise RecoveryPolicyError("automatic resume and immediate human gate conflict")
        if self.automatic_resume and (self.max_rounds == 0 or self.max_wait_seconds == 0):
            raise RecoveryPolicyError("automatic recovery requires finite positive bounds")

    def backoff_for_round(self, recovery_round: int) -> float:
        """Return the bounded delay for a zero-based recovery round."""
        if recovery_round < 0:
            raise RecoveryPolicyError("recovery_round must be non-negative")
        if not self.backoff_seconds:
            return 0.0
        return self.backoff_seconds[min(recovery_round, len(self.backoff_seconds) - 1)]


@dataclass(frozen=True)
class ProbeResult:
    """Typed result of a durable dependency/runtime/base probe."""

    healthy: bool
    observed_base_sha: str | None
    signal_revision: str | None
    authoritative_event_seq: int

    def __post_init__(self) -> None:
        for name in ("observed_base_sha", "signal_revision"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise RecoveryPolicyError(f"{name} must be null or non-empty text")
        if type(self.authoritative_event_seq) is not int or self.authoritative_event_seq < 0:
            raise RecoveryPolicyError("authoritative_event_seq must be non-negative")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ProbeResult:
        """Decode only the structured probe fields; reject ambiguous input."""
        healthy = value.get("healthy")
        sequence = value.get("authoritative_event_seq")
        if type(healthy) is not bool or type(sequence) is not int:
            raise RecoveryPolicyError("probe requires boolean healthy and integer event sequence")
        base_sha = value.get("observed_base_sha")
        revision = value.get("signal_revision")
        if base_sha is not None and not isinstance(base_sha, str):
            raise RecoveryPolicyError("observed_base_sha must be text or null")
        if revision is not None and not isinstance(revision, str):
            raise RecoveryPolicyError("signal_revision must be text or null")
        return cls(healthy, base_sha, revision, sequence)


@dataclass(frozen=True)
class RecoveryDecision:
    """Pure decision returned before any effect is journaled or dispatched."""

    action: RecoveryAction
    policy: RecoveryPolicy
    next_probe_at: float | None
    reason: str


_POLICIES = {
    BlockCause.BASE_CI_UNSTABLE: RecoveryPolicy(
        BlockCause.BASE_CI_UNSTABLE, 3, 1800.0, "base_ci", True, False, (30.0, 120.0, 300.0)
    ),
    BlockCause.EXTERNAL_DEPENDENCY: RecoveryPolicy(
        BlockCause.EXTERNAL_DEPENDENCY,
        3,
        1800.0,
        "dependency",
        True,
        False,
        (30.0, 120.0, 300.0),
    ),
    BlockCause.SCOPE_BOUNDARY: RecoveryPolicy(
        BlockCause.SCOPE_BOUNDARY, 0, 0.0, "none", False, True, ()
    ),
    BlockCause.HUMAN_DECISION_REQUIRED: RecoveryPolicy(
        BlockCause.HUMAN_DECISION_REQUIRED, 0, 0.0, "none", False, True, ()
    ),
    BlockCause.TOOLING_UNAVAILABLE: RecoveryPolicy(
        BlockCause.TOOLING_UNAVAILABLE, 0, 0.0, "none", False, True, ()
    ),
}
POLICIES: Mapping[BlockCause, RecoveryPolicy] = MappingProxyType(_POLICIES)


def policy_for_cause(cause: BlockCause | str) -> RecoveryPolicy:
    """Resolve a cause from the closed vocabulary, failing closed on drift."""
    try:
        parsed = cause if isinstance(cause, BlockCause) else BlockCause(cause)
    except (TypeError, ValueError) as error:
        raise RecoveryPolicyError(f"unknown recovery cause {cause!r}") from error
    return POLICIES[parsed]


def _evidence_text(state: RecoveryState, key: str) -> str | None:
    value = state.evidence.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _prior_event_seq(state: RecoveryState) -> int:
    value = state.evidence.get("last_authoritative_event_seq")
    return value if type(value) is int and value >= 0 else 0


def authoritative_recovery_signal(
    state: RecoveryState, result: ProbeResult, *, policy: RecoveryPolicy | None = None
) -> bool:
    """Accept a recovery signal only when it is typed, newer, and in scope."""
    selected = policy or policy_for_cause(state.cause)
    if not result.healthy or not result.signal_revision:
        return False
    if result.authoritative_event_seq <= _prior_event_seq(state):
        return False
    if selected.probe_kind == "base_ci":
        attribution = _evidence_text(state, "attribution")
        scope = _evidence_text(state, "scope_attribution")
        expected_base = _evidence_text(state, "base_sha")
        return (
            attribution == "base_pre_existing"
            and scope == "base"
            and result.observed_base_sha is not None
            and (expected_base is None or result.observed_base_sha == expected_base)
        )
    return True


def decide_recovery(
    state: RecoveryState,
    *,
    now: float,
    probe_result: ProbeResult | None = None,
) -> RecoveryDecision:
    """Return the next bounded action without mutating state or calling tools."""
    policy = policy_for_cause(state.cause)
    if policy.immediate_human_gate or state.phase is RecoveryPhase.HUMAN_GATE:
        return RecoveryDecision(RecoveryAction.HUMAN_GATE, policy, None, "human_decision_required")
    if (
        state.recovery_round >= policy.max_rounds
        or now - state.entered_at >= policy.max_wait_seconds
    ):
        return RecoveryDecision(RecoveryAction.EXHAUSTED, policy, None, "recovery_policy_exhausted")
    if probe_result is not None and authoritative_recovery_signal(
        state, probe_result, policy=policy
    ):
        action = RecoveryAction.RESUME if policy.automatic_resume else RecoveryAction.HUMAN_GATE
        return RecoveryDecision(action, policy, None, "authoritative_recovery_signal")
    if state.next_probe_at is not None and now < state.next_probe_at:
        return RecoveryDecision(RecoveryAction.WAIT, policy, state.next_probe_at, "backoff_active")
    return RecoveryDecision(
        RecoveryAction.PROBE,
        policy,
        now + policy.backoff_for_round(state.recovery_round),
        "probe_due",
    )


def schedule_probe(
    state: RecoveryState, *, now: float, event_seq: int | None = None
) -> RecoveryState:
    """Persist one probe schedule and its backoff before dispatching any effect."""
    policy = policy_for_cause(state.cause)
    decision = decide_recovery(state, now=now)
    if decision.action is not RecoveryAction.PROBE:
        return state
    evidence = dict(state.evidence)
    evidence["probe_kind"] = policy.probe_kind
    evidence["probe_count"] = state.probe_count + 1
    if event_seq is not None:
        evidence["last_probe_event_seq"] = event_seq
    return replace(
        state,
        phase=RecoveryPhase.WAITING_SIGNAL,
        next_action=f"probe:{policy.probe_kind}",
        next_probe_at=decision.next_probe_at,
        last_probe_at=now,
        probe_count=state.probe_count + 1,
        evidence=evidence,
    )


def apply_probe_result(state: RecoveryState, result: ProbeResult, *, now: float) -> RecoveryState:
    """Apply one authoritative result, preserving evidence for replay."""
    policy = policy_for_cause(state.cause)
    if not authoritative_recovery_signal(state, result, policy=policy):
        return state
    evidence = dict(state.evidence)
    evidence.update(
        {
            "observed_base_sha": result.observed_base_sha,
            "signal_revision": result.signal_revision,
            "last_authoritative_event_seq": result.authoritative_event_seq,
        }
    )
    target = RecoveryPhase.RESUME_INTENDED if policy.automatic_resume else RecoveryPhase.HUMAN_GATE
    return transition_recovery(
        state,
        target,
        next_action="resume_same_owner"
        if target is RecoveryPhase.RESUME_INTENDED
        else "open_human_gate",
        entered_at=now,
        evidence=evidence,
    )


__all__ = [
    "POLICIES",
    "ProbeKind",
    "ProbeResult",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryPolicy",
    "RecoveryPolicyError",
    "apply_probe_result",
    "authoritative_recovery_signal",
    "decide_recovery",
    "policy_for_cause",
    "schedule_probe",
]
