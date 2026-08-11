"""Bounded RLM decomposition into validated, owned implementation slices."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import cast

from tl_loop.client.transport import JsonObject, JsonValue
from tl_loop.state.schema import ParkCause

from .call import MAX_ATTEMPTS, JudgmentFailed, rlm
from .slice_spec import (
    SLICE_SPEC_SCHEMA,
    DecompositionValidationError,
    SliceSpec,
    validate_decomposition,
)

DECOMPOSE_PROMPT = """ANTI-PATTERNS (FIRST)
- Do not choose a harness, model, token budget, or parallelism; selection is owned by Python.
- Do not create overlapping path ownership or a cyclic depends_on graph.
- Do not invent a silent fallback, repair a malformed slice, or omit its test_plan.
- Do not add work outside the root specification.

READ FIRST
- Read the repository guidance and every path needed by the slice.
- Treat the root specification as the user-visible contract.

STEPS
- Return independent implementation slices with one owner per path.
- Give each slice a stable id, title, base_ref, paths, dependencies, steps,
  verification commands, boundary rules, and done criteria.
- Keep dependencies explicit and acyclic so ready slices can be scheduled.

VERIFY
- Every slice has a non-empty test_plan and concrete verify commands.
- Paths are repository-relative and pairwise disjoint.
- Every depends_on id exists and the dependency graph is acyclic.

DONE CRITERIA
- Return only the closed JSON schema requested by the caller.
- The result is a validated slice DAG ready for Python-owned selection and spawning.
"""


class DecompositionConfigurationError(ValueError):
    """The decompose boundary was not given an injected model choice."""


class DecompositionParked(RuntimeError):
    """Bounded decomposition retries ended without a safe slice DAG."""

    def __init__(self, attempts: int, violations: Sequence[str]) -> None:
        self.attempts = attempts
        self.violations = tuple(violations)
        self.cause = ParkCause.RETRIES_EXHAUSTED
        super().__init__(
            f"decomposition parked after {attempts} attempt(s): "
            + ("; ".join(self.violations) or "validation failed")
        )


def decompose(
    root_spec: Mapping[str, object],
    model_choice: object | None = None,
) -> list[SliceSpec]:
    """Produce a validated slice DAG through bounded structured RLM calls."""
    if not isinstance(root_spec, Mapping):
        raise TypeError("root_spec must be an object")
    if model_choice is None:
        raise DecompositionConfigurationError(
            "decompose requires an injected resolved model choice"
        )

    attempts = _max_attempts(model_choice)
    feedback: str | None = None
    violations: tuple[str, ...] = ()
    for attempt in range(1, attempts + 1):
        inputs = _decompose_inputs(root_spec, feedback)
        try:
            output = rlm("decompose", inputs, SLICE_SPEC_SCHEMA, model_choice)
        except JudgmentFailed as error:
            violations = (f"structured output failed: {error}",)
        else:
            try:
                return list(validate_decomposition(output))
            except DecompositionValidationError as error:
                violations = error.violations
        feedback = _feedback(violations, attempt + 1)
        if attempt == attempts:
            break

    raise DecompositionParked(attempts, violations)


def _decompose_inputs(
    root_spec: Mapping[str, object],
    feedback: str | None,
) -> JsonObject:
    sections: list[JsonObject] = [
        {
            "name": "instructions",
            "content": DECOMPOSE_PROMPT,
            "priority": 100,
            "required": True,
        },
        {
            "name": "root_spec",
            "content": cast(JsonValue, copy.deepcopy(dict(root_spec))),
            "priority": 90,
            "required": True,
        },
    ]
    if feedback is not None:
        sections.insert(
            0,
            {
                "name": "validation_feedback",
                "content": feedback,
                "priority": 110,
                "required": True,
            },
        )
    return {"sections": cast(JsonValue, sections)}


def _feedback(violations: Sequence[str], retry_attempt: int) -> str:
    return (
        f"The previous decomposition was rejected; this is retry {retry_attempt}. "
        "Correct these exact validation violations and return the complete "
        "closed schema again: "
        + "; ".join(violations)
    )


def _max_attempts(model_choice: object) -> int:
    value = _member(model_choice, "max_attempts")
    if value is None:
        return MAX_ATTEMPTS
    if type(value) is not int or not 1 <= value <= MAX_ATTEMPTS:
        raise DecompositionConfigurationError(
            "model choice max_attempts must be between one and three"
        )
    return value


def _member(value: object, key: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


__all__ = [
    "DECOMPOSE_PROMPT",
    "SLICE_SPEC_SCHEMA",
    "DecompositionConfigurationError",
    "DecompositionParked",
    "DecompositionValidationError",
    "SliceSpec",
    "decompose",
    "validate_decomposition",
]
