"""Startup reconciliation contracts for persisted TL slices."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tl_loop.loop.reconcile import reconcile_slice
from tl_loop.state.schema import SliceState, SliceStatus
from tl_loop.state.store import RunStore, _encode_slice, create


def _slice(status: SliceStatus) -> SliceState:
    spawned = status in {
        SliceStatus.SPAWNED,
        SliceStatus.IN_REVIEW,
        SliceStatus.REPAIRING,
    }
    return SliceState(
        id="slice-a",
        status=status,
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="codex",
        model="gpt-5",
        branch="task/slice-a",
        worktree=".worktrees/slice-a",
        pr_number=42 if status in {SliceStatus.IN_REVIEW, SliceStatus.REPAIRING} else None,
        reviewed_head=None,
        attempts=1,
        verdict=None,
        dispatch_intent_id="intent-a" if spawned else None,
        dispatch_agent_id="agent-a" if spawned else None,
        dispatch_authoritative_event_seq=7 if spawned else None,
    )


@pytest.mark.parametrize("status", list(SliceStatus))
def test_reconciliation_is_defined_for_every_slice_status(status: SliceStatus) -> None:
    result = reconcile_slice(
        _slice(status),
        authoritative_owner_id="agent-a",
        watcher={
            "found": True,
            "head_sha": "head-a",
            "review_state": "approved",
            "ci_status": "success",
            "merged": status is SliceStatus.MERGED,
        },
    )

    assert result.slice_id == "slice-a"
    expected_action = {
        SliceStatus.DISPATCHING: "await_authoritative_spawn_event",
        SliceStatus.DISPATCH_UNCONFIRMED: "await_authoritative_spawn_event",
        SliceStatus.SPAWNED: "await_review_event",
        SliceStatus.IN_REVIEW: "await_merge_event",
        SliceStatus.REPAIRING: "await_repair_event",
    }.get(status, "no_action")
    assert result.next_action == expected_action
    assert result.as_state()["next_action"] == result.next_action


def test_reconciliation_adopts_authoritative_review_and_ci_evidence() -> None:
    result = reconcile_slice(
        _slice(SliceStatus.IN_REVIEW),
        authoritative_owner_id="agent-a",
        watcher={
            "found": True,
            "head_sha": "head-a",
            "review_state": "approved",
            "ci_status": "success",
            "merged": False,
        },
    )

    assert result.authoritative_evidence == (
        "dispatch_owner",
        "runtime_owner",
        "published_pr",
        "published_head",
        "review_state",
        "ci_state",
        "pr_state_unknown",
    )
    assert result.missing_evidence == ()
    assert result.next_action == "await_merge_event"


def test_closed_unmerged_pr_is_terminal_reconciliation_evidence() -> None:
    result = reconcile_slice(
        _slice(SliceStatus.IN_REVIEW),
        authoritative_owner_id="agent-a",
        watcher={
            "found": True,
            "pr_number": 42,
            "head_sha": "head-a",
            "review_state": "changes_requested",
            "ci_status": "failure",
            "pr_state": "closed",
            "merged": False,
            "head_reachable": True,
        },
    )

    assert result.next_action == "park_closed_unmerged_pr"
    assert "pr_state" in result.authoritative_evidence


def test_open_unmerged_pr_still_waits_for_merge() -> None:
    result = reconcile_slice(
        _slice(SliceStatus.IN_REVIEW),
        authoritative_owner_id="agent-a",
        watcher={
            "found": True,
            "head_sha": "head-a",
            "review_state": "approved",
            "ci_status": "success",
            "pr_state": "open",
            "merged": False,
            "head_reachable": True,
        },
    )

    assert result.next_action == "await_merge_event"


def test_missing_pr_head_is_a_typed_terminal_reconciliation_observation() -> None:
    result = reconcile_slice(
        _slice(SliceStatus.IN_REVIEW),
        authoritative_owner_id="agent-a",
        watcher={
            "found": True,
            "head_sha": "head-a",
            "review_state": "approved",
            "ci_status": "success",
            "pr_state": "open",
            "merged": False,
            "head_reachable": False,
            "evidence_error": "pr_head_unreachable: object missing",
        },
    )

    assert result.next_action == "park_unreachable_pr_head"
    assert "pr_head_unreachable" in result.authoritative_evidence


def test_reconciliation_quarantines_conflicting_owner_and_head() -> None:
    slice_state = _slice(SliceStatus.IN_REVIEW)
    slice_state = slice_state.__class__(
        **{
            **slice_state.__dict__,
            "reviewed_head": "old-head",
        }
    )
    result = reconcile_slice(
        slice_state,
        authoritative_owner_id="different-agent",
        watcher={
            "found": True,
            "head_sha": "new-head",
            "review_state": "approved",
            "ci_status": "success",
            "merged": False,
        },
    )

    assert result.conflicts == (
        "authoritative owner disagrees with persisted dispatch owner",
        "authoritative head disagrees with review evidence",
    )
    assert result.next_action == "open_integrity_gate"


def test_reconciliation_waits_without_authoritative_evidence() -> None:
    result = reconcile_slice(
        _slice(SliceStatus.IN_REVIEW),
        authoritative_owner_id=None,
        watcher=None,
    )

    assert result.missing_evidence == ("runtime_owner", "published_pr")
    assert result.conflicts == ()
    assert result.next_action == "await_authoritative_evidence"


def test_reconciliation_recovers_missing_pr_number_from_watcher_evidence() -> None:
    """A slice whose pr_number was never persisted (crash between pr.filed
    being acknowledged and identity association) still reconciles when the
    caller recovered PR identity via a slice_id-scoped watcher lookup."""
    slice_state = _slice(SliceStatus.SPAWNED)
    assert slice_state.pr_number is None

    result = reconcile_slice(
        slice_state,
        authoritative_owner_id="agent-a",
        watcher={
            "found": True,
            "pr_number": 99,
            "head_sha": "head-a",
            "review_state": "approved",
            "ci_status": "success",
            "merged": False,
        },
    )

    assert "pr_number" not in result.missing_evidence
    assert "published_pr" in result.authoritative_evidence
    assert result.next_action == "await_review_event"


def test_reconciliation_still_reports_missing_pr_number_without_recovered_evidence() -> None:
    result = reconcile_slice(
        _slice(SliceStatus.SPAWNED),
        authoritative_owner_id="agent-a",
        watcher=None,
    )

    assert result.missing_evidence == ("pr_number",)
    assert result.next_action == "await_authoritative_evidence"


def test_reconciliation_evidence_round_trips_through_checkpoint(tmp_path) -> None:
    create(
        "reconcile",
        {"slices": {"slice-a": _encode_slice("slice-a", _slice(SliceStatus.SPAWNED))}},
        root_dir=tmp_path,
    )
    store = RunStore("reconcile", tmp_path)
    state = store.load()
    evidence = reconcile_slice(
        state.slices["slice-a"],
        authoritative_owner_id="agent-a",
        watcher=None,
    ).as_state()
    store.checkpoint(
        state.fsm,
        {
            **state.slices,
            "slice-a": replace(state.slices["slice-a"], reconciliation=evidence),
        },
        state.budgets,
        state.events.last_consumed_offset,
    )

    assert store.load().slices["slice-a"].reconciliation == evidence
