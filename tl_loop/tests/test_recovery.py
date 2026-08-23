"""Independent pre-publication recovery FSM and persistence coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from tl_loop.fsm.phase import TLPhase
from tl_loop.fsm.recovery import (
    RecoveryPhase,
    RecoveryTransitionError,
    begin_recovery,
    decode_recovery,
    encode_recovery,
    transition_recovery,
)
from tl_loop.state.schema import FSMState, SliceState, SliceStatus
from tl_loop.state.store import RunStore, create


def test_recovery_transitions_are_bounded_and_explicit() -> None:
    initial = begin_recovery(
        cause="human_decision_required",
        owner_run_id="run-1",
        slice_attempt=2,
        owner_agent_id="agent-1",
        entered_at=10.0,
        evidence={"classification": "missing_exit_marker"},
    )

    revalidated = transition_recovery(
        initial,
        RecoveryPhase.REVALIDATING,
        next_action="inspect_worktree",
        entered_at=11.0,
    )

    assert initial.phase is RecoveryPhase.DIAGNOSING
    assert revalidated.phase is RecoveryPhase.REVALIDATING
    assert revalidated.recovery_round == 1
    assert revalidated.evidence == initial.evidence
    with pytest.raises(RecoveryTransitionError, match="diagnosing to resuming"):
        transition_recovery(
            initial,
            RecoveryPhase.RESUMING,
            next_action="resume",
            entered_at=12.0,
        )


def test_recovery_json_shape_round_trips_without_review_state() -> None:
    initial = begin_recovery(
        cause="base_ci_unstable",
        owner_run_id="run-1",
        slice_attempt=1,
        owner_agent_id=None,
        entered_at=10.0,
        invocation_generation=3,
        plan_revision=7,
        evidence={"base_sha": "base-1"},
    )

    restored = decode_recovery(encode_recovery(initial))

    assert restored == initial
    assert restored is not None


def test_recovery_checkpoint_survives_resume(tmp_path: Path) -> None:
    store = RunStore("run-1", tmp_path)
    create("run-1", {}, root_dir=tmp_path)
    slice_state = SliceState(
        id="slice-a",
        status=SliceStatus.READY,
        paths=("src",),
        depends_on=(),
        base_ref="main",
        test_plan=(),
        agent_type="codex",
        model="gpt-5",
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=1,
        verdict=None,
        recovery=begin_recovery(
            cause="human_decision_required",
            owner_run_id="run-1",
            slice_attempt=1,
            owner_agent_id="agent-a",
            entered_at=10.0,
        ),
    )

    checkpointed = store.checkpoint(
        FSMState(TLPhase.TLPlanning, ()),
        {"slice-a": slice_state},
        store.load().budgets,
        offset=4,
    )
    resumed = store.resume()

    assert checkpointed.slices["slice-a"].recovery == slice_state.recovery
    assert resumed.slices["slice-a"].recovery == slice_state.recovery
    assert resumed.offset == 4
