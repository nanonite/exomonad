"""Deterministic, budget-bounded harness selection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from math import ceil
from typing import cast

from tl_loop.select.capability import CapabilityMap, load_capability
from tl_loop.select.classify import Classification, Difficulty, classify_task
from tl_loop.select.learned_policy import LearnedPolicy
from tl_loop.select.policy import HarnessPolicy, RolePolicy
from tl_loop.state.schema import BudgetLedger, SliceState, Verdict


class SelectionFailure(str, Enum):
    """Closed reasons why no allowed harness can be selected."""

    OVER_BUDGET = "over_budget"
    NO_CAPABLE_HARNESS = "no_capable_harness"


@dataclass(frozen=True)
class SelectionLedger:
    """Read-only budget snapshot consumed by the selector."""

    role_spent: Mapping[str, int] = field(default_factory=dict)
    role_reserved: Mapping[str, int] = field(default_factory=dict)
    harness_spent: Mapping[str, int] = field(default_factory=dict)
    harness_reserved: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessChoice:
    """Auditable harness selection and the candidates considered."""

    harness: str
    reason: str
    difficulty: Difficulty
    matched_rule: str
    estimated_cost: int
    candidate_set: tuple[str, ...]
    role: str = "worker"
    role_budget: int | None = None
    harness_budget: int | None = None

    @property
    def estimated_tokens(self) -> int:
        """Return the reservation amount under the selector's naming."""
        return self.estimated_cost


def select_agent_type(
    slice: SliceState,
    role: str,
    ledger: object,
    policy: HarnessPolicy,
    capabilities: CapabilityMap | None = None,
    learned_policy: LearnedPolicy | None = None,
) -> HarnessChoice | None:
    """Select the cheapest allowed capable harness that still has budget."""
    role_policy = _role_policy(policy, role)
    capability_map = capabilities or load_capability()
    classification = classify_task(slice)
    candidates = _candidates(
        slice, role, role_policy, ledger, capability_map, classification
    )
    if not candidates:
        return None
    selected = min(
        candidates,
        key=lambda item: _selection_key(item, role_policy, classification, role, learned_policy),
    )
    harness, estimated_cost = selected
    reason = _selection_reason(slice, classification, role_policy)
    return HarnessChoice(
        harness=harness,
        reason=reason,
        difficulty=classification.difficulty,
        matched_rule=classification.matched_rule_name,
        estimated_cost=estimated_cost,
        candidate_set=tuple(item[0] for item in candidates),
        role=role,
        role_budget=role_policy.token_budget,
        harness_budget=role_policy.per_harness_budget.get(harness),
    )


def selection_failure(
    slice: SliceState,
    role: str,
    ledger: object,
    policy: HarnessPolicy,
    capabilities: CapabilityMap | None = None,
) -> SelectionFailure:
    """Explain why the selector returned no choice."""
    role_policy = _role_policy(policy, role)
    capability_map = capabilities or load_capability()
    classification = classify_task(slice)
    capable = _capable_harnesses(slice, role_policy, capability_map, classification)
    if not capable:
        return SelectionFailure.NO_CAPABLE_HARNESS
    return SelectionFailure.OVER_BUDGET


def estimate_cost(
    slice: SliceState, difficulty: Difficulty, harness_rate: float = 1.0
) -> int:
    """Estimate tokens using the documented task shape and harness rate."""
    if harness_rate <= 0:
        raise ValueError("harness_rate must be positive")
    base = {
        Difficulty.TRIVIAL: 100,
        Difficulty.STANDARD: 500,
        Difficulty.HARD: 1000,
    }[difficulty]
    unscaled = base + 50 * len(slice.test_plan) + 100 * len(slice.paths) + 50 * len(slice.depends_on)
    return ceil(unscaled * harness_rate)


def _role_policy(policy: HarnessPolicy, role: str) -> RolePolicy:
    try:
        return policy.roles[role]
    except KeyError as error:
        raise ValueError(f"unknown policy role {role!r}") from error


def _selection_key(
    candidate: tuple[str, int],
    role_policy: RolePolicy,
    classification: Classification,
    role: str,
    learned_policy: LearnedPolicy | None,
) -> tuple[int, int, int]:
    harness, _cost = candidate
    learned_order = (
        learned_policy.preference_order(classification.matched_rule_name, role)
        if learned_policy is not None
        else ()
    )
    learned_rank = (
        learned_order.index(harness)
        if harness in learned_order
        else len(learned_order)
    )
    return (
        role_policy.cost_rank[harness],
        learned_rank,
        role_policy.allow.index(harness),
    )


