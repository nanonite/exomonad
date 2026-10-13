"""Protection rules for plan changes against persisted execution history."""

from __future__ import annotations

from collections.abc import Mapping

from tl_loop.fsm.child import ChildRecord
from tl_loop.fsm.scope import TLPlanning, TLRunning

from .plan_manifest import ManifestError, ManifestNode, PlanManifest, validate_manifest_revision
from .plan_revision_diff import changed_node_fields
from .plan_revision_types import PlanRevisionBlocker, ScopeState
from .schema import RunState, SliceState, SliceStatus


def collect_blockers(
    previous: PlanManifest,
    candidate: PlanManifest,
    scopes: Mapping[str, ScopeState],
) -> list[PlanRevisionBlocker]:
    """Find removals and identity edits that would rewrite protected history."""
    current = scopes.get(previous.scope_id)
    protected = _protected_node_ids(previous, current.state if current else None, scopes)
    blockers = _collect_node_blockers(previous, candidate, protected)
    _collect_child_scope_blockers(previous, candidate, scopes, blockers)
    _collect_runtime_rebase_blockers(previous, candidate, scopes, blockers)
    return blockers


def _collect_node_blockers(
    previous: PlanManifest,
    candidate: PlanManifest,
    protected: frozenset[str],
) -> list[PlanRevisionBlocker]:
    old_nodes = {node.node_id: node for node in previous.nodes}
    new_nodes = {node.node_id: node for node in candidate.nodes}
    blockers = []
    for node_id in sorted(set(old_nodes) | set(new_nodes)):
        blocker = _node_change_blocker(
            previous.scope_id,
            node_id,
            old_nodes.get(node_id),
            new_nodes.get(node_id),
            protected,
        )
        if blocker is not None:
            blockers.append(blocker)
    return blockers


def _node_change_blocker(
    scope_id: str,
    node_id: str,
    old: ManifestNode | None,
    new: ManifestNode | None,
    protected: frozenset[str],
) -> PlanRevisionBlocker | None:
    if old is not None and new is None:
        return PlanRevisionBlocker(
            "node_removal", "manifest revisions cannot remove declared nodes", scope_id, node_id
        )
    if old is None or new is None:
        return None
    if node_id in protected:
        blocker = _protected_mutation_blocker(scope_id, node_id, old, new)
        if blocker is not None:
            return blocker
    if old.kind != new.kind:
        return PlanRevisionBlocker(
            "node_kind_change",
            "a declared node cannot change between worker, leaf, and sub-TL roles",
            scope_id,
            node_id,
        )
    return None


def _protected_mutation_blocker(
    scope_id: str,
    node_id: str,
    old: ManifestNode,
    new: ManifestNode,
) -> PlanRevisionBlocker | None:
    fields = changed_node_fields(old, new)
    if not fields:
        return None
    return PlanRevisionBlocker(
        "protected_node_mutation",
        "protected node identity changed in fields: " + ", ".join(fields),
        scope_id,
        node_id,
    )


def _collect_runtime_rebase_blockers(
    previous: PlanManifest,
    candidate: PlanManifest,
    scopes: Mapping[str, ScopeState],
    blockers: list[PlanRevisionBlocker],
) -> None:
    current = scopes.get(previous.scope_id)
    _collect_scope_rebase_blockers(previous, candidate, current, blockers)
    for node_id, old_child in previous.child_manifests.items():
        new_child = candidate.child_manifests.get(node_id)
        if new_child is not None:
            _collect_runtime_rebase_blockers(old_child, new_child, scopes, blockers)


def _collect_scope_rebase_blockers(
    previous: PlanManifest,
    candidate: PlanManifest,
    current: ScopeState | None,
    blockers: list[PlanRevisionBlocker],
) -> None:
    if current is None or previous.digest == candidate.digest:
        return
    phase = current.state.recursive_fsm
    if not isinstance(phase, (TLPlanning, TLRunning)):
        blockers.append(_unsupported_phase_blocker(previous.scope_id))
        return
    if phase.plan_digest != previous.digest:
        blockers.append(_runtime_digest_blocker(previous.scope_id))
        return
    _collect_projection_blockers(previous, current, phase, blockers)
    if isinstance(phase, TLRunning):
        _collect_past_work_blockers(previous, candidate, phase, blockers)


def _unsupported_phase_blocker(scope_id: str) -> PlanRevisionBlocker:
    return PlanRevisionBlocker(
        "runtime_rebase_unsupported",
        "changed scope is not in a planning or running phase",
        scope_id,
    )


def _runtime_digest_blocker(scope_id: str) -> PlanRevisionBlocker:
    return PlanRevisionBlocker(
        "runtime_digest_drift",
        "recursive FSM digest does not match the current manifest",
        scope_id,
    )


def _collect_projection_blockers(
    manifest: PlanManifest,
    current: ScopeState,
    phase: TLPlanning | TLRunning,
    blockers: list[PlanRevisionBlocker],
) -> None:
    persisted = tuple((stage.order, stage.sub_tls) for stage in current.state.ordered_stages)
    _append_order_projection_blocker(
        manifest,
        persisted,
        isinstance(phase, TLRunning),
        blockers,
    )
    record_blocker = _runtime_record_blocker(manifest, phase)
    if record_blocker is not None:
        blockers.append(record_blocker)


