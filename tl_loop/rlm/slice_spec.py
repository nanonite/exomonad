"""Closed SliceSpec contract and cross-slice ownership validation."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase

from tl_loop.client.transport import JsonObject

from .schema import OutputSchemaError, validate_output

_STRING_ARRAY_SCHEMA: JsonObject = {
    "type": "array",
    "items": {"type": "string"},
}
_SLICE_SPEC_PROPERTIES: JsonObject = {
    "id": {"type": "string"},
    "title": {"type": "string"},
    "paths": copy.deepcopy(_STRING_ARRAY_SCHEMA),
    "depends_on": copy.deepcopy(_STRING_ARRAY_SCHEMA),
    "base_ref": {"type": "string"},
    "test_plan": copy.deepcopy(_STRING_ARRAY_SCHEMA),
    "steps": copy.deepcopy(_STRING_ARRAY_SCHEMA),
    "verify": copy.deepcopy(_STRING_ARRAY_SCHEMA),
    "boundary": copy.deepcopy(_STRING_ARRAY_SCHEMA),
    "done_criteria": copy.deepcopy(_STRING_ARRAY_SCHEMA),
}
SLICE_SPEC_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "slices": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _SLICE_SPEC_PROPERTIES,
                "required": list(_SLICE_SPEC_PROPERTIES),
                "additionalProperties": False,
            },
        }
    },
    "required": ["slices"],
    "additionalProperties": False,
}


class DecompositionValidationError(ValueError):
    """The model returned a schema-valid but unsafe decomposition."""

    def __init__(self, violations: Sequence[str]) -> None:
        self.violations = tuple(violations)
        super().__init__("; ".join(self.violations))


@dataclass(frozen=True)
class SliceSpec:
    """One closed, owned slice specification emitted by decompose."""

    id: str
    title: str
    paths: tuple[str, ...]
    depends_on: tuple[str, ...]
    base_ref: str
    test_plan: tuple[str, ...]
    steps: tuple[str, ...]
    verify: tuple[str, ...]
    boundary: tuple[str, ...]
    done_criteria: tuple[str, ...]

    def to_mapping(self) -> JsonObject:
        """Return the JSON shape consumed by state and spawn adapters."""
        return {
            "id": self.id,
            "title": self.title,
            "paths": list(self.paths),
            "depends_on": list(self.depends_on),
            "base_ref": self.base_ref,
            "test_plan": list(self.test_plan),
            "steps": list(self.steps),
            "verify": list(self.verify),
            "boundary": list(self.boundary),
            "done_criteria": list(self.done_criteria),
        }


def validate_decomposition(output: Mapping[str, object]) -> tuple[SliceSpec, ...]:
    """Validate schema output and all cross-slice ownership invariants."""
    try:
        validated = validate_output(output, SLICE_SPEC_SCHEMA)
    except OutputSchemaError as error:
        raise DecompositionValidationError((str(error),)) from error

    raw_slices = validated["slices"]
    if not isinstance(raw_slices, list):
        raise DecompositionValidationError(("output.slices must be an array",))

    slices = tuple(_slice_spec(raw, index) for index, raw in enumerate(raw_slices))
    violations = _cross_slice_violations(slices)
    if violations:
        raise DecompositionValidationError(violations)
    return slices


def _slice_spec(raw: object, index: int) -> SliceSpec:
    if not isinstance(raw, Mapping):
        raise DecompositionValidationError((f"output.slices[{index}] must be an object",))
    return SliceSpec(
        id=_text(raw, "id", index),
        title=_text(raw, "title", index),
        paths=_text_list(raw, "paths", index),
        depends_on=_text_list(raw, "depends_on", index, allow_empty=True),
        base_ref=_text(raw, "base_ref", index),
        test_plan=_text_list(raw, "test_plan", index),
        steps=_text_list(raw, "steps", index, allow_empty=True),
        verify=_text_list(raw, "verify", index, allow_empty=True),
        boundary=_text_list(raw, "boundary", index, allow_empty=True),
        done_criteria=_text_list(raw, "done_criteria", index),
    )


def _cross_slice_violations(slices: Sequence[SliceSpec]) -> tuple[str, ...]:
    violations: list[str] = []
    ids = [item.id for item in slices]
    seen: set[str] = set()
    for slice_spec in slices:
        if slice_spec.id in seen:
            violations.append(f"slice {slice_spec.id!r} has a duplicate id")
        seen.add(slice_spec.id)

    by_id = set(ids)
    graph: dict[str, tuple[str, ...]] = {}
    owned_paths: list[tuple[str, str]] = []
    for slice_spec in slices:
        graph[slice_spec.id] = slice_spec.depends_on
        for dependency in slice_spec.depends_on:
            if dependency not in by_id:
                violations.append(
                    f"slice {slice_spec.id!r} depends_on unknown id {dependency!r}"
                )
            if dependency == slice_spec.id:
                violations.append(
                    f"slice {slice_spec.id!r} depends_on itself"
                )
        for path in slice_spec.paths:
            if not _repository_relative(path):
                violations.append(
                    f"slice {slice_spec.id!r} path {path!r} is not repository-relative"
                )
            for other_id, other_path in owned_paths:
                if _paths_overlap(path, other_path):
                    violations.append(
                        f"slice {slice_spec.id!r} path {path!r} overlaps "
                        f"slice {other_id!r} path {other_path!r}"
                    )
            owned_paths.append((slice_spec.id, path))

    violations.extend(_cycle_violations(graph))
    return tuple(violations)


def _cycle_violations(graph: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    violations: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: tuple[str, ...]) -> None:
        if node in visiting:
            cycle = " -> ".join((*trail, node))
            violations.append(f"depends_on cycle: {cycle}")
            return
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph.get(node, ()):
            if dependency in graph:
                visit(dependency, (*trail, node))
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node, ())
    return tuple(violations)


def _paths_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    return (
        fnmatchcase(left, right)
        or fnmatchcase(right, left)
        or left.startswith(f"{right.rstrip('/')}/")
        or right.startswith(f"{left.rstrip('/')}/")
    )


def _repository_relative(path: str) -> bool:
    return bool(path) and not path.startswith("/") and ".." not in path.split("/")


def _text(value: Mapping[str, object], key: str, index: int) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        raise DecompositionValidationError(
            (f"output.slices[{index}].{key} must be a non-empty string",)
        )
    return candidate


def _text_list(
    value: Mapping[str, object],
    key: str,
    index: int,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    candidate = value.get(key)
    if not isinstance(candidate, list) or (not allow_empty and not candidate):
        raise DecompositionValidationError(
            (f"output.slices[{index}].{key} must be a non-empty string array",)
        )
    if any(not isinstance(item, str) or not item.strip() for item in candidate):
        raise DecompositionValidationError(
            (f"output.slices[{index}].{key} must contain non-empty strings",)
        )
    return tuple(candidate)


__all__ = [
    "SLICE_SPEC_SCHEMA",
    "DecompositionValidationError",
    "SliceSpec",
    "validate_decomposition",
]
