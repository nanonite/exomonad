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
    return _reserve(
        ledger,
        choice,
        slice_id=slice.id,
        attempt=slice.attempts + 1,
        continuation=None,
    )


def charge_continuation(
    ledger: LedgerInput,
    choice: HarnessChoice,
    slice: SliceState,
    continuation_id: str,
    *,
    attempt: int | None = None,
) -> dict[str, object]:
    """Reserve one same-slice continuation on the route it will actually run.

    A ``resume_pr`` or ``resume_blocked_leaf`` continuation extends a slice
    attempt with a new invocation. It names the continuation it charges so a
    replayed continuation is not charged twice, and it is otherwise the same
    reservation, ceiling check, and charge record a dispatch uses. The earlier
    dispatch's spend is never rewritten.
    """
    selected = slice.attempts if attempt is None else _integer(attempt, "attempt")
    if selected < 1 or selected not in {slice.attempts, slice.attempts + 1}:
        raise LedgerError(
            "a continuation attempt must be the slice attempt it extends or its successor"
        )
    return _reserve(
        ledger,
        choice,
        slice_id=slice.id,
        attempt=selected,
        continuation=_non_empty(continuation_id, "continuation_id"),
    )


def _reserve(
    ledger: LedgerInput,
    choice: HarnessChoice,
    *,
    slice_id: str,
    attempt: int,
    continuation: str | None,
) -> dict[str, object]:
    estimated = _non_negative(choice.estimated_cost, "choice.estimated_cost")
    if estimated == 0:
        raise LedgerError("choice.estimated_cost must be positive")
    role = _non_empty(choice.role, "choice.role")
    harness = _non_empty(choice.harness, "choice.harness")
    slice_id = _non_empty(slice_id, "slice.id")
    result = _document(ledger)
    charges = _charges(result)
    if any(
        item["slice_id"] == slice_id
        and item["attempt"] == attempt
        and _charge_continuation(item) == continuation
        for item in charges
    ):
        label = "continuation" if continuation is not None else "attempt"
        raise DuplicateCharge(
            f"slice {slice_id!r} attempt {attempt} {label} is already charged"
        )

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
    charge: dict[str, object] = {
        "slice_id": slice_id,
        "attempt": attempt,
        "role": role,
        "harness": harness,
        "estimated_tokens": estimated,
        "actual": UNKNOWN_ACTUAL,
        "delta_tokens": None,
        "warning": False,
        "reconciled": False,
    }
    if continuation is not None:
        charge["continuation"] = continuation
    charges.append(charge)
    result["charges"] = charges
    return result


def find_continuation_charge(
    ledger: BudgetLedger, slice_id: str, continuation_id: str
) -> BudgetCharge | None:
    """Return the charge recorded for one continuation, if it was reserved."""
    slice_id = _non_empty(slice_id, "slice_id")
    continuation_id = _non_empty(continuation_id, "continuation_id")
    matches = [
        charge
        for charge in ledger.charges
        if charge.slice_id == slice_id and charge.continuation == continuation_id
    ]
    if len(matches) > 1:
        raise LedgerError(
            f"slice {slice_id!r} continuation {continuation_id!r} is charged more than once"
        )
    return matches[0] if matches else None