def _append_order_projection_blocker(
    manifest: PlanManifest,
    persisted: tuple[tuple[int, tuple[str, ...]], ...],
    require_persisted: bool,
    blockers: list[PlanRevisionBlocker],
) -> None:
    if persisted and persisted != _manifest_ordered_stages(manifest):
        blockers.append(
            PlanRevisionBlocker(
                "runtime_order_drift",
                "persisted ordered stages do not match the current manifest",
                manifest.scope_id,
            )
        )
    if require_persisted and not persisted and manifest.ordered_stages:
        blockers.append(_missing_order_projection_blocker(manifest.scope_id))


def _missing_order_projection_blocker(scope_id: str) -> PlanRevisionBlocker:
    return PlanRevisionBlocker(
        "runtime_order_missing",
        "running scope is missing its persisted ordered-stage projection",
        scope_id,
    )


def _manifest_ordered_stages(manifest: PlanManifest) -> tuple[tuple[int, tuple[str, ...]], ...]:
    return tuple(
        (order, tuple(manifest.node(node_id).name for node_id in node_ids))
        for order, node_ids in manifest.ordered_stages
    )


def _collect_past_work_blockers(
    previous: PlanManifest,
    candidate: PlanManifest,
    phase: TLRunning,
    blockers: list[PlanRevisionBlocker],
) -> None:
    completed = set(phase.completed_children)
    for node in candidate.nodes:
        if node.name in completed:
            continue
        blocker = _past_work_blocker(previous.scope_id, node, phase.current_order)
        if blocker is not None:
            blockers.append(blocker)


def _past_work_blocker(
    scope_id: str,
    node: ManifestNode,
    current_order: int,
) -> PlanRevisionBlocker | None:
    if current_order <= 0:
        return None
    if node.kind in {"worker", "leaf"}:
        reason = "pending parallel work cannot be introduced after ordered execution begins"
    elif node.kind == "sub_tl" and (node.order is None or node.order < current_order):
        reason = "pending sub-TL work cannot be added to an already-passed order"
    else:
        return None
    return PlanRevisionBlocker(
        "runtime_rebase_past_work",
        reason,
        scope_id,
        node.node_id,
    )


def _collect_child_scope_blockers(
    previous: PlanManifest,
    candidate: PlanManifest,
    scopes: Mapping[str, ScopeState],
    blockers: list[PlanRevisionBlocker],
) -> None:
    for node_id, old_child in previous.child_manifests.items():
        new_child = candidate.child_manifests.get(node_id)
        if new_child is None:
            blockers.extend(_collect_removed_scope_blockers(old_child))
        else:
            blockers.extend(collect_blockers(old_child, new_child, scopes))


def _collect_removed_scope_blockers(manifest: PlanManifest) -> list[PlanRevisionBlocker]:
    blockers = [
        PlanRevisionBlocker(
            "node_removal",
            "manifest revisions cannot remove declared nodes",
            manifest.scope_id,
            node.node_id,
        )
        for node in manifest.nodes
    ]
    for child in manifest.child_manifests.values():
        blockers.extend(_collect_removed_scope_blockers(child))
    return blockers


def _protected_node_ids(
    manifest: PlanManifest,
    state: RunState | None,
    scopes: Mapping[str, ScopeState],
) -> frozenset[str]:
    if state is None:
        return frozenset()
    budgeted = {charge.slice_id for charge in state.budgets.charges}
    evidence_names = _recursive_evidence_names(state.recursive_fsm)
    protected: set[str] = set()
    for node in manifest.nodes:
        record = state.slices.get(node.name)
        if (
            record is None
            or node.name in budgeted
            or node.name in evidence_names
            or _slice_is_protected(record)
            or (node.kind == "sub_tl" and node.node_id in scopes)
        ):
            protected.add(node.node_id)
    return frozenset(protected)


def _recursive_evidence_names(recursive: object | None) -> frozenset[str]:
    if recursive is None:
        return frozenset()
    names: set[str] = set()
    for attribute in ("completed_children", "dispatch_intents", "evidence"):
        values = getattr(recursive, attribute, None)
        if isinstance(values, Mapping):
            names.update(str(name) for name in values)
    return frozenset(names)


def _slice_is_protected(record: SliceState) -> bool:
    if record.status not in {SliceStatus.PENDING, SliceStatus.READY}:
        return True
    return any(
        (
            _has_attempt_history(record),
            _has_dispatch_state(record),
            _has_lifecycle_evidence(record),
            _has_review_evidence(record),
        )
    )


def _has_attempt_history(record: SliceState) -> bool:
    return any(
        (
            record.attempts,
            record.repair_attempts,
            record.review_rounds,
            record.tl_intervention_attempts,
            record.dispatch_retry_attempt,
            record.dispatch_retry_for_attempt,
            record.dispatch_generation,
        )
    )


