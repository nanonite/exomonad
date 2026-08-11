"""Deterministic context budgets and section compaction for RLM calls."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from tl_loop.client.transport import JsonObject, JsonValue

COMPACTION_BUDGET_NUMERATOR = 4
COMPACTION_BUDGET_DENOMINATOR = 5
SECTION_PRIORITY_ORDER = (
    "system",
    "instructions",
    "task",
    "question",
    "evidence",
    "context",
    "history",
    "metadata",
)


class ContextBudgetError(ValueError):
    """The model or compaction configuration cannot provide a safe budget."""


class ContextOverflow(ContextBudgetError):
    """The required RLM context cannot fit within the model budget."""

    def __init__(
        self,
        budget: int,
        required_sections: tuple[str, ...],
        token_count: int,
    ) -> None:
        self.budget = budget
        self.required_sections = required_sections
        self.token_count = token_count
        names = ", ".join(required_sections) or "<none>"
        super().__init__(
            f"required RLM sections ({names}) need {token_count} tokens, "
            f"but the context budget is {budget}"
        )


@dataclass(frozen=True)
class InputSection:
    """One independently droppable, ordered RLM input section.

    Section priority is explicit: larger values are retained before smaller
    values, and equal priorities retain their input order. Required sections
    are never dropped. The regular RLM input mapping is treated as one
    required inputs section; callers that need compaction must use the
    explicit sections envelope.
    """

    name: str
    content: JsonValue
    priority: int
    required: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("section name must be non-empty")
        if type(self.priority) is not int:
            raise ValueError("section priority must be an integer")
        if type(self.required) is not bool:
            raise ValueError("section required flag must be a boolean")
        if not _is_json_value(self.content):
            raise ValueError("section content must be canonical JSON")


@dataclass(frozen=True)
class CompactionResult:
    """The deterministic prompt and accounting recorded for one RLM call."""

    prompt: JsonObject
    context_budget: int
    final_token_count: int
    token_count_method: str
    dropped_sections: tuple[str, ...]


@dataclass(frozen=True)
class ApproximateTokenCounter:
    """Deterministic fallback when a provider count API is unavailable.

    The approximation serializes the canonical JSON prompt and charges one
    token per four UTF-8 characters, rounded up. It is intentionally
    conservative enough to reserve headroom and is recorded in every event.
    """

    method: str = "canonical_json_utf8_chars_div_4"

    def count(self, value: JsonObject) -> int:
        """Count canonical JSON without depending on a provider or locale."""
        serialized = _canonical_json(value)
        return max(1, (len(serialized) + 3) // 4)


@dataclass(frozen=True)
class _ProviderTokenCounter:
    count_function: Callable[[JsonObject], object]
    method: str

    def count(self, value: JsonObject) -> int:
        raw = self.count_function(value)
        if type(raw) is not int or raw < 0:
            raise ContextBudgetError(
                "provider token counter must return a non-negative integer"
            )
        return raw


def context_budget(model: object) -> int:
    """Return floor(model context length * 0.8) from the resolved model."""
    context_length = _member(model, "context_length")
    if type(context_length) is not int or context_length <= 0:
        raise ContextBudgetError(
            "resolved model must provide a positive integer context_length"
        )
    return (
        context_length * COMPACTION_BUDGET_NUMERATOR
    ) // COMPACTION_BUDGET_DENOMINATOR


def compact_inputs(
    inputs: Mapping[str, object],
    model: object,
    *,
    token_counter: object | None = None,
) -> CompactionResult:
    """Budget an RLM input mapping using the model's resolved context length."""
    sections = sections_from_inputs(inputs)
    return compact_sections(
        sections,
        context_budget(model),
        token_counter=(
            resolve_token_counter(model)
            if token_counter is None
            else token_counter
        ),
    )


def sections_from_inputs(inputs: Mapping[str, object]) -> tuple[InputSection, ...]:
    """Parse the explicit section envelope without silently dropping fields."""
    raw_sections = inputs.get("sections")
    if raw_sections is None:
        return (
            InputSection(
                name="inputs",
                content=cast(JsonValue, dict(inputs)),
                priority=0,
                required=True,
            ),
        )
    if set(inputs) != {"sections"} or not isinstance(raw_sections, list):
        raise ContextBudgetError(
            "sectioned RLM inputs must contain only a list-valued sections key"
        )

    sections: list[InputSection] = []
    names: set[str] = set()
    for position, raw_section in enumerate(raw_sections):
        if not isinstance(raw_section, Mapping):
            raise ContextBudgetError(f"section {position} must be an object")
        required = {"name", "content", "priority", "required"}
        if set(raw_section) != required:
            raise ContextBudgetError(
                f"section {position} must contain exactly "
                "name, content, priority, and required"
            )
        section = InputSection(
            name=_string_field(raw_section["name"], "name", position),
            content=cast(JsonValue, raw_section["content"]),
            priority=_integer_field(raw_section["priority"], "priority", position),
            required=_boolean_field(raw_section["required"], "required", position),
        )
        if section.name in names:
            raise ContextBudgetError(f"duplicate section name: {section.name!r}")
        names.add(section.name)
        sections.append(section)
    return tuple(sections)


