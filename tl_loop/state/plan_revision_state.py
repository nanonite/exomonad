"""Read-only validation of root and nested RunStore checkpoints."""

from __future__ import annotations

import json
from pathlib import Path

from .migration import MigrationError, migrate_checkpoint_document
from .plan_manifest import ManifestError, ManifestNode, PlanManifest
from .plan_revision_types import PlanRevisionError, ScopeState
from .schema import RunState, SchemaError, validate
from .store import CorruptCheckpoint, _assert_consistent, _decode


def load_scope_tree(
    root_dir: Path,
    root_state: RunState,
    root_manifest: PlanManifest,
) -> dict[str, ScopeState]:
    """Validate every materialized child against its parent declaration."""
    scopes = {root_manifest.scope_id: ScopeState(root_state, root_manifest)}
    _load_child_scopes(root_dir, root_state, root_manifest, scopes)
    return scopes


def _load_child_scopes(
    scope_dir: Path,
    parent_state: RunState,
    manifest: PlanManifest,
    scopes: dict[str, ScopeState],
) -> None:
    declared = {node.name for node in manifest.nodes if node.kind == "sub_tl"}
    _reject_orphan_child_runs(scope_dir, declared)
    for node in manifest.nodes:
        if node.kind == "sub_tl":
            _load_declared_child(scope_dir, parent_state, manifest, node, scopes)


def _load_declared_child(
    scope_dir: Path,
    parent_state: RunState,
    parent_manifest: PlanManifest,
    node: ManifestNode,
    scopes: dict[str, ScopeState],
) -> None:
    child_manifest = parent_manifest.child_manifests[node.node_id]
    child_dir = scope_dir / node.name
    checkpoint_path = child_dir / "run.json"
    _reject_child_symlink(child_dir, checkpoint_path)
    if not checkpoint_path.exists():
        _reject_orphan_descendants(child_dir)
        return
    child_state = read_checkpoint(checkpoint_path)
    _validate_child_checkpoint(
        child_state, parent_state, node.name, child_manifest, checkpoint_path
    )
    scopes[child_manifest.scope_id] = ScopeState(child_state, child_manifest)
    _load_child_scopes(child_dir, child_state, child_manifest, scopes)


def _reject_child_symlink(child_dir: Path, checkpoint_path: Path) -> None:
    if child_dir.is_symlink() or checkpoint_path.is_symlink():
        raise PlanRevisionError(
            "runstore_symlink",
            f"refusing symlinked child RunStore at {checkpoint_path}",
        )


def _validate_child_checkpoint(
    child_state: RunState,
    parent_state: RunState,
    child_name: str,
    child_manifest: PlanManifest,
    checkpoint_path: Path,
) -> None:
    if child_state.run_id != child_name or child_state.parent_run_id != parent_state.run_id:
        raise PlanRevisionError(
            "runstore_parent_mismatch",
            f"child RunStore {checkpoint_path} does not identify its declared parent",
        )
    if child_state.plan_manifest != child_manifest:
        raise PlanRevisionError(
            "manifest_drift",
            f"child RunStore {checkpoint_path} does not match its parent manifest",
        )


def _reject_orphan_child_runs(scope_dir: Path, declared_names: set[str]) -> None:
    try:
        children = tuple(scope_dir.iterdir())
    except OSError as error:
        raise PlanRevisionError(
            "runstore_unreadable", f"cannot inspect {scope_dir}: {error}"
        ) from error
    for child_dir in children:
        if child_dir.is_symlink():
            raise PlanRevisionError(
                "runstore_symlink",
                f"refusing symlinked child path {child_dir}",
            )
        if not child_dir.is_dir() or child_dir.name in declared_names:
            continue
        if child_dir.name.startswith("root.invalid-"):
            continue
        if (child_dir / "run.json").is_file():
            raise PlanRevisionError(
                "orphan_runstore",
                f"undeclared child RunStore exists at {child_dir / 'run.json'}",
            )


def _reject_orphan_descendants(child_dir: Path) -> None:
    if not child_dir.exists():
        return
    try:
        orphan = next(child_dir.rglob("run.json"), None)
    except OSError as error:
        raise PlanRevisionError(
            "runstore_unreadable", f"cannot inspect {child_dir}: {error}"
        ) from error
    if orphan is not None:
        raise PlanRevisionError(
            "orphan_runstore",
            f"nested RunStore {orphan} has no parent checkpoint",
        )


def read_checkpoint(path: Path) -> RunState:
    """Decode a current checkpoint without triggering RunStore migrations."""
    if path.is_symlink() or path.parent.is_symlink():
        raise PlanRevisionError("runstore_symlink", f"refusing symlinked RunStore path {path}")
    return decode_checkpoint_bytes(path, _read_checkpoint_bytes(path))


def decode_checkpoint_bytes(path: Path, raw: bytes) -> RunState:
    """Validate checkpoint bytes already captured for a stable evidence read."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PlanRevisionError("runstore_unreadable", f"cannot decode RunStore {path}") from error
    return _decode_checkpoint(path, value)


def _read_checkpoint_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as error:
        raise PlanRevisionError(
            "runstore_missing", f"RunStore checkpoint is missing: {path}"
        ) from error
    except OSError as error:
        raise PlanRevisionError(
            "runstore_unreadable", f"cannot read RunStore {path}: {error}"
        ) from error


def _decode_checkpoint(path: Path, value: object) -> RunState:
    if not isinstance(value, dict):
        raise PlanRevisionError("runstore_invalid", f"RunStore {path} must contain a JSON object")
    return _decode_checkpoint_object(path, value)


def _decode_checkpoint_object(path: Path, value: dict[str, object]) -> RunState:
    try:
        migration = migrate_checkpoint_document(value, run_id=path.parent.name)
        if migration.migrated:
            raise PlanRevisionError(
                "runstore_needs_migration",
                f"RunStore {path} needs migration before plan revision",
            )
        validate(value)
        state = _decode(value)
        _assert_consistent(path, state)
    except PlanRevisionError:
        raise
    except (
        MigrationError,
        SchemaError,
        CorruptCheckpoint,
        ManifestError,
        ValueError,
        TypeError,
    ) as error:
        raise PlanRevisionError(
            "runstore_invalid", f"RunStore {path} is inconsistent: {error}"
        ) from error
    return state
