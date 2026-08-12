"""Closed-key validation for plan documents and inert control proposals."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from fnmatch import fnmatchcase
from pathlib import Path

_PLAN_KEYS = frozenset({"run_id", "budgets", "plan", "workers", "leaves", "sub_tls"})
_PROPOSAL_KEYS = frozenset({"plan", "workers", "leaves", "sub_tls"})
_TASK_KEYS = {
    "workers": frozenset({"name", "task", "agent_type"}),
    "leaves": frozenset(
        {
            "name",
            "task",
            "agent_type",
            "boundary",
            "context",
            "read_first",
            "steps",
            "verify",
            "done_criteria",
        }
    ),
    "sub_tls": frozenset(
        {"name", "plan", "workers", "leaves", "sub_tls", "agent_type", "worktree", "agent_id"}
    ),
}


class PlanValidationError(ValueError):
    """A plan or proposal violates the closed-key contract."""


def validate_plan_document(value: object, *, proposal: bool = False) -> dict[str, object]:
    """Validate a plan document and return a detached JSON-compatible copy.

    Proposal documents intentionally omit ``run_id`` and ``budgets``. Those
    fields are authority-bearing state and cannot be changed by the control
    client. The returned value is only a validated projection; this function
    never reads or writes a plan file.
    """
    if not isinstance(value, dict):
        raise PlanValidationError("plan document must be an object")
    allowed = _PROPOSAL_KEYS if proposal else _PLAN_KEYS
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PlanValidationError(f"plan document contains unknown keys: {', '.join(unknown)}")
    if not proposal:
        _validate_run_id(value.get("run_id"))
        _validate_budgets(value.get("budgets"))

    plan_value = value.get("plan")
    lifted = {key: value[key] for key in ("workers", "leaves", "sub_tls") if key in value}
    if plan_value is not None and lifted:
        raise PlanValidationError(
            "plan cannot be combined with top-level workers, leaves, or sub_tls"
        )
    if plan_value is None:
        plan_value = lifted
    if not isinstance(plan_value, Mapping):
        raise PlanValidationError("plan must contain a WorkPlan object")
    _validate_work_plan(plan_value, "plan")

    result: dict[str, object] = {"plan": deepcopy(dict(plan_value))}
    if not proposal:
        for key in ("run_id", "budgets"):
            if key in value:
                result[key] = deepcopy(value[key])
    return result


def validate_plan_proposal(value: object) -> dict[str, object]:
    """Validate the closed body accepted by ``/control`` plan proposals."""
    return validate_plan_document(value, proposal=True)


def _validate_work_plan(value: Mapping[str, object], path: str) -> None:
    from tl_loop.loop.driver import WorkPlan

    try:
        WorkPlan.from_mapping(value)
    except (TypeError, ValueError) as error:
        raise PlanValidationError(f"{path}: {error}") from error

    for kind in ("workers", "leaves", "sub_tls"):
        entries = value.get(kind, ())
        if not isinstance(entries, list):
            continue
        allowed = _TASK_KEYS[kind]
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                continue
            unknown = sorted(set(entry) - allowed)
            if unknown:
                raise PlanValidationError(
                    f"{path}.{kind}[{index}] contains unknown keys: {', '.join(unknown)}"
                )
            if kind == "sub_tls":
                nested = entry.get("plan")
                if nested is None:
                    nested = {
                        key: entry[key] for key in ("workers", "leaves", "sub_tls") if key in entry
                    }
                if not isinstance(nested, Mapping):
                    raise PlanValidationError(f"{path}.sub_tls[{index}].plan must be an object")
                _validate_work_plan(nested, f"{path}.sub_tls[{index}].plan")

    _validate_sibling_ownership(value, path)


def _validate_sibling_ownership(value: Mapping[str, object], path: str) -> None:
    owned: list[tuple[str, str]] = []
    for kind in ("workers", "leaves", "sub_tls"):
        entries = value.get(kind, ())
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name", f"{kind}[{index}]")
            if not isinstance(name, str):
                continue
            boundaries = entry.get("boundary") if kind == "leaves" else None
            paths = (
                boundaries if isinstance(boundaries, list) and boundaries else [f"tl-loop/{name}"]
            )
            for candidate in paths:
                if not isinstance(candidate, str) or not candidate:
                    continue
                for other_name, other_path in owned:
                    if _patterns_overlap(candidate, other_path):
                        raise PlanValidationError(
                            f"{path}.{kind}[{index}] path {candidate!r} overlaps "
                            f"{other_name!r} path {other_path!r}"
                        )
                owned.append((name, candidate))


def _validate_run_id(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise PlanValidationError("run_id must be a non-empty single path component")


def _validate_budgets(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise PlanValidationError("budgets must be an object")


def _patterns_overlap(left: str, right: str) -> bool:
    return left == right or fnmatchcase(left, right) or fnmatchcase(right, left)


__all__ = [
    "PlanValidationError",
    "validate_plan_document",
    "validate_plan_proposal",
]
