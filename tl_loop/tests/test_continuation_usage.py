"""A continuation settles from the finished invocation row, never the estimate.

A resume effect returns before its invocation runs, so usage is not known at
effect time. The reservation therefore stays until the controller observes the
invocation's authoritative ``agent.invocation.finished`` ledger row. A row that
reports usage settles the measured actual; a row without usage conserves the
estimate with a warning. Settlement is a single atomic reconcile, so an
unfinished invocation and a restart replay both leave the counter untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import tl_loop.select.ledger as ledger_module
from tl_loop.loop import driver as driver_module
from tl_loop.loop.driver import TLLoopConfig
from tl_loop.select.agent_type import HarnessChoice
from tl_loop.select.classify import Difficulty
from tl_loop.state.schema import BudgetCharge, RunState, SliceState, SliceStatus, validate
from tl_loop.state.store import RunStore, load
from tl_loop.tests.checkpoint_fixtures import create

CONTINUATION = "resume_pr:action-usage"
CHEAP = "opencode/deepseek-v4.1-flash"
LAUNCHED = "codex/gpt-6.1-sol"
INVOCATION = "invocation-usage-1"


def test_finished_continuation_settles_measured_usage_and_flags_the_delta(
    tmp_path: Path,
) -> None:
    """A finished row's usage replaces the estimate; a wide delta is flagged."""
    store = _charged_run(tmp_path, cost=10, harness=LAUNCHED)
    _write_finished(tmp_path, INVOCATION, {"input_tokens": 10, "output_tokens": 4})

    state = driver_module._settle_finished_continuations(
        store.load(), _config(tmp_path), store
    )

    charge = _charge(state)
    assert charge.reconciled is True
    assert charge.actual == 14
    assert charge.delta_tokens == 4
    assert charge.warning is True, "a delta over 20% is flagged"
    assert state.budgets.harness_spent == {LAUNCHED: 14}
    assert state.budgets.harness_reserved == {}
    assert state.budgets.role_spent == {"worker": 14}


def test_finished_row_with_the_host_usage_shape_settles_the_measured_actual(
    tmp_path: Path,
) -> None:
    """The host writes input, output, and total; total settles the charge once."""
    store = _charged_run(tmp_path, cost=10, harness=LAUNCHED)
    _write_finished(
        tmp_path,
        INVOCATION,
        {"input_tokens": 200, "output_tokens": 25, "total_tokens": 225},
    )

    state = driver_module._settle_finished_continuations(
        store.load(), _config(tmp_path), store
    )

    charge = _charge(state)
    assert charge.reconciled is True
    assert charge.actual == 225
    assert state.budgets.harness_spent == {LAUNCHED: 225}
    assert state.budgets.role_spent == {"worker": 225}


def test_finished_continuation_without_usage_conserves_the_estimate_and_warns(
    tmp_path: Path,
) -> None:
    """No reported usage is never invented: the estimate is conserved loudly."""
    store = _charged_run(tmp_path, cost=10, harness=LAUNCHED)
    _write_finished(tmp_path, INVOCATION, None)

    state = driver_module._settle_finished_continuations(
        store.load(), _config(tmp_path), store
    )

    charge = _charge(state)
    assert charge.reconciled is True
    assert charge.actual == "unknown"
    assert charge.delta_tokens is None
    assert charge.warning is True
    assert state.budgets.harness_spent == {LAUNCHED: 10}
    assert state.budgets.harness_reserved == {}


def test_a_zero_or_malformed_usage_report_conserves_the_estimate(
    tmp_path: Path,
) -> None:
    """Zero, negative, non-integer and non-object usage is never a measurement."""
    bad_reports: list[object] = [
        {"total_tokens": 0},
        {"input_tokens": 0, "output_tokens": 0},
        {"total_tokens": -5},
        {"total_tokens": "12"},
        {"input_tokens": 7, "output_tokens": -1},
        {"input_tokens": 7, "output_tokens": "x"},
        "12",
    ]
    for index, report in enumerate(bad_reports):
        run_dir = tmp_path / f"case-{index}"
        run_dir.mkdir()
        store = _charged_run(run_dir, cost=10, harness=LAUNCHED)
        _write_row(
            run_dir,
            {
                "type": "agent.invocation.finished",
                "agent_id": "slice-a",
                "data": {
                    "invocation_id": INVOCATION,
                    "slice_id": "slice-a",
                    "status": "exited",
                    "usage": report,
                },
            },
        )

        state = driver_module._settle_finished_continuations(
            store.load(), _config(run_dir), store
        )

        charge = _charge(state)
        assert charge.reconciled is True, report
        assert charge.actual == "unknown", report
        assert charge.warning is True, report
        assert state.budgets.harness_spent == {LAUNCHED: 10}, report


def test_continuation_before_its_invocation_finishes_is_not_settled(
    tmp_path: Path,
) -> None:
    """An absent finished row leaves the reservation conservative and untouched."""
    store = _charged_run(tmp_path, cost=10, harness=LAUNCHED)
    _write_started_only(tmp_path, INVOCATION)

    state = driver_module._settle_finished_continuations(
        store.load(), _config(tmp_path), store
    )

    charge = _charge(state)
    assert charge.reconciled is False
    assert charge.actual == "unknown"
    assert state.budgets.harness_spent == {}
    assert state.budgets.harness_reserved == {LAUNCHED: 10}


