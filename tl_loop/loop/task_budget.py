"""Operator-authorized, per-slice recovery from a task budget park."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from tl_loop.fsm.scope import TLFailed as RecursiveTLFailed
from tl_loop.fsm.scope import TLRunning as RecursiveTLRunning
from tl_loop.loop.journal import EffectJournal
from tl_loop.state.schema import (
    BudgetCharge,
    BudgetLedger,
    GateStatus,
    ParkCause,
    RunState,
    SliceState,
    SliceStatus,
)
from tl_loop.state.slice_transition import ActionChanged, SliceStatusChanged, slice_transition
from tl_loop.state.store import CheckpointConflict, RunStore

_TASK_TYPE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_MAX_TIMEOUT_SECONDS = 86_400.0
_MAX_ESTIMATED_TOKENS = 10_000_000


@dataclass(frozen=True)
class TaskBudgetResetRequest:
    """Closed operator request for one nested parked slice."""

    run_id: str
    child_run_id: str
    slice_id: str
    expected_parent_revision: int
    expected_child_revision: int
    expected_attempt: int
    expected_charge_attempt: int
    expected_task_timeout_seconds: float
    new_task_timeout_seconds: float
    expected_estimated_tokens: int
    new_estimated_tokens: int
    task_type: str
    operator_reason: str
    idempotency_key: str
    observed_tokens: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> TaskBudgetResetRequest:
        allowed = {
            "run_id", "child_run_id", "slice_id", "expected_parent_revision",
            "expected_child_revision", "expected_attempt", "expected_charge_attempt",
            "expected_task_timeout_seconds",
            "new_task_timeout_seconds", "expected_estimated_tokens", "new_estimated_tokens",
            "task_type", "operator_reason", "idempotency_key", "observed_tokens",
        }
        unknown = set(value) - allowed
        if unknown:
            raise TaskBudgetResetError(f"unknown task budget reset fields: {sorted(unknown)}")
        observed = value.get("observed_tokens")
        if observed is not None:
            observed = _non_negative_int(observed, "observed_tokens")
        request = cls(
            run_id=_text(value, "run_id"),
            child_run_id=_text(value, "child_run_id"),
            slice_id=_text(value, "slice_id"),
            expected_parent_revision=_non_negative_int(value.get("expected_parent_revision"), "expected_parent_revision"),
            expected_child_revision=_non_negative_int(value.get("expected_child_revision"), "expected_child_revision"),
            expected_attempt=_positive_int(value.get("expected_attempt"), "expected_attempt"),
            expected_charge_attempt=_positive_int(value.get("expected_charge_attempt"), "expected_charge_attempt"),
            expected_task_timeout_seconds=_positive_number(value.get("expected_task_timeout_seconds"), "expected_task_timeout_seconds"),
            new_task_timeout_seconds=_positive_number(value.get("new_task_timeout_seconds"), "new_task_timeout_seconds"),
            expected_estimated_tokens=_positive_int(value.get("expected_estimated_tokens"), "expected_estimated_tokens"),
            new_estimated_tokens=_positive_int(value.get("new_estimated_tokens"), "new_estimated_tokens"),
            task_type=_text(value, "task_type"),
            operator_reason=_text(value, "operator_reason"),
            idempotency_key=_text(value, "idempotency_key"),
            observed_tokens=cast(int | None, observed),
        )
        _validate_request(request)
        return request

    def to_document(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "child_run_id": self.child_run_id,
            "slice_id": self.slice_id,
            "expected_parent_revision": self.expected_parent_revision,
            "expected_child_revision": self.expected_child_revision,
            "expected_attempt": self.expected_attempt,
            "expected_charge_attempt": self.expected_charge_attempt,
            "expected_task_timeout_seconds": self.expected_task_timeout_seconds,
            "new_task_timeout_seconds": self.new_task_timeout_seconds,
            "expected_estimated_tokens": self.expected_estimated_tokens,
            "new_estimated_tokens": self.new_estimated_tokens,
            "task_type": self.task_type,
            "operator_reason": self.operator_reason,
            "idempotency_key": self.idempotency_key,
            "observed_tokens": self.observed_tokens,
        }


@dataclass(frozen=True)
class _TaskBudgetIntent:
    operation: str
    target: str
    arguments: Mapping[str, object]
    active: bool = True


class TaskBudgetResetError(RuntimeError):
    """A task budget reset failed closed at its state boundary."""


def execute_task_budget_reset(
    project_root: Path,
    request: TaskBudgetResetRequest,
) -> dict[str, object]:
    """Reset one parked slice idempotently through its action journal."""
    if request.run_id != request.run_id.strip():
        raise TaskBudgetResetError("run_id must not contain surrounding whitespace")
    parent_store = RunStore(request.run_id, project_root / ".exo" / "tl-loop")
    journal = EffectJournal(request.run_id, parent_store.run_dir / "action-journal.json")
    intent = _TaskBudgetIntent(
        "task_budget_reset",
        f"{request.child_run_id}/{request.slice_id}",
        request.to_document(),
    )
    existing = journal.existing(intent)
    if existing is not None and existing.get("status") == "confirmed":
        result = existing.get("result")
        if isinstance(result, Mapping):
            return dict(result)
        raise TaskBudgetResetError("confirmed task budget reset has no recorded result")
    for entry in journal.snapshot():
        arguments = entry.get("arguments")
        if (
            entry.get("operation") == "task_budget_reset"
            and entry.get("target") == intent.target
            and isinstance(arguments, Mapping)
            and arguments.get("idempotency_key") == request.idempotency_key
            and dict(arguments) != request.to_document()
        ):
            raise TaskBudgetResetError("idempotency_key was already used for a different reset")
    if existing is None:
        journal.append(intent)
    try:
        result = _apply_task_budget_reset(project_root, request, parent_store)
    except BaseException as error:
        journal.resolve_by_key(journal.key_for(intent), status="unknown", error=str(error))
        raise
    journal.resolve_by_key(journal.key_for(intent), status="confirmed", result=result)
    return result


def _apply_task_budget_reset(
    project_root: Path,
    request: TaskBudgetResetRequest,
    parent_store: RunStore,
) -> dict[str, object]:
    """Reset one parked slice, re-open its canonical child and parent phases."""
    child_store = RunStore(request.child_run_id, parent_store.run_dir)
    if not parent_store.path.is_file() or not child_store.path.is_file():
        raise TaskBudgetResetError("parent or child run checkpoint is missing")

    parent = parent_store.load()
    child = child_store.load()
    child_reset = (
        child.revision == request.expected_child_revision + 1
        and isinstance(child.recursive_fsm, RecursiveTLRunning)
        and _matches_reset(child, request)
    )
    parent_reopened = (
        parent.revision == request.expected_parent_revision + 1
        and not isinstance(parent.recursive_fsm, RecursiveTLFailed)
        and parent.slices.get(request.child_run_id) is not None
        and parent.slices[request.child_run_id].status is SliceStatus.SPAWNED
    )
    if not child_reset:
        _check_revision(child, request.expected_child_revision, "child")
    if not parent_reopened:
        _check_revision(parent, request.expected_parent_revision, "parent")
    if child.run_id != request.child_run_id or child.parent_run_id != parent.run_id:
        raise TaskBudgetResetError("child run ownership does not match the request")
    if child.plan_manifest is None or parent.plan_manifest is None:
        raise TaskBudgetResetError("parent and child plan manifests are required")
    parent_node = next(
        (node for node in parent.plan_manifest.nodes if node.name == request.child_run_id), None
    )
    expected_child_manifest = (
        parent.plan_manifest.child_manifests.get(parent_node.node_id)
        if parent_node is not None else None
    )
    if expected_child_manifest is None or expected_child_manifest.digest != child.plan_manifest.digest:
        raise TaskBudgetResetError("child manifest does not match its parent declaration")
    parent_slice = parent.slices.get(request.child_run_id)
    if parent_slice is None or (
        not parent_reopened
        and parent_slice.status not in {SliceStatus.FAILED, SliceStatus.PARKED}
    ):
        raise TaskBudgetResetError("parent child is not in a recoverable failed or parked state")
    gate_name = f"tl-ordered-child-recovery-{request.child_run_id}"
    if not any(gate.name == gate_name and gate.status is GateStatus.APPROVED for gate in parent.gates):
        raise TaskBudgetResetError(f"required approved parent recovery gate {gate_name!r} is missing")
    if not isinstance(parent.recursive_fsm, RecursiveTLFailed) and not parent_reopened:
        raise TaskBudgetResetError("parent recursive phase is not failed or already reopened")
    if not child_reset:
        target = child.slices.get(request.slice_id)
        if target is None:
            raise TaskBudgetResetError(f"slice {request.slice_id!r} is missing from the child")
        _validate_parked_target(target, child, request)
        updated_target = slice_transition(
            slice_transition(target, SliceStatusChanged(SliceStatus.IN_REVIEW)),
            ActionChanged(None),
        )
        updated_target = replace(
            updated_target,
            park_cause=None,
            park_issue_id=None,
            dispatch_started_at=time.time(),
            task_timeout_seconds=request.new_task_timeout_seconds,
            task_timeout_source="operator",
            task_budget_reset_key=request.idempotency_key,
            dispatch_last_boundary=None,
            dispatch_error=None,
            dispatch_error_code=None,
            reconciliation=None,
        )
        updated_budgets = _adjust_budget_charge(child.budgets, target, request)
        child = replace(
            child,
            slices={**child.slices, request.slice_id: updated_target},
            budgets=updated_budgets,
        )
        child = _reopen_child_from_manifest(child, child_store, request.expected_child_revision)
    elif child.plan_manifest is None:
        raise TaskBudgetResetError("child plan manifest disappeared during reset")

    # The parent checkpoint is a separate locked write. Retrying the same
    # idempotency key after a partial failure can finish this second boundary.
    parent = parent_store.load()
    if not parent_reopened and isinstance(parent.recursive_fsm, RecursiveTLFailed):
        from tl_loop.loop.driver import _reopen_ordered_scope, _work_plan_from_manifest

        if parent.plan_manifest is None:
            raise TaskBudgetResetError("parent plan manifest disappeared during reset")
        try:
            parent = _reopen_ordered_scope(
                parent,
                _work_plan_from_manifest(parent.plan_manifest),
                parent_store,
                request.child_run_id,
                authorized=True,
                expected_revision=request.expected_parent_revision,
            )
        except (CheckpointConflict, RuntimeError, ValueError) as error:
            raise TaskBudgetResetError(str(error)) from error
    elif not parent_reopened:
        raise TaskBudgetResetError("parent state changed outside this reset operation")

    reopened_child = child_store.load()
    reopened_parent = parent_store.load()
    reopened_slice = reopened_child.slices[request.slice_id]
    return {
        "status": "applied",
        "run_id": request.run_id,
        "child_run_id": request.child_run_id,
        "slice_id": request.slice_id,
        "task_type": request.task_type,
        "attempt": request.expected_attempt,
        "budget_charge_attempt": request.expected_charge_attempt,
        "old_task_timeout_seconds": request.expected_task_timeout_seconds,
        "new_task_timeout_seconds": request.new_task_timeout_seconds,
        "old_estimated_tokens": request.expected_estimated_tokens,
        "new_estimated_tokens": request.new_estimated_tokens,
        "observed_tokens": request.observed_tokens,
        "operator_reason": request.operator_reason,
        "idempotency_key": request.idempotency_key,
        "parent_revision": reopened_parent.revision,
        "child_revision": reopened_child.revision,
        "slice_status": reopened_slice.status.value,
        "pr_number": reopened_slice.pr_number,
        "invocation_id": reopened_slice.dispatch_invocation_id,
    }


def _reopen_child_from_manifest(
    child: RunState,
    child_store: RunStore,
    expected_revision: int,
) -> RunState:
    if child.plan_manifest is None:
        raise TaskBudgetResetError("child plan manifest is missing")
    from tl_loop.loop.driver import _reopen_ordered_scope, _work_plan_from_manifest

    # The standard recovery projection reconstructs pending/completed children
    # from the immutable manifest and current slice states, not a hand-set phase.
    try:
        return _reopen_ordered_scope(
            child,
            _work_plan_from_manifest(child.plan_manifest),
            child_store,
            None,
            expected_revision=expected_revision,
        )
    except (CheckpointConflict, RuntimeError, ValueError) as error:
        raise TaskBudgetResetError(str(error)) from error


def _validate_parked_target(
    target: SliceState,
    child: RunState,
    request: TaskBudgetResetRequest,
) -> None:
    if child.revision != request.expected_child_revision:
        raise TaskBudgetResetError("child revision changed before task budget reset")
    if target.status is not SliceStatus.PARKED or target.park_cause is not ParkCause.TASK_BUDGET_EXCEEDED:
        raise TaskBudgetResetError("target slice is not parked for task_budget_exceeded")
    if target.attempts != request.expected_attempt:
        raise TaskBudgetResetError("target slice attempt changed")
    if target.task_timeout_seconds != request.expected_task_timeout_seconds:
        raise TaskBudgetResetError("current task timeout changed")
    if target.pr_number is None or target.publication is None:
        raise TaskBudgetResetError(
            "task budget reset requires an existing PR publication; use the normal recovery path otherwise"
        )
    charge = _attempt_charge(child.budgets, target, request.expected_charge_attempt)
    if charge.estimated_tokens != request.expected_estimated_tokens:
        raise TaskBudgetResetError("current token reservation changed")


def _adjust_budget_charge(
    ledger: BudgetLedger,
    target: SliceState,
    request: TaskBudgetResetRequest,
) -> BudgetLedger:
    charges = list(ledger.charges)
    charge = _attempt_charge(ledger, target, request.expected_charge_attempt)
    index = next(
        index for index, candidate in enumerate(charges)
        if candidate.slice_id == target.id and candidate.attempt == request.expected_charge_attempt
    )
    if charge.estimated_tokens != request.expected_estimated_tokens:
        raise TaskBudgetResetError("budget ledger estimate changed")
    delta_estimate = request.new_estimated_tokens - charge.estimated_tokens
    role_reserved = dict(ledger.role_reserved)
    harness_reserved = dict(ledger.harness_reserved)
    role_spent = dict(ledger.role_spent)
    harness_spent = dict(ledger.harness_spent)
    total_tokens = ledger.tokens
    if charge.reconciled:
        old_applied = charge.actual if type(charge.actual) is int else charge.estimated_tokens
        new_applied = request.observed_tokens if request.observed_tokens is not None else (
            request.new_estimated_tokens if charge.actual == "unknown" else old_applied
        )
        role_spent[charge.role] = role_spent.get(charge.role, 0) + new_applied - old_applied
        harness_spent[charge.harness] = harness_spent.get(charge.harness, 0) + new_applied - old_applied
        total_tokens += new_applied - old_applied
        actual = request.observed_tokens if request.observed_tokens is not None else charge.actual
        delta = None if actual == "unknown" else cast(int, actual) - request.new_estimated_tokens
        warning = actual == "unknown" or abs(cast(int, delta or 0)) / max(request.new_estimated_tokens, 1) > 0.20
        charges[index] = replace(
            charge,
            estimated_tokens=request.new_estimated_tokens,
            actual=actual,
            delta_tokens=delta,
            warning=warning,
        )
    else:
        role_reserved[charge.role] = role_reserved.get(charge.role, 0) + delta_estimate
        harness_reserved[charge.harness] = harness_reserved.get(charge.harness, 0) + delta_estimate
        if request.observed_tokens is not None:
            role_reserved[charge.role] -= request.new_estimated_tokens
            harness_reserved[charge.harness] -= request.new_estimated_tokens
            role_spent[charge.role] = role_spent.get(charge.role, 0) + request.observed_tokens
            harness_spent[charge.harness] = harness_spent.get(charge.harness, 0) + request.observed_tokens
            total_tokens += request.observed_tokens
            delta = request.observed_tokens - request.new_estimated_tokens
            charges[index] = replace(
                charge,
                estimated_tokens=request.new_estimated_tokens,
                actual=request.observed_tokens,
                delta_tokens=delta,
                warning=abs(delta) / max(request.new_estimated_tokens, 1) > 0.20,
                reconciled=True,
            )
        else:
            charges[index] = replace(charge, estimated_tokens=request.new_estimated_tokens)
    for counters in (role_reserved, harness_reserved, role_spent, harness_spent):
        if any(value < 0 for value in counters.values()):
            raise TaskBudgetResetError("task budget adjustment would make a ledger counter negative")
    return replace(
        ledger,
        tokens=total_tokens,
        role_reserved={key: value for key, value in role_reserved.items() if value},
        harness_reserved={key: value for key, value in harness_reserved.items() if value},
        role_spent={key: value for key, value in role_spent.items() if value},
        harness_spent={key: value for key, value in harness_spent.items() if value},
        charges=tuple(charges),
    )


def _attempt_charge(
    ledger: BudgetLedger, target: SliceState, charge_attempt: int
) -> BudgetCharge:
    matches = [
        charge for charge in ledger.charges
        if charge.slice_id == target.id and charge.attempt == charge_attempt
    ]
    if len(matches) != 1:
        raise TaskBudgetResetError("expected exactly one budget charge for the requested charge attempt")
    return matches[0]


def _matches_reset(state: RunState, request: TaskBudgetResetRequest) -> bool:
    target = state.slices.get(request.slice_id)
    if target is None:
        return False
    try:
        charge = _attempt_charge(state.budgets, target, request.expected_charge_attempt)
    except TaskBudgetResetError:
        return False
    return (
        target.status is SliceStatus.IN_REVIEW
        and target.park_cause is None
        and target.task_timeout_seconds == request.new_task_timeout_seconds
        and target.task_timeout_source == "operator"
        and target.task_budget_reset_key == request.idempotency_key
        and charge.estimated_tokens == request.new_estimated_tokens
    )


def _check_revision(state: RunState, expected: int, label: str) -> None:
    if state.revision != expected:
        raise TaskBudgetResetError(
            f"{label} revision changed: expected {expected}, found {state.revision}"
        )


def _validate_request(request: TaskBudgetResetRequest) -> None:
    if request.run_id in {".", ".."} or "/" in request.run_id or "\\" in request.run_id:
        raise TaskBudgetResetError("run_id must be one path component")
    if request.child_run_id in {".", ".."} or "/" in request.child_run_id or "\\" in request.child_run_id:
        raise TaskBudgetResetError("child_run_id must be one path component")
    if request.slice_id in {".", ".."} or "/" in request.slice_id or "\\" in request.slice_id:
        raise TaskBudgetResetError("slice_id must be one path component")
    if not _TASK_TYPE.fullmatch(request.task_type):
        raise TaskBudgetResetError("task_type must be a lowercase snake_case identifier")
    if len(request.operator_reason) > 512:
        raise TaskBudgetResetError("operator_reason must be at most 512 characters")
    if not request.operator_reason.strip() or len(request.idempotency_key) > 128:
        raise TaskBudgetResetError("operator_reason and idempotency_key must be bounded and non-empty")
    if request.new_task_timeout_seconds > _MAX_TIMEOUT_SECONDS:
        raise TaskBudgetResetError("new task timeout exceeds the per-slice maximum")
    if request.new_estimated_tokens > _MAX_ESTIMATED_TOKENS:
        raise TaskBudgetResetError("new estimated token budget exceeds the per-slice maximum")
    if request.observed_tokens is not None and request.observed_tokens > _MAX_ESTIMATED_TOKENS:
        raise TaskBudgetResetError("observed token count exceeds the per-slice maximum")


def _text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise TaskBudgetResetError(f"{key} must be non-empty text")
    return item


def _non_negative_int(value: object, key: str) -> int:
    if type(value) is not int or value < 0:
        raise TaskBudgetResetError(f"{key} must be a non-negative integer")
    return value


def _positive_int(value: object, key: str) -> int:
    parsed = _non_negative_int(value, key)
    if parsed == 0:
        raise TaskBudgetResetError(f"{key} must be positive")
    return parsed


def _positive_number(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskBudgetResetError(f"{key} must be numeric")
    parsed = float(value)
    if not parsed > 0 or parsed > _MAX_TIMEOUT_SECONDS:
        raise TaskBudgetResetError(f"{key} must be positive and within the timeout maximum")
    return parsed
