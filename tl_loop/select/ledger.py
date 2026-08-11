"""Pure budget reservation and reconciliation for selector decisions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeAlias, cast

from tl_loop.select.agent_type import HarnessChoice
from tl_loop.state.schema import BudgetCharge, BudgetLedger, SliceState
from tl_loop.state.write import WriteHooks, apply

UNKNOWN_ACTUAL = "unknown"
RECONCILIATION_WARNING_THRESHOLD = 0.20
LedgerInput: TypeAlias = BudgetLedger | Mapping[str, object]
Document: TypeAlias = dict[str, object]
SpawnRecorder: TypeAlias = Callable[[Document], Document]


class LedgerError(ValueError):
    """Base error for invalid or unsafe budget mutations."""


class BudgetCeilingExceeded(LedgerError):
    """A reservation would exceed a role or harness ceiling."""


class DuplicateCharge(LedgerError):
    """A slice attempt already has a spawn charge."""


class ChargeNotFound(LedgerError):
    """No pending charge exists for the requested slice."""


def charge_spawn(
    ledger: LedgerInput, choice: HarnessChoice, slice: SliceState
) -> dict[str, object]:
    """Reserve one selected spawn without performing I/O or mutating the input."""
    estimated = _non_negative(choice.estimated_cost, "choice.estimated_cost")
    if estimated == 0:
        raise LedgerError("choice.estimated_cost must be positive")
    role = _non_empty(choice.role, "choice.role")
    harness = _non_empty(choice.harness, "choice.harness")
    attempt = slice.attempts + 1
    result = _document(ledger)
    charges = _charges(result)
    if any(
        item["slice_id"] == slice.id and item["attempt"] == attempt
        for item in charges
    ):
        raise DuplicateCharge(f"slice {slice.id!r} attempt {attempt} is already charged")

    role_spent = _counter(result, "role_spent")
    harness_spent = _counter(result, "harness_spent")
    role_reserved = _counter(result, "role_reserved")
    harness_reserved = _counter(result, "harness_reserved")
    role_limit = choice.role_budget
    harness_limit = choice.harness_budget
    if role_limit is not None and role_reserved.get(role, 0) + role_spent.get(role, 0) + estimated > role_limit:
        raise BudgetCeilingExceeded(f"role {role!r} budget would be exceeded")
    if (
        harness_limit is not None
        and harness_reserved.get(harness, 0) + harness_spent.get(harness, 0) + estimated > harness_limit
    ):
        raise BudgetCeilingExceeded(f"harness {harness!r} budget would be exceeded")

    role_reserved[role] = role_reserved.get(role, 0) + estimated
    harness_reserved[harness] = harness_reserved.get(harness, 0) + estimated
    result["role_spent"] = role_spent
    result["harness_spent"] = harness_spent
    result["role_reserved"] = role_reserved
    result["harness_reserved"] = harness_reserved
    charges.append(
        {
            "slice_id": slice.id,
            "attempt": attempt,
            "role": role,
            "harness": harness,
            "estimated_tokens": estimated,
            "actual": UNKNOWN_ACTUAL,
            "delta_tokens": None,
            "warning": False,
            "reconciled": False,
        }
    )
    result["charges"] = charges
    return result


def reconcile(
    ledger: LedgerInput, slice_id: str, actual_tokens: int | None
) -> dict[str, object]:
    """Move a reservation to spend and record the measured usage delta.

    None means no authoritative usage was available. The estimate is then
    conservatively applied to the ceilings while the charge records
    actual="unknown" and a warning.
    """
    slice_id = _non_empty(slice_id, "slice_id")
    if actual_tokens is not None:
        actual_tokens = _non_negative(actual_tokens, "actual_tokens")
    result = _document(ledger)
    charges = _charges(result)
    charge_index = _pending_charge_index(charges, slice_id)
    if charge_index is None:
        raise ChargeNotFound(f"no pending charge exists for slice {slice_id!r}")
    charge = charges[charge_index]
    estimate = _non_negative(charge["estimated_tokens"], "charge.estimated_tokens")
    role = _non_empty(cast(str, charge["role"]), "charge.role")
    harness = _non_empty(cast(str, charge["harness"]), "charge.harness")
    applied = estimate if actual_tokens is None else actual_tokens
    delta = None if actual_tokens is None else actual_tokens - estimate
    warning = actual_tokens is None or abs(cast(int, delta or 0)) / max(estimate, 1) > RECONCILIATION_WARNING_THRESHOLD

    role_reserved = _counter(result, "role_reserved")
    harness_reserved = _counter(result, "harness_reserved")
    if role_reserved.get(role, 0) < estimate or harness_reserved.get(harness, 0) < estimate:
        raise LedgerError("reservation is smaller than the charge estimate")
    role_reserved[role] -= estimate
    harness_reserved[harness] -= estimate
    _drop_zero(role_reserved, role)
    _drop_zero(harness_reserved, harness)

    role_spent = _counter(result, "role_spent")
    harness_spent = _counter(result, "harness_spent")
    role_spent[role] = role_spent.get(role, 0) + applied
    harness_spent[harness] = harness_spent.get(harness, 0) + applied
    result["role_spent"] = role_spent
    result["harness_spent"] = harness_spent
    result["role_reserved"] = role_reserved
    result["harness_reserved"] = harness_reserved
    result["tokens"] = _integer(result.get("tokens", 0), "ledger.tokens") + applied

    updated = dict(charge)
    updated["actual"] = UNKNOWN_ACTUAL if actual_tokens is None else actual_tokens
    updated["delta_tokens"] = delta
    updated["warning"] = warning
    updated["reconciled"] = True
    charges[charge_index] = updated
    result["charges"] = charges
    return result


def resolve_actual_tokens(
    chainlink_usage: int | None = None, harness_reported_usage: int | None = None
) -> int | None:
    """Prefer authoritative Chainlink usage, then a harness report.

    Returning None is intentional: callers must pass it to reconcile so
    missing usage is persisted as actual="unknown".
    """
    if chainlink_usage is not None:
        return _non_negative(chainlink_usage, "chainlink_usage")
    if harness_reported_usage is not None:
        return _non_negative(harness_reported_usage, "harness_reported_usage")
    return None


def apply_spawn_and_charge(
    run_dir: str | Path,
    choice: HarnessChoice,
    slice: SliceState,
    record_spawn: SpawnRecorder,
    *,
    lock_timeout: float = 5.0,
    hooks: WriteHooks | None = None,
) -> Document:
    """Record a spawn and its reservation in one state-writer apply call."""
    if not callable(record_spawn):
        raise TypeError("record_spawn must be callable")

    def mutate(document: Document) -> Document:
        updated = record_spawn(document)
        if not isinstance(updated, dict):
            raise TypeError("record_spawn must return an object")
        budgets = updated.get("budgets")
        if not isinstance(budgets, dict):
            raise LedgerError("run state is missing the budgets object")
        raw_ledger = budgets.get("ledger")
        if not isinstance(raw_ledger, Mapping):
            raise LedgerError("run state is missing the budget ledger")
        updated["budgets"] = {"ledger": charge_spawn(raw_ledger, choice, slice)}
        return updated

    return apply(run_dir, mutate, lock_timeout=lock_timeout, hooks=hooks)


def _document(ledger: LedgerInput) -> dict[str, object]:
    if isinstance(ledger, BudgetLedger):
        return {
            "tokens": ledger.tokens,
            "wall_seconds": ledger.wall_seconds,
            "role_spent": dict(ledger.role_spent),
            "harness_spent": dict(ledger.harness_spent),
            "role_reserved": dict(ledger.role_reserved),
            "harness_reserved": dict(ledger.harness_reserved),
            "charges": [_encode_charge(charge) for charge in ledger.charges],
        }
    if not isinstance(ledger, Mapping):
        raise LedgerError("ledger must be a BudgetLedger or object")
    result = dict(ledger)
    result.setdefault("tokens", 0)
    result.setdefault("wall_seconds", 0)
    return result


def _encode_charge(charge: BudgetCharge) -> dict[str, object]:
    return {
        "slice_id": charge.slice_id,
        "attempt": charge.attempt,
        "role": charge.role,
        "harness": charge.harness,
        "estimated_tokens": charge.estimated_tokens,
        "actual": charge.actual,
        "delta_tokens": charge.delta_tokens,
        "warning": charge.warning,
        "reconciled": charge.reconciled,
    }


def _counter(document: Mapping[str, object], key: str) -> dict[str, int]:
    value = document.get(key, {})
    if not isinstance(value, Mapping):
        raise LedgerError(f"ledger.{key} must be an object")
    result: dict[str, int] = {}
    for name, amount in value.items():
        result[_non_empty(cast(str, name), f"ledger.{key} key")] = _non_negative(
            amount, f"ledger.{key}.{name}"
        )
    return result


def _charges(document: Mapping[str, object]) -> list[dict[str, object]]:
    value = document.get("charges", [])
    if not isinstance(value, list):
        raise LedgerError("ledger.charges must be an array")
    result: list[dict[str, object]] = []
    for charge in value:
        if not isinstance(charge, Mapping):
            raise LedgerError("ledger.charges entries must be objects")
        result.append(dict(charge))
    return result


def _pending_charge_index(charges: list[dict[str, object]], slice_id: str) -> int | None:
    for index in range(len(charges) - 1, -1, -1):
        charge = charges[index]
        if charge.get("slice_id") == slice_id and charge.get("reconciled") is not True:
            return index
    return None


def _drop_zero(values: dict[str, int], key: str) -> None:
    if values[key] == 0:
        del values[key]


def _integer(value: object, path: str) -> int:
    if type(value) is not int:
        raise LedgerError(f"{path} must be an integer")
    return cast(int, value)


def _non_negative(value: object, path: str) -> int:
    number = _integer(value, path)
    if number < 0:
        raise LedgerError(f"{path} must be non-negative")
    return number


def _non_empty(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise LedgerError(f"{path} must be a non-empty string")
    return value


__all__ = [
    "RECONCILIATION_WARNING_THRESHOLD",
    "UNKNOWN_ACTUAL",
    "BudgetCeilingExceeded",
    "ChargeNotFound",
    "DuplicateCharge",
    "LedgerError",
    "apply_spawn_and_charge",
    "charge_spawn",
    "reconcile",
    "resolve_actual_tokens",
]
