from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.driver import LeafTask, WorkPlan
from tl_loop.loop.redispatch import RedispatchError, redispatch_slice
from tl_loop.state.schema import BudgetLedger, ParkCause, SliceState, SliceStatus
from tl_loop.state.store import RunStore, create


@dataclass
class RecordingTransport:
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        del role, name
        self.calls.append((tool_name, arguments))
        if tool_name == "spawn_leaf":
            return {
                "success": True,
                "result": {
                    "agent_id": "slice-a-attempt-2-opencode",
                    "invocation_id": "inv-2",
                },
            }
        return {"success": True, "result": {"run_seq": 2}}


def _store(tmp_path: Path, *, cause: ParkCause, attempts: int = 1) -> RunStore:
    state_root = tmp_path / ".exo" / "tl-loop"
    create("root", {}, root_dir=state_root)
    store = RunStore("root", state_root)
    slice_state = SliceState(
        id="slice-a",
        status=SliceStatus.PARKED,
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="opencode",
        model="old-model",
        branch="main.slice-a",
        worktree=".exo/worktrees/slice-a",
        pr_number=42,
        reviewed_head="old-head",
        attempts=attempts,
        verdict=None,
        park_cause=cause,
        park_audit={"attempts": attempts, "reason": "operator_requested"},
        dispatch_intent_id="old-intent",
        dispatch_agent_id="slice-a-opencode",
        dispatch_invocation_id="old-invocation",
        dispatch_authoritative_event_seq=1,
    )
    store.checkpoint(
        TLPhase.TLDispatching,
        {slice_state.id: slice_state},
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    return store


def _plan() -> WorkPlan:
    return WorkPlan(
        leaves=(LeafTask("slice-a", "implement from the plan spec", agent_type="opencode"),)
    )


def test_redispatch_uses_spec_and_fresh_runtime_identity(tmp_path: Path) -> None:
    store = _store(tmp_path, cause=ParkCause.ATTEMPT_ABANDONED)
    transport = RecordingTransport()

    result = redispatch_slice(
        tmp_path,
        "root",
        "slice-a",
        _plan(),
        effects=EffectClient(transport),
    )

    assert result["status"] == "dispatched"
    assert result["attempts"] == 2
    spawn = next(arguments for name, arguments in transport.calls if name == "spawn_leaf")
    assert spawn["name"] == "slice-a-attempt-2"
    assert spawn["task"] == "implement from the plan spec"
    current = store.load().slices["slice-a"]
    assert current.status is SliceStatus.DISPATCH_UNCONFIRMED
    assert current.attempts == 2
    assert current.pr_number is None
    assert current.reviewed_head is None
    assert current.branch is None
    assert current.worktree is None


def test_redispatch_is_only_for_abandoned_attempts(tmp_path: Path) -> None:
    _store(tmp_path, cause=ParkCause.PR_CLOSED_UNMERGED)

    with pytest.raises(RedispatchError, match="only ATTEMPT_ABANDONED"):
        redispatch_slice(
            tmp_path, "root", "slice-a", _plan(), effects=EffectClient(RecordingTransport())
        )


def test_redispatch_exhausts_retry_ceiling_without_dispatch(tmp_path: Path) -> None:
    store = _store(tmp_path, cause=ParkCause.ATTEMPT_ABANDONED, attempts=3)
    transport = RecordingTransport()

    result = redispatch_slice(
        tmp_path,
        "root",
        "slice-a",
        _plan(),
        effects=EffectClient(transport),
        max_attempts=3,
    )

    assert result["status"] == "retries_exhausted"
    assert store.load().slices["slice-a"].park_cause is ParkCause.RETRIES_EXHAUSTED
    assert transport.calls == []


def test_nested_plan_resolves_the_same_dispatch_path(tmp_path: Path) -> None:
    state_root = tmp_path / ".exo" / "tl-loop"
    create("child", {}, root_dir=state_root)
    store = RunStore("child", state_root)
    state = SliceState(
        id="nested-leaf",
        status=SliceStatus.PARKED,
        paths=("src/nested.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="opencode",
        model=None,
        branch="main.nested-leaf",
        worktree=".exo/worktrees/nested-leaf",
        pr_number=7,
        reviewed_head="old",
        attempts=1,
        verdict=None,
        park_cause=ParkCause.ATTEMPT_ABANDONED,
    )
    store.checkpoint(TLPhase.TLDispatching, {state.id: state}, BudgetLedger(0, 0), 0)
    transport = RecordingTransport()
    plan = WorkPlan.from_mapping(
        {
            "sub_tls": [
                {
                    "name": "child",
                    "plan": {"leaves": [{"name": "nested-leaf", "task": "nested spec"}]},
                }
            ]
        }
    )

    result = redispatch_slice(
        tmp_path,
        "child",
        "nested-leaf",
        plan,
        effects=EffectClient(transport),
    )

    assert result["status"] == "dispatched"
    assert any(name == "spawn_leaf" for name, _ in transport.calls)
