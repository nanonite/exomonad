"""Bounded recovery policy and durable probe decision coverage."""

from __future__ import annotations

import pytest

from tl_loop.events.envelope import BlockCause
from tl_loop.fsm.recovery import RecoveryPhase, begin_recovery, decode_recovery, encode_recovery
from tl_loop.loop.recovery_policy import (
    RecoveryAction,
    RecoveryPolicy,
    RecoveryPolicyError,
    ProbeResult,
    apply_probe_result,
    authoritative_recovery_signal,
    decide_recovery,
    policy_for_cause,
    schedule_probe,
)


def _base_recovery(*, entered_at: float = 0.0):
    return begin_recovery(
        cause=BlockCause.BASE_CI_UNSTABLE.value,
        owner_run_id="run-1",
        slice_attempt=1,
        owner_agent_id="agent-1",
        entered_at=entered_at,
        evidence={
            "base_sha": "base-1",
            "scope_attribution": "base",
            "attribution": "base_pre_existing",
        },
    )


def test_policy_table_covers_every_closed_cause_with_finite_values() -> None:
    assert set(policy_for_cause(cause).cause for cause in BlockCause) == set(BlockCause)
    for cause in BlockCause:
        policy = policy_for_cause(cause)
        assert policy.max_rounds >= 0
        assert policy.max_wait_seconds >= 0
        if policy.automatic_resume:
            assert policy.max_rounds > 0
            assert policy.max_wait_seconds > 0


def test_human_causes_gate_without_a_probe() -> None:
    state = begin_recovery(
        cause=BlockCause.SCOPE_BOUNDARY.value,
        owner_run_id="run-1",
        slice_attempt=1,
        owner_agent_id="agent-1",
        entered_at=10.0,
    )
    decision = decide_recovery(state, now=10.0)
    assert decision.action is RecoveryAction.HUMAN_GATE
    assert decision.policy.probe_kind == "none"


def test_base_probe_rejects_head_attribution_and_accepts_new_signal() -> None:
    state = _base_recovery()
    result = ProbeResult(True, "base-1", "ci-revision-2", 2)
    assert authoritative_recovery_signal(state, result)

    head_state = begin_recovery(
        cause=BlockCause.BASE_CI_UNSTABLE.value,
        owner_run_id="run-1",
        slice_attempt=1,
        owner_agent_id="agent-1",
        entered_at=0.0,
        evidence={
            "base_sha": "base-1",
            "scope_attribution": "head",
            "attribution": "head_introduced",
        },
    )
    assert not authoritative_recovery_signal(head_state, result)


def test_probe_schedule_is_durable_and_replay_safe() -> None:
    state = _base_recovery()
    scheduled = schedule_probe(state, now=10.0, event_seq=4)
    assert scheduled.phase is RecoveryPhase.WAITING_SIGNAL
    assert scheduled.probe_count == 1
    assert scheduled.next_probe_at == 40.0
    assert decode_recovery(encode_recovery(scheduled)) == scheduled
    assert decide_recovery(scheduled, now=39.9).action is RecoveryAction.WAIT
    assert decide_recovery(scheduled, now=40.0).action is RecoveryAction.PROBE


def test_authoritative_probe_transitions_to_same_owner_resume() -> None:
    state = schedule_probe(_base_recovery(), now=10.0)
    resumed = apply_probe_result(
        state,
        ProbeResult(True, "base-1", "ci-revision-2", 2),
        now=50.0,
    )
    assert resumed.phase is RecoveryPhase.RESUME_INTENDED
    assert resumed.next_action == "resume_same_owner"
    assert resumed.evidence["last_authoritative_event_seq"] == 2
    assert (
        apply_probe_result(resumed, ProbeResult(True, "base-1", "ci-revision-2", 2), now=51.0)
        == resumed
    )


def test_policy_rejects_unbounded_automatic_recovery() -> None:
    with pytest.raises(RecoveryPolicyError, match="finite positive bounds"):
        RecoveryPolicy(BlockCause.BASE_CI_UNSTABLE, 0, 0.0, "base_ci", True, False, (1.0,))
