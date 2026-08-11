"""Hermetic coverage for validated RLM slice decomposition."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import pytest

from tl_loop.rlm.decompose import (
    DecompositionParked,
    SliceSpec,
    decompose,
    validate_decomposition,
)
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest
from tl_loop.state.schema import ParkCause


@dataclass
class FakeBackend:
    responses: list[object]
    requests: list[RlmRequest] = field(default_factory=list)

    def complete(self, request: RlmRequest) -> object:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("backend was called more times than expected")
        return self.responses.pop(0)


def _choice(backend: FakeBackend) -> RlmModelChoice:
    return RlmModelChoice(
        model_id="test-model",
        backend=backend,
        store=RlmCallStore(),
        context_length=10_000,
    )


def _slice(
    slice_id: str,
    path: str,
    *,
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": slice_id,
        "title": f"Implement {slice_id}",
        "paths": [path],
        "depends_on": depends_on or [],
        "base_ref": "main",
        "test_plan": ["just tl-loop-test"],
        "steps": [f"Implement {slice_id}"],
        "verify": ["just tl-loop-test"],
        "boundary": ["Do not edit unrelated paths"],
        "done_criteria": [f"{slice_id} is complete"],
    }


def _output(*slices: dict[str, object]) -> dict[str, object]:
    return {"slices": list(slices)}


def test_valid_decomposition_returns_typed_owned_slices() -> None:
    backend = FakeBackend(
        [_output(_slice("api", "src/api.py"), _slice("tests", "tests/api.py"))]
    )

    result = decompose({"task": "split the API work"}, _choice(backend))

    assert result == [
        SliceSpec(
            id="api",
            title="Implement api",
            paths=("src/api.py",),
            depends_on=(),
            base_ref="main",
            test_plan=("just tl-loop-test",),
            steps=("Implement api",),
            verify=("just tl-loop-test",),
            boundary=("Do not edit unrelated paths",),
            done_criteria=("api is complete",),
        ),
        SliceSpec(
            id="tests",
            title="Implement tests",
            paths=("tests/api.py",),
            depends_on=(),
            base_ref="main",
            test_plan=("just tl-loop-test",),
            steps=("Implement tests",),
            verify=("just tl-loop-test",),
            boundary=("Do not edit unrelated paths",),
            done_criteria=("tests is complete",),
        ),
    ]
    assert backend.requests[0].tools == ()
    assert backend.requests[0].dropped_sections == ()


def test_overlapping_paths_are_rejected_and_specific_feedback_retries() -> None:
    backend = FakeBackend(
        [
            _output(_slice("api", "src/shared.py"), _slice("tests", "src/shared.py")),
            _output(_slice("api", "src/api.py"), _slice("tests", "tests/api.py")),
        ]
    )

    result = decompose({"task": "split the API work"}, _choice(backend))

    assert [item.id for item in result] == ["api", "tests"]
    assert len(backend.requests) == 2
    retry_sections = cast(list[dict[str, object]], backend.requests[1].inputs["sections"])
    feedback = next(section for section in retry_sections if section["name"] == "validation_feedback")
    feedback_content = feedback["content"]
    assert isinstance(feedback_content, str)
    assert "overlaps" in feedback_content
    assert len(retry_sections) == 3


def test_cyclic_dependencies_park_after_the_bounded_retry_limit() -> None:
    backend = FakeBackend(
        [
            _output(
                _slice("api", "src/api.py", depends_on=["tests"]),
                _slice("tests", "tests/api.py", depends_on=["api"]),
            )
            for _ in range(3)
        ]
    )

    with pytest.raises(DecompositionParked) as raised:
        decompose({"task": "split the API work"}, _choice(backend))

    assert raised.value.cause is ParkCause.RETRIES_EXHAUSTED
    assert raised.value.attempts == 3
    assert any("depends_on cycle" in violation for violation in raised.value.violations)
    assert len(backend.requests) == 3


def test_schema_valid_decomposition_has_all_closed_fields() -> None:
    output = _output(_slice("api", "src/api.py"))

    result = validate_decomposition(output)

    assert result[0].to_mapping()["done_criteria"] == ["api is complete"]
