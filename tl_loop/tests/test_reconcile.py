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


@pytest.mark.parametrize(
    "status",
    [
        SliceStatus.PENDING,
        SliceStatus.READY,
        SliceStatus.DISPATCHING,
        SliceStatus.DISPATCH_UNCONFIRMED,
        SliceStatus.SPAWNED,
        SliceStatus.IN_REVIEW,
        SliceStatus.REPAIRING,
        SliceStatus.MERGED,
        SliceStatus.FAILED,
        SliceStatus.PARKED,
        SliceStatus.BLOCKED,
    ],
)
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
    assert result.next_action
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
    )
    assert result.missing_evidence == ()
    assert result.next_action == "await_merge_event"


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
