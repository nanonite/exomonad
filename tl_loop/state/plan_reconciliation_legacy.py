"""Proven, field-limited compatibility for legacy manifest declarations."""

from __future__ import annotations

from tl_loop.fsm.child import ChildRecord
from tl_loop.state.plan_revision_diff import changed_node_fields

from .plan_manifest import ManifestNode, PlanManifest
from .plan_reconciliation_ownership import _records_for
from .schema import RunState

_DEFAULT_INTEGRATION_OWNERS = {
    "leaf_review_owner": "leaf",
    "aggregate_review_owner": "aggregate",
    "aggregate_repair_owner": "aggregate",
}
_PROVEN_COMPATIBILITY_SCHEMA_VERSION = 1


def proven_legacy_default_fields(
    current: PlanManifest,
    accepted: PlanManifest,
    state: RunState,
) -> dict[str, tuple[str, ...]]:
    """Return only omissions proven equivalent by the accepted runtime identity.

    Schema-one manifests may omit fields written by newer serializers. Schema
    version alone grants no equivalence: the verified accepted snapshot,
    matching FSM digest, and durable dispatch record must bind the node to the
    exact accepted manifest.
    """
    if not _same_legacy_schema(current, accepted) or not _fsm_matches_accepted(state, accepted):
        return {}
    records = {record.child_id: record for record in _records_for(state.recursive_fsm)}
    accepted_nodes = {node.node_id: node for node in accepted.nodes}
    equivalent: dict[str, tuple[str, ...]] = {}
    for node in current.nodes:
        target = accepted_nodes.get(node.node_id)
        if target is None or not _has_accepted_dispatch(
            records.get(node.name), target, accepted.manifest_revision
        ):
            continue
        changed = set(changed_node_fields(node, target))
        allowed = _equivalent_order_explicit(node, target, changed)
        allowed.update(_equivalent_integration_owners(node, target, changed))
        if allowed:
            equivalent[node.node_id] = tuple(sorted(allowed))
    return equivalent


def _same_legacy_schema(current: PlanManifest, accepted: PlanManifest) -> bool:
    return (
        current.schema_version == _PROVEN_COMPATIBILITY_SCHEMA_VERSION
        and accepted.schema_version == _PROVEN_COMPATIBILITY_SCHEMA_VERSION
    )


def _fsm_matches_accepted(state: RunState, accepted: PlanManifest) -> bool:
    return getattr(state.recursive_fsm, "plan_digest", None) == accepted.digest


def _has_accepted_dispatch(
    record: ChildRecord | None,
    target: ManifestNode,
    manifest_revision: int,
) -> bool:
    if record is None:
        return False
    return (
        record.kind.value == target.kind
        and record.manifest_node_id == target.node_id
        and record.manifest_revision == manifest_revision
        and bool(record.dispatch_intent_id or record.evidence.get("dispatch_intent_id"))
        and bool(record.invocation_id or record.evidence.get("invocation_id"))
    )


def _equivalent_order_explicit(
    node: ManifestNode,
    target: ManifestNode,
    changed: set[str],
) -> set[str]:
    field = "declaration.order_explicit"
    if (
        field in changed
        and node.kind == "sub_tl"
        and "order_explicit" not in node.declaration
        and target.declaration.get("order_explicit") is True
        and node.order == target.order
    ):
        return {field}
    return set()


def _equivalent_integration_owners(
    node: ManifestNode,
    target: ManifestNode,
    changed: set[str],
) -> set[str]:
    current = node.integration_contract
    accepted = target.integration_contract
    return {
        f"integration_contract.{name}"
        for name, default in _DEFAULT_INTEGRATION_OWNERS.items()
        if f"integration_contract.{name}" in changed
        and name not in current
        and accepted.get(name) == default
    }
