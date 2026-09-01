"""Deterministic event source for replaying an immutable ledger prefix."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .envelope import EventEnvelope


class ReplayError(RuntimeError):
    """A replay stream cannot provide the evidence required by the reducer."""


class ReplayTruncated(ReplayError):
    """The recorded stream ended before the reducer reached a terminal state."""

    def __init__(self, cursor: int, *, consumed: int) -> None:
        self.cursor = cursor
        self.consumed = consumed
        super().__init__(
            f"recorded replay stream ended before TL completion "
            f"(cursor={cursor}, consumed={consumed})"
        )


def order_events(events: Iterable[EventEnvelope]) -> list[EventEnvelope]:
    """Return ledger order while retaining duplicate rows for reducer replay.

    Ledger rows are ordered by their immutable global sequence.  Equal
    sequences are retained, rather than collapsed here: the canonical reducer
    must observe duplicate delivery and prove that it is a no-op.  Stable
    sorting preserves the source order for equal-sequence rows.
    """
    ordered = list(events)
    for event in ordered:
        if type(event.run_seq) is not int or event.run_seq < 0:
            raise ValueError("replay events require a non-negative run_seq")
    return sorted(ordered, key=lambda event: event.run_seq)


@dataclass
class ReplayEventSource:
    """Queue-compatible replay source with an explicit durable cursor."""

    events: list[EventEnvelope]
    start_cursor: int = 0
    acknowledged: list[int] = field(default_factory=list)
    _cursor: int = field(init=False, repr=False)
    _delivered: int = field(init=False, default=0, repr=False)

    def __post_init__(self) -> None:
        if type(self.start_cursor) is not int or self.start_cursor < 0:
            raise ValueError("replay start_cursor must be a non-negative integer")
        self.events = [
            event for event in order_events(self.events) if event.run_seq > self.start_cursor
        ]
        self._cursor = self.start_cursor

    @property
    def cursor(self) -> int:
        """Return the greatest acknowledged sequence."""
        return self._cursor

    @property
    def delivered(self) -> int:
        """Return the number of rows handed to the reducer."""
        return self._delivered

    def get(self, timeout: float | None = None) -> EventEnvelope:
        """Return the next row or fail closed when the prefix is truncated."""
        del timeout
        if not self.events:
            raise ReplayTruncated(self._cursor, consumed=self._delivered)
        self._delivered += 1
        return self.events.pop(0)

    def acknowledge(self, event: EventEnvelope) -> int:
        """Advance the cursor monotonically after reducer success."""
        if type(event.run_seq) is not int or event.run_seq < 0:
            raise ValueError("acknowledged replay events require a non-negative run_seq")
        self._cursor = max(self._cursor, event.run_seq)
        self.acknowledged.append(event.run_seq)
        return self._cursor


__all__ = ["ReplayError", "ReplayEventSource", "ReplayTruncated", "order_events"]
