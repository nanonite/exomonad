"""Extraction of actual TL tool calls from canonical server ledger rows."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ActualReadError(RuntimeError):
    """An actual tool-call source could not be read without guessing."""


@dataclass(frozen=True)
class ActualAction:
    """One actual server-observed tool call."""

    kind: str
    target: str
    arguments: Mapping[str, object]
    event_seq: int
    rationale: str
    agent_id: str | None = None


class ActualActionReader:
    """Read ``tool.called`` rows from immutable L1 ledger segments."""

    def __init__(self, segments_dir: str | Path = Path(".exo/ledger/segments")) -> None:
        self.segments_dir = Path(segments_dir)

    def read(self, run_id: str) -> tuple[ActualAction, ...]:
        """Return all actual tool calls for ``run_id`` in sequence order."""
        rows: list[ActualAction] = []
        for segment in self._segments():
            try:
                with segment.open(encoding="utf-8") as stream:
                    for line_number, line in enumerate(stream, start=1):
                        if not line.strip():
                            continue
                        try:
                            value = json.loads(line)
                        except json.JSONDecodeError as error:
                            raise ActualReadError(
                                f"could not parse {segment}:{line_number}: {error}"
                            ) from error
                        action = self._decode(value, run_id, segment, line_number)
                        if action is not None:
                            rows.append(action)
            except OSError as error:
                raise ActualReadError(f"could not read {segment}: {error}") from error
        rows.sort(key=lambda action: action.event_seq)
        return tuple(rows)

    def _segments(self) -> tuple[Path, ...]:
        try:
            return tuple(sorted(self.segments_dir.glob("segment-*.jsonl")))
        except OSError as error:
            raise ActualReadError(f"could not list {self.segments_dir}: {error}") from error

    @staticmethod
    def _decode(
        value: object,
        run_id: str,
        segment: Path,
        line_number: int,
    ) -> ActualAction | None:
        if not isinstance(value, dict):
            raise ActualReadError(f"{segment}:{line_number}: ledger row must be an object")
        if value.get("run_id") != run_id or value.get("type") != "tool.called":
            return None
        event_seq = value.get("run_seq")
        agent_id = value.get("agent_id")
        data = value.get("data")
        if type(event_seq) is not int or event_seq < 0:
            raise ActualReadError(f"{segment}:{line_number}: tool.called requires run_seq")
        if not isinstance(agent_id, str) or not agent_id:
            raise ActualReadError(f"{segment}:{line_number}: tool.called requires agent_id")
        if not isinstance(data, dict):
            raise ActualReadError(f"{segment}:{line_number}: tool.called data must be an object")
        tool_name = data.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            raise ActualReadError(f"{segment}:{line_number}: tool.called requires tool_name")
        arguments = data.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ActualReadError(f"{segment}:{line_number}: tool.called arguments must be an object")
        target = _target(data, arguments, agent_id)
        return ActualAction(
            kind=tool_name,
            target=target,
            arguments=arguments,
            event_seq=event_seq,
            rationale=_rationale(data),
            agent_id=agent_id,
        )


def _target(data: Mapping[str, object], arguments: Mapping[str, object], agent_id: str) -> str:
    for source in (arguments, data):
        for key in ("target", "slice_id", "recipient", "name", "branch"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return agent_id


def _rationale(data: Mapping[str, object]) -> str:
    error = data.get("error")
    if isinstance(error, str) and error:
        return f"actual tool call failed: {error}"
    return "actual tool call observed by the server"


__all__ = ["ActualAction", "ActualActionReader", "ActualReadError"]
