"""Fail-closed, revisioned clarification for an existing TL plan.

Clarification is deliberately separate from plan mutation.  A continuation may
be proposed only when the plan's authority-bearing invariants are unchanged;
material changes produce a human-gate decision and never rewrite ``plan.json``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from tl_loop.client.transport import JsonObject

MATERIAL_PLAN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "scope",
        "paths",
        "boundary",
        "dependencies",
        "ownership",
        "harness",
        "definition_of_done",
        "verification",
        "plan_structure",
        "base_ref",
        "timeout",
    }
)
_INVARIANT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "paths",
        "boundary",
        "depends_on",
        "base_ref",
        "test_plan",
        "verify",
        "done_criteria",
        "agent_type",
        "worktree",
        "agent_id",
        "order",
        "integration",
        "task_timeout_seconds",
        "workers",
        "leaves",
        "sub_tls",
    }
)


class PlanClarificationError(ValueError):
    """A clarification is stale, ambiguous, or changes plan authority."""


@dataclass(frozen=True)
class PlanClarification:
    """One bounded continuation proposal against a known plan revision."""

    prior_revision: int
    proposed_revision: int
    invariant_digest: str
    continuation_task: str
    changed_fields: tuple[str, ...]
    requires_human: bool

    def __post_init__(self) -> None:
        if type(self.prior_revision) is not int or self.prior_revision < 0:
            raise PlanClarificationError("prior_revision must be non-negative")
        if self.proposed_revision != self.prior_revision + 1:
            raise PlanClarificationError("proposed_revision must advance exactly one revision")
        if not isinstance(self.invariant_digest, str) or len(self.invariant_digest) != 64:
            raise PlanClarificationError("invariant_digest must be a SHA-256 hex digest")
        try:
            int(self.invariant_digest, 16)
        except ValueError as error:
            raise PlanClarificationError("invariant_digest must be hexadecimal") from error
        if not isinstance(self.continuation_task, str) or not self.continuation_task.strip():
            raise PlanClarificationError("continuation_task must be non-empty")
        if type(self.requires_human) is not bool:
            raise PlanClarificationError("requires_human must be a boolean")
        if any(not isinstance(field, str) for field in self.changed_fields):
            raise PlanClarificationError("changed_fields must contain only strings")
        if tuple(sorted(set(self.changed_fields))) != self.changed_fields:
            raise PlanClarificationError("changed_fields must be sorted and unique")
        unknown = set(self.changed_fields) - MATERIAL_PLAN_FIELDS
        if unknown:
            raise PlanClarificationError(
                f"changed_fields contain unknown authority fields: {', '.join(sorted(unknown))}"
            )
        expected_gate = bool(self.changed_fields)
        if self.requires_human != expected_gate:
            raise PlanClarificationError("requires_human must match changed_fields")


def invariant_digest(plan: Mapping[str, object]) -> str:
    """Hash only authority-bearing plan fields in canonical JSON form."""
    if not isinstance(plan, Mapping):
        raise PlanClarificationError("plan must be an object")
    projection = _invariant_projection(plan)
    encoded = json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def classify_plan_changes(
    prior_plan: Mapping[str, object], proposed_plan: Mapping[str, object]
) -> tuple[str, ...]:
    """Classify authority-bearing differences without inspecting prompts."""
    before = _invariant_projection(prior_plan)
    after = _invariant_projection(proposed_plan)
    if before == after:
        return ()
    changes: set[str] = set()
    _classify_mapping_changes(before, after, changes)
    return tuple(sorted(changes or {"plan_structure"}))


def build_clarification(
    *,
    prior_revision: int,
    prior_plan: Mapping[str, object],
    proposed_plan: Mapping[str, object],
    continuation_task: str,
) -> PlanClarification:
    """Build a clarification without applying or persisting a plan mutation."""
    changed = classify_plan_changes(prior_plan, proposed_plan)
    return PlanClarification(
        prior_revision=prior_revision,
        proposed_revision=prior_revision + 1,
        invariant_digest=invariant_digest(prior_plan),
        continuation_task=continuation_task,
        changed_fields=changed,
        requires_human=bool(changed),
    )


def validate_clarification(
    clarification: PlanClarification,
    *,
    current_revision: int,
    current_digest: str,
    human_authorized: bool = False,
) -> None:
    """Validate compare-and-set identity before a caller resumes an owner."""
    if clarification.prior_revision != current_revision:
        raise PlanClarificationError("clarification revision is stale")
    if clarification.invariant_digest != current_digest:
        raise PlanClarificationError("clarification invariant digest is stale")
    if clarification.requires_human and not human_authorized:
        raise PlanClarificationError("material plan clarification requires human authorization")


def clarification_audit(clarification: PlanClarification) -> JsonObject:
    """Return safe aggregate fields without the continuation prompt."""
    return {
        "prior_revision": clarification.prior_revision,
        "proposed_revision": clarification.proposed_revision,
        "invariant_digest": clarification.invariant_digest,
        "changed_fields": list(clarification.changed_fields),
        "requires_human": clarification.requires_human,
    }


def _invariant_projection(value: Mapping[str, object]) -> dict[str, object]:
    projection: dict[str, object] = {}
    for key in sorted(value):
        if key not in _INVARIANT_KEYS:
            continue
        member = value[key]
        if isinstance(member, Mapping):
            projection[key] = _invariant_projection(member)
        elif isinstance(member, list):
            projection[key] = [
                _invariant_projection(item) if isinstance(item, Mapping) else item
                for item in member
            ]
        else:
            projection[key] = member
    return projection


def _classify_mapping_changes(
    before: Mapping[str, object], after: Mapping[str, object], changes: set[str]
) -> None:
    for key in set(before) | set(after):
        if before.get(key) == after.get(key):
            continue
        if key in {"workers", "leaves", "sub_tls"}:
            before_items = _named_items(before.get(key))
            after_items = _named_items(after.get(key))
            if before_items is None or after_items is None or set(before_items) != set(after_items):
                changes.add("plan_structure")
            else:
                for name in before_items:
                    _classify_mapping_changes(before_items[name], after_items[name], changes)
            continue
        if key in {"paths", "boundary"}:
            changes.update({"scope", key})
        elif key == "depends_on":
            changes.add("dependencies")
        elif key in {"agent_type", "worktree", "agent_id"}:
            changes.add("ownership" if key != "agent_type" else "harness")
        elif key == "done_criteria":
            changes.add("definition_of_done")
        elif key in {"verify", "test_plan"}:
            changes.add("verification")
        elif key == "base_ref":
            changes.add("base_ref")
        elif key == "task_timeout_seconds":
            changes.add("timeout")
        else:
            changes.add("plan_structure")


def _named_items(value: object) -> dict[str, Mapping[str, object]] | None:
    if not isinstance(value, list):
        return None
    result: dict[str, Mapping[str, object]] = {}
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            return None
        result[item["name"]] = item
    return result


__all__ = [
    "MATERIAL_PLAN_FIELDS",
    "PlanClarification",
    "PlanClarificationError",
    "build_clarification",
    "clarification_audit",
    "classify_plan_changes",
    "invariant_digest",
    "validate_clarification",
]
