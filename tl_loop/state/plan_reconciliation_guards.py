"""Plan reconciliation support."""

from __future__ import annotations

from tl_loop.fsm.child import ChildRecord
from tl_loop.fsm.post_merge import PostMergePhase
from tl_loop.fsm.scope import TLRunning

from .plan_manifest import ManifestNode, PlanManifest
from .plan_reconciliation_models import ReconciliationScope
from .plan_reconciliation_ownership import _records_for
from .plan_reconciliation_report import (
    _persisted_stages,
    _safe_text,
    _stage_projection,
)
from .plan_revision_diff import changed_node_fields
from .plan_revision_types import PlanRevisionBlocker
from .schema import RunState, SliceStatus


def _scope_blockers(view: ReconciliationScope) -> list[PlanRevisionBlocker]:
    blockers: list[PlanRevisionBlocker] = []
    target = view.accepted
    if target is None:
        return [_blocker("accepted_scope_missing", view.current.scope_id)]
    if not view.parent_manifest_matches:
        blockers.append(_blocker("parent_manifest_mismatch", view.current.scope_id))
    if not view.run_identity_matches:
        blockers.append(_blocker("runstore_owner_mismatch", view.current.scope_id))
    if _scope_identity(view.current) != _scope_identity(target):
        blockers.append(_blocker("scope_identity_drift", view.current.scope_id))
    blockers.extend(_node_blockers(view, target))
    blockers.extend(_runtime_blockers(view, target))
    return blockers


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


def _node_blockers(
    view: ReconciliationScope,
    target: PlanManifest,
) -> list[PlanRevisionBlocker]:
    current_nodes = {node.node_id: node for node in view.current.nodes}
    target_nodes = {node.node_id: node for node in target.nodes}
    blockers = []
    for node_id in sorted(set(current_nodes) | set(target_nodes)):
        current, accepted = current_nodes.get(node_id), target_nodes.get(node_id)
        if current is None or accepted is None:
            blockers.append(_blocker("node_topology_drift", node_id, view.current.scope_id))
        elif changed_node_fields(current, accepted) and _protected_change_unproven(
            view, current, accepted
        ):
            blockers.append(_blocker("protected_identity_unproven", node_id, view.current.scope_id))
    return blockers


def _protected_change_unproven(
    view: ReconciliationScope,
    current: ManifestNode,
    accepted: ManifestNode,
) -> bool:
    fields = changed_node_fields(current, accepted)
    if fields == ("child_manifest_digest",) and _has_materialized_child(view, current.name):
        return False
    return _node_is_protected(view.state, current.name)


def _has_materialized_child(view: ReconciliationScope, name: str) -> bool:
    child = view.run_dir / name
    return not child.is_symlink() and (child / "run.json").is_file()


def _node_is_protected(state: RunState, node_name: str) -> bool:
    record = state.slices.get(node_name)
    if record is None or record.status is not SliceStatus.PENDING or record.attempts > 0:
        return True
    fields = (
        record.pr_number,
        record.dispatch_intent_id,
        record.dispatch_agent_id,
        record.dispatch_invocation_id,
        record.publication,
        record.handoff,
        record.action,
        record.recovery,
        record.suspended_dependency,
        record.review_evidence,
        record.verdict,
    )
    if any(value is not None for value in fields):
        return True
    if record.post_merge is not None and (
        record.post_merge.phase is not PostMergePhase.NOT_STARTED or record.post_merge.evidence
    ):
        return True
    phase = state.recursive_fsm
    return any(
        _record_has_evidence(item) for item in _records_for(phase) if item.child_id == node_name
    )


def _record_has_evidence(record: ChildRecord) -> bool:
    return any(
        value is not None
        for value in (record.dispatch_intent_id, record.invocation_id, record.lane_id)
    ) or bool(set(record.evidence) - {"manifest_node_id", "manifest_revision"})


def _runtime_blockers(
    view: ReconciliationScope,
    target: PlanManifest,
) -> list[PlanRevisionBlocker]:
    blockers: list[PlanRevisionBlocker] = []
    phase = view.state.recursive_fsm
    digest = getattr(phase, "plan_digest", None)
    if digest not in {view.current.digest, target.digest}:
        blockers.append(_blocker("runtime_digest_unproven", view.current.scope_id))
    blockers.extend(_record_blockers(view, target))
    persisted = _persisted_stages(view.state)
    expected = _stage_projection(target)
    if persisted and persisted != expected:
        blockers.append(_blocker("stage_projection_drift", view.current.scope_id))
    if isinstance(phase, TLRunning) and expected and not persisted:
        blockers.append(_blocker("stage_projection_missing", view.current.scope_id))
    return blockers


def _record_blockers(view: ReconciliationScope, target: PlanManifest) -> list[PlanRevisionBlocker]:
    target_nodes = {node.name: node for node in target.nodes}
    blockers = []
    for record in _records_for(view.state.recursive_fsm):
        node = target_nodes.get(record.child_id)
        if node is None or record.manifest_node_id != node.node_id:
            blockers.append(
                _blocker("runtime_record_identity_unproven", record.child_id, target.scope_id)
            )
        elif record.manifest_revision != target.manifest_revision:
            blockers.append(
                _blocker("runtime_record_revision_unproven", record.child_id, target.scope_id)
            )
        elif record.kind.value != node.kind:
            blockers.append(
                _blocker("runtime_record_kind_mismatch", record.child_id, target.scope_id)
            )
    return blockers


def _blocker(code: str, node_or_scope: str, scope_id: str | None = None) -> PlanRevisionBlocker:
    return PlanRevisionBlocker(
        code=code,
        reason=_blocker_reason(code),
        scope_id=_safe_text(scope_id or node_or_scope),
        node_id=_safe_text(node_or_scope) if scope_id else None,
    )


def _blocker_reason(code: str) -> str:
    return {
        "accepted_scope_missing": "accepted snapshot has no corresponding scope",
        "parent_manifest_mismatch": "materialized child does not match its parent declaration",
        "runstore_owner_mismatch": "RunStore owner or parent identity does not match its path",
        "scope_identity_drift": "scope ownership or ordered-stage identity differs from accepted plan",
        "node_topology_drift": "node addition or removal cannot be reconstructed safely",
        "scope_topology_drift": "child scope topology differs from accepted plan",
        "protected_identity_unproven": "changed node has execution history; accepted identity cannot be proven from dispatch evidence",
        "runtime_digest_unproven": "runtime digest matches neither the current nor accepted manifest",
        "runtime_record_identity_unproven": "runtime child record does not identify the accepted node",
        "runtime_record_revision_unproven": "runtime child record revision does not match the accepted scope revision",
        "runtime_record_kind_mismatch": "runtime child kind differs from the accepted declaration",
        "stage_projection_drift": "persisted stage projection differs from the accepted plan",
        "stage_projection_missing": "running scope lacks its persisted accepted-stage projection",
        "orphan_runstore": "materialized child RunStore has no declared sub-TL owner",
        "materialized_scope_missing": "executed sub-TL has no materialized child RunStore evidence",
    }.get(code, "plan reconciliation evidence is incomplete")
