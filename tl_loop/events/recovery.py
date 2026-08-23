"""Privacy-safe recovery dimensions for Failure Atlas projections.

Recovery evidence is intentionally richer locally than at the aggregate
boundary.  This module turns counters and elapsed durations into closed,
bounded dimensions so environmental recovery cannot be mistaken for an
implementation or harness capability failure.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from tl_loop.select.classify import Difficulty

from .types import BlockCause


class RecoveryTelemetryError(ValueError):
    """A recovery telemetry payload is outside the closed vocabulary."""


class AuthorizationSource(str, Enum):
    """The authority that selected a recovery action."""

    POLICY = "policy"
    HUMAN = "human"


class RecoveryOutcome(str, Enum):
    """One terminal outcome for one invocation generation."""

    RECOVERED = "recovered"
    ESCALATED = "escalated"
    ABANDONED = "abandoned"


class ParallelImpact(str, Enum):
    """Bounded effect of recovery on sibling scheduling."""

    NONE = "none"
    SIBLING_PROGRESS = "sibling_progress"
    SIBLING_WAIT = "sibling_wait"


class PolicyDecision(str, Enum):
    """Normalized action selected by policy or a human gate."""

    RETRY = "retry"
    WAIT = "wait"
    APPROVE_SCOPE = "approve_scope"
    ABANDON = "abandon"
    NONE = "none"


_ENVIRONMENTAL_CAUSES = frozenset(
    {
        BlockCause.BASE_CI_UNSTABLE,
        BlockCause.EXTERNAL_DEPENDENCY,
        BlockCause.TOOLING_UNAVAILABLE,
        BlockCause.HUMAN_DECISION_REQUIRED,
        BlockCause.SCOPE_BOUNDARY,
    }
)
_COUNTER_BUCKETS = ((1, "1"), (2, "2"), (4, "3-4"))
_DURATION_BUCKETS = ((60.0, "0-60s"), (300.0, "1-5m"), (1800.0, "5-30m"))
_DIFFICULTY_RULES = frozenset(
    {
        "high_risk_path",
        "cross_language_span",
        "broad_path_scope",
        "dependency_fan_in",
        "long_test_plan",
        "missing_test_plan",
        "focused_slice",
        "standard_slice",
        "watcher_ci_attribution",
        "recovery",
        "unknown",
    }
)


def _counter_bucket(value: int) -> str:
    if type(value) is not int or value < 0:
        raise RecoveryTelemetryError("recovery counters must be non-negative integers")
    if value == 0:
        return "0"
    for upper, label in _COUNTER_BUCKETS:
        if value <= upper:
            return label
    return "5+"


def _attempt_bucket(value: int) -> str:
    if type(value) is not int or value <= 0:
        raise RecoveryTelemetryError("slice attempt must be a positive integer")
    return _counter_bucket(value)


def _duration_bucket(value: object) -> str:
    if value is None:
        return "unknown"
    if type(value) not in (int, float) or isinstance(value, bool):
        raise RecoveryTelemetryError("durations must be non-negative numbers or null")
    if not math.isfinite(float(value)) or value < 0:
        raise RecoveryTelemetryError("durations must be finite and non-negative")
    for upper, label in _DURATION_BUCKETS:
        if value <= upper:
            return label
    return "30m+"


def _enum_value(enum_type: type[Enum], value: object, field: str) -> str:
    try:
        return enum_type(value).value  # type: ignore[no-any-return]
    except (TypeError, ValueError) as error:
        raise RecoveryTelemetryError(f"{field} is outside the closed vocabulary") from error


@dataclass(frozen=True)
class RecoveryDimensions:
    """Bounded dimensions suitable for aggregate Failure Atlas exports."""

    normalized_cause: BlockCause
    slice_attempt_bucket: str
    invocation_generation_bucket: str
    recovery_round_bucket: str
    authorization_source: Literal["policy", "human"]
    outcome: Literal["recovered", "escalated", "abandoned"]
    recursive_depth_bucket: str
    parallel_impact: Literal["none", "sibling_progress", "sibling_wait"] = "none"
    policy_decision: Literal["retry", "wait", "approve_scope", "abandon", "none"] = "none"
    execution_time_bucket: str = "unknown"
    recovery_wait_time_bucket: str = "unknown"
    human_wait_time_bucket: str = "unknown"
    review_time_bucket: str = "unknown"
    declared_difficulty: Difficulty = Difficulty.STANDARD
    matched_difficulty_rule: str = "unknown"
    difficulty_attribution: Literal["implementation", "environmental_recovery"] = (
        "environmental_recovery"
    )

    @classmethod
    def from_values(
        cls,
        *,
        cause: BlockCause | str,
        slice_attempt: int,
        invocation_generation: int,
        recovery_round: int,
        authorization_source: AuthorizationSource | str,
        outcome: RecoveryOutcome | str,
        recursive_depth: int,
        parallel_impact: ParallelImpact | str = ParallelImpact.NONE,
        policy_decision: PolicyDecision | str = PolicyDecision.NONE,
        execution_seconds: object = None,
        recovery_wait_seconds: object = None,
        human_wait_seconds: object = None,
        review_seconds: object = None,
        declared_difficulty: Difficulty | str = Difficulty.STANDARD,
        matched_difficulty_rule: str = "unknown",
    ) -> RecoveryDimensions:
        try:
            normalized_cause = BlockCause(cause)
            difficulty = Difficulty(declared_difficulty)
        except (TypeError, ValueError) as error:
            raise RecoveryTelemetryError("cause or declared difficulty is invalid") from error
        if not isinstance(matched_difficulty_rule, str) or not matched_difficulty_rule:
            raise RecoveryTelemetryError("matched difficulty rule must be non-empty")
        matched_difficulty_rule = (
            matched_difficulty_rule if matched_difficulty_rule in _DIFFICULTY_RULES else "unknown"
        )
        source = _enum_value(AuthorizationSource, authorization_source, "authorization_source")
        terminal = _enum_value(RecoveryOutcome, outcome, "outcome")
        impact = _enum_value(ParallelImpact, parallel_impact, "parallel_impact")
        decision = _enum_value(PolicyDecision, policy_decision, "policy_decision")
        attribution = (
            "environmental_recovery"
            if normalized_cause in _ENVIRONMENTAL_CAUSES
            else "implementation"
        )
        return cls(
            normalized_cause=normalized_cause,
            slice_attempt_bucket=_attempt_bucket(slice_attempt),
            invocation_generation_bucket=_counter_bucket(invocation_generation),
            recovery_round_bucket=_counter_bucket(recovery_round),
            authorization_source=source,  # type: ignore[arg-type]
            outcome=terminal,  # type: ignore[arg-type]
            recursive_depth_bucket=_counter_bucket(recursive_depth),
            parallel_impact=impact,  # type: ignore[arg-type]
            policy_decision=decision,  # type: ignore[arg-type]
            execution_time_bucket=_duration_bucket(execution_seconds),
            recovery_wait_time_bucket=_duration_bucket(recovery_wait_seconds),
            human_wait_time_bucket=_duration_bucket(human_wait_seconds),
            review_time_bucket=_duration_bucket(review_seconds),
            declared_difficulty=difficulty,
            matched_difficulty_rule=matched_difficulty_rule,
            difficulty_attribution=attribution,  # type: ignore[arg-type]
        )

    @classmethod
    def from_payload(
        cls, payload: object, *, envelope_generation: int | None = None
    ) -> RecoveryDimensions:
        """Project a recovery outcome while ignoring local-only evidence fields."""
        if not isinstance(payload, Mapping):
            raise RecoveryTelemetryError("recovery payload must be an object")
        generation = payload.get("invocation_generation", envelope_generation)
        if generation is None:
            generation = 0
        return cls.from_values(
            cause=payload.get("cause"),
            slice_attempt=payload.get("slice_attempt", payload.get("attempt", 0)),
            invocation_generation=generation,
            recovery_round=payload.get("recovery_round", 0),
            authorization_source=payload.get("authorization_source", "policy"),
            outcome=payload.get("outcome"),
            recursive_depth=payload.get("recursive_depth", payload.get("depth", 0)),
            parallel_impact=payload.get("parallel_impact", "none"),
            policy_decision=payload.get("policy_decision", "none"),
            execution_seconds=payload.get("execution_seconds"),
            recovery_wait_seconds=payload.get("recovery_wait_seconds"),
            human_wait_seconds=payload.get("human_wait_seconds"),
            review_seconds=payload.get("review_seconds"),
            declared_difficulty=payload.get("declared_difficulty", Difficulty.STANDARD.value),
            matched_difficulty_rule=payload.get("matched_difficulty_rule", "unknown"),
        )

    def aggregate_dimensions(self) -> dict[str, str]:
        """Return only bounded, normalized fields; no identity or raw evidence."""
        return {
            "cause": self.normalized_cause.value,
            "slice_attempt_bucket": self.slice_attempt_bucket,
            "invocation_generation_bucket": self.invocation_generation_bucket,
            "recovery_round_bucket": self.recovery_round_bucket,
            "authorization_source": self.authorization_source,
            "outcome": self.outcome,
            "recursive_depth_bucket": self.recursive_depth_bucket,
            "parallel_impact": self.parallel_impact,
            "policy_decision": self.policy_decision,
            "execution_time_bucket": self.execution_time_bucket,
            "recovery_wait_time_bucket": self.recovery_wait_time_bucket,
            "human_wait_time_bucket": self.human_wait_time_bucket,
            "review_time_bucket": self.review_time_bucket,
            "declared_difficulty": self.declared_difficulty.value,
            "matched_difficulty_rule": self.matched_difficulty_rule,
            "difficulty_attribution": self.difficulty_attribution,
        }


__all__ = [
    "AuthorizationSource",
    "ParallelImpact",
    "PolicyDecision",
    "RecoveryDimensions",
    "RecoveryOutcome",
    "RecoveryTelemetryError",
]
