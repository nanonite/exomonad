"""Bounded recovery telemetry and outcome-attribution coverage."""

from __future__ import annotations

import pytest

from tl_loop.events.recovery import RecoveryDimensions, RecoveryTelemetryError


def test_environmental_recovery_preserves_declared_difficulty_without_capability_failure() -> None:
    dimensions = RecoveryDimensions.from_values(
        cause="base_ci_unstable",
        slice_attempt=5,
        invocation_generation=7,
        recovery_round=6,
        authorization_source="human",
        outcome="escalated",
        recursive_depth=3,
        parallel_impact="sibling_wait",
        policy_decision="approve_scope",
        execution_seconds=61,
        recovery_wait_seconds=301,
        human_wait_seconds=1801,
        review_seconds=30,
        declared_difficulty="hard",
        matched_difficulty_rule="high_risk_path",
    )

    assert dimensions.difficulty_attribution == "environmental_recovery"
    assert dimensions.declared_difficulty.value == "hard"
    assert dimensions.aggregate_dimensions() == {
        "cause": "base_ci_unstable",
        "slice_attempt_bucket": "5+",
        "invocation_generation_bucket": "5+",
        "recovery_round_bucket": "5+",
        "authorization_source": "human",
        "outcome": "escalated",
        "recursive_depth_bucket": "3-4",
        "parallel_impact": "sibling_wait",
        "policy_decision": "approve_scope",
        "execution_time_bucket": "1-5m",
        "recovery_wait_time_bucket": "5-30m",
        "human_wait_time_bucket": "30m+",
        "review_time_bucket": "0-60s",
        "declared_difficulty": "hard",
        "matched_difficulty_rule": "high_risk_path",
        "difficulty_attribution": "environmental_recovery",
    }


def test_aggregate_projection_has_no_identity_or_raw_evidence() -> None:
    dimensions = RecoveryDimensions.from_payload(
        {
            "slice_id": "secret-slice",
            "invocation_id": "secret-invocation",
            "cause": "external_dependency",
            "outcome": "recovered",
            "attempt": 1,
            "invocation_generation": 0,
            "branch": "/private/worktree/branch",
            "message": "secret diagnostic",
        }
    )
    aggregate = dimensions.aggregate_dimensions()
    assert "slice_id" not in aggregate
    assert "invocation_id" not in aggregate
    assert "branch" not in aggregate
    assert "message" not in aggregate


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outcome", "completed"),
        ("authorization_source", "automatic"),
        ("parallel_impact", "all_siblings"),
        ("execution_seconds", -1),
    ],
)
def test_recovery_dimensions_reject_unknown_or_unbounded_values(field: str, value: object) -> None:
    payload: dict[str, object] = {
        "cause": "tooling_unavailable",
        "outcome": "abandoned",
        "authorization_source": "policy",
        "parallel_impact": "none",
        "execution_seconds": 0,
    }
    payload[field] = value
    with pytest.raises(RecoveryTelemetryError):
        RecoveryDimensions.from_payload(payload)