def _candidates(
    slice: SliceState,
    role: str,
    role_policy: RolePolicy,
    ledger: object,
    capabilities: CapabilityMap,
    classification: Classification,
) -> list[tuple[str, int]]:
    capable = _capable_harnesses(slice, role_policy, capabilities, classification)
    result: list[tuple[str, int]] = []
    for harness in capable:
        cost = estimate_cost(
            slice, classification.difficulty, role_policy.cost_rank[harness]
        )
        if _within_budget(ledger, role, role_policy, harness, cost):
            result.append((harness, cost))
    return result


def _capable_harnesses(
    slice: SliceState,
    role_policy: RolePolicy,
    capabilities: CapabilityMap,
    classification: Classification,
) -> tuple[str, ...]:
    failed = _failed_harness(slice, role_policy)
    return tuple(
        harness
        for harness in role_policy.allow
        if harness != failed and capabilities.is_capable(harness, classification.difficulty)
    )


def _failed_harness(slice: SliceState, role_policy: RolePolicy) -> str | None:
    if slice.verdict is not Verdict.NO_GO:
        return None
    if slice.attempts < role_policy.escalate_after_attempts:
        return None
    return slice.agent_type if slice.agent_type in role_policy.allow else None


def _selection_reason(
    slice: SliceState,
    classification: Classification,
    role_policy: RolePolicy,
) -> str:
    if classification.difficulty is Difficulty.HARD:
        return "hard_classification"
    if _failed_harness(slice, role_policy) is not None:
        return "escalated_after_no_go"
    return "cheapest_capable"


def _within_budget(
    ledger: object, role: str, role_policy: RolePolicy, harness: str, cost: int
) -> bool:
    role_used = _spent(ledger, "role", role, harness)
    if role_used + cost > role_policy.token_budget:
        return False
    limit = role_policy.per_harness_budget.get(harness)
    return limit is None or _spent(ledger, "harness", role, harness) + cost <= limit


def _spent(ledger: object, kind: str, role: str, harness: str) -> int:
    if isinstance(ledger, SelectionLedger):
        if kind == "role":
            return ledger.role_spent.get(role, 0) + ledger.role_reserved.get(role, 0)
        return ledger.harness_spent.get(harness, 0) + ledger.harness_reserved.get(harness, 0)
    if isinstance(ledger, BudgetLedger):
        if kind == "role":
            if ledger.role_spent or ledger.role_reserved:
                return (
                    _non_negative(ledger.role_spent.get(role, 0), "ledger.role_spent")
                    + _non_negative(ledger.role_reserved.get(role, 0), "ledger.role_reserved")
                )
            return _non_negative(ledger.tokens, "ledger.tokens")
        return (
            _non_negative(ledger.harness_spent.get(harness, 0), "ledger.harness_spent")
            + _non_negative(ledger.harness_reserved.get(harness, 0), "ledger.harness_reserved")
        )
    if isinstance(ledger, Mapping):
        direct_spent = ledger.get(f"{kind}_spent")
        direct_reserved = ledger.get(f"{kind}_reserved")
        if direct_spent is not None or direct_reserved is not None:
            return _counter_value(direct_spent, role, harness, kind, "spent") + _counter_value(
                direct_reserved, role, harness, kind, "reserved"
            )
        spent = _mapping_value(ledger, "spent", kind, role, harness)
        reserved = _mapping_value(ledger, "reserved", kind, role, harness)
        return spent + reserved
    return 0


def _mapping_value(
    ledger: Mapping[str, object], key: str, kind: str, role: str, harness: str
) -> int:
    value = ledger.get(key, {})
    if isinstance(value, Mapping):
        nested = value.get(kind)
        if isinstance(nested, Mapping):
            value = nested
        lookup = role if kind == "role" else harness
        return _non_negative(value.get(lookup, 0), f"ledger.{key}.{lookup}")
    return _non_negative(value, f"ledger.{key}") if kind == "role" else 0


def _counter_value(
    value: object, role: str, harness: str, kind: str, label: str
) -> int:
    if value is None:
        return 0
    if not isinstance(value, Mapping):
        raise TypeError(f"ledger.{kind}_{label} must be an object")
    lookup = role if kind == "role" else harness
    return _non_negative(value.get(lookup, 0), f"ledger.{kind}_{label}.{lookup}")


def _non_negative(value: object, path: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{path} must be a non-negative integer")
    return cast(int, value)


__all__ = [
    "HarnessChoice",
    "SelectionFailure",
    "SelectionLedger",
    "estimate_cost",
    "select_agent_type",
    "selection_failure",
]
