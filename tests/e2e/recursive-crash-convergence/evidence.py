"""Fail-closed assertions for recursive crash/restart evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any


class AcceptanceError(RuntimeError):
    """The production-shaped acceptance evidence is incomplete or inconsistent."""


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AcceptanceError(f"could not read JSON evidence {path}") from error


def crash_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise AcceptanceError(f"crash marker is missing: {path}")
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise AcceptanceError(f"crash marker is not an object: {value!r}")
        records.append(value)
    return records


def assert_crash_record(path: Path, boundary: str, point: str) -> str:
    records = crash_records(path)
    matches = [
        item
        for item in records
        if item.get("boundary") == boundary and item.get("point") == point
    ]
    if len(matches) != 1:
        raise AcceptanceError(
            f"expected one {boundary}:{point} crash record, found {matches!r}"
        )
    identity = matches[0].get("identity")
    if not isinstance(identity, str) or not identity:
        raise AcceptanceError(f"crash record has no effect identity: {matches[0]!r}")
    return identity


def assert_resume_not_redispatched(
    path: Path, crashed_identity: str, *, boundary: str, point: str
) -> int:
    """Correlate resumed UDS calls with the crashed effect identity."""
    if not path.is_file():
        raise AcceptanceError(f"resume call trace is missing: {path}")
    calls: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise AcceptanceError(f"resume call trace is malformed: {path}") from error
        if isinstance(value, dict):
            calls.append(value)
    matches = [call for call in calls if call.get("identity") == crashed_identity]
    if boundary in {"review", "adoption"}:
        return len(matches)
    expected = 1 if point == "before" else 0
    if len(matches) != expected:
        raise AcceptanceError(
            f"resumed {boundary} effect cardinality was {len(matches)}, expected {expected}: {matches!r}"
        )
    return len(matches)


def journal(path: Path) -> list[dict[str, Any]]:
    value = read_json(path)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise AcceptanceError(f"action journal is not an object list: {path}")
    return value


def assert_journal_terminal(path: Path) -> dict[str, int]:
    entries = journal(path)
    keys = [entry.get("key") for entry in entries]
    if any(not isinstance(key, str) or not key for key in keys):
        raise AcceptanceError(f"action journal contains an unkeyed effect: {entries!r}")
    if len(set(keys)) != len(keys):
        raise AcceptanceError(f"action journal duplicated an effect key: {keys!r}")
    unresolved = [
        entry for entry in entries if entry.get("status") in {"intended", "unknown"}
    ]
    if unresolved:
        raise AcceptanceError(f"action journal has unresolved effects: {unresolved!r}")
    counts: dict[str, int] = {}
    for entry in entries:
        operation = entry.get("operation")
        if isinstance(operation, str):
            counts[operation] = counts.get(operation, 0) + 1
    return counts


def assert_checkpoint_progression(paths: Sequence[Path]) -> None:
    if not paths:
        raise AcceptanceError("no checkpoints were captured")
    versions: list[int] = []
    cursors: list[int] = []
    for path in paths:
        document = read_json(path)
        if not isinstance(document, dict):
            raise AcceptanceError(f"checkpoint is not an object: {path}")
        version = document.get("state_version")
        if type(version) is not int:
            raise AcceptanceError(f"checkpoint has no integer state_version: {path}")
        versions.append(version)
        events = document.get("events")
        cursor = (
            events.get("last_consumed_offset") if isinstance(events, dict) else None
        )
        if type(cursor) is not int:
            raise AcceptanceError(f"checkpoint has no integer event cursor: {path}")
        cursors.append(cursor)
    if any(left > right for left, right in pairwise(versions)):
        raise AcceptanceError(f"checkpoint state_version regressed: {versions!r}")
    if any(left > right for left, right in pairwise(cursors)):
        raise AcceptanceError(f"checkpoint event cursor regressed: {cursors!r}")


def event_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    data = event.get("data")
    if isinstance(data, Mapping):
        payload = data.get("payload")
        if isinstance(payload, Mapping):
            return payload
        return data
    return event


def ledger_events(repo: Path) -> list[dict[str, Any]]:
    segments = repo / ".exo" / "ledger" / "segments"
    events: list[dict[str, Any]] = []
    for path in sorted(segments.glob("*")):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
    return events


def assert_effect_events(repo: Path, expected_run_id: str) -> dict[str, int]:
    """Require one queued/decided/reconciled merge per durable merge key."""
    selected = [
        item for item in ledger_events(repo) if item.get("run_id") == expected_run_id
    ]
    queued: list[Mapping[str, Any]] = []
    decisions: list[Mapping[str, Any]] = []
    reconciled: list[Mapping[str, Any]] = []
    for event in selected:
        payload = event_payload(event)
        event_type = event.get("type", event.get("event_type"))
        if event_type == "tl.action_queued" and payload.get("action") in {
            "merge",
            "merge_aggregate",
        }:
            queued.append(payload)
        elif event_type == "tl.merge_decided" and payload.get("decision") == "merge":
            decisions.append(payload)
        elif event_type == "tl.merge_reconciled":
            reconciled.append(payload)
    action_keys = [item.get("action_key") for item in queued]
    if any(not isinstance(key, str) or not key for key in action_keys):
        raise AcceptanceError("merge intent omitted its durable action key")
    if len(set(action_keys)) != len(action_keys):
        raise AcceptanceError(
            f"merge action was queued more than once: {action_keys!r}"
        )
    if len(decisions) != len(queued) or len(reconciled) != len(queued):
        raise AcceptanceError(
            "merge effect cardinality did not converge: "
            f"queued={len(queued)} decisions={len(decisions)} reconciled={len(reconciled)}"
        )
    return {
        "merge_intents": len(queued),
        "merge_decisions": len(decisions),
        "merge_reconciliations": len(reconciled),
    }
