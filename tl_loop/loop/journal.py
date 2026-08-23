"""Durable idempotency journal for authoritative TL lifecycle effects."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tl_loop.client.effects import ToolResult
from tl_loop.fsm.recovery import (
    RecoveryIntent,
    decode_recovery_intent,
    encode_recovery_intent,
)
from tl_loop.state.serialization import dumps as dumps_json
from tl_loop.state.serialization import to_jsonable

MUTATING_OPERATIONS = frozenset(
    {
        "spawn_worker",
        "spawn_leaf",
        "spawn_reviewer",
        "file_pr",
        "update_pr",
        "merge_pr",
        "resume_pr",
        "cleanup_reviewer_leaf",
        "close_reviewer_window",
        "cleanup_orphan",
        "cleanup_leaf",
        "cleanup",
        "close_worker_pane",
        "close_issue_and_cleanup",
        "emit_controller_event",
        "recovery_command",
    }
)
RECOVERY_INTENT_STATES = frozenset({"intended", "confirmed", "unknown", "reconciled"})


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
    encoded = dumps_json(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical(value: object) -> object:
    return to_jsonable(value)


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
        if isinstance(intent, RecoveryIntent):
            self.append_recovery_intent(intent)
            return
        super().append(intent)
        if intent.operation not in MUTATING_OPERATIONS:
            return
        self._upsert(
            {
                "key": self.key_for(intent),
                "run_id": self.run_id,
                "operation": intent.operation,
                "target": intent.target,
                "arguments": to_jsonable(intent.arguments),
                "status": "intended",
            }
        )

    def recovery_key(self, intent: RecoveryIntent) -> str:
        """Return a stable key for one recovery identity and action."""
        payload = encode_recovery_intent(intent)
        payload.pop("state", None)
        payload["run_id"] = self.run_id
        encoded = dumps_json(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def append_recovery_intent(self, intent: RecoveryIntent) -> None:
        """Journal recovery intent before its probe, resume, gate, or abandon effect."""
        entry = {
            "kind": "recovery",
            "key": self.recovery_key(intent),
            **encode_recovery_intent(intent),
        }
        self._upsert(entry)

    def existing_recovery(self, intent: RecoveryIntent) -> Mapping[str, object] | None:
        """Read a recovery intent by immutable identity."""
        key = self.recovery_key(intent)
        return next(
            (
                entry
                for entry in self._read()
                if isinstance(entry, dict)
                and entry.get("kind") == "recovery"
                and entry.get("key") == key
            ),
            None,
        )

    def mark_recovery_intent(
        self,
        intent: RecoveryIntent,
        state: str,
        *,
        result: object | None = None,
        error: str | None = None,
    ) -> None:
        """Record confirmed, unknown, or reconciled recovery outcome."""
        if state not in RECOVERY_INTENT_STATES:
            raise ActionJournalError(f"invalid recovery intent state {state!r}")
        entry = self.existing_recovery(intent)
        if entry is None:
            raise ActionJournalError(f"recovery intent {intent.intent_id} was not recorded")
        updates: dict[str, object] = {"state": state}
        if result is not None:
            updates["result"] = to_jsonable(result)
        if error is not None:
            updates["error"] = error
        self.resolve_recovery_by_key(self.recovery_key(intent), **updates)

    def pending_recovery_entries(self) -> list[dict[str, object]]:
        """Return recovery intents whose effects need restart reconciliation."""
        return [
            entry
            for entry in self._read()
            if isinstance(entry, dict)
            and entry.get("kind") == "recovery"
            and entry.get("state") in {"intended", "unknown"}
        ]

    def load_recovery_intent(self, entry: Mapping[str, object]) -> RecoveryIntent:
        """Decode one journal entry without trusting caller-provided identity."""
        return decode_recovery_intent(entry)

    def resolve_recovery_by_key(self, key: str, **updates: object) -> None:
        """Apply a durable restart reconciliation to one recovery intent."""
        entries = self._read()
        for entry in entries:
            if (
                isinstance(entry, dict)
                and entry.get("kind") == "recovery"
                and entry.get("key") == key
            ):
                entry.update(updates)
                self._write(entries)
                return
        raise ActionJournalError(f"recovery intent {key} was not recorded")

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
            dumps_json(entries, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.path)