def test_restart_replay_does_not_settle_the_same_continuation_twice(
    tmp_path: Path,
) -> None:
    """The reconciled charge is never found again, so replay moves no counter."""
    store = _charged_run(tmp_path, cost=10, harness=LAUNCHED)
    _write_finished(tmp_path, INVOCATION, {"total_tokens": 16})
    config = _config(tmp_path)

    first = driver_module._settle_finished_continuations(store.load(), config, store)
    replayed = driver_module._settle_finished_continuations(store.load(), config, store)

    charge = _charge(replayed)
    assert charge.reconciled is True
    assert charge.actual == 16
    assert replayed.budgets.harness_spent == first.budgets.harness_spent == {LAUNCHED: 16}
    assert replayed.budgets.role_spent == first.budgets.role_spent == {"worker": 16}
    assert len([item for item in replayed.budgets.charges if item.continuation]) == 1


def test_usage_settles_the_launched_route_after_the_charge_is_retargeted(
    tmp_path: Path,
) -> None:
    """The settled counters name the launched harness, not the pre-effect guess."""
    store = _charged_run(tmp_path, cost=10, harness=CHEAP)
    ledger_module.apply_continuation_retarget(
        store.run_dir,
        "slice-a",
        CONTINUATION,
        _choice(LAUNCHED, cost=10),
    )
    ledger_module.apply_continuation_launch(
        store.run_dir, "slice-a", CONTINUATION, INVOCATION
    )
    _write_finished(tmp_path, INVOCATION, {"input_tokens": 3, "output_tokens": 2})

    state = driver_module._settle_finished_continuations(
        store.load(), _config(tmp_path), store
    )

    charge = _charge(state)
    assert charge.harness == LAUNCHED
    assert charge.reconciled is True
    assert charge.actual == 5
    assert state.budgets.harness_spent == {LAUNCHED: 5}
    assert state.budgets.harness_spent.get(CHEAP, 0) == 0
    assert state.budgets.harness_reserved == {}


def test_old_charge_without_a_launched_invocation_decodes_unchanged(
    tmp_path: Path,
) -> None:
    """A checkpoint written before the new field decodes and validates as-is."""
    store = _charged_run(tmp_path, cost=10, harness=LAUNCHED)
    document = cast(
        dict[str, object], json.loads((store.run_dir / "run.json").read_text("utf-8"))
    )
    budgets = cast(dict[str, object], document["budgets"])
    ledger = cast(dict[str, object], budgets["ledger"])
    charges = cast(list[dict[str, object]], ledger["charges"])
    charges[0].pop("launched_invocation_id")
    (store.run_dir / "run.json").write_text(json.dumps(document), encoding="utf-8")

    validate(json.loads((store.run_dir / "run.json").read_text(encoding="utf-8")))
    state = load(store.run_dir / "run.json")

    assert state.budgets.charges[0].launched_invocation_id is None
    assert state.budgets.charges[0].continuation == CONTINUATION
    assert driver_module._settle_finished_continuations(
        state, _config(tmp_path), store
    ).budgets.harness_spent == {}


def _config(tmp_path: Path) -> TLLoopConfig:
    return TLLoopConfig(active=False, project_root=tmp_path)


def _charged_run(tmp_path: Path, *, cost: int, harness: str) -> RunStore:
    create("run-1", {"slices": {"slice-a": _slice_record()}}, root_dir=tmp_path)
    store = RunStore("run-1", tmp_path)
    ledger_module.apply_continuation_and_charge(
        store.run_dir,
        _choice(harness, cost=cost),
        _slice(),
        CONTINUATION,
        lambda document: document,
    )
    ledger_module.apply_continuation_launch(
        store.run_dir, "slice-a", CONTINUATION, INVOCATION
    )
    return store


def _charge(state: RunState) -> BudgetCharge:
    charges = [
        charge
        for charge in state.budgets.charges
        if charge.continuation == CONTINUATION
    ]
    assert len(charges) == 1
    return charges[0]


def _write_finished(
    tmp_path: Path, invocation_id: str, usage: dict[str, int] | None
) -> None:
    data: dict[str, object] = {
        "invocation_id": invocation_id,
        "slice_id": "slice-a",
        "status": "exited",
    }
    if usage is not None:
        data["usage"] = usage
    _write_row(
        tmp_path,
        {"type": "agent.invocation.finished", "agent_id": "slice-a", "data": data},
    )


def _write_started_only(tmp_path: Path, invocation_id: str) -> None:
    _write_row(
        tmp_path,
        {
            "type": "agent.invocation.started",
            "agent_id": "slice-a",
            "data": {"invocation_id": invocation_id, "slice_id": "slice-a"},
        },
    )


def _write_row(tmp_path: Path, row: dict[str, object]) -> None:
    segments = tmp_path / ".exo" / "ledger" / "segments"
    segments.mkdir(parents=True, exist_ok=True)
    (segments / "segment-000000000001.jsonl").write_text(
        json.dumps(row, sort_keys=True) + "\n", encoding="utf-8"
    )


def _choice(harness: str, *, cost: int) -> HarnessChoice:
    return HarnessChoice(
        harness=harness,
        reason="test",
        difficulty=Difficulty.TRIVIAL,
        matched_rule="focused_slice",
        estimated_cost=cost,
        candidate_set=(harness,),
        role="worker",
        role_budget=1000,
        harness_budget=1000,
    )


def _slice() -> SliceState:
    return SliceState(
        id="slice-a",
        status=SliceStatus.REPAIRING,
        paths=("src/task.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("pytest",),
        agent_type=None,
        model=None,
        branch=None,
        worktree=None,
        pr_number=42,
        reviewed_head="head-a",
        attempts=1,
        verdict=None,
    )


def _slice_record() -> dict[str, object]:
    return {
        "id": "slice-a",
        "status": "repairing",
        "paths": ["src/task.py"],
        "depends_on": [],
        "base_ref": "main",
        "test_plan": ["pytest"],
        "agent_type": None,
        "model": None,
        "branch": None,
        "worktree": None,
        "pr_number": 42,
        "reviewed_head": "head-a",
        "attempts": 1,
        "verdict": None,
    }
