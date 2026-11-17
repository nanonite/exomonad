"""A same-slice continuation is charged to the harness it runs on.

``resume_pr`` and ``resume_blocked_leaf`` continue an existing attempt with a
new invocation. They reserve budget through the same atomic ledger primitives a
dispatch uses, under a continuation identity that is stable across a controller
restart.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import tl_loop.select.ledger as ledger_module
from tl_loop.select.agent_type import HarnessChoice
from tl_loop.select.classify import Difficulty
from tl_loop.state.schema import SliceState, SliceStatus, validate
from tl_loop.state.store import load
from tl_loop.tests.checkpoint_fixtures import create

CONTINUATION = "resume_pr:action-1"
OPENCODE = "opencode/deepseek-v4.1-flash"
CODEX = "codex/gpt-luna"


def test_continuation_charge_names_the_new_harness_and_keeps_the_old_spend() -> None:
    first = ledger_module.reconcile(
        ledger_module.charge_spawn({}, _choice(CODEX, cost=10, budget=1000), _slice()),
        "slice-a",
        7,
    )
    charged = ledger_module.charge_continuation(
        first,
        _choice(OPENCODE, cost=20, budget=50),
        replace(_slice(), attempts=1),
        CONTINUATION,
    )

    assert cast(dict[str, int], charged["harness_spent"]) == {CODEX: 7}
    assert cast(dict[str, int], charged["role_spent"]) == {"worker": 7}
    assert cast(dict[str, int], charged["harness_reserved"]) == {OPENCODE: 20}
    assert cast(dict[str, int], charged["role_reserved"]) == {"worker": 20}
    charge = cast(list[dict[str, object]], charged["charges"])[-1]
    assert charge["continuation"] == CONTINUATION
    assert charge["harness"] == OPENCODE
    assert charge["role"] == "worker"
    assert charge["attempt"] == 1
    assert charge["estimated_tokens"] == 20


def test_continuation_charge_refuses_the_new_harness_ceiling_without_a_leak() -> None:
    first = ledger_module.reconcile(
        ledger_module.charge_spawn({}, _choice(CODEX, cost=10, budget=1000), _slice()),
        "slice-a",
        7,
    )
    before = json.loads(json.dumps(first))

    with pytest.raises(ledger_module.BudgetCeilingExceeded):
        ledger_module.charge_continuation(
            first,
            _choice(OPENCODE, cost=25, budget=20),
            replace(_slice(), attempts=1),
            CONTINUATION,
        )

    assert first == before, "a refused continuation must not mutate the ledger"


def test_continuation_charge_refuses_the_role_ceiling_without_a_leak() -> None:
    first = ledger_module.reconcile(
        ledger_module.charge_spawn({}, _choice(CODEX, cost=10, budget=1000), _slice()),
        "slice-a",
        7,
    )
    before = json.loads(json.dumps(first))
    role_limited = HarnessChoice(
        harness=OPENCODE,
        reason="test",
        difficulty=Difficulty.TRIVIAL,
        matched_rule="focused_slice",
        estimated_cost=20,
        candidate_set=(OPENCODE,),
        role="worker",
        role_budget=20,
        harness_budget=1000,
    )

    with pytest.raises(ledger_module.BudgetCeilingExceeded):
        ledger_module.charge_continuation(
            first, role_limited, replace(_slice(), attempts=1), CONTINUATION
        )

    assert first == before, "a refused continuation must not mutate the ledger"


def test_continuation_charge_is_idempotent_by_continuation_identity() -> None:
    choice = _choice(OPENCODE, cost=20, budget=1000)
    slice = replace(_slice(), attempts=1)
    charged = ledger_module.charge_continuation({}, choice, slice, CONTINUATION)

    with pytest.raises(ledger_module.DuplicateCharge):
        ledger_module.charge_continuation(charged, choice, slice, CONTINUATION)

    other = ledger_module.charge_continuation(charged, choice, slice, CONTINUATION + ":2")
    charges = cast(list[dict[str, object]], other["charges"])
    assert [charge["continuation"] for charge in charges] == [CONTINUATION, CONTINUATION + ":2"]
    assert [charge["attempt"] for charge in charges] == [1, 1]

    # A dispatch charge for the next attempt is a different identity.
    dispatched = ledger_module.charge_spawn(
        other, _choice(OPENCODE, cost=20, budget=1000), replace(slice, attempts=1)
    )
    dispatch_charge = cast(list[dict[str, object]], dispatched["charges"])[-1]
    assert dispatch_charge["attempt"] == 2
    assert "continuation" not in dispatch_charge


@pytest.mark.parametrize(
    ("actual", "expected_delta", "warning"),
    [(18, -2, False), (30, 10, True)],
)
def test_continuation_reconcile_with_known_actual_moves_the_estimate_to_spend(
    actual: int, expected_delta: int, warning: bool
) -> None:
    charged = ledger_module.charge_continuation(
        {}, _choice(OPENCODE, cost=20, budget=1000), replace(_slice(), attempts=1), CONTINUATION
    )

    result = ledger_module.reconcile(charged, "slice-a", actual, continuation=CONTINUATION)

    assert result["role_reserved"] == {}
    assert result["harness_reserved"] == {}
    assert result["role_spent"] == {"worker": actual}
    assert result["harness_spent"] == {OPENCODE: actual}
    charge = cast(list[dict[str, object]], result["charges"])[0]
    assert charge["actual"] == actual
    assert charge["delta_tokens"] == expected_delta
    assert charge["warning"] is warning
    assert charge["reconciled"] is True


def test_continuation_reconcile_without_usage_conserves_the_estimate() -> None:
    charged = ledger_module.charge_continuation(
        {}, _choice(OPENCODE, cost=20, budget=1000), replace(_slice(), attempts=1), CONTINUATION
    )

    result = ledger_module.reconcile(charged, "slice-a", None, continuation=CONTINUATION)

    charge = cast(list[dict[str, object]], result["charges"])[0]
    assert charge["actual"] == "unknown"
    assert charge["delta_tokens"] is None
    assert charge["warning"] is True
    assert result["role_spent"] == {"worker": 20}
    assert result["harness_spent"] == {OPENCODE: 20}
    assert result["role_reserved"] == {}


def test_continuation_release_returns_the_reservation() -> None:
    charged = ledger_module.charge_continuation(
        {}, _choice(OPENCODE, cost=20, budget=1000), replace(_slice(), attempts=1), CONTINUATION
    )

    released = ledger_module.reconcile(charged, "slice-a", 0, continuation=CONTINUATION)

    assert released["role_reserved"] == {}
    assert released["harness_reserved"] == {}
    assert released["harness_spent"] == {OPENCODE: 0}
    charge = cast(list[dict[str, object]], released["charges"])[0]
    assert charge["actual"] == 0
    assert charge["delta_tokens"] == -20
    assert charge["reconciled"] is True


def test_dispatch_reconcile_does_not_consume_a_pending_continuation() -> None:
    charged = ledger_module.charge_continuation(
        {}, _choice(OPENCODE, cost=20, budget=1000), replace(_slice(), attempts=1), CONTINUATION
    )

    with pytest.raises(ledger_module.ChargeNotFound):
        ledger_module.reconcile(charged, "slice-a", 5)


def test_continuation_charge_round_trips_through_the_run_state(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    choice = _choice(OPENCODE, cost=20, budget=1000)
    slice = replace(_slice(), attempts=1)

    ledger_module.apply_continuation_and_charge(
        run_dir, choice, slice, CONTINUATION, lambda document: document
    )

    state = load(run_dir / "run.json")
    charge = ledger_module.find_continuation_charge(state.budgets, "slice-a", CONTINUATION)
    assert charge is not None
    assert charge.harness == OPENCODE
    assert charge.attempt == 1
    assert charge.continuation == CONTINUATION
    assert state.budgets.harness_reserved == {OPENCODE: 20}
    validate(json.loads((run_dir / "run.json").read_text(encoding="utf-8")))

    ledger_module.apply_continuation_reconcile(run_dir, "slice-a", CONTINUATION, None)
    settled = load(run_dir / "run.json")
    charge = ledger_module.find_continuation_charge(settled.budgets, "slice-a", CONTINUATION)
    assert charge is not None and charge.reconciled is True
    assert charge.actual == "unknown"
    assert settled.budgets.harness_spent == {OPENCODE: 20}
    assert settled.budgets.harness_reserved == {}


def test_old_charge_without_a_continuation_field_decodes_and_behaves_unchanged(
    tmp_path: Path,
) -> None:
    run_dir = _make_run(tmp_path)
    document = cast(
        dict[str, object], json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    )
    budgets = cast(dict[str, object], document["budgets"])
    ledger = cast(dict[str, object], budgets["ledger"])
    ledger["tokens"] = 7
    ledger["role_spent"] = {"worker": 7}
    ledger["harness_spent"] = {CODEX: 7}
    ledger["role_reserved"] = {"worker": 10}
    ledger["harness_reserved"] = {CODEX: 10}
    ledger["charges"] = [
        {
            "slice_id": "slice-a",
            "attempt": 1,
            "role": "worker",
            "harness": CODEX,
            "estimated_tokens": 10,
            "actual": "unknown",
            "delta_tokens": None,
            "warning": False,
            "reconciled": False,
        }
    ]
    (run_dir / "run.json").write_text(json.dumps(document), encoding="utf-8")

    validate(json.loads((run_dir / "run.json").read_text(encoding="utf-8")))
    state = load(run_dir / "run.json")
    assert state.budgets.charges[0].continuation is None

    reconciled = ledger_module.reconcile(state.budgets, "slice-a", 12)
    charge = cast(list[dict[str, object]], reconciled["charges"])[0]
    assert charge["actual"] == 12
    assert charge["reconciled"] is True
    assert "continuation" not in charge


def _choice(harness: str, *, cost: int, budget: int | None = None) -> HarnessChoice:
    limit = cost if budget is None else budget
    return HarnessChoice(
        harness=harness,
        reason="test",
        difficulty=Difficulty.TRIVIAL,
        matched_rule="focused_slice",
        estimated_cost=cost,
        candidate_set=(harness,),
        role="worker",
        role_budget=limit,
        harness_budget=limit,
    )


def _make_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run-1"
    create("run-1", {"slices": {"slice-a": _slice_record()}}, root_dir=tmp_path)
    return run_dir


def _slice() -> SliceState:
    return SliceState(
        id="slice-a",
        status=SliceStatus.IN_REVIEW,
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
        "status": "in_review",
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
