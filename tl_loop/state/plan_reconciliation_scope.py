"""Plan reconciliation support."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from .plan_manifest import ManifestNode, PlanManifest
from .plan_reconciliation_guards import _blocker, _node_is_protected, _scope_blockers
from .plan_reconciliation_models import ReconciliationScope
from .plan_reconciliation_report import _safe_text
from .plan_revision_files import read_regular_file, relative_to_root, safe_target
from .plan_revision_state import decode_checkpoint_bytes
from .plan_revision_types import PlanRevisionBlocker, PlanRevisionError


def _read_scope(
    project: Path,
    relative_dir: str,
    run_id: str,
    parent_manifest: PlanManifest | None,
    inputs: dict[str, bytes | None],
) -> ReconciliationScope:
    checkpoint = f".exo/tl-loop/{relative_dir}/run.json"
    path = safe_target(project, checkpoint)
    raw, _mode = read_regular_file(path)
    if raw is None:
        raise PlanRevisionError(
            "reconciliation_input_missing", f"required input is missing: {checkpoint}"
        )
    inputs[checkpoint] = raw
    try:
        state = decode_checkpoint_bytes(path, cast(bytes, raw))
    except PlanRevisionError as error:
        raise PlanRevisionError(
            "runstore_invalid", f"RunStore for {_safe_text(run_id)} failed validation"
        ) from error
    manifest = state.plan_manifest
    if manifest is None:
        raise PlanRevisionError(
            "manifest_missing", f"RunStore {_safe_text(relative_dir)} has no plan manifest"
        )
    parent_matches = parent_manifest is None or manifest == parent_manifest
    is_root = relative_dir == run_id
    identity_matches = (
        state.run_id == run_id
        and state.parent_run_id == manifest.parent_scope_id
        and (
            not is_root
            or (
                manifest.scope_id == run_id
                and manifest.role == "root"
                and manifest.parent_scope_id is None
            )
        )
    )
    return ReconciliationScope(
        run_dir=path.parent,
        relative_checkpoint=checkpoint,
        raw_checkpoint=cast(bytes, raw),
        state=state,
        current=manifest,
        accepted=None,
        parent_manifest_matches=parent_matches,
        run_identity_matches=identity_matches,
        protected_node_ids=tuple(
            node.node_id for node in manifest.nodes if _node_is_protected(state, node.name)
        ),
    )


def _validate_plan_run_id(
    snapshot: dict[str, object],
    candidate: dict[str, object],
    run_id: str,
) -> None:
    for label, document in (("accepted snapshot", snapshot), ("candidate", candidate)):
        if document.get("run_id") not in (None, run_id):
            raise PlanRevisionError("run_id_changed", f"{label} run_id differs from the active run")


def _collect_scope_tree(
    project: Path,
    view: ReconciliationScope,
    accepted: PlanManifest,
    *,
    parent_manifest_matches: bool,
    inputs: dict[str, bytes | None],
    scopes: list[ReconciliationScope],
    blockers: list[PlanRevisionBlocker],
) -> None:
    accepted_view = ReconciliationScope(
        run_dir=view.run_dir,
        relative_checkpoint=view.relative_checkpoint,
        raw_checkpoint=view.raw_checkpoint,
        state=view.state,
        current=view.current,
        accepted=accepted,
        parent_manifest_matches=parent_manifest_matches,
        run_identity_matches=view.run_identity_matches,
        protected_node_ids=view.protected_node_ids,
    )
    scopes.append(accepted_view)
    blockers.extend(_scope_blockers(accepted_view))
    _check_child_directories(view, accepted, project, blockers)
    for node in view.current.nodes:
        if node.kind != "sub_tl":
            continue
        accepted_child = accepted.child_manifests.get(node.node_id)
        current_child = view.current.child_manifests.get(node.node_id)
        child_dir = _child_relative_dir(view.relative_checkpoint, node)
        child_file = f".exo/tl-loop/{child_dir}/run.json"
        child_path = safe_target(project, child_file)
        if not child_path.exists():
            if _node_is_protected(view.state, node.name):
                blockers.append(
                    _blocker("materialized_scope_missing", node.node_id, view.current.scope_id)
                )
            continue
        if accepted_child is None or current_child is None:
            blockers.append(_blocker("scope_topology_drift", node.node_id, view.current.scope_id))
            continue
        child_view = _read_scope(
            project,
            child_dir,
            node.name,
            current_child,
            inputs,
        )
        _collect_scope_tree(
            project,
            child_view,
            accepted_child,
            parent_manifest_matches=child_view.parent_manifest_matches,
            inputs=inputs,
            scopes=scopes,
            blockers=blockers,
        )


def _child_relative_dir(checkpoint: str, node: ManifestNode) -> str:
    if Path(node.name).name != node.name or node.name in {".", ".."}:
        raise PlanRevisionError("scope_path_invalid", "sub-TL name is not a safe path component")
    parent_dir = str(Path(checkpoint).parent.relative_to(Path(".exo/tl-loop")))
    return str(Path(parent_dir) / node.name)


def _check_child_directories(
    view: ReconciliationScope,
    accepted: PlanManifest,
    project: Path,
    blockers: list[PlanRevisionBlocker],
) -> None:
    known = {
        node.name
        for manifest in (view.current, accepted)
        for node in manifest.nodes
        if node.kind == "sub_tl"
    }
    try:
        children = tuple(view.run_dir.iterdir())
    except OSError as error:
        raise PlanRevisionError(
            "runstore_unreadable", "cannot inspect materialized scopes"
        ) from error
    for child in children:
        if child.is_symlink():
            raise PlanRevisionError("runstore_symlink", "refusing symlink in materialized scopes")
        if not child.is_dir() or child.name in known:
            continue
        extra = child / "run.json"
        relative = relative_to_root(project, extra)
        if safe_target(project, relative).exists():
            blockers.append(_blocker("orphan_runstore", child.name, view.current.scope_id))
