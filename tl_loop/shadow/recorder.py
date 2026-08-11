"""Durable recording of shadow intended actions."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.shadow import IntendedAction


class RecorderError(RuntimeError):
    """An intended action could not be recorded or decoded."""


class IntendedActionRecorder:
    """Append every shadow action to one run-local JSONL file."""

    def __init__(self, run_id: str, *, root_dir: str | Path = Path(".exo/tl-loop/shadow")) -> None:
        _validate_run_id(run_id)
        self.run_id = run_id
        self.run_dir = Path(root_dir) / run_id
        self.path = self.run_dir / "intended.jsonl"

    def record(self, action: IntendedAction) -> None:
        """Synchronously append one complete action record."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        document = _encode(action)
        try:
            with self.path.open("a", encoding="utf-8") as stream:
                json.dump(document, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, TypeError, ValueError) as error:
            raise RecorderError(f"could not append {self.path}: {error}") from error

    def record_many(self, actions: Iterable[IntendedAction]) -> None:
        """Record actions in their supplied order."""
        for action in actions:
            self.record(action)

    def read(self) -> tuple[dict[str, object], ...]:
        """Read recorded JSON objects without dropping malformed rows."""
        if not self.path.exists():
            return ()
        rows: list[dict[str, object]] = []
        try:
            with self.path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise RecorderError(f"{self.path}:{line_number}: action must be an object")
                    rows.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RecorderError(f"could not read {self.path}: {error}") from error
        return tuple(rows)

    def read_actions(self) -> tuple[IntendedAction, ...]:
        """Decode every persisted row as an intended action."""
        actions: list[IntendedAction] = []
        for row in self.read():
            try:
                arguments = row["arguments"]
                event_seq = row["event_seq"]
                phase_before = row["phase_before"]
                phase_after = row["phase_after"]
                if not isinstance(arguments, dict) or type(event_seq) is not int:
                    raise TypeError("arguments/event_seq have invalid types")
                if not isinstance(phase_before, str) or not isinstance(phase_after, str):
                    raise TypeError("phase fields have invalid types")
                actions.append(
                    IntendedAction(
                        _string(row, "kind"),
                        _string(row, "target"),
                        arguments,
                        _string(row, "rationale"),
                        event_seq,
                        TLPhase(phase_before),
                        TLPhase(phase_after),
                    )
                )
            except (KeyError, TypeError, ValueError) as error:
                raise RecorderError(f"invalid intended action row in {self.path}: {error}") from error
        return tuple(actions)


def _encode(action: IntendedAction) -> dict[str, object]:
    return {
        "kind": action.kind,
        "target": action.target,
        "arguments": dict(action.arguments),
        "rationale": action.rationale,
        "event_seq": action.event_seq,
        "phase_before": action.phase_before.value,
        "phase_after": action.phase_after.value,
    }


def _validate_run_id(run_id: str) -> None:
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be a non-empty single path component")


def _string(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{key} must be a non-empty string")
    return value


__all__ = ["IntendedActionRecorder", "RecorderError"]
