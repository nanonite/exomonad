"""Termination, action deduplication, and convergence telemetry contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.loop.convergence import ConvergenceInvariantError, ConvergenceTracker
from tl_loop.loop.reconcile import ExternalIntent, Quiescent
from tl_loop.state.schema import SliceStatus
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


def test_post_merge_action_identity_advances_only_with_persisted_evidence() -> None:
    tracker = ConvergenceTracker()
    merge_evidence = {
        "child_id": "slice-a",
        "repository": "https://forge.example.test/owner/repo.git",
        "parent_branch": "main",
        "pr_number": "42",
        "head_sha": "head-a",
        "merge_journal_id": "merge-journal",
        "lane_epoch": "1",
    }
    state = replace(
        _mergeable(),
        status=SliceStatus.MERGED,
        post_merge=PostMergeState(
            PostMergePhase.REMOTE_MERGE_ADOPTED,
            merge_evidence,
        ),
    )

    first = tracker.reduce(state).decision
    assert isinstance(first, ExternalIntent)
    assert first.operation == "post_merge_recovery"
    with pytest.raises(ConvergenceInvariantError, match="repeated_state_version_action"):
        tracker.reduce(state)

    progressed = replace(
        state,
        post_merge=PostMergeState(
            PostMergePhase.PARENT_BRANCH_SYNCED,
            {
                **merge_evidence,
                "parent_commit_sha": "parent-head",
            },
        ),
    )
    next_step = tracker.reduce(progressed).decision
    assert isinstance(next_step, ExternalIntent)
    assert next_step.operation == "post_merge_recovery"
    assert next_step.arguments != first.arguments
    with pytest.raises(ConvergenceInvariantError, match="repeated_state_version_action"):
        tracker.reduce(progressed)
