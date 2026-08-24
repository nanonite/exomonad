"""Termination, action deduplication, and convergence telemetry contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tl_loop.loop.convergence import ConvergenceInvariantError, ConvergenceTracker
from tl_loop.loop.reconcile import ExternalIntent, Quiescent
from tl_loop.tests.test_merge_policy import _mergeable


def test_merge_intent_is_queued_once_for_one_state_version() -> None:
    tracker = ConvergenceTracker()
    state = _mergeable()

    result = tracker.reduce(state)

    assert isinstance(result.decision, ExternalIntent)
    assert [event.event_type for event in result.events] == ["tl.action_queued"]
    assert result.events[0].payload["state_version"] == 0
    started = tracker.action_started(state, result.decision)
    assert started.event_type == "tl.action_started"

    with pytest.raises(ConvergenceInvariantError, match="repeated_state_version_action"):
        tracker.reduce(state)


def test_quiescent_wait_reason_is_emitted_only_when_it_changes() -> None:
    tracker = ConvergenceTracker()
    state = replace(_mergeable(), handoff=None)

    first = tracker.reduce(state)
    second = tracker.reduce(state)

    assert first.decision == Quiescent("await_handoff")
    assert [event.event_type for event in first.events] == ["tl.wait_reason_changed"]
    assert second.events == ()


def test_internal_transition_increments_run_state_version() -> None:
    tracker = ConvergenceTracker()
    state = replace(_mergeable(), publication=replace(_mergeable().publication, head_sha="head-b"))
    # A direct slice has no run-level version; the reducer still emits the
    # transition and leaves the slice immutable for its caller to checkpoint.
    result = tracker.reduce(state)
    assert result.decision.transition == "in_review"
    assert result.state == state


def test_action_outcome_is_bounded_and_typed() -> None:
    tracker = ConvergenceTracker()
    state = _mergeable()
    intent = tracker.reduce(state).decision
    assert isinstance(intent, ExternalIntent)

    unknown = tracker.action_outcome(state, intent, outcome="unknown", error="transport lost")
    reconciled = tracker.action_outcome(state, intent, outcome="reconciled")
    assert unknown.event_type == "tl.action_unknown"
    assert unknown.payload["error"] == "transport lost"
    assert reconciled.event_type == "tl.action_reconciled"
