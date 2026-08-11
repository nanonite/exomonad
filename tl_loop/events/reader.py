"""Read-only replay of the immutable ledger by global ``run_seq``."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from itertools import pairwise
from pathlib import Path
from typing import TypeAlias, cast

from tl_loop.state.store import DEFAULT_ROOT
from tl_loop.state.write import apply

from .envelope import MAPPED_EVENT_TYPES, EventEnvelope, UnmappedEventType, project

DEFAULT_LEDGER_ROOT = Path(".exo/ledger/segments")
LedgerDocument: TypeAlias = dict[str, object]


class LedgerReadError(RuntimeError):
    """A ledger segment or row could not be read without guessing."""


class SequenceStatus(str, Enum):
    """The same three sequence states returned by Rust ``sequence_status``."""

    UNKNOWN = "unknown"
    PARTIAL = "partial"
    COMPLETE = "complete"


class FindingKind(str, Enum):
    """Non-durable reader findings surfaced to the controller."""

    MISSING_RUN_SEQ = "missing_run_seq"


@dataclass(frozen=True)
class LedgerFinding:
    """A read-time finding that must not be hidden by replay."""

    kind: FindingKind
    event_type: str
    segment: Path
    line_number: int
    message: str
    hard: bool = True


@dataclass(frozen=True)
class LedgerRow:
    """One parsed JSONL row with immutable-segment provenance."""

    segment: Path
    line_number: int
    document: LedgerDocument


@dataclass(frozen=True)
class ReadResult:
    """Projected replay rows plus sequence status and read-time findings."""

    events: tuple[EventEnvelope, ...]
    sequence_status: SequenceStatus
    findings: tuple[LedgerFinding, ...]

    def __iter__(self) -> Iterator[EventEnvelope]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)


class LedgerReader:
    """Replay mapped TL events from immutable, lexically ordered segments."""

    def __init__(
        self,
        segments_dir: str | Path = DEFAULT_LEDGER_ROOT,
        *,
        run_dir: str | Path | None = None,
        run_id: str | None = None,
        state_root: str | Path = DEFAULT_ROOT,
    ) -> None:
        if run_dir is not None and run_id is not None:
            raise ValueError("reader accepts either run_dir or run_id, not both")
        self.segments_dir = Path(segments_dir)
        self.run_dir: Path | None
        if run_dir is not None:
            self.run_dir = _run_directory(run_dir)
        elif run_id is not None:
            self.run_dir = Path(state_root) / run_id
        else:
            self.run_dir = None

    def cursor(self) -> int:
        """Return the persisted global cursor, or zero for a new local reader."""
        if self.run_dir is None:
            return 0
        from tl_loop.state.store import load

        return load(self.run_dir).events.last_consumed_offset

    def read_from(self, cursor: int | None = None) -> ReadResult:
        """Replay events with ``run_seq`` greater than the supplied cursor."""
        effective_cursor = self.cursor() if cursor is None else cursor
        if type(effective_cursor) is not int or effective_cursor < 0:
            raise ValueError("replay cursor must be a non-negative integer")
        rows = _resolve_superseded(self._read_rows())
        status = sequence_status(_run_sequences(row.document for row in rows))
        findings: list[LedgerFinding] = []
        projected: list[EventEnvelope] = []
        for row in rows:
            event_type = _raw_event_type(row.document)
            if event_type not in MAPPED_EVENT_TYPES:
                continue
            envelope = _project_row(row)
            if envelope.run_seq is None:
                findings.append(
                    LedgerFinding(
                        kind=FindingKind.MISSING_RUN_SEQ,
                        event_type=envelope.event_type,
                        segment=row.segment,
                        line_number=row.line_number,
                        message=(
                            f"required ledger event {envelope.event_type!r} has no run_seq; "
                            "server-side emit gap"
                        ),
                    )
                )
                continue
            if envelope.run_seq > effective_cursor:
                projected.append(envelope)
        projected.sort(key=lambda event: cast(int, event.run_seq))
        return ReadResult(tuple(projected), status, tuple(findings))

    def acknowledge(self, event_or_run_seq: EventEnvelope | int) -> int:
        """Persist a consumed global sequence through the M2.2 writer."""
        if self.run_dir is None:
            raise ValueError("acknowledgement requires a run-state directory")
        run_seq = event_or_run_seq.run_seq if isinstance(event_or_run_seq, EventEnvelope) else event_or_run_seq
        if type(run_seq) is not int or run_seq < 0:
            raise ValueError("acknowledged run_seq must be a non-negative integer")

        def advance(document: dict[str, object]) -> dict[str, object]:
            events = cast(dict[str, object], document["events"])
            current = events["last_consumed_offset"]
            if type(current) is not int:
                raise LedgerReadError("run-state event cursor is not an integer")
            events["last_consumed_offset"] = max(current, run_seq)
            return document

        document = apply(self.run_dir, advance)
        events = cast(dict[str, object], document["events"])
        return cast(int, events["last_consumed_offset"])

    def _read_rows(self) -> list[LedgerRow]:
        rows: list[LedgerRow] = []
        for segment in self._segment_paths():
            try:
                with segment.open("r", encoding="utf-8") as stream:
                    for line_number, line in enumerate(stream, start=1):
                        if not line.strip():
                            continue
                        try:
                            value = json.loads(line)
                        except json.JSONDecodeError as error:
                            raise LedgerReadError(
                                f"could not parse {segment} line {line_number}: {error}"
                            ) from error
                        if not isinstance(value, dict):
                            raise LedgerReadError(
                                f"{segment} line {line_number}: ledger row must be an object"
                            )
                        rows.append(LedgerRow(segment, line_number, value))
            except FileNotFoundError:
                continue
            except OSError as error:
                raise LedgerReadError(f"could not read ledger segment {segment}: {error}") from error
        return rows

    def _segment_paths(self) -> list[Path]:
        try:
            return sorted(
                path
                for path in self.segments_dir.iterdir()
                if path.is_file() and path.suffix == ".jsonl"
            )
        except FileNotFoundError:
            return []
        except OSError as error:
            raise LedgerReadError(f"could not enumerate ledger segments: {error}") from error


def sequence_status(run_sequences: Iterable[int | None]) -> SequenceStatus:
    """Match Rust ``sequence_status``: ignore nulls, then require contiguity."""
    sequences = sorted(sequence for sequence in run_sequences if sequence is not None)
    if not sequences:
        return SequenceStatus.UNKNOWN
    if all(right == left + 1 for left, right in pairwise(sequences)):
        return SequenceStatus.COMPLETE
    return SequenceStatus.PARTIAL


def _run_sequences(documents: Iterable[LedgerDocument]) -> Iterable[int | None]:
    for document in documents:
        value = document.get("run_seq")
        if value is None:
            yield None
        elif type(value) is int and value >= 0:
            yield value


def _run_directory(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate.parent if candidate.name == "run.json" else candidate


def _raw_event_type(document: LedgerDocument) -> str:
    value = document.get("type", document.get("event_type"))
    return value if isinstance(value, str) else ""


def _project_row(row: LedgerRow) -> EventEnvelope:
    try:
        return project(row.document)
    except UnmappedEventType:
        raise
    except ValueError as error:
        raise LedgerReadError(f"could not project {row.segment} line {row.line_number}: {error}") from error


def _resolve_superseded(rows: list[LedgerRow]) -> list[LedgerRow]:
    superseded: set[str] = set()
    for row in rows:
        if _raw_event_type(row.document) != "event.superseded":
            continue
        data = row.document.get("data")
        if not isinstance(data, Mapping):
            continue
        target = data.get("superseded_event_id")
        if target is None:
            target = data.get("old_event_id")
        if isinstance(target, str):
            superseded.add(target)
    return [row for row in rows if _event_id(row.document) not in superseded]


def _event_id(document: LedgerDocument) -> str | None:
    value = document.get("event_id")
    return value if isinstance(value, str) else None


__all__ = [
    "DEFAULT_LEDGER_ROOT",
    "FindingKind",
    "LedgerFinding",
    "LedgerReadError",
    "LedgerReader",
    "LedgerRow",
    "ReadResult",
    "SequenceStatus",
    "sequence_status",
]
