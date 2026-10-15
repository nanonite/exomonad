"""Journal and ledger proof for legacy leaf dispatches without invocation IDs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from tl_loop.loop.journal import stable_action_key
from tl_loop.state.plan_manifest import ManifestNode
from tl_loop.state.schema import SliceState

if TYPE_CHECKING:
    from .plan_reconciliation_models import ReconciliationScope


def legacy_leaf_spawn_journal_matches(
    scope: ReconciliationScope,
    node: ManifestNode,
    slice_state: SliceState,
) -> bool:
    """Bind a legacy receipt to the accepted declaration, journal, and ledger event."""
    accepted = scope.accepted.node(node.node_id) if scope.accepted is not None else None
    if not _journal_is_eligible(scope, slice_state, accepted):
        return False
    entries = [
        entry
        for entry in scope.action_journal_entries or ()
        if _targets_spawn_entry(scope, node, slice_state, entry)
    ]
    return len(entries) == 1 and _spawn_entry_matches(scope, accepted, slice_state, entries[0])


def _journal_is_eligible(
    scope: ReconciliationScope,
    slice_state: SliceState,
    accepted: ManifestNode | None,
) -> bool:
    return bool(
        accepted is not None
        and scope.action_journal_status == "valid"
        and scope.action_journal_entries
        and slice_state.dispatch_invocation_id is None
        and slice_state.dispatch_intent_id
    )


def _targets_spawn_entry(
    scope: ReconciliationScope,
    node: ManifestNode,
    slice_state: SliceState,
    entry: Mapping[str, object],
) -> bool:
    arguments = _arguments(entry)
    return (
        entry.get("run_id") == scope.state.run_id
        and entry.get("operation") == "spawn_leaf"
        and entry.get("target") == node.name
        and arguments.get("intent_id") == slice_state.dispatch_intent_id
        and entry.get("key")
        == stable_action_key(scope.state.run_id, "spawn_leaf", node.name, arguments)
    )


def _spawn_entry_matches(
    scope: ReconciliationScope,
    accepted: ManifestNode,
    slice_state: SliceState,
    entry: Mapping[str, object],
) -> bool:
    arguments = _arguments(entry)
    declaration_matches = (
        entry.get("status") == "confirmed"
        and _confirmed_result(entry)
        and arguments.get("name") == accepted.name
        and arguments.get("task") == accepted.task
        and arguments.get("boundary", []) == list(accepted.boundary)
    )
    return declaration_matches and _legacy_spawn_event_matches(scope, slice_state, entry)


def _legacy_spawn_event_matches(
    scope: ReconciliationScope,
    slice_state: SliceState,
    entry: Mapping[str, object],
) -> bool:
    result = _spawn_result(entry)
    sequence = slice_state.dispatch_authoritative_event_seq
    events = scope.dispatch_event_records.get(sequence, ()) if type(sequence) is int else ()
    proof = scope.scope_emitter_proof
    return bool(
        len(events) == 1
        and proof is not None
        and proof.agent_id is not None
        and result is not None
        and result.get("agent_id") is None
        and result.get("invocation_id") is None
        and _result_coordinates_match(result, slice_state)
        and _event_matches_slice(scope, slice_state, events[0], proof.agent_id)
    )


def _event_matches_slice(
    scope: ReconciliationScope,
    slice_state: SliceState,
    event: Mapping[str, object],
    emitter: str,
) -> bool:
    data = event.get("data")
    return bool(
        isinstance(data, Mapping)
        and scope.dispatch_event_status.get(slice_state.dispatch_authoritative_event_seq) == "found"
        and event.get("type", event.get("event_type")) == "agent.spawned"
        and event.get("run_id") == scope.state.ledger_run_id
        and event.get("run_seq") == slice_state.dispatch_authoritative_event_seq
        and event.get("agent_id") == emitter
        and event.get("invocation_id") is None
        and data.get("intent_id") == slice_state.dispatch_intent_id
        and data.get("child_agent") == slice_state.dispatch_agent_id
        and data.get("branch", data.get("branch_name")) == slice_state.branch
    )


def _result_coordinates_match(result: Mapping[str, object], slice_state: SliceState) -> bool:
    worktree = result.get("worktree_path")
    return (
        isinstance(slice_state.branch, str)
        and bool(slice_state.branch)
        and result.get("branch_name") == slice_state.branch
        and isinstance(worktree, str)
        and bool(worktree)
        and Path(worktree).is_absolute()
    )


def _arguments(entry: Mapping[str, object]) -> Mapping[str, object]:
    value = entry.get("arguments")
    return value if isinstance(value, Mapping) else {}


def _confirmed_result(entry: Mapping[str, object]) -> bool:
    result = entry.get("result")
    return isinstance(result, Mapping) and result.get("success") is True


def _spawn_result(entry: Mapping[str, object]) -> Mapping[str, object] | None:
    outcome = entry.get("result")
    result = outcome.get("result") if isinstance(outcome, Mapping) else None
    return result if isinstance(result, Mapping) else None
