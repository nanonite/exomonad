"""Closed review adjudication result contract and schema."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass

from tl_loop.client.transport import JsonObject, JsonValue
from tl_loop.state.schema import Verdict

_VERDICTS = tuple(verdict.value for verdict in Verdict)
_REASON_SEVERITIES = ("blocking", "nit", "info")
_REVIEW_REASON_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": list(_REASON_SEVERITIES)},
        "file": {"type": "string"},
        "line": {"type": "integer"},
        "claim": {"type": "string"},
    },
    "required": ["severity", "file", "line", "claim"],
    "additionalProperties": False,
}
ADJUDICATION_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(_VERDICTS)},
        "reviewed_head": {"type": "string"},
        "reasons": {"type": "array", "items": _REVIEW_REASON_SCHEMA},
        "blocking_count": {"type": "integer"},
    },
    "required": ["verdict", "reviewed_head", "reasons", "blocking_count"],
    "additionalProperties": False,
}


class AdjudicationError(RuntimeError):
    """The review judgment cannot safely authorize its next controller step."""


class AdjudicationInputError(ValueError):
    """Review input or policy metadata is malformed or incomplete."""


class ReviewedHeadMismatch(AdjudicationError):
    """The model judged a different head than the caller supplied."""


class AdjudicationValidationError(AdjudicationError):
    """The structured judgment is internally inconsistent."""


@dataclass(frozen=True)
class AdjudicationResult:
    """A model verdict plus Python-owned merge and second-review decisions."""

    verdict: Verdict
    reviewed_head: str
    reasons: tuple[JsonObject, ...]
    blocking_count: int
    second_review_required: bool
    mergeable: bool

    def to_mapping(self) -> JsonObject:
        """Return the closed output fields for state or replay consumers."""
        return {
            "verdict": self.verdict.value,
            "reviewed_head": self.reviewed_head,
            "reasons": [copy.deepcopy(reason) for reason in self.reasons],
            "blocking_count": self.blocking_count,
        }

    def __getitem__(self, key: str) -> JsonValue:
        """Allow callers to inspect the closed output like an RLM object."""
        return self.to_mapping()[key]


@dataclass(frozen=True)
class ReviewPolicy:
    """The review-policy gates enforced by Python adjudication."""

    min_review_rounds: int
    external_review_threshold: int
    external_review_paths: tuple[str, ...]
    require_second_reviewer_complexity: bool
    complexity_line_threshold: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ReviewPolicy:
        """Parse the task-owned policy keys without silent defaults."""
        return cls(
            min_review_rounds=_non_negative_int(value, "min_review_rounds"),
            external_review_threshold=_non_negative_int(
                value, "external_review_threshold"
            ),
            external_review_paths=_string_tuple(value, "external_review_paths"),
            require_second_reviewer_complexity=_boolean(
                value, "require_second_reviewer_complexity"
            ),
            complexity_line_threshold=_non_negative_int(
                value, "complexity_line_threshold"
            ),
        )


def _non_negative_int(value: Mapping[str, object], key: str) -> int:
    candidate = value.get(key)
    if type(candidate) is not int or candidate < 0:
        raise AdjudicationInputError(
            f"review policy {key} must be a non-negative integer"
        )
    return candidate


def _boolean(value: Mapping[str, object], key: str) -> bool:
    candidate = value.get(key)
    if type(candidate) is not bool:
        raise AdjudicationInputError(f"review policy {key} must be boolean")
    return candidate


def _string_tuple(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    candidate = value.get(key)
    if not isinstance(candidate, list) or any(
        not isinstance(item, str) or not item for item in candidate
    ):
        raise AdjudicationInputError(
            f"review policy {key} must be an array of strings"
        )
    return tuple(candidate)


__all__ = [
    "ADJUDICATION_SCHEMA",
    "AdjudicationError",
    "AdjudicationInputError",
    "AdjudicationResult",
    "AdjudicationValidationError",
    "ReviewPolicy",
    "ReviewedHeadMismatch",
]
