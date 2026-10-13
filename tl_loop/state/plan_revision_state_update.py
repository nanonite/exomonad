"""Safe RunStore projection updates for an audited plan revision."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

from tl_loop.fsm.child import ChildKind, ChildRecord
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.scope import TLPlanning, TLRunning

from .plan_manifest import ManifestNode, PlanManifest
from .plan_revision_diff import changed_node_fields
from .plan_revision_state import read_checkpoint
from .plan_revision_types import PlanRevisionError
from .schema import IntegrationRuntimeState, SliceStatus, validate
from .serialization import dumps as dumps_json
from .store import _assert_consistent, _decode, _encode_fsm, _encode_integration


def revise_scope_checkpoint(
    raw: bytes,
    previous: PlanManifest,
    candidate: PlanManifest,
    path: Path,
) -> bytes:
    """Rebind pending runtime declarations while preserving execution evidence."""
    document = _decode_run_document(raw, path)
    state = read_checkpoint(path)
    if state.recursive_fsm is None:
        raise PlanRevisionError(
            "runtime_rebase_unsupported",
            f"RunStore {path} has no canonical recursive state to rebase",
        )
    _apply_scope_revision(document, state.recursive_fsm, previous, candidate, path)
    return _validate_and_encode(document, path)


def _decode_run_document(raw: bytes, path: Path) -> dict[str, object]:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PlanRevisionError("runstore_invalid", f"cannot decode RunStore {path}") from error
    if not isinstance(document, dict):
        raise PlanRevisionError("runstore_invalid", f"RunStore {path} must be an object")
    return document


def _apply_scope_revision(
    document: dict[str, object],
    phase: TLPlanning | TLRunning,
    previous: PlanManifest,
    candidate: PlanManifest,
    path: Path,
) -> None:
    slices = document.get("slices")
    if not isinstance(slices, dict):
        raise PlanRevisionError("runstore_invalid", f"RunStore {path} slices must be an object")
    slices = _revise_slices(slices, previous, candidate)
    revised_phase = _revise_phase(phase, previous, candidate, slices, path)
    document["slices"] = slices
    document["plan_manifest"] = candidate.to_document()
    document["fsm"] = _encode_fsm(revised_phase)
    _revise_ordered_state(document, candidate)
    document["revision"] = _next_run_revision(document, path)


def _validate_and_encode(document: dict[str, object], path: Path) -> bytes:
    try:
        validate(document)
        updated_state = _decode(document)
        _assert_consistent(path, updated_state)
    except Exception as error:
        raise PlanRevisionError(
            "runtime_rebase_invalid",
            f"revised RunStore {path} failed validation or consistency checks",
        ) from error
    return (dumps_json(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _revise_slices(
    slices: dict[str, object], previous: PlanManifest, manifest: PlanManifest
) -> dict[str, object]:
    revised = copy.deepcopy(slices)
    old_nodes = {node.node_id: node for node in previous.nodes}
    for node in manifest.nodes:
        raw = revised.get(node.name)
        old_node = old_nodes.get(node.node_id)
        record = _pending_slice(node) if raw is None else raw
        if not isinstance(record, dict):
            raise PlanRevisionError("runstore_invalid", f"slice {node.name!r} is malformed")
        if old_node is None:
            record["manifest_revision"] = manifest.manifest_revision
        else:
            _update_changed_declaration_fields(record, old_node, node)
            record["manifest_revision"] = manifest.manifest_revision
        record["manifest_node_id"] = node.node_id
        revised[node.name] = record
    return revised


def _update_changed_declaration_fields(
    record: dict[str, object], previous: ManifestNode, candidate: ManifestNode
) -> None:
    fields = set(changed_node_fields(previous, candidate))
    if "boundary" in fields:
        record["paths"] = list(candidate.boundary or (f"tl-loop/{candidate.name}",))
    if "depends_on" in fields:
        record["depends_on"] = list(candidate.depends_on)
    if "owned_branch" in fields:
        record["branch"] = candidate.owned_branch
    if "worktree" in fields:
        record["worktree"] = candidate.worktree
    if "agent_type" in fields:
        record["requested_harness"] = candidate.agent_type
    if "chainlink_issue_id" in fields:
        if candidate.chainlink_issue_id is None:
            record.pop("chainlink_issue_id", None)
        else:
            record["chainlink_issue_id"] = candidate.chainlink_issue_id


def _pending_slice(node: ManifestNode) -> dict[str, object]:
    return {
        "id": node.name,
        "status": SliceStatus.PENDING.value,
        "paths": list(node.boundary or (f"tl-loop/{node.name}",)),
        "depends_on": list(node.depends_on),
        "base_ref": None,
        "test_plan": ["controller"],
        "agent_type": None,
        "requested_harness": node.agent_type,
        "resolved_harness": None,
        "model": None,
        "branch": node.owned_branch,
        "worktree": node.worktree,
        "pr_number": None,
        "review_findings": {},
        "ci_state": {},
        "reviewer_attempt": {},
        "manifest_node_id": node.node_id,
        "manifest_revision": 1,
        **_optional_issue_binding(node),
    }


def _optional_issue_binding(node: ManifestNode) -> dict[str, int]:
    if node.chainlink_issue_id is None:
        return {}
    return {"chainlink_issue_id": node.chainlink_issue_id}


def _revise_phase(
    phase: object,
    previous: PlanManifest,
    candidate: PlanManifest,
    slices: dict[str, object],
    path: Path,
) -> TLPlanning | TLRunning:
    if isinstance(phase, TLPlanning):
        return _planning_phase(candidate, slices, phase.scope_path)
    if isinstance(phase, TLRunning):
        return _running_phase(phase, previous, candidate, slices, path)
    raise PlanRevisionError(
        "runtime_rebase_unsupported",
        f"RunStore {path} is in {type(phase).__name__}; only planning or running scopes can be revised",
    )


def _planning_phase(
    manifest: PlanManifest,
    slices: dict[str, object],
    scope_path: tuple[str, ...],
) -> TLPlanning:
    records = {
        node.node_id: _child_record(node, slices.get(node.name), manifest.manifest_revision)
        for node in manifest.nodes
    }
    return TLPlanning(
        ordered_children=tuple(
            (order, tuple(records[node_id] for node_id in node_ids))
            for order, node_ids in manifest.ordered_stages
        ),
        scope_path=scope_path,
        plan_digest=cast(str, manifest.digest),
        parallel_children=tuple(
            records[node.node_id] for node in manifest.nodes if node.kind in {"worker", "leaf"}
        ),
    )


def _running_phase(
    phase: TLRunning,
    previous: PlanManifest,
    candidate: PlanManifest,
    slices: dict[str, object],
    path: Path,
) -> TLRunning:
    completed = dict(phase.completed_children)
    _ensure_scope_order_preserved(phase, previous, candidate, completed, path)
    records = _running_records(phase, candidate, slices, completed)
    parallel_pending, pending_by_order = _pending_projections(
        phase, candidate, records, completed, path
    )
    return _make_running_phase(
        phase,
        previous,
        candidate,
        completed,
        records,
        parallel_pending,
        pending_by_order,
    )


def _make_running_phase(
    phase: TLRunning,
    previous: PlanManifest,
    candidate: PlanManifest,
    completed: dict[str, ChildRecord],
    records: dict[str, ChildRecord],
    parallel_pending: tuple[ChildRecord, ...],
    pending_by_order: dict[int, tuple[ChildRecord, ...]],
) -> TLRunning:
    completed_projection, post_merge, dispatch_intents, lane_bindings = _running_projections(
        phase, previous, candidate, completed, records
    )
    return TLRunning(
        current_order=phase.current_order,
        pending_by_order=pending_by_order,
        scope_path=phase.scope_path,
        plan_digest=cast(str, candidate.digest),
        parallel_pending=parallel_pending,
        completed_children=completed_projection,
        post_merge=post_merge,
        dispatch_intents=dispatch_intents,
        evidence=phase.evidence,
        lane_bindings=lane_bindings,
    )


def _running_projections(
    phase: TLRunning,
    previous: PlanManifest,
    candidate: PlanManifest,
    completed: dict[str, ChildRecord],
    records: dict[str, ChildRecord],
) -> tuple[
    dict[str, ChildRecord],
    dict[str, PostMergeState],
    dict[str, object],
    dict[str, object],
]:
    completed_projection = _completed_projection(candidate, records, completed)
    dispatch_intents, lane_bindings = _retained_running_maps(phase, candidate)
    return (
        completed_projection,
        _updated_post_merge(phase, previous, candidate),
        dispatch_intents,
        lane_bindings,
    )


def _completed_projection(
    candidate: PlanManifest,
    records: dict[str, ChildRecord],
    completed: dict[str, ChildRecord],
) -> dict[str, ChildRecord]:
    return {node.name: records[node.node_id] for node in candidate.nodes if node.name in completed}


def _retained_running_maps(
    phase: TLRunning,
    candidate: PlanManifest,
) -> tuple[dict[str, object], dict[str, object]]:
    names = {node.name for node in candidate.nodes}
    dispatch = {name: item for name, item in phase.dispatch_intents.items() if name in names}
    lanes = {name: item for name, item in phase.lane_bindings.items() if name in names}
    return dispatch, lanes


def _ensure_scope_order_preserved(
    phase: TLRunning,
    previous: PlanManifest,
    candidate: PlanManifest,
    completed: dict[str, ChildRecord],
    path: Path,
) -> None:
    old_nodes = {node.node_id: node for node in previous.nodes}
    new_nodes = {node.node_id: node for node in candidate.nodes}
    _ensure_not_reopening_past_work(phase, old_nodes, new_nodes, completed, path)


def _running_records(
    phase: TLRunning,
    candidate: PlanManifest,
    slices: dict[str, object],
    completed: dict[str, ChildRecord],
) -> dict[str, ChildRecord]:
    return {
        node.node_id: _rebound_record(
            node,
            slices.get(node.name),
            candidate.manifest_revision,
            completed.get(node.name) or _find_pending_record(phase, node.name),
        )
        for node in candidate.nodes
    }


def _pending_projections(
    phase: TLRunning,
    candidate: PlanManifest,
    records: dict[str, ChildRecord],
    completed: dict[str, ChildRecord],
    path: Path,
) -> tuple[tuple[ChildRecord, ...], dict[int, tuple[ChildRecord, ...]]]:
    parallel = _parallel_pending(phase, candidate, records, completed, path)
    ordered = _ordered_pending(phase, candidate, records, completed, path)
    return parallel, ordered


def _parallel_pending(
    phase: TLRunning,
    candidate: PlanManifest,
    records: dict[str, ChildRecord],
    completed: dict[str, ChildRecord],
    path: Path,
) -> tuple[ChildRecord, ...]:
    pending = tuple(
        records[node.node_id]
        for node in candidate.nodes
        if node.kind in {"worker", "leaf"} and node.name not in completed
    )
    if phase.current_order > 0 and pending:
        raise PlanRevisionError(
            "runtime_rebase_past_work",
            f"RunStore {path} would add parallel work after ordered execution began",
        )
    return pending


def _ordered_pending(
    phase: TLRunning,
    candidate: PlanManifest,
    records: dict[str, ChildRecord],
    completed: dict[str, ChildRecord],
    path: Path,
) -> dict[int, tuple[ChildRecord, ...]]:
    pending_by_order = {
        order: tuple(
            records[node_id] for node_id in node_ids if records[node_id].child_id not in completed
        )
        for order, node_ids in candidate.ordered_stages
    }
    pending_by_order = {order: nodes for order, nodes in pending_by_order.items() if nodes}
    if phase.current_order > 0 and phase.current_order not in pending_by_order:
        raise PlanRevisionError(
            "runtime_rebase_past_work",
            f"RunStore {path} no longer has pending work in current order {phase.current_order}",
        )
    return pending_by_order


def _ensure_not_reopening_past_work(
    phase: TLRunning,
    old_nodes: dict[str, ManifestNode],
    new_nodes: dict[str, ManifestNode],
    completed: dict[str, ChildRecord],
    path: Path,
) -> None:
    if phase.current_order == 0:
        return
    for node_id, node in new_nodes.items():
        blocker = _past_order_blocker(
            phase.current_order,
            node,
            old_nodes.get(node_id),
            completed,
            path,
        )
        if blocker is not None:
            raise blocker


def _past_order_blocker(
    current_order: int,
    node: ManifestNode,
    previous: ManifestNode | None,
    completed: dict[str, ChildRecord],
    path: Path,
) -> PlanRevisionError | None:
    if current_order == 0 or node.kind in {"worker", "leaf"} or node.name in completed:
        return None
    previous_order = previous.order if previous is not None else None
    if node.order is None or node.order < current_order:
        return PlanRevisionError(
            "runtime_rebase_past_work",
            f"RunStore {path} would add or move pending node {node.name!r} into a passed order",
        )
    if previous_order is not None and previous_order < current_order:
        return PlanRevisionError(
            "runtime_rebase_past_work",
            f"RunStore {path} would reopen pending node {node.name!r} from a passed order",
        )
    return None


def _updated_post_merge(
    phase: TLRunning,
    previous: PlanManifest,
    manifest: PlanManifest,
) -> dict[str, PostMergeState]:
    existing = dict(phase.post_merge)
    old_kinds = {node.name: node.kind for node in previous.nodes}
    nodes_by_name = {node.name: node for node in manifest.nodes}
    result: dict[str, PostMergeState] = {}
    for name, node in nodes_by_name.items():
        prior = existing.get(name)
        if prior is not None and old_kinds.get(name) == node.kind:
            result[name] = prior
        else:
            result[name] = PostMergeState(
                PostMergePhase.NOT_REQUIRED if node.kind == "worker" else PostMergePhase.NOT_STARTED
            )
    return result


def _find_pending_record(phase: TLRunning, child_id: str) -> ChildRecord | None:
    for record in (
        *phase.parallel_pending,
        *(item for group in phase.pending_by_order.values() for item in group),
    ):
        if record.child_id == child_id:
            return record
    return None


def _rebound_record(
    node: ManifestNode,
    slice_value: object,
    manifest_revision: int,
    previous: ChildRecord | None,
) -> ChildRecord:
    record = previous or _child_record(node, slice_value, manifest_revision)
    evidence = dict(record.evidence)
    evidence["manifest_node_id"] = node.node_id
    evidence["manifest_revision"] = str(manifest_revision)
    return replace(
        record,
        child_id=node.name,
        kind=ChildKind(node.kind),
        evidence=evidence,
        manifest_node_id=node.node_id,
        manifest_revision=manifest_revision,
    )


def _child_record(node: ManifestNode, slice_value: object, manifest_revision: int) -> ChildRecord:
    action = slice_value.get("action") if isinstance(slice_value, dict) else None
    intent_id = action.get("intent_id") if isinstance(action, dict) else None
    invocation_id = (
        slice_value.get("dispatch_invocation_id") if isinstance(slice_value, dict) else None
    )
    return ChildRecord(
        child_id=node.name,
        kind=ChildKind(node.kind),
        dispatch_intent_id=intent_id if isinstance(intent_id, str) else None,
        invocation_id=invocation_id if isinstance(invocation_id, str) else None,
        evidence={
            "manifest_node_id": node.node_id,
            "manifest_revision": str(manifest_revision),
        },
        manifest_node_id=node.node_id,
        manifest_revision=manifest_revision,
    )


def _revise_ordered_state(
    document: dict[str, object],
    candidate: PlanManifest,
) -> None:
    if candidate.ordered_stages or "ordered_stages" in document:
        document["ordered_stages"] = [
            {
                "order": order,
                "sub_tls": [candidate.node(node_id).name for node_id in node_ids],
            }
            for order, node_ids in candidate.ordered_stages
        ]
    if not candidate.ordered_stages and "ordered_stages" not in document:
        return
    integration = document.get("integration")
    if not isinstance(integration, dict):
        integration = _encode_integration(IntegrationRuntimeState())
        document["integration"] = integration
    states = integration.get("sub_tl_states")
    if not isinstance(states, dict):
        raise PlanRevisionError("runstore_invalid", "integration sub_tl_states must be an object")
    for node in candidate.nodes:
        if node.kind == "sub_tl":
            states.setdefault(node.name, "RUNNING")


def _next_run_revision(document: dict[str, object], path: Path) -> int:
    revision = document.get("revision")
    if type(revision) is not int or revision < 0:
        raise PlanRevisionError("runstore_invalid", f"RunStore {path} has an invalid revision")
    return revision + 1
