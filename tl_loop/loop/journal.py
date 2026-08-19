"""Durable idempotency journal for authoritative TL lifecycle effects."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tl_loop.client.effects import ToolResult

MUTATING_OPERATIONS = frozenset(
    {
        "spawn_worker",
        "spawn_leaf",
        "spawn_reviewer",
        "file_pr",
        "update_pr",
        "merge_pr",
        "cleanup_reviewer_leaf",
        "close_reviewer_window",
        "cleanup_orphan",
        "cleanup_leaf",
        "close_issue_and_cleanup",
        "emit_controller_event",
    }
)


class ActionJournalError(RuntimeError):
    """Raised when the durable idempotency record cannot be trusted."""


def stable_action_key(
    run_id: str,
    operation: str,
    target: str,
    arguments: Mapping[str, object],
) -> str:
    """Return a deterministic key for one lifecycle side effect."""
    payload = {
        "run_id": run_id,
        "operation": operation,
        "target": target,
        "arguments": _canonical(arguments),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


class EffectJournal(list[Any]):
    """EffectIntent-compatible list with durable before/after action records."""

    def __init__(self, run_id: str, path: Path) -> None:
        super().__init__()
        self.run_id = run_id
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def key_for(self, intent: Any) -> str:
        return stable_action_key(
            self.run_id,
            intent.operation,
            intent.target,
            intent.arguments,
        )

    def existing(self, intent: Any) -> Mapping[str, object] | None:
        key = self.key_for(intent)
        return next(
            (
                entry
                for entry in self._read()
                if isinstance(entry, dict) and entry.get("key") == key
            ),
            None,
        )

    def append(self, intent: Any) -> None:
        super().append(intent)
        if intent.operation not in MUTATING_OPERATIONS:
            return
        self._upsert(
            {
                "key": self.key_for(intent),
                "run_id": self.run_id,
                "operation": intent.operation,
                "target": intent.target,
                "arguments": dict(intent.arguments),
                "status": "intended",
            }
        )

    def mark_not_dispatched(self, intent: Any) -> None:
        if intent.operation in MUTATING_OPERATIONS:
            self._update(intent, status="not_dispatched")

    def mark_result(self, intent: Any, result: ToolResult) -> None:
        if intent.operation not in MUTATING_OPERATIONS:
            return
        status = "confirmed" if result.success is True else "rejected"
        self._update(intent, status=status, result=result.raw, error=result.error)

    def mark_unknown(self, intent: Any, error: BaseException) -> None:
        if intent.operation in MUTATING_OPERATIONS:
            self._update(intent, status="unknown", error=str(error))

    def pending_entries(self) -> list[dict[str, object]]:
        """Intended/unknown entries that block retry until reconciled."""
        return [
            entry
            for entry in self._read()
            if isinstance(entry, dict) and entry.get("status") in {"intended", "unknown"}
        ]

    def confirmed_entries(self, operation: str, target: str) -> list[dict[str, object]]:
        """Confirmed entries for one operation+target.

        Scans by operation/target/status rather than a recomputed stable key,
        since a caller reconstructing arguments (e.g. reconciliation replaying
        a live-path effect) is not guaranteed to produce byte-identical
        arguments to the original dispatch.
        """
        return [
            entry
            for entry in self._read()
            if isinstance(entry, dict)
            and entry.get("operation") == operation
            and entry.get("target") == target
            and entry.get("status") == "confirmed"
        ]

    def resolve_by_key(self, key: str, **updates: object) -> None:
        """Apply a reconciliation decision to an entry found by its stable key."""
        entries = self._read()
        for entry in entries:
            if isinstance(entry, dict) and entry.get("key") == key:
                entry.update(updates)
                self._write(entries)
                return
        raise ActionJournalError(f"action entry {key} was not recorded")

    def replay(self, entry: Mapping[str, object]) -> ToolResult:
        raw = entry.get("result")
        if not isinstance(raw, dict):
            raise ActionJournalError(
                f"confirmed action {entry.get('key', '<unknown>')} has no result"
            )
        return ToolResult.from_raw(raw)

    def _update(self, intent: Any, **updates: object) -> None:
        key = self.key_for(intent)
        entries = self._read()
        for entry in entries:
            if isinstance(entry, dict) and entry.get("key") == key:
                entry.update(updates)
                break
        else:
            raise ActionJournalError(f"action intent {key} was not recorded")
        self._write(entries)

    def _upsert(self, new_entry: dict[str, object]) -> None:
        entries = self._read()
        for index, entry in enumerate(entries):
            if isinstance(entry, dict) and entry.get("key") == new_entry["key"]:
                entries[index] = {**entry, **new_entry}
                self._write(entries)
                return
        entries.append(new_entry)
        self._write(entries)

    def _read(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ActionJournalError(f"invalid action journal {self.path}") from error
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ActionJournalError(f"invalid action journal root {self.path}")
        return payload

    def _write(self, entries: list[dict[str, object]]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(entries, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.path)
