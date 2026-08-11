"""Atomic spawn charging and estimate reconciliation coverage."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
from typing import Any, cast

import pytest

import tl_loop.select.ledger as ledger_module
from tl_loop.select.agent_type import HarnessChoice
from tl_loop.select.classify import Difficulty
from tl_loop.select.ledger import (
    BudgetCeilingExceeded,
    DuplicateCharge,
    apply_spawn_and_charge,
    charge_spawn,
    reconcile,
    resolve_actual_tokens,
)
from tl_loop.state.schema import SliceState, SliceStatus, validate
from tl_loop.state.store import create, load


def test_ceiling_failure_happens_before_any_state_write(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, budget=50)
    before = (run_dir / "run.json").read_bytes()

    with pytest.raises(BudgetCeilingExceeded):
        apply_spawn_and_charge(run_dir, _choice(cost=60, budget=50), _slice(), _record_spawn)

    assert (run_dir / "run.json").read_bytes() == before


def test_spawn_and_charge_share_one_atomic_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run(tmp_path, budget=100)
    real_apply = ledger_module.apply
    calls = 0

    def counted_apply(*args: Any, **kwargs: Any) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(ledger_module, "apply", counted_apply)
    apply_spawn_and_charge(run_dir, _choice(cost=60, budget=100), _slice(), _record_spawn)

    assert calls == 1
    document = cast(dict[str, object], json.loads((run_dir / "run.json").read_text()))
    slices = cast(dict[str, object], document["slices"])
    slice_record = cast(dict[str, object], slices["slice-a"])
    assert slice_record["status"] == "spawned"
    ledger = cast(dict[str, object], cast(dict[str, object], document["budgets"])["ledger"])
    assert ledger["role_reserved"] == {"worker": 60}
    validate(document)


def test_concurrent_spawns_cannot_consume_the_last_budget_twice(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, budget=60)
    context_name = "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    context = multiprocessing.get_context(context_name)
    results: Any = context.Queue()
    process_type = cast(Any, context).Process
    processes = [
        process_type(target=_spawn_process, args=(str(run_dir), results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=2) for _ in processes]

    assert sorted(outcomes) == ["exceeded", "ok"]
    state = load(run_dir / "run.json")
    assert state.budgets.role_reserved == {"worker": 60}
    assert state.revision == 1


@pytest.mark.parametrize(
    ("actual", "expected_delta", "warning"),
    [(130, 30, True), (80, -20, False)],
)
def test_reconcile_corrects_over_and_under_estimates(
    actual: int, expected_delta: int, warning: bool
) -> None:
    charged = charge_spawn({}, _choice(cost=100), _slice())
    result = reconcile(charged, "slice-a", actual)

    assert result["role_reserved"] == {}
    assert result["role_spent"] == {"worker": actual}
    charge = cast(list[dict[str, object]], result["charges"])[0]
    assert charge["actual"] == actual
    assert charge["delta_tokens"] == expected_delta
    assert charge["warning"] is warning
    assert charge["reconciled"] is True


def test_unknown_usage_is_explicit_and_conservative() -> None:
    charged = charge_spawn({}, _choice(cost=100), _slice())
    result = reconcile(charged, "slice-a", resolve_actual_tokens())

    charge = cast(list[dict[str, object]], result["charges"])[0]
    assert charge["actual"] == "unknown"
    assert charge["delta_tokens"] is None
    assert charge["warning"] is True
    assert result["role_spent"] == {"worker": 100}
    assert result["role_reserved"] == {}


def test_chainlink_usage_precedes_harness_report() -> None:
    assert resolve_actual_tokens(42, 99) == 42
    assert resolve_actual_tokens(None, 99) == 99


def _spawn_process(run_dir: str, results: Any) -> None:
    try:
        apply_spawn_and_charge(Path(run_dir), _choice(cost=60, budget=60), _slice(), _record_spawn)
    except (BudgetCeilingExceeded, DuplicateCharge):
        results.put("exceeded")
    else:
        results.put("ok")


def _make_run(tmp_path: Path, *, budget: int) -> Path:
    run_dir = tmp_path / "run-1"
    create(
        "run-1",
        {
            "slices": {"slice-a": _slice_record()},
        },
        root_dir=tmp_path,
    )
    return run_dir


def _record_spawn(document: dict[str, object]) -> dict[str, object]:
    slices = cast(dict[str, object], document["slices"])
    slice_record = cast(dict[str, object], slices["slice-a"])
    slice_record["status"] = "spawned"
    return document


def _choice(*, cost: int, budget: int | None = None) -> HarnessChoice:
    limit = cost if budget is None else budget
    return HarnessChoice(
        harness="codex/gpt-luna",
        reason="test",
        difficulty=Difficulty.TRIVIAL,
        matched_rule="focused_slice",
        estimated_cost=cost,
        candidate_set=("codex/gpt-luna",),
        role="worker",
        role_budget=limit,
        harness_budget=limit,
    )


def _slice() -> SliceState:
    return SliceState(
        id="slice-a",
        status=SliceStatus.PENDING,
        paths=("src/task.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("pytest",),
        agent_type=None,
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=0,
        verdict=None,
    )


def _slice_record() -> dict[str, object]:
    return {
        "id": "slice-a",
        "status": "pending",
        "paths": ["src/task.py"],
        "depends_on": [],
        "base_ref": "main",
        "test_plan": ["pytest"],
        "agent_type": None,
        "model": None,
        "branch": None,
        "worktree": None,
        "pr_number": None,
        "reviewed_head": None,
        "attempts": 0,
        "verdict": None,
    }
