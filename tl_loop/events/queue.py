"""Bounded blocking queue fed by a read-only ledger tailer."""

from __future__ import annotations

import logging
import queue as queue_module
import threading
from typing import TypeAlias

from .envelope import EventEnvelope
from .reader import LedgerFinding, LedgerReader, ReadResult, SequenceStatus

FindingKey: TypeAlias = tuple[str, str, str, int]
LOGGER = logging.getLogger(__name__)


def _exception_chain(error: BaseException) -> tuple[str, ...]:
    chain: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return tuple(chain)


class QueueError(RuntimeError):
    """The ledger tailer stopped because it encountered an unrecoverable error."""

    def __init__(
        self,
        *,
        cursor: int,
        sequence_status: SequenceStatus,
        cause: BaseException,
    ) -> None:
        self.cursor = cursor
        self.sequence_status = sequence_status
        self.cause = cause
        details = " -> ".join(_exception_chain(cause))
        super().__init__(
            "ledger tailer stopped "
            f"(cursor={cursor}, sequence_status={sequence_status.value}): {details}"
        )


class LedgerQueue:
    """A bounded, at-least-once queue for mapped ledger projections."""

    def __init__(
        self,
        reader: LedgerReader,
        *,
        maxsize: int = 128,
        poll_interval: float = 0.25,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("queue maxsize must be positive")
        if poll_interval <= 0:
            raise ValueError("queue poll interval must be positive")
        self.reader = reader
        self.poll_interval = poll_interval
        self._events: queue_module.Queue[EventEnvelope] = queue_module.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._findings: list[LedgerFinding] = []
        self._finding_keys: set[FindingKey] = set()
        self._lock = threading.Lock()
        self._sequence_status = SequenceStatus.UNKNOWN
        self._cursor = 0

    def start(self) -> LedgerQueue:
        """Start the single tailer thread."""
        if self._thread is not None:
            raise RuntimeError("ledger queue already started")
        self._thread = threading.Thread(target=self._run, name="tl-ledger-reader", daemon=True)
        self._thread.start()
        return self

    def close(self, timeout: float | None = None) -> None:
        """Stop the tailer and wait for its bounded put to finish."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def get(self, timeout: float | None = None) -> EventEnvelope:
        """Block until an event is available or the queue timeout expires."""
        try:
            return self._events.get(timeout=timeout)
        except queue_module.Empty:
            error = self._error
            if error is not None:
                raise QueueError(
                    cursor=self._cursor,
                    sequence_status=self.sequence_status,
                    cause=error,
                ) from error
            raise

    def task_done(self) -> None:
        """Mark the most recently consumed queue item complete."""
        self._events.task_done()

    def acknowledge(self, event: EventEnvelope | int) -> int:
        """Persist an event's run sequence after successful handling."""
        return self.reader.acknowledge(event)

    @property
    def findings(self) -> tuple[LedgerFinding, ...]:
        """Return deduplicated hard findings observed by the tailer."""
        with self._lock:
            return tuple(self._findings)

    @property
    def sequence_status(self) -> SequenceStatus:
        """Return the latest Rust-compatible status observed by the tailer."""
        with self._lock:
            return self._sequence_status

    def _run(self) -> None:
        try:
            cursor = self.reader.cursor()
            self._cursor = cursor
            while not self._stop.is_set():
                result = self.reader.read_from(cursor)
                self._record_result(result)
                for event in result.events:
                    if self._stop.is_set():
                        return
                    self._put_until_stopped(event)
                    cursor = event.run_seq if event.run_seq is not None else cursor
                    self._cursor = cursor
                self._stop.wait(self.poll_interval)
        except Exception as error:
            # Surface any ordinary tailer failure to the consumer thread.
            if not self._stop.is_set():
                self._error = error
                LOGGER.exception(
                    "ledger tailer stopped cursor=%d sequence_status=%s",
                    self._cursor,
                    self.sequence_status.value,
                )

    def _put_until_stopped(self, event: EventEnvelope) -> None:
        while not self._stop.is_set():
            try:
                self._events.put(event, timeout=self.poll_interval)
                return
            except queue_module.Full:
                continue

    def _record_result(self, result: ReadResult) -> None:
        with self._lock:
            self._sequence_status = result.sequence_status
            for finding in result.findings:
                key = (finding.kind.value, finding.event_type, str(finding.segment), finding.line_number)
                if key not in self._finding_keys:
                    self._finding_keys.add(key)
                    self._findings.append(finding)


EventQueue = LedgerQueue


__all__ = ["EventQueue", "LedgerQueue", "QueueError"]
