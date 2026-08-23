"""DAG, width, ownership, and deadlock scheduler coverage."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from tl_loop.fsm.phase import TLPhase
from tl_loop.fsm.recovery import begin_recovery
from tl_loop.loop.schedule import (
    ScheduleDeadlock,
    active_count,
    propagate_abandonment,
    ready,
    restore_dependents,
    suspend_dependents,
)
from tl_loop.state.schema import SCHEMA_VERSION, SchemaError, SliceState, SliceStatus, validate


def test_diamond_dag_returns_roots_then_join() -> None:
    slices = {
        "a": _slice("a", paths=("src/a.py",)),
        "b": _slice("b", paths=("src/b.py",)),
        "join": _slice("join", paths=("src/join.py",), depends_on=("a", "b")),
    }

    assert [state.id for state in ready(slices)] == ["a", "b"]
    slices["a"] = _slice("a", SliceStatus.MERGED, paths=("src/a.py",))
    slices["b"] = _slice("b", SliceStatus.MERGED, paths=("src/b.py",))

    assert [state.id for state in ready(slices)] == ["join"]


def test_width_gate_counts_active_repair_slices() -> None:
    slices = {
        "a": _slice("a", SliceStatus.SPAWNED, paths=("src/a.py",)),
        "b": _slice("b", SliceStatus.REPAIRING, paths=("src/b.py",)),
        "c": _slice("c", paths=("src/c.py",)),
        "d": _slice("d", paths=("src/d.py",)),
    }

    assert active_count(slices) == 2
    assert [state.id for state in ready(slices, max_parallel_slices=3)] == ["c"]
    assert ready(slices, max_parallel_slices=2) == []


def test_recovering_slice_frees_width_but_keeps_path_occupied() -> None:
    recovery = begin_recovery(
        cause="base_ci_unstable",
        owner_run_id="run",
        slice_attempt=1,
        owner_agent_id="agent",
        entered_at=1,
    )
    slices = {
        "recovering": replace(
            _slice("recovering", SliceStatus.SPAWNED, paths=("src/shared.py",)),
            recovery=recovery,
        ),
        "sibling": _slice("sibling", paths=("src/sibling.py",)),
        "conflict": _slice("conflict", paths=("src/shared.py",)),
    }

    assert active_count(slices) == 0
    assert [state.id for state in ready(slices, max_parallel_slices=1)] == ["sibling"]


def test_dependency_recovery_suspends_restores_and_abandons_idempotently() -> None:
    slices = {
        "blocker": replace(
            _slice("blocker", SliceStatus.SPAWNED),
            recovery=begin_recovery(
                cause="external_dependency",
                owner_run_id="run",
                slice_attempt=1,
                owner_agent_id="agent",
                entered_at=1,
            ),
        ),
        "dependent": _slice("dependent", depends_on=("blocker",)),
        "transitive": _slice("transitive", depends_on=("dependent",)),
        "sibling": _slice("sibling"),
    }

    suspended = suspend_dependents(slices, "blocker", 0)
    assert suspended["dependent"].status is SliceStatus.PENDING
    assert suspended["dependent"].suspended_dependency is not None
    assert suspended["transitive"].suspended_dependency is not None
    assert suspended["sibling"].suspended_dependency is None
    assert suspend_dependents(suspended, "blocker", 0) == suspended

    recovered = dict(suspended)
    recovered["blocker"] = replace(recovered["blocker"], recovery=None)
    restored = restore_dependents(recovered, "blocker", 0)
    assert restored["dependent"].suspended_dependency is None
    assert restored["transitive"].suspended_dependency is None
    assert restore_dependents(restored, "blocker", 0) == restored

    abandoned = propagate_abandonment(suspended, "blocker", 0)
    assert abandoned["dependent"].status is SliceStatus.BLOCKED
    assert abandoned["transitive"].status is SliceStatus.BLOCKED
    assert abandoned["sibling"].status is SliceStatus.PENDING


def test_overlapping_paths_are_serialized() -> None:
    slices = {
        "first": _slice("first", paths=("src/shared/*.py",)),
        "second": _slice("second", paths=("src/shared/main.py",)),
        "independent": _slice("independent", paths=("src/other.py",)),
    }

    assert [state.id for state in ready(slices)] == ["first", "independent"]
    slices["first"] = _slice("first", SliceStatus.SPAWNED, paths=("src/shared/*.py",))
    assert [state.id for state in ready(slices)] == ["independent"]


def test_deadlock_names_blocked_slices_and_dependencies() -> None:
    slices = {
        "a": _slice("a", depends_on=("b",)),
        "b": _slice("b", depends_on=("a",)),
    }

    with pytest.raises(ScheduleDeadlock, match="a.*b"):
        ready(slices)


def test_schema_rejects_cycle_before_scheduler_runs() -> None:
    document = _valid_document()
    slices = cast(dict[str, object], document["slices"])
    slice_a = cast(dict[str, object], slices["a"])
    slice_a["depends_on"] = ["b"]
    slices["b"] = _record("b", depends_on=["a"])

    with pytest.raises(SchemaError, match="depends_on cycle"):
        validate(document)


def _slice(
    slice_id: str,
    status: SliceStatus = SliceStatus.PENDING,
    *,
    paths: tuple[str, ...] = ("src/task.py",),
    depends_on: tuple[str, ...] = (),
) -> SliceState:
    return SliceState(
        id=slice_id,
        status=status,
        paths=paths,
        depends_on=depends_on,
        base_ref="main",
        test_plan=("just tl-loop-test",),
        agent_type=None,
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=0,
        verdict=None,
    )


def _valid_document() -> dict[str, object]:
    return {
        "version": SCHEMA_VERSION,
        "revision": 0,
        "run_id": "schedule-test",
        "fsm": {"phase": TLPhase.TLPlanning.value, "waiting": []},
        "slices": {"a": _record("a"), "b": _record("b")},
        "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        "gates": [],
        "events": {"last_consumed_offset": 0},
    }


def _record(slice_id: str, *, depends_on: list[str] | None = None) -> dict[str, object]:
    return {
        "id": slice_id,
        "status": "pending",
        "paths": [f"src/{slice_id}.py"],
        "depends_on": depends_on or [],
        "base_ref": "main",
        "test_plan": ["just tl-loop-test"],
        "agent_type": None,
        "model": None,
        "branch": None,
        "worktree": None,
        "pr_number": None,
        "reviewed_head": None,
        "attempts": 0,
        "verdict": None,
    }