def retarget_continuation(
    ledger: LedgerInput,
    slice_id: str,
    continuation_id: str,
    choice: HarnessChoice,
) -> dict[str, object]:
    """Move one pending continuation reservation to the route actually launched.

    This rewrites the same reservation and charge record; it does not create a
    second charge or a second counter path. The old reservation is released
    before the new ceiling check, so a retarget can never widen the role or
    per-harness ceiling. A charge already naming the requested route and
    estimate is returned unchanged, which makes a replayed continuation
    idempotent.
    """
    slice_id = _non_empty(slice_id, "slice_id")
    continuation_id = _non_empty(continuation_id, "continuation_id")
    role = _non_empty(choice.role, "choice.role")
    harness = _non_empty(choice.harness, "choice.harness")
    estimated = _non_negative(choice.estimated_cost, "choice.estimated_cost")
    if estimated == 0:
        raise LedgerError("choice.estimated_cost must be positive")
    result = _document(ledger)
    charges = _charges(result)
    index = _pending_charge_index(charges, slice_id, continuation_id)
    if index is None:
        raise ChargeNotFound(
            f"no pending continuation charge exists for slice {slice_id!r}"
        )
    charge = dict(charges[index])
    old_estimate = _non_negative(charge["estimated_tokens"], "charge.estimated_tokens")
    old_role = _non_empty(cast(str, charge["role"]), "charge.role")
    old_harness = _non_empty(cast(str, charge["harness"]), "charge.harness")
    if (old_role, old_harness, old_estimate) == (role, harness, estimated):
        return result

    role_reserved = _counter(result, "role_reserved")
    harness_reserved = _counter(result, "harness_reserved")
    role_spent = _counter(result, "role_spent")
    harness_spent = _counter(result, "harness_spent")
    if (
        role_reserved.get(old_role, 0) < old_estimate
        or harness_reserved.get(old_harness, 0) < old_estimate
    ):
        raise LedgerError("reservation is smaller than the charge estimate")
    role_reserved[old_role] -= old_estimate
    harness_reserved[old_harness] -= old_estimate
    _drop_zero(role_reserved, old_role)
    _drop_zero(harness_reserved, old_harness)

    role_limit = choice.role_budget
    harness_limit = choice.harness_budget
    if (
        role_limit is not None
        and role_reserved.get(role, 0) + role_spent.get(role, 0) + estimated > role_limit
    ):
        raise BudgetCeilingExceeded(f"role {role!r} budget would be exceeded")
    if (
        harness_limit is not None
        and harness_reserved.get(harness, 0) + harness_spent.get(harness, 0) + estimated
        > harness_limit
    ):
        raise BudgetCeilingExceeded(f"harness {harness!r} budget would be exceeded")

    role_reserved[role] = role_reserved.get(role, 0) + estimated
    harness_reserved[harness] = harness_reserved.get(harness, 0) + estimated
    result["role_reserved"] = role_reserved
    result["harness_reserved"] = harness_reserved
    result["role_spent"] = role_spent
    result["harness_spent"] = harness_spent
    charge["role"] = role
    charge["harness"] = harness
    charge["estimated_tokens"] = estimated
    charges[index] = charge
    result["charges"] = charges
    return result



def reconcile(
    ledger: LedgerInput,
    slice_id: str,
    actual_tokens: int | None,
    *,
    continuation: str | None = None,
) -> dict[str, object]:
    """Move a reservation to spend and record the measured usage delta.

    None means no authoritative usage was available. The estimate is then
    conservatively applied to the ceilings while the charge records
    actual="unknown" and a warning.

    ``continuation`` names the continuation charge to reconcile. Without it the
    pending dispatch charge for the slice is reconciled, so a pending
    continuation can never be consumed by the dispatch settlement.
    """
    slice_id = _non_empty(slice_id, "slice_id")
    selected_continuation = (
        None if continuation is None else _non_empty(continuation, "continuation")
    )
    if actual_tokens is not None:
        actual_tokens = _non_negative(actual_tokens, "actual_tokens")
    result = _document(ledger)
    charges = _charges(result)
    charge_index = _pending_charge_index(charges, slice_id, selected_continuation)
    if charge_index is None:
        label = "continuation" if selected_continuation is not None else "dispatch"
        raise ChargeNotFound(f"no pending {label} charge exists for slice {slice_id!r}")
    charge = charges[charge_index]
    estimate = _non_negative(charge["estimated_tokens"], "charge.estimated_tokens")
    role = _non_empty(cast(str, charge["role"]), "charge.role")
    harness = _non_empty(cast(str, charge["harness"]), "charge.harness")
    applied = estimate if actual_tokens is None else actual_tokens
    delta = None if actual_tokens is None else actual_tokens - estimate
    warning = actual_tokens is None or abs(delta or 0) / max(estimate, 1) > RECONCILIATION_WARNING_THRESHOLD

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