def compact_sections(
    sections: Sequence[InputSection],
    budget: int,
    *,
    token_counter: object | None = None,
) -> CompactionResult:
    """Drop the lowest-priority optional sections until the prompt fits.

    All sections are rendered in descending priority order, with input order
    breaking ties. Optional sections are removed in ascending priority order,
    with input order breaking ties. Required sections are never removed; an
    over-budget required prompt raises ContextOverflow instead of truncating
    content.
    """
    if type(budget) is not int or budget <= 0:
        raise ContextBudgetError("context budget must be a positive integer")
    if not sections:
        raise ContextBudgetError("at least one input section is required")
    counter = _coerce_token_counter(
        ApproximateTokenCounter() if token_counter is None else token_counter
    )
    section_list = tuple(sections)
    retained = list(range(len(section_list)))
    prompt = _render(section_list, retained)
    token_count = counter.count(prompt)
    if token_count <= budget:
        return _result(prompt, budget, token_count, counter.method, ())

    required_indices = [
        index for index, section in enumerate(section_list) if section.required
    ]
    required_names = tuple(section_list[index].name for index in required_indices)
    required_prompt = _render(section_list, required_indices)
    required_count = counter.count(required_prompt)
    if required_count > budget:
        raise ContextOverflow(budget, required_names, required_count)

    dropped: list[str] = []
    removable = sorted(
        (
            index
            for index, section in enumerate(section_list)
            if not section.required
        ),
        key=lambda index: (section_list[index].priority, index),
    )
    for index in removable:
        retained.remove(index)
        dropped.append(section_list[index].name)
        prompt = _render(section_list, retained)
        token_count = counter.count(prompt)
        if token_count <= budget:
            return _result(
                prompt,
                budget,
                token_count,
                counter.method,
                tuple(dropped),
            )

    raise ContextOverflow(budget, required_names, required_count)


def resolve_token_counter(model: object) -> object:
    """Resolve a provider counter or the documented deterministic fallback."""
    candidate = _member(model, "token_counter")
    return ApproximateTokenCounter() if candidate is None else candidate


def _coerce_token_counter(candidate: object) -> ApproximateTokenCounter | _ProviderTokenCounter:
    if isinstance(candidate, ApproximateTokenCounter):
        return candidate
    count_function = getattr(candidate, "count", None)
    if not callable(count_function):
        count_function = getattr(candidate, "count_tokens", None)
    if not callable(count_function) and callable(candidate):
        count_function = candidate
    if not callable(count_function):
        raise ContextBudgetError(
            "token counter must provide count(prompt) or be callable"
        )
    method = getattr(candidate, "method", "provider")
    if not isinstance(method, str) or not method:
        raise ContextBudgetError("token counter method must be a non-empty string")
    return _ProviderTokenCounter(count_function, method)


def _render(sections: Sequence[InputSection], indices: Sequence[int]) -> JsonObject:
    ordered = sorted(
        indices,
        key=lambda index: (-sections[index].priority, index),
    )
    return {
        "sections": [
            {
                "name": sections[index].name,
                "content": copy.deepcopy(sections[index].content),
            }
            for index in ordered
        ]
    }


def _result(
    prompt: JsonObject,
    budget: int,
    token_count: int,
    method: str,
    dropped: tuple[str, ...],
) -> CompactionResult:
    return CompactionResult(
        prompt=prompt,
        context_budget=budget,
        final_token_count=token_count,
        token_count_method=method,
        dropped_sections=dropped,
    )


def _canonical_json(value: JsonObject) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ContextBudgetError(f"prompt is not canonical JSON: {error}") from error


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(child) for child in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(child)
            for key, child in value.items()
        )
    return False


def _member(value: object, key: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _string_field(value: object, name: str, position: int) -> str:
    if not isinstance(value, str) or not value:
        raise ContextBudgetError(
            f"section {position} {name} must be a non-empty string"
        )
    return value


def _integer_field(value: object, name: str, position: int) -> int:
    if type(value) is not int:
        raise ContextBudgetError(f"section {position} {name} must be an integer")
    return value


def _boolean_field(value: object, name: str, position: int) -> bool:
    if type(value) is not bool:
        raise ContextBudgetError(f"section {position} {name} must be a boolean")
    return value


__all__ = [
    "SECTION_PRIORITY_ORDER",
    "ApproximateTokenCounter",
    "CompactionResult",
    "ContextBudgetError",
    "ContextOverflow",
    "InputSection",
    "compact_inputs",
    "compact_sections",
    "context_budget",
    "resolve_token_counter",
    "sections_from_inputs",
]
