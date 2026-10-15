"""Versioned compatibility rules for accepted plan declarations."""

from __future__ import annotations

from collections.abc import Mapping

from tl_loop.compatibility import PLAN_COMPATIBILITY_VERSION
from tl_loop.state.plan_revision_diff import changed_node_fields

from .plan_manifest import ManifestNode, PlanManifest

_LEGACY_MANIFEST_SCHEMA_VERSION = 1
AGGREGATE_REVIEW_COMPATIBILITY_TRANSITION_VERSION = 1
AGGREGATE_REVIEW_OWNER_FIELD = "integration_contract.aggregate_review_owner"
_AGGREGATE_REVIEW_TRANSITION_FIELDS = frozenset(
    {AGGREGATE_REVIEW_OWNER_FIELD, "child_manifest_digest"}
)


def aggregate_review_transition_candidate(
    current: PlanManifest,
    accepted: PlanManifest,
    accepted_plan: Mapping[str, object] | None,
    *,
    candidate_matches_snapshot: bool,
) -> bool:
    """Recognize only the snapshot-identical aggregate-to-none default change."""
    if (
        not candidate_matches_snapshot
        or accepted_plan is None
        or not _same_legacy_schema(current, accepted)
        or current.plan_compatibility_version != 1
        or accepted.plan_compatibility_version != PLAN_COMPATIBILITY_VERSION
        or _scope_identity(current) != _scope_identity(accepted)
    ):
        return False
    changed_default = False
    old_nodes = {node.node_id: node for node in current.nodes}
    new_nodes = {node.node_id: node for node in accepted.nodes}
    if old_nodes.keys() != new_nodes.keys():
        return False
    for node_id, node in old_nodes.items():
        target = new_nodes[node_id]
        fields = set(changed_node_fields(node, target))
        if not fields:
            continue
        if not fields <= (_AGGREGATE_REVIEW_TRANSITION_FIELDS | {"chainlink_issue_id"}):
            return False
        if AGGREGATE_REVIEW_OWNER_FIELD in fields:
            if (
                node.integration_contract.get("aggregate_review_owner") != "aggregate"
                or target.integration_contract.get("aggregate_review_owner") != "none"
                or not _snapshot_omits_aggregate_review_owner(accepted_plan, node)
            ):
                return False
            changed_default = True
        if "child_manifest_digest" in fields and not _child_digest_is_derived(
            current, accepted, node, target
        ):
            return False
        if "chainlink_issue_id" in fields and not (
            node.kind == "leaf"
            and node.chainlink_issue_id is None
            and type(target.chainlink_issue_id) is int
        ):
            return False
    return changed_default


def _same_legacy_schema(current: PlanManifest, accepted: PlanManifest) -> bool:
    return (
        current.schema_version == _LEGACY_MANIFEST_SCHEMA_VERSION
        and accepted.schema_version == _LEGACY_MANIFEST_SCHEMA_VERSION
    )


def _scope_identity(manifest: PlanManifest) -> tuple[object, ...]:
    return (
        manifest.scope_id,
        manifest.parent_scope_id,
        manifest.role,
        manifest.owned_branch,
        manifest.parent_integration_target,
        manifest.source_order,
        manifest.ordered_stages,
    )


def _child_digest_is_derived(
    current: PlanManifest,
    accepted: PlanManifest,
    node: ManifestNode,
    target: ManifestNode,
) -> bool:
    old_child = current.child_manifests.get(node.node_id)
    new_child = accepted.child_manifests.get(node.node_id)
    return bool(
        node.kind == "sub_tl"
        and old_child is not None
        and new_child is not None
        and node.child_manifest_digest == old_child.digest
        and target.child_manifest_digest == new_child.digest
        and old_child.scope_id == new_child.scope_id
    )


def _snapshot_omits_aggregate_review_owner(
    plan: Mapping[str, object],
    node: ManifestNode,
) -> bool:
    scope = plan.get("plan") if isinstance(plan.get("plan"), Mapping) else plan
    kind_key = {"worker": "workers", "leaf": "leaves", "sub_tl": "sub_tls"}.get(node.kind)
    entries = scope.get(kind_key, ()) if kind_key is not None else ()
    if not isinstance(entries, list | tuple):
        return False
    matches = [
        item for item in entries if isinstance(item, Mapping) and item.get("name") == node.name
    ]
    if len(matches) != 1:
        return False
    integration = matches[0].get("integration")
    return integration is None or (
        isinstance(integration, Mapping) and "aggregate_review_owner" not in integration
    )


def explicit_aggregate_review_override(document: object) -> str | None:
    """Return the first node that explicitly requests the unsupported owner."""
    root = document.get("plan") if isinstance(document, Mapping) else None
    visit = root if isinstance(root, Mapping) else document
    if isinstance(visit, Mapping):
        return _find_explicit_aggregate_override(visit, "plan")
    return None


def _find_explicit_aggregate_override(scope: Mapping[str, object], path: str) -> str | None:
    for key in ("workers", "leaves", "sub_tls"):
        entries = scope.get(key, ())
        if not isinstance(entries, list | tuple):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name")
            node_path = f"{path}.{key}[{index}]"
            integration = entry.get("integration")
            if (
                isinstance(integration, Mapping)
                and integration.get("aggregate_review_owner") == "aggregate"
            ):
                return node_path if not isinstance(name, str) else f"{node_path} ({name})"
            nested = entry.get("plan")
            if isinstance(nested, Mapping):
                found = _find_explicit_aggregate_override(nested, f"{node_path}.plan")
                if found is not None:
                    return found
    return None
