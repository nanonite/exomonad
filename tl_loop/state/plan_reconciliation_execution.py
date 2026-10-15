"""Execution evidence for narrow restoration of completed declarations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tl_loop.fsm.child import ChildRecord
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.scope import TLAllMerged, TLFinalizing, TLPRFiled
from tl_loop.loop.journal import stable_action_key
from tl_loop.state.plan_revision_diff import changed_node_fields

from .plan_manifest import ManifestNode
from .plan_reconciliation_handoff import terminal_aggregate_handoff_matches
from .plan_reconciliation_legacy_dispatch import legacy_leaf_spawn_journal_matches
from .plan_reconciliation_ownership import _records_for
from .plan_reconciliation_report import _safe_text
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
    correlation: tuple[tuple[str, object], ...] = ()


def evaluate_protected_declaration_restore(
    scope: ReconciliationScope,
    current: ManifestNode,
    accepted: ManifestNode,
    *,
    fields: tuple[str, ...] | None = None,
) -> ProtectedDeclarationProof | None:
    """Prove only task and issue-ID restoration from the original dispatch."""
    fields = fields or changed_node_fields(current, accepted)
    if not _restorable_declaration_fields(current, accepted, fields):
        return None
    slice_state = scope.state.slices.get(current.name)
    if slice_state is None or slice_state.status is not SliceStatus.MERGED:
        return None
    reason = _declaration_restore_blocker(scope, current, slice_state)
    if reason is not None:
        return _blocked(
            current,
            fields,
            reason,
            _scope_emitter_failure_correlation(scope, slice_state, reason),
        )
    entry, reason = _confirmed_spawn_entry(
        scope, current, accepted, slice_state, slice_state.dispatch_intent_id
    )
    if entry is None:
        return _blocked(
            current,
            fields,
            reason,
            _scope_emitter_failure_correlation(scope, slice_state, reason),
        )
    if not _issue_binding_matches(current, accepted, slice_state, entry):
        return _blocked(current, fields, "issue_binding_evidence_mismatch")
    return _proven_declaration(scope, current, accepted, fields, slice_state, entry)


def _restorable_declaration_fields(
    current: ManifestNode,
    accepted: ManifestNode,
    fields: tuple[str, ...],
) -> bool:
    return (
        current.kind == "leaf"
        and accepted.kind == "leaf"
        and bool(fields)
        and set(fields) <= _RESTORABLE_FIELDS
    )


def _declaration_restore_blocker(
    scope: ReconciliationScope,
    node: ManifestNode,
    slice_state: SliceState,
) -> str | None:
    if slice_state.dispatch_invocation_id is None:
        proof = scope.scope_emitter_proof
        if proof is None or proof.agent_id is None:
            return (
                proof.reason_code if proof is not None else "scope_controller_identity_unavailable"
            )
    if not _fsm_matches_accepted(scope):
        return "accepted_fsm_digest_mismatch"
    if not _slice_binds_declaration(scope, slice_state, node):
        return _slice_identity_failure(slice_state)
    if not _dispatch_identity_is_durable(scope, node, slice_state, slice_state.dispatch_intent_id):
        return "runtime_dispatch_identity_mismatch"
    return None


def _slice_identity_failure(slice_state: SliceState) -> str:
    if slice_state.dispatch_invocation_id is None:
        return "legacy_dispatch_completion_unproven"
    return "slice_dispatch_identity_mismatch"


def _proven_declaration(
    scope: ReconciliationScope,
    current: ManifestNode,
    accepted: ManifestNode,
    fields: tuple[str, ...],
    slice_state: SliceState,
    entry: Mapping[str, object],
) -> ProtectedDeclarationProof:
    evidence = _declaration_evidence(scope, current, accepted, slice_state)
    correlation = (
        _legacy_correlation(scope, current, accepted, slice_state, entry)
        if slice_state.dispatch_invocation_id is None
        else ()
    )
    return ProtectedDeclarationProof(current.node_id, fields, True, "proven", evidence, correlation)


def _declaration_evidence(
    scope: ReconciliationScope,
    current: ManifestNode,
    accepted: ManifestNode,
    slice_state: SliceState,
) -> tuple[str, ...]:
    evidence = [
        "accepted_snapshot_manifest_digest",
        "matching_fsm_plan_digest",
        "slice_dispatch_identity",
        _completion_evidence_source(scope, current.name),
        "confirmed_spawn_action_with_accepted_task",
    ]
    if slice_state.dispatch_invocation_id is None:
        evidence.extend(
            (
                "proven_scope_controller_identity",
                "authoritative_dispatch_event_matches_owner_and_branch",
                "confirmed_spawn_result_records_actual_worktree",
                "completed_pr_receipt_matches_leaf_and_base",
                "missing_dispatch_invocation_preserved_as_unknown",
            )
        )
    if current.chainlink_issue_id != accepted.chainlink_issue_id:
        evidence.append("completed_issue_closure_receipt")
    return tuple(evidence)


def _completion_evidence_source(scope: ReconciliationScope, slice_id: str) -> str:
    if any(record.child_id == slice_id for record in _records_for(scope.state.recursive_fsm)):
        return "recursive_child_dispatch_record"
    return "all_merged_completion_receipt"


def _fsm_matches_accepted(scope: ReconciliationScope) -> bool:
    digest = getattr(scope.state.recursive_fsm, "plan_digest", None)
    if scope.compatibility_transition and digest == scope.current.digest:
        return True
    return scope.accepted is not None and digest == scope.accepted.digest


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
        and record.dispatch_authoritative_event_seq is not None
        and (
            bool(record.dispatch_invocation_id)
            or _legacy_completion_receipt_matches(scope, node, record)
        )
    )


def _dispatch_identity_is_durable(
    scope: ReconciliationScope,
    node: ManifestNode,
    slice_state: SliceState,
    intent_id: str,
) -> bool:
    matches = [
        record for record in _records_for(scope.state.recursive_fsm) if record.child_id == node.name
    ]
    if len(matches) == 1:
        return _dispatch_record_matches(scope, node, slice_state, matches[0], intent_id)
    if matches:
        return False
    if isinstance(scope.state.recursive_fsm, (TLAllMerged, TLFinalizing, TLPRFiled)):
        return _legacy_completion_receipt_matches(scope, node, slice_state)
    return False


def _dispatch_record_matches(
    scope: ReconciliationScope,
    node: ManifestNode,
    slice_state: SliceState,
    record: ChildRecord,
    intent_id: str,
) -> bool:
    return (
        record.kind.value == node.kind
        and record.manifest_node_id == node.node_id
        and record.manifest_revision == scope.current.manifest_revision
        and record.dispatch_intent_id == intent_id
        and record.invocation_id == slice_state.dispatch_invocation_id
    )


def _confirmed_spawn_entry(
    scope: ReconciliationScope,
    current: ManifestNode,
    accepted: ManifestNode,
    slice_state: SliceState,
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
    reason = _spawn_entry_failure(scope, current, accepted, slice_state, entry)
    return (None, reason) if reason else (entry, "proven")


def _spawn_entry_failure(
    scope: ReconciliationScope,
    current: ManifestNode,
    accepted: ManifestNode,
    slice_state: SliceState,
    entry: Mapping[str, object],
) -> str | None:
    arguments = _arguments(entry)
    if entry.get("status") != "confirmed" or not _confirmed_result(entry):
        return "spawn_action_unconfirmed"
    matches, reason = _spawn_result_matches_slice(scope, current, slice_state, entry)
    if not matches:
        return reason
    if arguments.get("name") != accepted.name or arguments.get("task") != accepted.task:
        return "spawn_declaration_mismatch"
    boundary = arguments.get("boundary", [])
    if boundary != list(accepted.boundary):
        return "spawn_declaration_mismatch"
    return None


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


def _spawn_result_matches_slice(
    scope: ReconciliationScope,
    node: ManifestNode,
    slice_state: SliceState,
    entry: Mapping[str, object],
) -> tuple[bool, str]:
    outcome = entry.get("result")
    if not isinstance(outcome, Mapping):
        return False, "spawn_result_identity_mismatch"
    result = outcome.get("result")
    if not isinstance(result, Mapping):
        return False, "spawn_result_identity_mismatch"
    if (
        slice_state.dispatch_invocation_id is not None
        and result.get("agent_id") == slice_state.dispatch_agent_id
        and result.get("invocation_id") == slice_state.dispatch_invocation_id
    ):
        return True, "proven"
    legacy, reason = _legacy_spawn_result_matches(scope, node, slice_state, entry)
    return (True, "proven") if legacy else (False, reason)


def _legacy_spawn_result_matches(
    scope: ReconciliationScope,
    node: ManifestNode,
    slice_state: SliceState,
    entry: Mapping[str, object],
) -> tuple[bool, str]:
    result = _spawn_result(entry)
    if not isinstance(result, Mapping):
        return False, "spawn_result_identity_mismatch"
    reason = _legacy_spawn_result_metadata_failure(slice_state, result)
    if reason is not None:
        return False, reason
    event, reason = _legacy_dispatch_event(scope, slice_state)
    if event is None:
        return False, reason
    proof = scope.scope_emitter_proof
    if proof is None or proof.agent_id is None:
        return False, (
            proof.reason_code if proof is not None else "scope_controller_identity_unavailable"
        )
    if event.get("agent_id") != proof.agent_id:
        return False, "legacy_scope_emitter_mismatch"
    if not _legacy_event_matches_slice(
        scope,
        slice_state,
        event,
        proven_scope_emitter=proof.agent_id,
    ):
        return False, "legacy_dispatch_coordinates_mismatch"
    if not _legacy_result_coordinates_match(result, slice_state):
        return False, "legacy_dispatch_coordinates_mismatch"
    if not _legacy_completion_receipt_matches(scope, node, slice_state):
        return False, "legacy_dispatch_completion_mismatch"
    return True, "proven"


def _legacy_spawn_result_metadata_failure(
    slice_state: SliceState,
    result: Mapping[str, object],
) -> str | None:
    if slice_state.dispatch_invocation_id is not None:
        return "spawn_result_identity_mismatch"
    if result.get("agent_id") is not None or result.get("invocation_id") is not None:
        return "legacy_dispatch_result_identity_conflict"
    worktree = result.get("worktree_path")
    if not isinstance(worktree, str) or not worktree or not Path(worktree).is_absolute():
        return "legacy_spawn_result_worktree_missing"
    return None


def _spawn_result(entry: Mapping[str, object]) -> Mapping[str, object] | None:
    outcome = entry.get("result")
    result = outcome.get("result") if isinstance(outcome, Mapping) else None
    return result if isinstance(result, Mapping) else None


def _legacy_dispatch_event(
    scope: ReconciliationScope,
    slice_state: SliceState,
) -> tuple[Mapping[str, object] | None, str]:
    sequence = slice_state.dispatch_authoritative_event_seq
    if type(sequence) is not int:
        return None, "legacy_dispatch_event_sequence_missing"
    status = scope.dispatch_event_status.get(sequence, "ledger_missing")
    if status != "found":
        return None, f"legacy_dispatch_event_{status}"
    events = scope.dispatch_event_records.get(sequence, ())
    return (events[0], "proven") if len(events) == 1 else (None, "legacy_dispatch_event_ambiguous")


def _legacy_event_matches_slice(
    scope: ReconciliationScope,
    slice_state: SliceState,
    event: Mapping[str, object],
    *,
    proven_scope_emitter: str,
) -> bool:
    data = event.get("data")
    if not isinstance(data, Mapping):
        return False
    sequence = slice_state.dispatch_authoritative_event_seq
    branch = data.get("branch", data.get("branch_name"))
    return (
        event.get("type", event.get("event_type")) == "agent.spawned"
        and event.get("run_id") == scope.state.ledger_run_id
        and event.get("run_seq") == sequence
        and event.get("agent_id") == proven_scope_emitter
        and event.get("invocation_id") is None
        and data.get("intent_id") == slice_state.dispatch_intent_id
        and data.get("child_agent") == slice_state.dispatch_agent_id
        and isinstance(slice_state.branch, str)
        and bool(slice_state.branch)
        and branch == slice_state.branch
    )


def _legacy_result_coordinates_match(
    result: Mapping[str, object],
    slice_state: SliceState,
) -> bool:
    worktree = result.get("worktree_path")
    return (
        isinstance(slice_state.branch, str)
        and bool(slice_state.branch)
        and result.get("branch_name") == slice_state.branch
        and isinstance(worktree, str)
        and bool(worktree)
        and Path(worktree).is_absolute()
    )


def _legacy_completion_receipt_matches(
    scope: ReconciliationScope,
    node: ManifestNode,
    slice_state: SliceState,
) -> bool:
    completed = _completed_leaf_receipt(scope, node, slice_state)
    return (
        completed is not None
        and slice_state.post_merge == completed
        and _completion_receipt_coordinates_match(node, slice_state, completed)
    )


def _completed_leaf_receipt(
    scope: ReconciliationScope,
    node: ManifestNode,
    slice_state: SliceState,
) -> PostMergeState | None:
    """Read completion from phase-owned receipts without rewriting history.

    TLAllMerged carries its receipt map. Non-root TLFinalizing and TLPRFiled
    carry an aggregate handoff instead, so they require the unchanged slice
    receipt plus parent, publication, journal, and ledger corroboration. Root
    TLDone and running, failed, or parked scopes do not qualify here.
    """
    phase = scope.state.recursive_fsm
    if isinstance(phase, TLAllMerged):
        completed = phase.completed_children.get(node.name)
        return _complete_receipt(completed)
    if isinstance(phase, (TLFinalizing, TLPRFiled)):
        receipt = slice_state.post_merge
        if (
            receipt is not None
            and receipt.phase is PostMergePhase.COMPLETE
            and terminal_aggregate_handoff_matches(scope, phase)
            and legacy_leaf_spawn_journal_matches(scope, node, slice_state)
        ):
            return receipt
    return None


def _complete_receipt(receipt: PostMergeState | None) -> PostMergeState | None:
    return receipt if receipt is not None and receipt.phase is PostMergePhase.COMPLETE else None


def _completion_receipt_coordinates_match(
    node: ManifestNode,
    slice_state: SliceState,
    completed: PostMergeState,
) -> bool:
    if slice_state.pr_number is None:
        return False
    evidence = completed.evidence
    pr_number = str(slice_state.pr_number)
    head_sha = evidence.get("head_sha")
    return (
        evidence.get("child_id") == node.name
        and evidence.get("pr_number") == pr_number
        and evidence.get("forgejo_pr_number") == pr_number
        and evidence.get("forgejo_merged") == "true"
        and evidence.get("parent_branch") == slice_state.base_ref
        and _is_commit_sha(head_sha)
        and evidence.get("forgejo_head_sha") == head_sha
        and bool(evidence.get("merge_journal_id"))
    )


def _is_commit_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


def _legacy_correlation(
    scope: ReconciliationScope,
    node: ManifestNode,
    accepted: ManifestNode,
    slice_state: SliceState,
    entry: Mapping[str, object],
) -> tuple[tuple[str, object], ...]:
    values = _legacy_dispatch_correlation(scope, node, accepted, slice_state, entry)
    values.update(_legacy_publication_correlation(slice_state))
    proof = scope.scope_emitter_proof
    if proof is not None:
        values.update(dict(proof.correlation))
        events = scope.dispatch_event_records.get(slice_state.dispatch_authoritative_event_seq, ())
        observed = events[0].get("agent_id") if len(events) == 1 else None
        values["dispatch_event_expected_emitter_agent_id"] = _safe_text(proof.agent_id)
        values["dispatch_event_observed_emitter_agent_id"] = (
            _safe_text(observed) if isinstance(observed, str) else ""
        )
    return tuple(sorted(values.items()))


def _legacy_dispatch_correlation(
    scope: ReconciliationScope,
    node: ManifestNode,
    accepted: ManifestNode,
    slice_state: SliceState,
    entry: Mapping[str, object],
) -> dict[str, object]:
    values = _legacy_dispatch_identity_fields(scope, node, accepted, slice_state, entry)
    values.update(_legacy_worktree_fields(slice_state, entry))
    return values


def _legacy_dispatch_identity_fields(
    scope: ReconciliationScope,
    node: ManifestNode,
    accepted: ManifestNode,
    slice_state: SliceState,
    entry: Mapping[str, object],
) -> dict[str, object]:
    return {
        **_legacy_run_scope_fields(scope, node, slice_state),
        **_legacy_invocation_fields(scope, slice_state, entry),
        **_legacy_declaration_fields(accepted),
    }


def _legacy_run_scope_fields(
    scope: ReconciliationScope,
    node: ManifestNode,
    slice_state: SliceState,
) -> dict[str, object]:
    return {
        "ledger_run_id": _safe_text(scope.state.ledger_run_id),
        "scope_run_id": _safe_text(scope.state.run_id),
        "scope_id": _safe_text(scope.current.scope_id),
        "slice_id": _safe_text(node.name),
        "dispatch_owner_agent_id": _safe_text(slice_state.dispatch_agent_id),
        "branch": _safe_text(slice_state.branch),
        "base_branch": _safe_text(slice_state.base_ref),
    }


def _legacy_invocation_fields(
    scope: ReconciliationScope,
    slice_state: SliceState,
    entry: Mapping[str, object],
) -> dict[str, object]:
    sequence = slice_state.dispatch_authoritative_event_seq
    events = scope.dispatch_event_records.get(sequence, ()) if type(sequence) is int else ()
    event = events[0] if len(events) == 1 else {}
    return {
        "dispatch_intent_id": _safe_text(slice_state.dispatch_intent_id),
        "dispatch_invocation_id": None,
        "dispatch_invocation_status": "unavailable_in_legacy_records",
        "dispatch_event_seq": slice_state.dispatch_authoritative_event_seq,
        "dispatch_event_sha256": _json_digest(event),
        "dispatch_event_emitter_agent_id": _safe_text(event.get("agent_id")),
        "spawn_action_key": _safe_text(entry.get("key")),
        "spawn_result_agent_id": None,
        "spawn_result_invocation_id": None,
    }


def _legacy_declaration_fields(accepted: ManifestNode) -> dict[str, str]:
    return {
        "accepted_task_sha256": _text_digest(accepted.task),
        "accepted_boundary_sha256": _json_digest(list(accepted.boundary)),
    }


def _legacy_worktree_fields(
    slice_state: SliceState,
    entry: Mapping[str, object],
) -> dict[str, object]:
    result = _spawn_result(entry)
    spawn_worktree = result.get("worktree_path") if result is not None else None
    spawn_path = spawn_worktree if isinstance(spawn_worktree, str) else ""
    slice_path = slice_state.worktree or ""
    # The plan-derived slice path and server-returned worker path are distinct
    # records; preserve both instead of pretending they name the same path.
    return {
        "slice_worktree_sha256": _text_digest(slice_path),
        "spawn_result_worktree_sha256": _text_digest(spawn_path),
        "worktree_paths_match": spawn_path == slice_path,
    }


def _legacy_publication_correlation(slice_state: SliceState) -> dict[str, object]:
    receipt = slice_state.post_merge
    evidence = receipt.evidence if receipt is not None else {}
    return {
        "pr_number": slice_state.pr_number,
        "pr_head_sha": _safe_text(evidence.get("head_sha")),
        "pr_base_branch": _safe_text(evidence.get("parent_branch")),
        "completion_receipt_sha256": _json_digest(dict(evidence)),
    }


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
    post_merge = getattr(slice_state, "post_merge", None)
    evidence = post_merge.evidence if post_merge is not None else {}
    if post_merge is None or post_merge.phase is not PostMergePhase.COMPLETE:
        return False
    if evidence.get("pr_number") != str(getattr(slice_state, "pr_number", None)):
        return False
    if not evidence.get("issue_close_intent_id") or not evidence.get("issue_close_journal_id"):
        return False
    if not evidence.get("merge_journal_id"):
        return False
    if current.chainlink_issue_id is not None and accepted.chainlink_issue_id is None:
        return getattr(
            slice_state, "chainlink_issue_id", None
        ) == current.chainlink_issue_id and evidence.get("issue_id") == str(
            current.chainlink_issue_id
        )
    return (
        current.chainlink_issue_id is None
        and accepted.chainlink_issue_id is not None
        and getattr(slice_state, "chainlink_issue_id", None) is None
        and evidence.get("issue_id") == str(accepted.chainlink_issue_id)
    )


def _blocked(
    node: ManifestNode,
    fields: tuple[str, ...],
    reason_code: str,
    correlation: tuple[tuple[str, object], ...] = (),
) -> ProtectedDeclarationProof:
    return ProtectedDeclarationProof(
        node.node_id,
        fields,
        False,
        reason_code,
        correlation=correlation,
    )


def _scope_emitter_failure_correlation(
    scope: ReconciliationScope,
    slice_state: SliceState,
    reason_code: str,
) -> tuple[tuple[str, object], ...]:
    if not (
        reason_code.startswith("scope_controller_")
        or reason_code == "legacy_scope_emitter_mismatch"
    ):
        return ()
    proof = scope.scope_emitter_proof
    values = dict(proof.correlation) if proof is not None else {}
    expected = (
        proof.agent_id
        if proof is not None and proof.agent_id is not None
        else values.get("expected")
    )
    events = scope.dispatch_event_records.get(slice_state.dispatch_authoritative_event_seq, ())
    observed = events[0].get("agent_id") if len(events) == 1 else None
    values.update(
        {
            "dispatch_event_expected_emitter_agent_id": (
                _safe_text(expected) if isinstance(expected, str) else ""
            ),
            "dispatch_event_observed_emitter_agent_id": _safe_text(observed)
            if isinstance(observed, str)
            else "",
            "scope_emitter_reason_code": proof.reason_code if proof is not None else reason_code,
        }
    )
    return tuple(sorted(values.items()))
