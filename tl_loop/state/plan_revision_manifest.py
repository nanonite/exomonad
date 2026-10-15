"""Recursive manifest rebuilds and monotonic revision assignment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from .plan_manifest import ManifestNode, PlanManifest


def retain_scope_revisions(
    candidate: PlanManifest,
    previous: PlanManifest | None,
    *,
    preserve_compatibility_version: bool = True,
) -> PlanManifest:
    """Align matching candidate scopes with their current revision numbers."""
    children = {
        node_id: retain_scope_revisions(
            child,
            previous.child_manifests.get(node_id) if previous else None,
            preserve_compatibility_version=preserve_compatibility_version,
        )
        for node_id, child in candidate.child_manifests.items()
    }
    revision = previous.manifest_revision if previous is not None else 1
    nodes = tuple(
        replace(node, child_manifest_digest=children[node.node_id].digest)
        if node.node_id in children
        else node
        for node in candidate.nodes
    )
    source = (
        replace(candidate, plan_compatibility_version=previous.plan_compatibility_version, digest=None)
        if previous is not None and preserve_compatibility_version
        else candidate
    )
    return rebuild_manifest(source, nodes, children, revision)


def rebuild_manifest(
    source: PlanManifest,
    nodes: tuple[ManifestNode, ...],
    children: Mapping[str, PlanManifest],
    revision: int,
) -> PlanManifest:
    """Create a canonical parent after recursively updating its child digests."""
    return PlanManifest(
        scope_id=source.scope_id,
        parent_scope_id=source.parent_scope_id,
        role=source.role,
        owned_branch=source.owned_branch,
        parent_integration_target=source.parent_integration_target,
        source_order=source.source_order,
        nodes=nodes,
        ordered_stages=source.ordered_stages,
        child_manifest_digests={
            node_id: cast(str, child.digest) for node_id, child in children.items()
        },
        child_manifests=children,
        schema_version=source.schema_version,
        plan_compatibility_version=source.plan_compatibility_version,
        manifest_revision=revision,
    )


def changed_scope_ids(previous: PlanManifest, candidate: PlanManifest) -> frozenset[str]:
    """Return each scope whose canonical declaration differs from its baseline."""
    changed: set[str] = set()

    def visit(old: PlanManifest | None, new: PlanManifest) -> None:
        old_children = old.child_manifests if old is not None else {}
        for child_id in sorted(set(old_children) | set(new.child_manifests)):
            child = new.child_manifests.get(child_id)
            if child is not None:
                visit(old_children.get(child_id), child)
        if old is None or _manifest_content(old) != _manifest_content(new):
            changed.add(new.scope_id)

    visit(previous, candidate)
    return frozenset(changed)


def increment_changed_revisions(
    candidate: PlanManifest,
    previous: PlanManifest | None,
    changed_scopes: frozenset[str],
) -> PlanManifest:
    """Increment only changed scope streams, then recompute recursive digests."""
    children = _revise_child_manifests(candidate, previous, changed_scopes)
    revision = _next_manifest_revision(candidate, previous, changed_scopes)
    nodes = _bind_child_manifest_digests(candidate, children)
    return rebuild_manifest(candidate, nodes, children, revision)


def _revise_child_manifests(
    candidate: PlanManifest,
    previous: PlanManifest | None,
    changed_scopes: frozenset[str],
) -> dict[str, PlanManifest]:
    previous_children = previous.child_manifests if previous is not None else {}
    return {
        node_id: increment_changed_revisions(
            child,
            previous_children.get(node_id),
            changed_scopes,
        )
        for node_id, child in candidate.child_manifests.items()
    }


def _next_manifest_revision(
    candidate: PlanManifest,
    previous: PlanManifest | None,
    changed_scopes: frozenset[str],
) -> int:
    if previous is None:
        return 1
    if candidate.scope_id in changed_scopes:
        return previous.manifest_revision + 1
    return previous.manifest_revision


def _bind_child_manifest_digests(
    candidate: PlanManifest,
    children: dict[str, PlanManifest],
) -> tuple[ManifestNode, ...]:
    return tuple(
        replace(node, child_manifest_digest=children[node.node_id].digest)
        if node.node_id in children
        else node
        for node in candidate.nodes
    )


def _manifest_content(manifest: PlanManifest) -> dict[str, object]:
    document = manifest.to_document()
    document.pop("manifest_revision", None)
    document.pop("digest", None)
    return document
