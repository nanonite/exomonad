"""Observational bridge from the server ledger queue into TL handling."""

from __future__ import annotations

import logging
from typing import Mapping, TypeAlias

from .envelope import EventEnvelope, project
from .queue import LedgerQueue
from .reader import LedgerReader

BridgeInput: TypeAlias = Mapping[str, object]
LOGGER = logging.getLogger(__name__)


class BridgeError(RuntimeError):
    """The server event could not be bridged without losing fidelity."""


def bridge_event(event: BridgeInput, *, logger: logging.Logger | None = None) -> EventEnvelope:
    """Project one server ledger row, logging before, after, and on error."""
    active_logger = logger or LOGGER
    event_type = _event_type_for_log(event)
    active_logger.info("bridge before event_type=%s", event_type)
    try:
        envelope = project(event)
    except Exception as error:
        active_logger.exception("bridge error event_type=%s error=%s", event_type, error)
        raise BridgeError(f"could not bridge server event {event_type!r}: {error}") from error
    _log_after(active_logger, envelope)
    return envelope


class EventBridge:
    """Consume the existing ledger queue without adding another event path."""

    def __init__(
        self,
        reader: LedgerReader | None = None,
        *,
        queue: LedgerQueue | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if reader is None and queue is None:
            raise ValueError("event bridge requires a ledger reader or queue")
        if reader is not None and queue is not None and queue.reader is not reader:
            raise ValueError("event bridge reader and queue must refer to the same reader")
        if queue is None:
            if reader is None:
                raise ValueError("event bridge requires a ledger reader or queue")
            queue = LedgerQueue(reader)
        self.queue = queue
        self.logger = logger or LOGGER

    def start(self) -> EventBridge:
        """Start tailing the existing ledger segments."""
        self.queue.start()
        return self

    def close(self, timeout: float | None = None) -> None:
        """Stop the in-process tailer."""
        self.queue.close(timeout)

    def get(self, timeout: float | None = None) -> EventEnvelope:
        """Read one bridged event with lifecycle logging around the blocking get."""
        self.logger.info("bridge before queue_get timeout=%s", timeout)
        try:
            envelope = self.queue.get(timeout=timeout)
        except Exception as error:
            self.logger.exception("bridge error queue_get error=%s", error)
            raise
        _log_after(self.logger, envelope)
        return envelope

    def acknowledge(self, event: EventEnvelope | int) -> int:
        """Persist a handled global sequence through the queue's reader."""
        self.logger.info("bridge before acknowledge run_seq=%s", _run_seq(event))
        try:
            cursor = self.queue.acknowledge(event)
        except Exception as error:
            self.logger.exception("bridge error acknowledge error=%s", error)
            raise
        self.logger.info("bridge after acknowledge run_seq=%s", cursor)
        return cursor


def _log_after(logger: logging.Logger, envelope: EventEnvelope) -> None:
    logger.info(
        "bridge after event_type=%s kind=%s run_seq=%s agent_id=%s slice_id=%s pr_number=%s head_sha=%s",
        envelope.event_type,
        envelope.kind.value,
        envelope.run_seq,
        envelope.agent_id,
        envelope.slice_id,
        envelope.pr_number,
        envelope.reviewed_head,
    )


def _event_type_for_log(event: BridgeInput) -> str:
    value = event.get("type", event.get("event_type"))
    return value if isinstance(value, str) else "<invalid>"


def _run_seq(event: EventEnvelope | int) -> int | None:
    return event if isinstance(event, int) else event.run_seq


__all__ = ["BridgeError", "BridgeInput", "EventBridge", "bridge_event"]
