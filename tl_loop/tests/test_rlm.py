"""Hermetic coverage for bounded structured RLM judgments."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from tl_loop.rlm.call import JudgmentFailed, judgment_hash, rlm
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest, RlmResponse

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["go", "stop"]},
        "confidence": {"type": "integer"},
    },
    "required": ["decision"],
}
INPUTS = {"question": "is the evidence sufficient?"}


@dataclass
class FakeBackend:
    responses: list[object]
    requests: list[RlmRequest] = field(default_factory=list)

    def complete(self, request: RlmRequest) -> object:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("backend was called more times than expected")
        return self.responses.pop(0)


def _choice(backend: FakeBackend, *, replay: dict[str, object] | None = None) -> RlmModelChoice:
    return RlmModelChoice(
        model_id="claude-sonnet-4-6",
        backend=backend,
        store=RlmCallStore(),
        replay=replay if replay is not None else {},
    )


def test_schema_valid_response_is_returned_and_logged() -> None:
    backend = FakeBackend([RlmResponse({"decision": "go", "confidence": 3}, 4, 6, 12)])
    choice = _choice(backend)

    result = rlm("review-evidence", INPUTS, SCHEMA, choice)

    assert result == {"decision": "go", "confidence": 3}
    assert backend.requests[0].tools == ()
    assert choice.store.ledger.spent == {"worker": 10}
    assert choice.store.events[0]["result"] == result
    assert choice.store.events[0]["attempt"] == 1


def test_invalid_response_retries_with_validation_error() -> None:
    backend = FakeBackend(
        [
            RlmResponse({"decision": "go", "unexpected": True}, 1, 2, 4),
            RlmResponse({"decision": "stop"}, 2, 3, 5),
        ]
    )
    choice = _choice(backend)

    assert rlm("review-evidence", INPUTS, SCHEMA, choice) == {"decision": "stop"}

    assert len(backend.requests) == 2
    assert backend.requests[1].retry_error is not None
    assert "unknown key" in backend.requests[1].retry_error
    assert len(choice.store.events) == 2
    assert choice.store.ledger.spent == {"worker": 8}


def test_exhausted_retries_raise_judgment_failed() -> None:
    backend = FakeBackend(
        [RlmResponse({"unexpected": "value"}, 1, 1, 1) for _ in range(3)]
    )
    choice = _choice(backend)

    with pytest.raises(JudgmentFailed) as raised:
        rlm("review-evidence", INPUTS, SCHEMA, choice)

    assert raised.value.name == "review-evidence"
    assert len(raised.value.errors) == 3
    assert len(choice.store.events) == 3
    assert choice.store.ledger.spent == {"worker": 6}


def test_replay_hit_skips_backend_and_records_replayed_call() -> None:
    response = RlmResponse({"decision": "go"}, 5, 7, 9)
    key = judgment_hash("review-evidence", INPUTS, SCHEMA, "claude-sonnet-4-6")
    backend = FakeBackend([])
    choice = _choice(backend, replay={key: response})

    assert rlm("review-evidence", INPUTS, SCHEMA, choice) == {"decision": "go"}

    assert backend.requests == []
    assert choice.store.events[0]["replayed"] is True
    assert choice.store.ledger.spent == {"worker": 12}


def test_replay_miss_calls_backend_and_records_response() -> None:
    response = RlmResponse({"decision": "stop"}, 2, 3, 4)
    backend = FakeBackend([response])
    replay: dict[str, object] = {}
    choice = _choice(backend, replay=replay)

    assert rlm("review-evidence", INPUTS, SCHEMA, choice) == {"decision": "stop"}

    key = judgment_hash("review-evidence", INPUTS, SCHEMA, "claude-sonnet-4-6")
    assert replay[key] == response
    assert choice.store.events[0]["replayed"] is False