def apply_continuation_and_charge(
    run_dir: str | Path,
    choice: HarnessChoice,
    slice: SliceState,
    continuation_id: str,
    record_continuation: SpawnRecorder,
    *,
    attempt: int | None = None,
    lock_timeout: float = 5.0,
    hooks: WriteHooks | None = None,
) -> Document:
    """Record a continuation and its reservation in one state-writer apply call."""
    if not callable(record_continuation):
        raise TypeError("record_continuation must be callable")

    def mutate(document: Document) -> Document:
        updated = record_continuation(document)
        if not isinstance(updated, dict):
            raise TypeError("record_continuation must return an object")
        updated["budgets"] = {
            "ledger": charge_continuation(
                _document_mapping(updated, "continuation"),
                choice,
                slice,
                continuation_id,
                attempt=attempt,
            )
        }
        return updated

    return apply(run_dir, mutate, lock_timeout=lock_timeout, hooks=hooks)


def apply_continuation_reconcile(
    run_dir: str | Path,
    slice_id: str,
    continuation_id: str,
    actual_tokens: int | None,
    *,
    lock_timeout: float = 5.0,
    hooks: WriteHooks | None = None,
) -> Document:
    """Settle one continuation reservation through the same atomic ledger path."""

    def mutate(document: Document) -> Document:
        document["budgets"] = {
            "ledger": reconcile(
                _document_mapping(document, "continuation"),
                slice_id,
                actual_tokens,
                continuation=continuation_id,
            )
        }
        return document

    return apply(run_dir, mutate, lock_timeout=lock_timeout, hooks=hooks)


def apply_continuation_retarget(
    run_dir: str | Path,
    slice_id: str,
    continuation_id: str,
    choice: HarnessChoice,
    *,
    lock_timeout: float = 5.0,
    hooks: WriteHooks | None = None,
) -> Document:
    """Move one pending continuation reservation to the launched route atomically."""

    def mutate(document: Document) -> Document:
        document["budgets"] = {
            "ledger": retarget_continuation(
                _document_mapping(document, "continuation"),
                slice_id,
                continuation_id,
                choice,
            )
        }
        return document

    return apply(run_dir, mutate, lock_timeout=lock_timeout, hooks=hooks)


def _document_mapping(document: Mapping[str, object], label: str) -> Mapping[str, object]:
    budgets = document.get("budgets")
    if not isinstance(budgets, Mapping):
        raise LedgerError(f"run state is missing the budgets object for {label}")
    raw_ledger = budgets.get("ledger")
    if not isinstance(raw_ledger, Mapping):
        raise LedgerError(f"run state is missing the budget ledger for {label}")
    return raw_ledger


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
    encoded: dict[str, object] = {
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
    if charge.continuation is not None:
        encoded["continuation"] = charge.continuation
    return encoded


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


def _pending_charge_index(
    charges: list[dict[str, object]],
    slice_id: str,
    continuation: str | None,
) -> int | None:
    for index in range(len(charges) - 1, -1, -1):
        charge = charges[index]
        if (
            charge.get("slice_id") == slice_id
            and charge.get("reconciled") is not True
            and _charge_continuation(charge) == continuation
        ):
            return index
    return None


def _charge_continuation(charge: Mapping[str, object]) -> str | None:
    value = charge.get("continuation")
    return value if isinstance(value, str) and value else None


def _drop_zero(values: dict[str, int], key: str) -> None:
    if values[key] == 0:
        del values[key]


def _integer(value: object, path: str) -> int:
    if type(value) is not int:
        raise LedgerError(f"{path} must be an integer")
    return value


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
    "apply_continuation_and_charge",
    "apply_continuation_reconcile",
    "apply_continuation_retarget",
    "apply_spawn_and_charge",
    "charge_continuation",
    "charge_spawn",
    "find_continuation_charge",
    "reconcile",
    "resolve_actual_tokens",
    "retarget_continuation",
]
