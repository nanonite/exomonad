"""Direct tests for the fail-closed narrowing guards added in #1324."""

from __future__ import annotations

import pytest

from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.driver import TLLoopError, _require_plan_manifest
from tl_loop.loop.recovery_control import RecoveryCommandError, _require_recovery
from tl_loop.state.schema import (
    BudgetLedger,
    EventCursor,
    FSMState,
    RunState,
    SliceState,
    SliceStatus,
)


def _slice(slice_id: str = "slice-a") -> SliceState:
    return SliceState(
        id=slice_id,
        status=SliceStatus.SPAWNED,
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="codex",
        model="gpt-5",
        branch="task/slice-a",
        worktree=".worktrees/slice-a",
        pr_number=None,
        reviewed_head=None,
        attempts=1,
        verdict=None,
    )


def _state(*, slice_state: SliceState) -> RunState:
    return RunState(
        version=1,
        revision=0,
        run_id="guard-run",
        fsm=FSMState(TLPhase.TLPlanning, ("slice-a",)),
        slices={slice_state.id: slice_state},
        budgets=BudgetLedger(0, 0),
        gates=(),
        events=EventCursor(0),
    )


def test_require_plan_manifest_rejects_a_run_without_a_manifest() -> None:
    state = _state(slice_state=_slice())
    assert state.plan_manifest is None

    with pytest.raises(TLLoopError) as raised:
        _require_plan_manifest(state)

    assert str(raised.value) == "run checkpoint has no plan manifest"


def test_require_recovery_rejects_a_slice_without_recovery() -> None:
    slice_state = _slice("slice-without-recovery")
    assert slice_state.recovery is None

    with pytest.raises(RecoveryCommandError) as raised:
        _require_recovery(slice_state)

    assert str(raised.value) == "slice 'slice-without-recovery' has no active recovery"
