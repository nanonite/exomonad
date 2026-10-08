"""Operator task-budget reset compare-and-set and replay coverage."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from tl_loop.loop.journal import EffectJournal
from tl_loop.loop.task_budget import (
    TaskBudgetResetError,
    TaskBudgetResetRequest,
    execute_task_budget_reset,
)
from tl_loop.state.schema import (
    BudgetCharge,
    BudgetLedger,
    GateStatus,
    ParkCause,
    SliceStatus,
)
from tl_loop.state.store import RunStore
from tl_loop.tests.test_ordered_continuation_recovery import (
    GATE,
    _failed_ordered_run,
)


def _task_budget_case(
    tmp_path: Path,
    *,
    parked: bool = True,
) -> tuple[Path, RunStore, RunStore, TaskBudgetResetRequest]:
    project_root = tmp_path / "project"
    state_root = project_root / ".exo" / "tl-loop"
    parent_store, _, _ = _failed_ordered_run(
        project_root,
        child_status=SliceStatus.IN_REVIEW,
        child_publication=True,
        root_dir=state_root,
    )
    parent_store.set_gate(GATE)
    parent_store.answer_gate(GATE, GateStatus.APPROVED)

    child_store = RunStore("child", parent_store.run_dir)
    child = child_store.load()
    target = child.slices["leaf"]
    target = replace(
        target,
        status=SliceStatus.PARKED if parked else SliceStatus.IN_REVIEW,
        attempts=1,
        park_cause=ParkCause.TASK_BUDGET_EXCEEDED if parked else None,
        task_timeout_seconds=60.0,
        task_timeout_source="plan",
    )
    budgets = BudgetLedger(
        tokens=0,
        wall_seconds=0,
        role_reserved={"worker": 100},
        harness_reserved={"codex/gpt-luna": 100},
        charges=(
            BudgetCharge(
                slice_id="leaf",
                attempt=1,
                role="worker",
                harness="codex/gpt-luna",
                estimated_tokens=100,
                actual="unknown",
                delta_tokens=None,
                warning=False,
                reconciled=False,
            ),
        ),
    )
    child_store.checkpoint(
        child.recursive_fsm or child.fsm,
        {**child.slices, "leaf": target},
        budgets,
        child.events.last_consumed_offset,
        current_order=child.current_order,
        ordered_stages=child.ordered_stages,
        integration=child.integration,
        expected_revision=child.revision,
    )

    parent = parent_store.load()
    child = child_store.load()
    request = TaskBudgetResetRequest.from_mapping(
        {
            "run_id": "parent",
            "child_run_id": "child",
            "slice_id": "leaf",
            "expected_parent_revision": parent.revision,
            "expected_child_revision": child.revision,
            "expected_attempt": 1,
            "expected_charge_attempt": 1,
            "expected_task_timeout_seconds": 60.0,
            "new_task_timeout_seconds": 120.0,
            "expected_estimated_tokens": 100,
            "new_estimated_tokens": 200,
            "task_type": "feature",
            "operator_reason": "Continue the reviewed implementation",
            "idempotency_key": "task-budget-reset-1",
        }
    )
    return project_root, parent_store, child_store, request


@pytest.mark.parametrize("scope", ["parent", "child"])
def test_reset_rejects_stale_parent_or_child_revision(
    tmp_path: Path, scope: str
) -> None:
    project_root, parent_store, child_store, request = _task_budget_case(tmp_path)
    if scope == "parent":
        request = replace(
            request,
            expected_parent_revision=request.expected_parent_revision + 1,
        )
    else:
        request = replace(
            request,
            expected_child_revision=request.expected_child_revision + 1,
        )
    before_parent = parent_store.load()
    before_child = child_store.load()

    with pytest.raises(TaskBudgetResetError, match="revision changed"):
        execute_task_budget_reset(project_root, request)

    assert parent_store.load().revision == before_parent.revision
    after_child = child_store.load()
    assert after_child.revision == before_child.revision
    assert after_child.slices == before_child.slices
    assert after_child.budgets == before_child.budgets


def test_reset_rejects_a_slice_that_is_not_parked_for_task_budget(
    tmp_path: Path,
) -> None:
    project_root, parent_store, child_store, request = _task_budget_case(
        tmp_path, parked=False
    )
    before_parent = parent_store.load()
    before_child = child_store.load()

    with pytest.raises(TaskBudgetResetError, match="not parked for task_budget_exceeded"):
        execute_task_budget_reset(project_root, request)

    assert parent_store.load().revision == before_parent.revision
    after_child = child_store.load()
    assert after_child.revision == before_child.revision
    assert after_child.slices == before_child.slices
    assert after_child.budgets == before_child.budgets


def test_confirmed_reset_replay_returns_one_receipt_without_another_write(
    tmp_path: Path,
) -> None:
    project_root, parent_store, child_store, request = _task_budget_case(tmp_path)

    first = execute_task_budget_reset(project_root, request)
    after_first_parent = parent_store.load()
    after_first_child = child_store.load()
    second = execute_task_budget_reset(project_root, request)
    journal = EffectJournal(
        "parent", parent_store.run_dir / "action-journal.json"
    )
    entries = [
        entry
        for entry in journal.snapshot()
        if entry.get("operation") == "task_budget_reset"
    ]

    assert second == first
    assert parent_store.load().revision == after_first_parent.revision
    assert child_store.load().revision == after_first_child.revision
    assert len(entries) == 1
    assert entries[0]["status"] == "confirmed"


def test_reset_changes_slice_reservation_without_changing_global_ceilings(
    tmp_path: Path,
) -> None:
    project_root, _, child_store, request = _task_budget_case(tmp_path)
    policy_path = project_root / ".exo" / "harness_policy.toml"
    policy_path.write_text(
        "[roles.worker]\n"
        'allow = ["codex/gpt-luna"]\n'
        'cost_rank = { "codex/gpt-luna" = 1 }\n'
        "token_budget = 1000\n"
        "escalate_after_attempts = 1\n",
        encoding="utf-8",
    )
    config_path = project_root / ".exo" / "config.toml"
    config_path.write_text("tl_task_timeout_seconds = 3600.0\n", encoding="utf-8")
    original_policy = policy_path.read_bytes()
    original_config = config_path.read_bytes()
    role_ceiling = tomllib.loads(original_policy.decode("utf-8"))["roles"]["worker"][
        "token_budget"
    ]

    execute_task_budget_reset(project_root, request)

    state = child_store.load()
    assert state.budgets.role_reserved == {"worker": 200}
    assert state.budgets.harness_reserved == {"codex/gpt-luna": 200}
    assert tomllib.loads(policy_path.read_text(encoding="utf-8"))["roles"]["worker"][
        "token_budget"
    ] == role_ceiling
    assert policy_path.read_bytes() == original_policy
    assert config_path.read_bytes() == original_config
