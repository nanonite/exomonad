"""Hermetic replay coverage for operator-intent judgments."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from tl_loop.rlm.call import judgment_hash
from tl_loop.rlm.intent import _intent_inputs, interpret_operator_intent
from tl_loop.rlm.intent_contract import OPERATOR_INTENT_SCHEMA, QueryIntent
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest, RlmResponse

FIXTURE = Path(__file__).parent / "fixtures" / "intent-replay.json"


@dataclass
class FailingBackend:
    requests: list[RlmRequest] = field(default_factory=list)

    def complete(self, request: RlmRequest) -> object:
        self.requests.append(request)
        raise AssertionError("replay hit must not invoke the backend")


@dataclass
class RecordingBackend:
    response: RlmResponse
    requests: list[RlmRequest] = field(default_factory=list)

    def complete(self, request: RlmRequest) -> object:
        self.requests.append(request)
        return self.response


def _fixture() -> dict[str, object]:
    return cast(dict[str, object], json.loads(FIXTURE.read_text(encoding="utf-8")))


def _inputs(fixture: dict[str, object]) -> tuple[str, dict[str, object]]:
    utterance = fixture["utterance"]
    read_model = fixture["read_model"]
    assert isinstance(utterance, str)
    assert isinstance(read_model, dict)
    return utterance, read_model


def _response(fixture: dict[str, object]) -> RlmResponse:
    output = fixture["output"]
    assert isinstance(output, dict)
    return RlmResponse(
        output,
        input_tokens=cast(int, fixture["input_tokens"]),
        output_tokens=cast(int, fixture["output_tokens"]),
        latency_ms=cast(int, fixture["latency_ms"]),
    )


def test_fixture_hash_matches_the_canonical_judgment_contract() -> None:
    fixture = _fixture()
    utterance, read_model = _inputs(fixture)
    model_id = cast(str, fixture["model_id"])

    actual = judgment_hash(
        cast(str, fixture["judgment_name"]),
        _intent_inputs(utterance, read_model),
        OPERATOR_INTENT_SCHEMA,
        model_id,
    )

    assert actual == fixture["input_hash"]


def test_replay_hit_skips_backend_and_preserves_usage() -> None:
    fixture = _fixture()
    utterance, read_model = _inputs(fixture)
    response = _response(fixture)
    replay_key = cast(str, fixture["input_hash"])
    backend = FailingBackend()
    choice = RlmModelChoice(
        model_id=cast(str, fixture["model_id"]),
        backend=backend,
        store=RlmCallStore(),
        replay={replay_key: response},
        context_length=10_000,
    )

    result = interpret_operator_intent(utterance, read_model, model_choice=choice)

    assert result == QueryIntent("root", "run")
    assert backend.requests == []
    assert choice.store.events[0]["replayed"] is True
    assert choice.store.events[0]["total_tokens"] == 26
    assert choice.store.ledger.spent == {"worker": 26}


def test_replay_miss_records_the_same_canonical_key() -> None:
    fixture = _fixture()
    utterance, read_model = _inputs(fixture)
    response = _response(fixture)
    backend = RecordingBackend(response)
    replay: dict[str, object] = {}
    choice = RlmModelChoice(
        model_id=cast(str, fixture["model_id"]),
        backend=backend,
        store=RlmCallStore(),
        replay=replay,
        context_length=10_000,
    )

    interpret_operator_intent(utterance, read_model, model_choice=choice)

    assert len(backend.requests) == 1
    assert replay[cast(str, fixture["input_hash"])] == response
