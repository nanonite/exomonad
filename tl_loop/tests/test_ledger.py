"""Atomic spawn charging and estimate reconciliation coverage."""

from __future__ import annotations

import json
import multiprocessing
from dataclasses import replace
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
from tl_loop.state.store import load
from tl_loop.state.write import Document, Mutator, Validator, WriteHooks, apply as state_apply
from tl_loop.tests.checkpoint_fixtures import create


def test_ceiling_failure_happens_before_any_state_write(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, budget=50)
    before = (run_dir / "run.json").read_bytes()

    with pytest.raises(BudgetCeilingExceeded):
        apply_spawn_and_charge(run_dir, _choice(cost=60, budget=50), _slice(), _record_spawn)

    assert (run_dir / "run.json").read_bytes() == before


def test_spawn_and_charge_share_one_atomic_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run(tmp_path, budget=100)
    real_apply = state_apply
    calls = 0

    def counted_apply(
        run_dir: str | Path,
        mutator: Mutator,
        *,
        lock_timeout: float = 5.0,
        hooks: WriteHooks | None = None,
        initial: Document | None = None,
        target_name: str = "run.json",
        lock_name: str = "run.lock",
        validator: Validator = validate,
    ) -> Document:
        nonlocal calls
        calls += 1
        return real_apply(
            run_dir,
            mutator,
            lock_timeout=lock_timeout,
            hooks=hooks,
            initial=initial,
            target_name=target_name,
            lock_name=lock_name,
            validator=validator,
        )

    monkeypatch.setitem(ledger_module.__dict__, "apply", counted_apply)
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
        process_type(target=_spawn_process, args=(str(run_dir), results)) for _ in range(2)
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
    slice_record["dispatch_intent_id"] = "ledger-intent-1"
    slice_record["dispatch_agent_id"] = "agent-slice-a"
    slice_record["dispatch_authoritative_event_seq"] = 1
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


def test_harness_change_charges_the_new_harness_and_keeps_the_old_spend() -> None:
    """A continuation on another harness charges that harness, not the old one."""
    codex = _choice(cost=10)
    opencode = HarnessChoice(
        harness="opencode/deepseek-v4.1-flash",
        reason="test",
        difficulty=Difficulty.TRIVIAL,
        matched_rule="focused_slice",
        estimated_cost=20,
        candidate_set=("opencode/deepseek-v4.1-flash",),
        role="worker",
        role_budget=1000,
        harness_budget=50,
    )

    first = charge_spawn({}, codex, _slice())
    first_spent = reconcile(first, "slice-a", 7)
    assert cast(dict[str, int], first_spent["harness_spent"]) == {"codex/gpt-luna": 7}
    assert "codex/gpt-luna" not in cast(dict[str, int], first_spent["harness_reserved"])

    continuation = charge_spawn(
        first_spent, opencode, replace(_slice(), attempts=1)
    )
    assert cast(dict[str, int], continuation["harness_spent"]) == {"codex/gpt-luna": 7}
    assert cast(dict[str, int], continuation["harness_reserved"]) == {
        "opencode/deepseek-v4.1-flash": 20
    }
    charges = cast(list[dict[str, object]], continuation["charges"])
    assert charges[-1]["harness"] == "opencode/deepseek-v4.1-flash"

    second_spent = reconcile(continuation, "slice-a", 4)
    assert cast(dict[str, int], second_spent["harness_spent"]) == {
        "codex/gpt-luna": 7,
        "opencode/deepseek-v4.1-flash": 4,
    }
    assert cast(dict[str, int], second_spent["harness_reserved"]) == {}
    assert second_spent["tokens"] == 11


def test_harness_budget_is_charged_to_the_harness_that_actually_runs() -> None:
    codex = _choice(cost=10, budget=10)
    opencode = HarnessChoice(
        harness="opencode/deepseek-v4.1-flash",
        reason="test",
        difficulty=Difficulty.TRIVIAL,
        matched_rule="focused_slice",
        estimated_cost=25,
        candidate_set=("opencode/deepseek-v4.1-flash",),
        role="worker",
        role_budget=1000,
        harness_budget=20,
    )
    first_spent = reconcile(charge_spawn({}, codex, _slice()), "slice-a", 10)

    with pytest.raises(BudgetCeilingExceeded):
        charge_spawn(first_spent, opencode, replace(_slice(), attempts=1))

    affordable = replace(opencode, estimated_cost=20)
    charged = charge_spawn(first_spent, affordable, replace(_slice(), attempts=1))
    assert charged["harness_spent"] == {"codex/gpt-luna": 10}
    assert charged["harness_reserved"] == {"opencode/deepseek-v4.1-flash": 20}


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
