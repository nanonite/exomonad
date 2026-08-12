"""Tool-free operator-intent RLM boundary coverage."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from tl_loop.rlm.call import RlmResponse
from tl_loop.rlm.intent import IntentInputError, interpret_operator_intent
from tl_loop.rlm.intent_contract import (
    GateAnswerIntent,
    IntentValidationError,
    QueryIntent,
    UnclearIntent,
)
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest


@dataclass
class FakeBackend:
    responses: list[object]
    requests: list[RlmRequest] = field(default_factory=list)

    def complete(self, request: RlmRequest) -> object:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("backend was called more times than expected")
        return self.responses.pop(0)


def _choice(backend: FakeBackend, *, context_length: int = 10_000) -> RlmModelChoice:
    return RlmModelChoice(
        model_id="test-model",
        backend=backend,
        store=RlmCallStore(),
        context_length=context_length,
    )


def test_interpretation_uses_injected_backend_and_no_tools() -> None:
    backend = FakeBackend([RlmResponse({"kind": "query", "run_id": "root", "view": "run"})])
    choice = _choice(backend)

    result = interpret_operator_intent(
        "What is this run doing?",
        {"run_id": "root", "phase": "tl_waiting", "body": "untrusted"},
        model_choice=choice,
    )

    assert result == QueryIntent("root", "run")
    request = backend.requests[0]
    assert request.name == "interpret_operator_intent"
    assert request.tools == ()
    assert request.context_budget == 8_000
    sections = request.inputs["sections"]
    assert isinstance(sections, list)
    assert [section["name"] for section in sections] == [
        "utterance",
        "instructions",
        "read_model",
    ]
    assert choice.store.events[0]["name"] == "interpret_operator_intent"


def test_gate_and_unclear_intents_are_returned_as_typed_results() -> None:
    backend = FakeBackend(
        [
            RlmResponse(
                {
                    "kind": "gate_answer",
                    "run_id": "root",
                    "gate_name": "tl-timeout",
                    "decision": "reject",
                }
            ),
            RlmResponse(
                {
                    "kind": "unclear",
                    "reason": "two runs match",
                    "question": "Which run should I inspect?",
                }
            ),
        ]
    )
    choice = _choice(backend)

    gate = interpret_operator_intent("Reject the timeout gate", {}, model_choice=choice)
    unclear = interpret_operator_intent("Handle it", {}, model_choice=choice)

    assert gate == GateAnswerIntent("root", "tl-timeout", "reject")
    assert unclear == UnclearIntent("two runs match", "Which run should I inspect?")
    assert all(request.tools == () for request in backend.requests)


def test_missing_capability_or_input_is_rejected_before_model_call() -> None:
    backend = FakeBackend([])

    with pytest.raises(IntentInputError, match="utterance"):
        interpret_operator_intent(" ", {}, model_choice=_choice(backend))
    with pytest.raises(IntentInputError, match="model choice"):
        interpret_operator_intent("status", {})
    with pytest.raises(IntentInputError, match="read_model"):
        interpret_operator_intent("status", [], model_choice=_choice(backend))  # type: ignore[arg-type]

    assert backend.requests == []


def test_semantically_invalid_model_output_is_not_silently_authorized() -> None:
    backend = FakeBackend(
        [
            RlmResponse(
                {
                    "kind": "query",
                    "run_id": "root",
                    "view": "slice",
                }
            )
        ]
    )

    with pytest.raises(IntentValidationError, match="require slice_id"):
        interpret_operator_intent("inspect the slice", {}, model_choice=_choice(backend))
