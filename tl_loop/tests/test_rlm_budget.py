"""Hermetic coverage for deterministic RLM context budgeting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from tl_loop.client.transport import JsonObject
from tl_loop.rlm.budget import (
    ContextBudgetError,
    ContextOverflow,
    InputSection,
    compact_sections,
    context_budget,
)
from tl_loop.rlm.call import rlm
from tl_loop.rlm.store import (
    RlmCallStore,
    RlmModelChoice,
    RlmRequest,
    RlmResponse,
)


@dataclass(frozen=True)
class LengthCounter:
    """Small deterministic counter used to make compaction choices explicit."""

    method: str = "test_content_length"

    def count(self, prompt: JsonObject) -> int:
        raw_sections = prompt["sections"]
        if not isinstance(raw_sections, list):
            raise TypeError("test prompt must contain a section list")
        sections = cast(list[JsonObject], raw_sections)
        return sum(len(cast(str, section["content"])) for section in sections)


def test_context_budget_uses_resolved_model_context_length() -> None:
    assert context_budget({"context_length": 101}) == 80

    with pytest.raises(ContextBudgetError):
        context_budget({"model_id": "missing-context-length"})


def test_compaction_is_deterministic_and_drops_lowest_priority_first() -> None:
    sections = (
        InputSection("system", "keep", priority=100, required=True),
        InputSection("high", "yy", priority=50),
        InputSection("low", "xxxxxxxxxx", priority=10),
    )
    first = compact_sections(sections, 8, token_counter=LengthCounter())
    second = compact_sections(sections, 8, token_counter=LengthCounter())

    assert first == second
    assert first.dropped_sections == ("low",)
    prompt_sections = cast(list[JsonObject], first.prompt["sections"])
    assert [cast(str, item["name"]) for item in prompt_sections] == [
        "system",
        "high",
    ]
    assert first.final_token_count == 6
    assert first.token_count_method == "test_content_length"


def test_required_context_overflow_is_not_truncated() -> None:
    sections = (
        InputSection("system", "essential", priority=100, required=True),
        InputSection("optional", "drop me", priority=1),
    )

    with pytest.raises(ContextOverflow) as raised:
        compact_sections(sections, 5, token_counter=LengthCounter())

    assert raised.value.required_sections == ("system",)
    assert raised.value.token_count == len("essential")


def test_rlm_records_compaction_accounting_in_each_event() -> None:
    class Backend:
        def __init__(self) -> None:
            self.requests: list[object] = []

        def complete(self, request: object) -> object:
            self.requests.append(request)
            return RlmResponse({"decision": "go"})

    backend = Backend()
    choice = RlmModelChoice(
        model_id="test-model",
        backend=backend,
        store=RlmCallStore(),
        context_length=10,
        token_counter=LengthCounter(),
    )
    inputs = {
        "sections": [
            {
                "name": "system",
                "content": "keep",
                "priority": 100,
                "required": True,
            },
            {
                "name": "high",
                "content": "yy",
                "priority": 50,
                "required": False,
            },
            {
                "name": "low",
                "content": "xxxxxxxxxx",
                "priority": 10,
                "required": False,
            },
        ]
    }

    assert rlm(
        "budgeted-review",
        inputs,
        {"type": "object", "properties": {"decision": {"type": "string"}}},
        choice,
    ) == {"decision": "go"}

    request = cast(RlmRequest, backend.requests[0])
    assert request.dropped_sections == ("low",)
    assert request.token_count == 6
    assert request.token_count_method == "test_content_length"
    assert choice.store.events[0]["context_budget"] == 8
    assert choice.store.events[0]["final_token_count"] == 6
    assert choice.store.events[0]["dropped_sections"] == ["low"]
