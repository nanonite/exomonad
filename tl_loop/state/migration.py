"""Pure and restart-safe migration of legacy TL checkpoints."""

from __future__ import annotations

import copy
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .plan_manifest import ManifestError, PlanManifest, build_legacy_manifest
from .schema import REDUCER_VERSION, SCHEMA_VERSION
from .serialization import dumps as dumps_json

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
    if source_version == CURRENT_CHECKPOINT_VERSION and isinstance(
        value.get("plan_manifest"), dict
    ):
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
    document.setdefault("reducer_version", REDUCER_VERSION)
    changes.extend(_migrate_slices(document["slices"]))
    if document.get("plan_manifest") is None:
        try:
            manifest = build_legacy_manifest(document, run_id=run_id)
        except ManifestError as error:
            raise MigrationError(f"legacy plan manifest is ambiguous: {error}") from error
        document["plan_manifest"] = manifest.to_document()
        _bind_migrated_slices(document["slices"], manifest)
        changes.append("plan_manifest")
        if manifest.nodes:
            gates = document.get("gates")
            if not isinstance(gates, list):
                raise MigrationError("legacy gates must be an array")
            gates.append(
                {
                    "name": (
                        "plan-manifest-migration: direct child kind and recursive ownership "
                        "cannot be proven from this checkpoint"
                    ),
                    "status": "pending",
                }
            )
            changes.append("plan_manifest_recovery_gate")
    return MigrationResult(document, source_version, True, tuple(changes))


def _bind_migrated_slices(value: object, manifest: PlanManifest) -> None:
    if not isinstance(value, dict):
        raise MigrationError("legacy slices must be an object")
    nodes = {node.name: node for node in manifest.nodes}
    for slice_id, raw in value.items():
        if not isinstance(raw, dict) or not isinstance(slice_id, str):
            raise MigrationError(f"legacy slice {slice_id!r} cannot be bound")
        node = nodes.get(slice_id)
        if node is None:
            raise MigrationError(f"legacy slice {slice_id!r} is not declared by its manifest")
        raw["manifest_node_id"] = node.node_id
        raw["manifest_revision"] = manifest.manifest_revision


def install_migration(path: Path, result: MigrationResult) -> Path:
    """Preserve the original, report the migration, then install the result.

    Every write below must be safe to redo from scratch if the process is
    interrupted partway through -- this is called on every load() of a
    legacy checkpoint until the live document itself reaches
    CURRENT_CHECKPOINT_VERSION, at which point migrate_checkpoint_document
    stops calling it at all. That makes the final checkpoint overwrite the
    one truly irreversible step, so it goes last:

    1. The backup name is a deterministic function of the source/target
       versions (not a timestamp), so an interrupted-and-retried install
       reuses the same backup file instead of accumulating a fresh
       "legacy" copy on every restart before the checkpoint write lands.
    2. The report is written next, while the on-disk checkpoint is still
       the pre-migration document, so a report always exists before the
       transition that makes the loader treat this checkpoint as already
       current -- an interruption here still leaves migrate_checkpoint_document
       able to detect and retry the whole migration on the next load.
    3. Only then is the live checkpoint overwritten. If this step is what
       gets interrupted, the report and backup are already correct and the
       retry above will redo it idempotently; if it completes, a
       "migrated checkpoint with no report" state is now unreachable.
    """
    if not result.migrated:
        raise MigrationError("cannot install a non-migrated checkpoint")
    backup = path.with_name(
        f"{path.name}.legacy-v{result.source_version}-to-v{CURRENT_CHECKPOINT_VERSION}"
    )
    if not backup.exists():
        _atomic_write_bytes(backup, path.read_bytes())
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
    _atomic_write_json(path, result.document)
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
            ("reviewer_agent_id", None),
            ("repair_attempts", 0),
        ):
            if key not in raw:
                raw[key] = copy.deepcopy(default)
                changes.append(f"{slice_id}.{key}")
        if raw.get("verdict") is not None and "review_validation_required" not in raw:
            raw["review_validation_required"] = True
            changes.append(f"{slice_id}.review_validation_required")
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


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_bytes(
        path,
        dumps_json(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )
