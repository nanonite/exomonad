"""Atomic in-memory state for bounded RLM calls."""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Protocol

from tl_loop.client.transport import JsonObject

LOGGER = logging.getLogger(__name__)
JudgmentEmitter = Callable[[JsonObject], object]


@dataclass(frozen=True)
class RlmResponse:
    """One provider response with authoritative usage metadata when available."""

    output: object
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("latency_ms", self.latency_ms),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @property
    def total_tokens(self) -> int:
        """Return input plus output tokens for budget charging."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class RlmRequest:
    """Stateless, tool-free request passed to one model invocation."""

    name: str
    model_id: str
    inputs: JsonObject
    output_schema: Mapping[str, object]
    attempt: int
    retry_error: str | None = None
    context_budget: int = 0
    token_count: int = 0
    token_count_method: str = ""
    dropped_sections: tuple[str, ...] = ()
    tools: tuple[object, ...] = ()


class RlmBackend(Protocol):
    """The only capability an RLM call may use to reach a model."""

    def complete(self, request: RlmRequest) -> object:
        """Return one response without tool or filesystem capabilities."""


@dataclass
class RlmRoleLedger:
    """Monotonic token spend for RLM judgments by controller role."""

    spent: dict[str, int] = field(default_factory=dict)
    budgets: Mapping[str, int] = field(default_factory=dict)

    def charge(self, role: str, tokens: int) -> None:
        """Charge one completed attempt, enforcing an optional role ceiling."""
        if not role:
            raise ValueError("role must be non-empty")
        if type(tokens) is not int or tokens < 0:
            raise ValueError("tokens must be a non-negative integer")
        current = self.spent.get(role, 0)
        ceiling = self.budgets.get(role)
        if ceiling is not None and current + tokens > ceiling:
            raise BudgetExceeded(
                f"role {role!r} budget exceeded: {current + tokens} > {ceiling}"
            )
        self.spent[role] = current + tokens


class BudgetExceeded(ValueError):
    """An RLM attempt cannot be charged within its role budget."""


@dataclass
class RlmCallStore:
    """Atomic call store coupling a role charge to its event record."""

    ledger: RlmRoleLedger = field(default_factory=RlmRoleLedger)
    events: list[JsonObject] = field(default_factory=list)
    judgment_emitter: JudgmentEmitter | None = None
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def commit(self, role: str, tokens: int, event: JsonObject) -> None:
        """Commit charge and event together, rolling back if either write fails."""
        with self._lock:
            before_spent = dict(self.ledger.spent)
            before_events = len(self.events)
            try:
                self.ledger.charge(role, tokens)
                self.events.append(copy.deepcopy(event))
            except BaseException:
                self.ledger.spent.clear()
                self.ledger.spent.update(before_spent)
                del self.events[before_events:]
                raise

    def emit_judgment(self, payload: JsonObject) -> None:
        """Best-effort delivery of an aggregate judgment outside the local store."""
        if self.judgment_emitter is None:
            return
        try:
            self.judgment_emitter(copy.deepcopy(payload))
        except Exception as error:  # noqa: BLE001 - aggregate emission is fail-open
            LOGGER.warning("RLM judgment emission failed: %s", error)


@dataclass
class RlmModelChoice:
    """Provider-independent model choice plus explicit RLM runtime capabilities."""

    model_id: str
    backend: object
    role: str = "worker"
    store: RlmCallStore = field(default_factory=RlmCallStore)
    replay: MutableMapping[str, object] = field(default_factory=dict)
    max_attempts: int = 3
    context_length: int | None = None
    token_counter: object | None = None

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ValueError("model_id must be non-empty")
        if not self.role:
            raise ValueError("role must be non-empty")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        if self.context_length is not None and (
            type(self.context_length) is not int or self.context_length <= 0
        ):
            raise ValueError("context_length must be a positive integer")


def normalize_response(raw: object) -> RlmResponse:
    """Normalize a provider response or a bare structured response."""
    if isinstance(raw, RlmResponse):
        return raw
    if isinstance(raw, Mapping) and "output" in raw:
        usage = raw.get("usage")
        input_tokens = raw.get("input_tokens", 0)
        output_tokens = raw.get("output_tokens", 0)
        latency_ms = raw.get("latency_ms", 0)
        if isinstance(usage, Mapping):
            input_tokens = usage.get("input_tokens", input_tokens)
            output_tokens = usage.get("output_tokens", output_tokens)
            latency_ms = usage.get("latency_ms", latency_ms)
        return RlmResponse(
            raw["output"],
            _counter(input_tokens, "input_tokens"),
            _counter(output_tokens, "output_tokens"),
            _counter(latency_ms, "latency_ms"),
        )
    return RlmResponse(raw)


def _counter(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


__all__ = [
    "BudgetExceeded",
    "RlmBackend",
    "RlmCallStore",
    "RlmModelChoice",
    "RlmRequest",
    "RlmResponse",
    "RlmRoleLedger",
    "normalize_response",
]
