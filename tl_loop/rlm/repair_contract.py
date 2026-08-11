"""Closed contract for bounded PR repair handoffs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tl_loop.client.transport import JsonObject, JsonValue

REPAIR_HANDOFF_FIELDS = (
    "root_cause",
    "proposed_solution",
    "read_first",
    "steps",
    "verify",
    "boundary",
    "done_criteria",
)
_NON_EMPTY_STRING = {"type": "string", "minLength": 1}
_NON_EMPTY_LIST = {
    "type": "array",
    "minItems": 1,
    "items": _NON_EMPTY_STRING,
}
REPAIR_HANDOFF_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "root_cause": _NON_EMPTY_STRING,
        "proposed_solution": _NON_EMPTY_STRING,
        "read_first": _NON_EMPTY_LIST,
        "steps": _NON_EMPTY_LIST,
        "verify": _NON_EMPTY_LIST,
        "boundary": _NON_EMPTY_LIST,
        "done_criteria": _NON_EMPTY_LIST,
    },
    "required": list(REPAIR_HANDOFF_FIELDS),
    "additionalProperties": False,
}


class RepairError(RuntimeError):
    """Base class for repair composition and dispatch failures."""


class RepairInputError(ValueError):
    """The repair inputs do not identify a safe existing PR."""


class RepairPRStateError(RepairError):
    """The existing PR cannot safely receive a repair."""


class RepairBoundaryError(RepairError):
    """A generated handoff references a path outside its slice."""


class RepairHandoffRejected(RepairBoundaryError):
    """Bounded repair retries ended with no in-boundary handoff."""


class RepairDispatchError(RepairError):
    """The existing PR owner could not be resumed."""


@dataclass(frozen=True)
class RepairHandoff:
    """The exact seven-section payload carried into resume_pr."""

    root_cause: str
    proposed_solution: str
    read_first: tuple[str, ...]
    steps: tuple[str, ...]
    verify: tuple[str, ...]
    boundary: tuple[str, ...]
    done_criteria: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RepairHandoff:
        """Validate the closed handoff shape and its non-empty sections."""
        if set(value) != set(REPAIR_HANDOFF_FIELDS):
            raise RepairInputError(
                "repair handoff must contain exactly "
                + ", ".join(REPAIR_HANDOFF_FIELDS)
            )
        return cls(
            root_cause=_required_text(value, "root_cause"),
            proposed_solution=_required_text(value, "proposed_solution"),
            read_first=_required_text_list(value, "read_first"),
            steps=_required_text_list(value, "steps"),
            verify=_required_text_list(value, "verify"),
            boundary=_required_text_list(value, "boundary"),
            done_criteria=_required_text_list(value, "done_criteria"),
        )

    def to_mapping(self) -> JsonObject:
        """Return only the closed schema fields sent to the resume boundary."""
        return {
            "root_cause": self.root_cause,
            "proposed_solution": self.proposed_solution,
            "read_first": list(self.read_first),
            "steps": list(self.steps),
            "verify": list(self.verify),
            "boundary": list(self.boundary),
            "done_criteria": list(self.done_criteria),
        }

    def __getitem__(self, key: str) -> JsonValue:
        """Allow callers to inspect a handoff like its JSON representation."""
        return self.to_mapping()[key]


def _required_text(value: Mapping[str, object], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        raise RepairInputError(f"repair handoff {key} must be non-empty")
    return candidate.strip()


def _required_text_list(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    candidate = value.get(key)
    if not isinstance(candidate, list) or not candidate:
        raise RepairInputError(f"repair handoff {key} must be a non-empty array")
    if any(not isinstance(item, str) or not item.strip() for item in candidate):
        raise RepairInputError(f"repair handoff {key} must contain non-empty strings")
    return tuple(item.strip() for item in candidate)


__all__ = [
    "REPAIR_HANDOFF_FIELDS",
    "REPAIR_HANDOFF_SCHEMA",
    "RepairBoundaryError",
    "RepairDispatchError",
    "RepairError",
    "RepairHandoff",
    "RepairHandoffRejected",
    "RepairInputError",
    "RepairPRStateError",
]
