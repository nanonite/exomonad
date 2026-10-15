"""Proven, field-limited compatibility for legacy manifest declarations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from tl_loop.fsm.child import ChildRecord
from tl_loop.state.plan_revision_diff import changed_node_fields

from .plan_compatibility import (
    AGGREGATE_REVIEW_OWNER_FIELD,
    _child_digest_is_derived,
    _same_legacy_schema,
    _snapshot_omits_aggregate_review_owner,
)
from .plan_manifest import ManifestNode, PlanManifest
from .plan_reconciliation_execution import _legacy_completion_receipt_matches
from .plan_reconciliation_ownership import _records_for
from .schema import RunState, SliceState

if TYPE_CHECKING:
    from .plan_reconciliation_models import ReconciliationScope

_DEFAULT_INTEGRATION_OWNERS = {
    "leaf_review_owner": "leaf",
    "aggregate_review_owner": "none",
    "aggregate_repair_owner": "aggregate",
}


def proven_legacy_default_fields(scope: ReconciliationScope) -> dict[str, tuple[str, ...]]:
    """Return only omissions proven equivalent by the accepted runtime identity.

    Schema-one manifests may omit fields written by newer serializers. Schema
    version alone grants no equivalence: the verified accepted snapshot,
    matching FSM digest, and durable dispatch record must bind the node to the
    exact accepted manifest.
    """
    current = scope.current
    accepted = scope.accepted
    state = scope.state
    if accepted is None or not _same_legacy_schema(current, accepted):
        return {}
    records = {record.child_id: record for record in _records_for(state.recursive_fsm)}
    accepted_nodes = {node.node_id: node for node in accepted.nodes}
    equivalent: dict[str, tuple[str, ...]] = {}
    for node in current.nodes:
        target = accepted_nodes.get(node.node_id)
        if target is None:
            continue
        changed = set(changed_node_fields(node, target))
        allowed: set[str] = set()
        if scope.compatibility_transition:
            if not _fsm_matches_current(state, current):
                continue
            allowed = _aggregate_review_default_fields(scope, node, target, changed, records)
        elif _fsm_matches_accepted(state, accepted):
            allowed.update(_equivalent_order_explicit(node, target, changed))
            allowed.update(_equivalent_integration_owners(node, target, changed))
            allowed.update(_equivalent_legacy_aggregate_review_phase(node, target, changed))
        if allowed:
            equivalent[node.node_id] = tuple(sorted(allowed))
    return equivalent


def _aggregate_review_default_fields(
    scope: ReconciliationScope,
    node: ManifestNode,
    target: ManifestNode,
    changed: set[str],
    records: Mapping[str, ChildRecord],
) -> set[str]:
    accepted = scope.accepted
    if accepted is None:
        return set()
    allowed: set[str] = set()
    if (
        AGGREGATE_REVIEW_OWNER_FIELD in changed
        and _snapshot_omits_aggregate_review_owner(scope.accepted_plan or {}, node)
        and _node_execution_proves_default(scope, node, target, records)
    ):
        allowed.add(AGGREGATE_REVIEW_OWNER_FIELD)
    if (
        "child_manifest_digest" in changed
        and _child_digest_is_derived(scope.current, accepted, node, target)
        and _node_execution_proves_default(scope, node, target, records)
    ):
        allowed.add("child_manifest_digest")
    return allowed


def _node_execution_proves_default(
    scope: ReconciliationScope,
    node: ManifestNode,
    target: ManifestNode,
    records: Mapping[str, ChildRecord],
) -> bool:
    if node.kind == "sub_tl":
        record = records.get(node.name)
        slice_state = scope.state.slices.get(node.name)
        if (
            slice_state is not None
            and slice_state.status.value == "pending"
            and slice_state.attempts == 0
            and slice_state.dispatch_intent_id is None
            and slice_state.dispatch_invocation_id is None
            and record is None
        ):
            return (
                slice_state.manifest_node_id == node.node_id
                and slice_state.manifest_revision == scope.current.manifest_revision
                and slice_state.branch == node.owned_branch
            )
        return bool(
            _has_accepted_dispatch(record, node, scope.current.manifest_revision)
            and slice_state is not None
            and slice_state.manifest_node_id == node.node_id
            and slice_state.manifest_revision == scope.current.manifest_revision
            and slice_state.dispatch_intent_id == record.dispatch_intent_id
            and slice_state.dispatch_invocation_id == record.invocation_id
            and slice_state.branch == node.owned_branch
        )
    if node.kind == "leaf":
        slice_state = scope.state.slices.get(node.name)
        if slice_state is None:
            return False
        if slice_state.status.value == "pending":
            return (
                slice_state.attempts == 0
                and slice_state.dispatch_intent_id is None
                and slice_state.dispatch_invocation_id is None
                and slice_state.manifest_node_id == node.node_id
                and slice_state.manifest_revision == scope.current.manifest_revision
            )
        if slice_state.status.value != "merged":
            return False
        return bool(
            slice_state.manifest_node_id == node.node_id
            and slice_state.manifest_revision == scope.current.manifest_revision
            and _legacy_completion_receipt_matches(scope, node, slice_state)
            and _completed_issue_binding_matches(scope, target, slice_state)
        )
    return False


def _completed_issue_binding_matches(
    scope: ReconciliationScope, target: ManifestNode, slice_state: SliceState
) -> bool:
    if target.chainlink_issue_id is None:
        return True
    post_merge = slice_state.post_merge
    evidence = post_merge.evidence if post_merge is not None else {}
    return (
        evidence.get("issue_id") == str(target.chainlink_issue_id)
        and bool(evidence.get("issue_close_intent_id"))
        and bool(evidence.get("issue_close_journal_id"))
    )


def _fsm_matches_accepted(state: RunState, accepted: PlanManifest) -> bool:
    return getattr(state.recursive_fsm, "plan_digest", None) == accepted.digest


def _fsm_matches_current(state: RunState, current: PlanManifest) -> bool:
    return getattr(state.recursive_fsm, "plan_digest", None) == current.digest


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


def _equivalent_legacy_aggregate_review_phase(
    node: ManifestNode,
    target: ManifestNode,
    changed: set[str],
) -> set[str]:
    """Bind the schema-one aggregate-review default to the leaf-only contract."""
    field = "integration_contract.aggregate_review_owner"
    if (
        field in changed
        and node.kind == "sub_tl"
        and node.integration_contract.get("aggregate_review_owner") == "none"
        and target.integration_contract.get("aggregate_review_owner") == "aggregate"
    ):
        return {field}
    return set()