def _has_dispatch_state(record: SliceState) -> bool:
    return any(
        (
            record.pr_number,
            record.dispatch_intent_id,
            record.dispatch_started_at,
            record.dispatch_last_boundary,
            record.dispatch_error,
            record.dispatch_error_code,
            record.dispatch_next_attempt_at,
            record.dispatch_agent_id,
            record.dispatch_invocation_id,
            record.dispatch_authoritative_event_seq,
            record.dispatch_ledger_floor,
            record.reconciliation,
            record.resolved_harness,
            record.reviewer_agent_id,
            record.task_budget_reset_key,
            record.review_validation_failure_reason,
            record.blocked_by,
            record.stall_classification,
        )
    )


def _has_lifecycle_evidence(record: SliceState) -> bool:
    return any(
        (
            record.publication,
            record.handoff,
            record.action,
            record.post_merge,
            record.recovery,
            record.suspended_dependency,
            record.deadline_ledger,
            record.observation_provenance,
        )
    )


def _has_review_evidence(record: SliceState) -> bool:
    return any(
        (
            record.review_evidence,
            record.reviewed_head,
            record.verdict,
            record.verdict_at,
            record.review_validation_disposition,
            record.park_cause,
            record.park_issue_id,
            record.park_audit,
            record.review_findings,
            record.review_patch_digests,
            record.review_contract,
            record.ci_state,
            record.reviewer_attempt,
        )
    )


def check_manifest_revision_rules(
    previous: PlanManifest,
    candidate: PlanManifest,
    scopes: Mapping[str, ScopeState],
) -> list[PlanRevisionBlocker]:
    """Apply the manifest module's authoritative revision checks recursively."""
    if previous.digest == candidate.digest:
        return []
    current = scopes.get(previous.scope_id)
    protected = _protected_node_ids(previous, current.state if current else None, scopes)
    blockers = _manifest_revision_blockers(previous, candidate, protected)
    identity_blocker = _scope_identity_blocker(previous, candidate)
    if identity_blocker is not None:
        blockers.append(identity_blocker)
    for node_id, old_child in previous.child_manifests.items():
        new_child = candidate.child_manifests.get(node_id)
        if new_child is not None:
            blockers.extend(check_manifest_revision_rules(old_child, new_child, scopes))
    return blockers


def _manifest_revision_blockers(
    previous: PlanManifest,
    candidate: PlanManifest,
    protected: frozenset[str],
) -> list[PlanRevisionBlocker]:
    blockers: list[PlanRevisionBlocker] = []
    try:
        validate_manifest_revision(previous, candidate, protected_node_ids=protected)
    except ManifestError as error:
        blockers.append(
            PlanRevisionBlocker("manifest_revision_rejected", str(error), previous.scope_id)
        )
    return blockers


def _scope_identity_blocker(
    previous: PlanManifest,
    candidate: PlanManifest,
) -> PlanRevisionBlocker | None:
    if (
        previous.scope_id != candidate.scope_id
        or previous.parent_scope_id != candidate.parent_scope_id
        or previous.role != candidate.role
        or previous.owned_branch != candidate.owned_branch
        or previous.parent_integration_target != candidate.parent_integration_target
    ):
        return PlanRevisionBlocker(
            "scope_ownership_change",
            "scope identity and ownership cannot change",
            previous.scope_id,
        )
    return None


def _runtime_record_blocker(
    manifest: PlanManifest, phase: TLPlanning | TLRunning
) -> PlanRevisionBlocker | None:
    records = _phase_records(phase)
    expected = {node.name: node for node in manifest.nodes}
    by_name = {record.child_id: record for record in records}
    if set(by_name) != set(expected) or len(records) != len(expected):
        return PlanRevisionBlocker(
            "runtime_records_drift",
            "recursive FSM child records do not match the current manifest nodes",
            manifest.scope_id,
        )
    return _record_binding_blocker(manifest, expected, by_name)


def _record_binding_blocker(
    manifest: PlanManifest,
    expected: Mapping[str, ManifestNode],
    records: Mapping[str, ChildRecord],
) -> PlanRevisionBlocker | None:
    for name, record in records.items():
        node = expected[name]
        if _record_binding_matches(record, node, manifest.manifest_revision):
            continue
        return PlanRevisionBlocker(
            "runtime_records_drift",
            f"recursive FSM binding for node {node.node_id} does not match the current manifest",
            manifest.scope_id,
            node.node_id,
        )
    return None


def _record_binding_matches(record: ChildRecord, node: ManifestNode, revision: int) -> bool:
    return record.manifest_node_id == node.node_id and record.manifest_revision == revision


def _phase_records(phase: TLPlanning | TLRunning) -> tuple[ChildRecord, ...]:
    if isinstance(phase, TLPlanning):
        return (
            *phase.parallel_children,
            *(record for _, records in phase.ordered_children for record in records),
        )
    return (
        *phase.parallel_pending,
        *(record for records in phase.pending_by_order.values() for record in records),
        *phase.completed_children.values(),
    )
