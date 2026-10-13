"""Structural plan diffs and deterministic audit summaries."""

from __future__ import annotations

from .plan_manifest import ManifestNode, PlanManifest
from .plan_revision_types import PlanRevisionBlocker, PlanRevisionNodeChange, PlanRevisionScope


def collect_node_changes(
    previous: PlanManifest,
    candidate: PlanManifest,
) -> tuple[PlanRevisionNodeChange, ...]:
    """Describe every node addition, removal, or changed field by name only."""
    changes = _collect_local_node_changes(previous, candidate)
    changes.extend(_collect_nested_node_changes(previous, candidate))
    return tuple(sorted(changes, key=_change_sort_key))


def _collect_local_node_changes(
    previous: PlanManifest,
    candidate: PlanManifest,
) -> list[PlanRevisionNodeChange]:
    changes: list[PlanRevisionNodeChange] = []
    old_nodes = {node.node_id: node for node in previous.nodes}
    new_nodes = {node.node_id: node for node in candidate.nodes}
    for node_id in sorted(set(old_nodes) | set(new_nodes)):
        change = _node_change(previous, candidate, node_id, old_nodes, new_nodes)
        if change is not None:
            changes.append(change)
    return changes


def _node_change(
    previous: PlanManifest,
    candidate: PlanManifest,
    node_id: str,
    old_nodes: dict[str, ManifestNode],
    new_nodes: dict[str, ManifestNode],
) -> PlanRevisionNodeChange | None:
    old = old_nodes.get(node_id)
    new = new_nodes.get(node_id)
    if old is None and new is not None:
        return PlanRevisionNodeChange(candidate.scope_id, node_id, "added", _node_field_names(new))
    if new is None and old is not None:
        return PlanRevisionNodeChange(previous.scope_id, node_id, "removed", _node_field_names(old))
    if old is not None and new is not None:
        fields = changed_node_fields(old, new)
        if fields:
            return PlanRevisionNodeChange(previous.scope_id, node_id, "updated", fields)
    return None


def _collect_nested_node_changes(
    previous: PlanManifest,
    candidate: PlanManifest,
) -> list[PlanRevisionNodeChange]:
    changes: list[PlanRevisionNodeChange] = []
    for node_id, old_child in previous.child_manifests.items():
        new_child = candidate.child_manifests.get(node_id)
        if new_child is None:
            changes.extend(_collect_removed_scope_changes(old_child))
        else:
            changes.extend(collect_node_changes(old_child, new_child))
    for node_id, new_child in candidate.child_manifests.items():
        if node_id not in previous.child_manifests:
            changes.extend(_collect_added_scope_changes(new_child))
    return changes


def _collect_added_scope_changes(manifest: PlanManifest) -> list[PlanRevisionNodeChange]:
    changes = [
        PlanRevisionNodeChange(manifest.scope_id, node.node_id, "added", _node_field_names(node))
        for node in manifest.nodes
    ]
    for child in manifest.child_manifests.values():
        changes.extend(_collect_added_scope_changes(child))
    return changes


def _collect_removed_scope_changes(manifest: PlanManifest) -> list[PlanRevisionNodeChange]:
    changes = [
        PlanRevisionNodeChange(manifest.scope_id, node.node_id, "removed", _node_field_names(node))
        for node in manifest.nodes
    ]
    for child in manifest.child_manifests.values():
        changes.extend(_collect_removed_scope_changes(child))
    return changes


def _node_field_names(node: ManifestNode) -> tuple[str, ...]:
    fields = set(node.to_document()) - {"declaration", "integration_contract"}
    fields.update(f"declaration.{key}" for key in node.declaration)
    fields.update(f"integration_contract.{key}" for key in node.integration_contract)
    return tuple(sorted(fields))


def changed_node_fields(previous: ManifestNode, candidate: ManifestNode) -> tuple[str, ...]:
    """Return changed field paths without exposing their user-supplied values."""
    old = previous.to_document()
    new = candidate.to_document()
    fields = {
        key
        for key in set(old) | set(new)
        if key not in {"declaration", "integration_contract"} and old.get(key) != new.get(key)
    }
    fields.update(
        f"declaration.{key}"
        for key in set(previous.declaration) | set(candidate.declaration)
        if previous.declaration.get(key) != candidate.declaration.get(key)
    )
    fields.update(
        f"integration_contract.{key}"
        for key in set(previous.integration_contract) | set(candidate.integration_contract)
        if previous.integration_contract.get(key) != candidate.integration_contract.get(key)
    )
    return tuple(sorted(fields))


def collect_changed_scope_summaries(
    previous: PlanManifest,
    candidate: PlanManifest,
) -> tuple[PlanRevisionScope, ...]:
    """Return sorted old and new revision coordinates for changed scopes."""
    result: list[PlanRevisionScope] = []
    if previous.digest != candidate.digest:
        result.append(_changed_scope_summary(previous, candidate))
    result.extend(_nested_scope_summaries(previous, candidate))
    return tuple(sorted(result, key=lambda scope: scope.scope_id))


def _changed_scope_summary(
    previous: PlanManifest,
    candidate: PlanManifest,
) -> PlanRevisionScope:
    return PlanRevisionScope(
        previous.scope_id,
        previous.manifest_revision,
        candidate.manifest_revision,
        previous.digest,
        candidate.digest,
    )


def _nested_scope_summaries(
    previous: PlanManifest,
    candidate: PlanManifest,
) -> list[PlanRevisionScope]:
    result: list[PlanRevisionScope] = []
    for node_id, old_child in previous.child_manifests.items():
        new_child = candidate.child_manifests.get(node_id)
        if new_child is None:
            result.extend(_collect_removed_scope_summaries(old_child))
        else:
            result.extend(collect_changed_scope_summaries(old_child, new_child))
    for node_id, new_child in candidate.child_manifests.items():
        if node_id not in previous.child_manifests:
            result.extend(_collect_new_scope_summaries(new_child))
    return result


def _collect_new_scope_summaries(manifest: PlanManifest) -> list[PlanRevisionScope]:
    result = [
        PlanRevisionScope(
            manifest.scope_id, None, manifest.manifest_revision, None, manifest.digest
        )
    ]
    for child in manifest.child_manifests.values():
        result.extend(_collect_new_scope_summaries(child))
    return result


def _collect_removed_scope_summaries(manifest: PlanManifest) -> list[PlanRevisionScope]:
    result = [
        PlanRevisionScope(
            manifest.scope_id, manifest.manifest_revision, None, manifest.digest, None
        )
    ]
    for child in manifest.child_manifests.values():
        result.extend(_collect_removed_scope_summaries(child))
    return result


def unique_blockers(blockers: list[PlanRevisionBlocker]) -> tuple[PlanRevisionBlocker, ...]:
    """Remove duplicate reasons and order the diagnostics deterministically."""
    unique = {
        (blocker.code, blocker.reason, blocker.scope_id, blocker.node_id): blocker
        for blocker in blockers
    }
    return tuple(
        unique[key]
        for key in sorted(unique, key=lambda item: (item[0], item[2] or "", item[3] or "", item[1]))
    )


def _change_sort_key(change: PlanRevisionNodeChange) -> tuple[str, str, str, tuple[str, ...]]:
    return (change.scope_id, change.node_id, change.action, change.fields)
