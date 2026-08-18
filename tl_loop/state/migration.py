"""Pure and restart-safe migration of legacy TL checkpoints."""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import SCHEMA_VERSION

MIGRATION_VERSION = 1
# The migration target always tracks the loader's own accepted version, so
# the two can never drift the way pre-#898 schema_version=1 checkpoints did:
# CURRENT_CHECKPOINT_VERSION previously duplicated SCHEMA_VERSION as a
# separate literal, so a document already stamped version=1 was treated as
# "current" and skipped _migrate_slices entirely even after schema.py grew
# new requirements (e.g. dispatch identity fields for spawned slices) that
# those legacy documents never satisfied.
CURRENT_CHECKPOINT_VERSION = SCHEMA_VERSION


class MigrationError(ValueError):
    """Raised when a legacy checkpoint cannot be migrated safely."""


@dataclass(frozen=True)
class MigrationResult:
    document: dict[str, object]
    source_version: int
    migrated: bool
    changes: tuple[str, ...]


def migrate_checkpoint_document(
    value: object,
    *,
    run_id: str,
) -> MigrationResult:
    """Transform supported legacy documents without mutating the input."""
    if not isinstance(value, dict):
        raise MigrationError("checkpoint must contain a JSON object")
    source_version = _source_version(value)
    if source_version > CURRENT_CHECKPOINT_VERSION:
        raise MigrationError(
            f"checkpoint version {source_version} is newer than supported "
            f"version {CURRENT_CHECKPOINT_VERSION}"
        )
    if source_version == CURRENT_CHECKPOINT_VERSION:
        return MigrationResult(copy.deepcopy(value), source_version, False, ())

    document = copy.deepcopy(value)
    changes: list[str] = []
    document.pop("schema_version", None)
    if document.get("version") != CURRENT_CHECKPOINT_VERSION:
        document["version"] = CURRENT_CHECKPOINT_VERSION
        changes.append("version")
    if not isinstance(document.get("run_id"), str) or not document["run_id"]:
        document["run_id"] = run_id
        changes.append("run_id")
    document.setdefault("revision", 0)
    document.setdefault("fsm", {"phase": "tl_planning", "waiting": []})
    document.setdefault("slices", {})
    document.setdefault("budgets", {"ledger": {"tokens": 0, "wall_seconds": 0}})
    document.setdefault("gates", [])
    document.setdefault("events", {"last_consumed_offset": 0})
    changes.extend(_migrate_slices(document["slices"]))
    return MigrationResult(document, source_version, True, tuple(changes))


def install_migration(path: Path, result: MigrationResult) -> Path:
    """Atomically preserve the original, install the result, and report it."""
    if not result.migrated:
        raise MigrationError("cannot install a non-migrated checkpoint")
    backup = _collision_safe(path, f".legacy-{time.time_ns()}")
    if not backup.exists():
        _atomic_write_bytes(backup, path.read_bytes())
    _atomic_write_json(path, result.document)
    report = path.with_name("migration-report.json")
    _atomic_write_json(
        report,
        {
            "migration_version": MIGRATION_VERSION,
            "source_version": result.source_version,
            "target_version": CURRENT_CHECKPOINT_VERSION,
            "checkpoint": str(path),
            "original": str(backup),
            "changes": list(result.changes),
            "status": "complete",
        },
    )
    return backup


def record_migration_failure(path: Path, error: BaseException) -> Path:
    """Persist a blocked diagnostic without changing the source artifact."""
    report = path.with_name("migration-error.json")
    _atomic_write_json(
        report,
        {
            "migration_version": MIGRATION_VERSION,
            "checkpoint": str(path),
            "status": "blocked",
            "error": str(error),
        },
    )
    return report


def _source_version(value: dict[str, object]) -> int:
    raw = value.get("version", value.get("schema_version", 0))
    if type(raw) is not int or raw < 0:
        raise MigrationError("checkpoint version must be a non-negative integer")
    return raw


def _migrate_slices(value: object) -> list[str]:
    if not isinstance(value, dict):
        raise MigrationError("legacy slices must be an object")
    changes: list[str] = []
    for slice_id, raw in value.items():
        if not isinstance(raw, dict):
            raise MigrationError(f"legacy slice {slice_id!r} must be an object")
        if "id" not in raw:
            raw["id"] = slice_id
            changes.append(f"{slice_id}.id")
        if "status" not in raw:
            raw["status"] = raw.pop("state", "pending")
            changes.append(f"{slice_id}.status")
        for key, default in (
            ("paths", ["."]),
            ("depends_on", []),
            ("test_plan", []),
            ("base_ref", None),
            ("agent_type", None),
            ("model", None),
            ("branch", None),
            ("worktree", None),
            ("pr_number", None),
            ("reviewed_head", None),
            ("attempts", 0),
            ("verdict", None),
            ("review_findings", {}),
            ("ci_state", {}),
            ("reviewer_attempt", {}),
            ("repair_attempts", 0),
        ):
            if key not in raw:
                raw[key] = copy.deepcopy(default)
                changes.append(f"{slice_id}.{key}")
        if raw.get("status") == "spawned" and not _has_spawn_evidence(raw):
            raw["status"] = "dispatch_unconfirmed"
            raw["dispatch_last_boundary"] = "legacy_migration"
            raw["dispatch_error"] = (
                "legacy spawned checkpoint lacks authoritative dispatch evidence"
            )
            changes.append(f"{slice_id}.status=dispatch_unconfirmed")
    return changes


def _has_spawn_evidence(value: dict[str, object]) -> bool:
    return (
        isinstance(value.get("dispatch_intent_id"), str)
        and bool(value["dispatch_intent_id"])
        and isinstance(value.get("dispatch_agent_id"), str)
        and bool(value["dispatch_agent_id"])
        and type(value.get("dispatch_authoritative_event_seq")) is int
    )


def _collision_safe(path: Path, suffix: str) -> Path:
    candidate = path.with_name(path.name + suffix)
    index = 0
    while candidate.exists():
        index += 1
        candidate = path.with_name(f"{path.name}{suffix}-{index}")
    return candidate


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_bytes(
        path,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )
