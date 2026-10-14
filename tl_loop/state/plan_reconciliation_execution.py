"""Execution evidence for narrow restoration of completed declarations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tl_loop.fsm.child import ChildRecord
from tl_loop.fsm.post_merge import PostMergePhase
from tl_loop.loop.journal import stable_action_key
from tl_loop.state.plan_revision_diff import changed_node_fields

from .plan_manifest import ManifestNode
from .plan_reconciliation_ownership import _records_for
from .schema import SliceState, SliceStatus

if TYPE_CHECKING:
    from .plan_reconciliation_models import ReconciliationScope

_RESTORABLE_FIELDS = frozenset({"task", "chainlink_issue_id"})


@dataclass(frozen=True)
class ProtectedDeclarationProof:
    """Decision and bounded evidence for one completed-node restoration."""

    node_id: str
    fields: tuple[str, ...]
    proven: bool
    reason_code: str
    evidence: tuple[str, ...] = ()


def evaluate_protected_declaration_restore(
    scope: ReconciliationScope,
    current: ManifestNode,
    accepted: ManifestNode,
) -> ProtectedDeclarationProof | None:
    """Prove only task and issue-ID restoration from the original dispatch."""
    fields = changed_node_fields(current, accepted)
    if (
        current.kind != "leaf"
        or accepted.kind != "leaf"
        or not fields
        or not set(fields) <= _RESTORABLE_FIELDS
    ):
        return None
    slice_state = scope.state.slices.get(current.name)
    if slice_state is None or slice_state.status is not SliceStatus.MERGED:
        return None
    if not _fsm_matches_accepted(scope):
        return _blocked(current, fields, "accepted_fsm_digest_mismatch")
    if not _slice_binds_declaration(scope, slice_state, current):
        return _blocked(current, fields, "slice_dispatch_identity_mismatch")
    child_record = _dispatch_record(scope, current, slice_state.dispatch_intent_id)
    if child_record is None:
        return _blocked(current, fields, "runtime_dispatch_identity_mismatch")
    entry, reason = _confirmed_spawn_entry(scope, current, accepted, slice_state.dispatch_intent_id)
    if entry is None:
        return _blocked(current, fields, reason)
    if not _issue_binding_matches(current, accepted, slice_state, entry):
        return _blocked(current, fields, "issue_binding_evidence_mismatch")
    evidence = [
        "accepted_snapshot_manifest_digest",
        "matching_fsm_plan_digest",
        "slice_and_recursive_dispatch_identity",
        "confirmed_spawn_action_with_accepted_task",
    ]
    if current.chainlink_issue_id != accepted.chainlink_issue_id:
        evidence.append("completed_issue_closure_receipt")
    return ProtectedDeclarationProof(
        current.node_id,
        fields,
        True,
        "proven",
        tuple(evidence),
    )


def _fsm_matches_accepted(scope: ReconciliationScope) -> bool:
    return (
        scope.accepted is not None
        and getattr(scope.state.recursive_fsm, "plan_digest", None) == scope.accepted.digest
    )


def _slice_binds_declaration(
    scope: ReconciliationScope,
    record: SliceState | None,
    node: ManifestNode,
) -> bool:
    return (
        record is not None
        and record.status is SliceStatus.MERGED
        and record.attempts > 0
        and record.manifest_node_id == node.node_id
        and record.manifest_revision == scope.current.manifest_revision
        and bool(record.dispatch_intent_id)
        and bool(record.dispatch_agent_id)
        and bool(record.dispatch_invocation_id)
        and record.dispatch_authoritative_event_seq is not None
    )


def _dispatch_record(
    scope: ReconciliationScope,
    node: ManifestNode,
    intent_id: str,
) -> ChildRecord | None:
    matches = [
        record for record in _records_for(scope.state.recursive_fsm) if record.child_id == node.name
    ]
    if len(matches) != 1:
        return None
    record = matches[0]
    slice_state = scope.state.slices[node.name]
    if (
        record.kind.value != node.kind
        or record.manifest_node_id != node.node_id
        or record.manifest_revision != scope.current.manifest_revision
        or record.dispatch_intent_id != intent_id
        or record.invocation_id != slice_state.dispatch_invocation_id
    ):
        return None
    return record


def _confirmed_spawn_entry(
    scope: ReconciliationScope,
    current: ManifestNode,
    accepted: ManifestNode,
    intent_id: str,
) -> tuple[Mapping[str, object] | None, str]:
    if scope.action_journal_status == "invalid":
        return None, "action_journal_invalid"
    if scope.action_journal_status != "valid" or not scope.action_journal_entries:
        return None, "action_journal_missing"
    correlated = [
        entry
        for entry in scope.action_journal_entries
        if _entry_targets_dispatch(entry, scope.state.run_id, current.name, intent_id)
    ]
    if not correlated:
        return None, "spawn_action_missing"
    if len(correlated) != 1:
        return None, "spawn_action_ambiguous"
    entry = correlated[0]
    arguments = _arguments(entry)
    if entry.get("status") != "confirmed" or not _confirmed_result(entry):
        return None, "spawn_action_unconfirmed"
    if arguments.get("name") != accepted.name or arguments.get("task") != accepted.task:
        return None, "spawn_declaration_mismatch"
    boundary = arguments.get("boundary", [])
    if boundary != list(accepted.boundary):
        return None, "spawn_declaration_mismatch"
    return entry, "proven"


def _entry_targets_dispatch(
    entry: Mapping[str, object], run_id: str, slice_id: str, intent_id: str
) -> bool:
    arguments = _arguments(entry)
    return (
        entry.get("run_id") == run_id
        and entry.get("operation") == "spawn_leaf"
        and entry.get("target") == slice_id
        and arguments.get("intent_id") == intent_id
        and entry.get("key") == stable_action_key(run_id, "spawn_leaf", slice_id, arguments)
    )


def _arguments(entry: Mapping[str, object]) -> Mapping[str, object]:
    value = entry.get("arguments")
    return value if isinstance(value, Mapping) else {}


def _confirmed_result(entry: Mapping[str, object]) -> bool:
    result = entry.get("result")
    return isinstance(result, Mapping) and result.get("success") is True


def _issue_binding_matches(
    current: ManifestNode,
    accepted: ManifestNode,
    slice_state: object,
    entry: Mapping[str, object],
) -> bool:
    arguments = _arguments(entry)
    if (
        "chainlink_issue_id" in arguments
        and arguments.get("chainlink_issue_id") != accepted.chainlink_issue_id
    ):
        return False
    if current.chainlink_issue_id == accepted.chainlink_issue_id:
        return True
    if (
        accepted.chainlink_issue_id is not None
        or current.chainlink_issue_id is None
        or getattr(slice_state, "chainlink_issue_id", None) != current.chainlink_issue_id
    ):
        return False
    post_merge = getattr(slice_state, "post_merge", None)
    evidence = post_merge.evidence if post_merge is not None else {}
    return (
        post_merge is not None
        and post_merge.phase is PostMergePhase.COMPLETE
        and evidence.get("issue_id") == str(current.chainlink_issue_id)
        and bool(evidence.get("issue_close_intent_id"))
        and bool(evidence.get("issue_close_journal_id"))
        and evidence.get("pr_number") == str(getattr(slice_state, "pr_number", None))
        and bool(evidence.get("merge_journal_id"))
    )


def _blocked(
    node: ManifestNode,
    fields: tuple[str, ...],
    reason_code: str,
) -> ProtectedDeclarationProof:
    return ProtectedDeclarationProof(node.node_id, fields, False, reason_code)
