"""The auditable crash matrix for recursive TL acceptance runs.

This module deliberately contains no controller logic. It defines the
observable effect boundaries and the identity material used to correlate a
crash, its journal entry, and the resumed operation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CrashBoundary:
    """One before/after process-death point in the production effect path."""

    name: str
    tool_name: str
    phase: str
    point: str

    def __post_init__(self) -> None:
        if not self.name or not self.tool_name or not self.phase:
            raise ValueError("crash boundary identity fields are required")
        if self.point not in {"before", "after"}:
            raise ValueError("crash boundary point must be before or after")


_LOGICAL_BOUNDARIES: tuple[tuple[str, str, str], ...] = (
    ("spawn", "emit_controller_event", "dispatch"),
    ("publication", "file_pr", "publication"),
    ("review", "watcher_pr_state", "review"),
    ("repair", "resume_pr", "repair"),
    ("merge_intent", "emit_controller_event", "merge_intent"),
    ("remote_merge", "merge_pr", "remote_merge"),
    ("adoption", "watcher_pr_state", "adoption"),
    ("parent_sync", "post_merge_parent_sync", "post_merge"),
    ("issue_close", "chainlink_issue_close", "post_merge"),
    ("changelog", "post_merge_changelog", "post_merge"),
    ("push", "post_merge_push", "post_merge"),
    ("stage_release", "emit_controller_event", "stage_release"),
    ("aggregate_publication", "file_pr", "aggregate_publication"),
    ("root_finalization", "root_branch_finalize", "root_finalization"),
)


CRASH_BOUNDARIES: tuple[CrashBoundary, ...] = tuple(
    CrashBoundary(name, tool_name, phase, point)
    for name, tool_name, phase in _LOGICAL_BOUNDARIES
    for point in ("before", "after")
)


LOGICAL_BOUNDARY_NAMES = frozenset(name for name, _, _ in _LOGICAL_BOUNDARIES)


def boundary_for(name: str, point: str) -> CrashBoundary:
    """Resolve a matrix entry without allowing an untracked boundary."""
    for boundary in CRASH_BOUNDARIES:
        if boundary.name == name and boundary.point == point:
            return boundary
    raise KeyError(f"unregistered crash boundary: {name}:{point}")


_SENSITIVE_KEYS = frozenset(
    {"body", "task", "context", "acceptance_criteria", "findings", "reason"}
)


def effect_identity(arguments: Mapping[str, Any], operation: str | None = None) -> str:
    """Return a stable, body-free identity for one external effect."""
    flattened = dict(arguments)
    payload = arguments.get("payload")
    if isinstance(payload, Mapping):
        flattened.update(payload)
    fields = {
        key: flattened[key]
        for key in (
            "event_type",
            "action_key",
            "intent_id",
            "push_intent_id",
            "push_journal_id",
            "child_id",
            "pr_number",
            "issue_id",
            "lane_epoch",
            "generation",
            "expected_base_sha",
            "expected_head_sha",
            "pushed_commit",
            "branch",
            "parent_branch",
        )
        if key in flattened and flattened[key] is not None
    }
    if operation is not None:
        fields["operation"] = operation
    if not fields:
        fields = {
            key: value
            for key, value in sorted(flattened.items())
            if key not in _SENSITIVE_KEYS and isinstance(value, (str, int, bool))
        }
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def redacted_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Keep trace identity while excluding authored task/review content."""
    return {
        key: _redact_value(value)
        for key, value in arguments.items()
        if key not in _SENSITIVE_KEYS
    }


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "<redacted>" if key in _SENSITIVE_KEYS else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    return value


def validate_matrix() -> None:
    """Fail fast if a requested logical operation loses before/after coverage."""
    for name in LOGICAL_BOUNDARY_NAMES:
        points = {
            boundary.point for boundary in CRASH_BOUNDARIES if boundary.name == name
        }
        if points != {"before", "after"}:
            raise AssertionError(f"incomplete crash matrix for {name}: {points!r}")
