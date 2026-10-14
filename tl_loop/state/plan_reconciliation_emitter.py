"""Durable ownership proof for legacy spawn-event emitters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tl_loop.fsm.child import ChildKind
from tl_loop.state.plan_manifest import ManifestNode
from tl_loop.state.plan_reconciliation_ownership import _records_for
from tl_loop.state.plan_revision_files import read_regular_file
from tl_loop.state.plan_revision_types import PlanRevisionError

from .plan_reconciliation_report import _safe_text
from .schema import SliceState

if TYPE_CHECKING:
    from .plan_reconciliation_models import ReconciliationScope


@dataclass(frozen=True)
class ScopeEmitterProof:
    """Resolved controller identity and bounded evidence for one scope."""

    agent_id: str | None
    status: str
    reason_code: str
    evidence: tuple[str, ...] = ()
    correlation: tuple[tuple[str, object], ...] = ()

    def to_document(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "agent_id": _safe_text(self.agent_id),
            "evidence": list(self.evidence),
            "correlation": dict(self.correlation),
        }


def root_scope_emitter(scope: ReconciliationScope) -> ScopeEmitterProof:
    """Use the canonical root run identity only for a positively identified root."""
    valid = (
        scope.state.run_id == scope.current.scope_id
        and scope.current.role == "root"
        and scope.current.parent_scope_id is None
        and scope.state.parent_run_id is None
        and scope.state.parent_agent_id is None
    )
    if not valid:
        return _unproven(
            "scope_controller_root_identity_unproven",
            expected=scope.current.scope_id,
            observed=scope.state.run_id,
        )
    return ScopeEmitterProof(
        scope.state.run_id,
        "proven",
        "proven",
        ("canonical_root_run_identity", "root_manifest_role"),
        (
            ("scope_id", _safe_text(scope.current.scope_id)),
            ("scope_run_id", _safe_text(scope.state.run_id)),
        ),
    )


def child_scope_emitter(
    project: Path,
    parent: ReconciliationScope,
    child: ReconciliationScope,
    declaration: Mapping[str, object] | None,
    inputs: dict[str, bytes | None],
) -> ScopeEmitterProof:
    """Resolve a child controller from accepted declaration and host registration."""
    if declaration is None:
        return _unproven("scope_controller_declaration_missing")
    expected, reason = _declared_controller_id(declaration, child.state.run_id)
    if expected is None:
        return _unproven(reason or "scope_controller_declaration_invalid")
    if not _valid_agent_segment(expected):
        return _unproven(
            "scope_controller_declaration_invalid",
            expected=expected,
        )
    reason, parent_slice, owner_record = _parent_dispatch_proof(parent, child, expected)
    if reason is not None or parent_slice is None:
        return _unproven(
            reason or "scope_controller_dispatch_unproven",
            expected=expected,
            observed=parent_slice.dispatch_agent_id if parent_slice is not None else None,
        )
    relative = f".exo/agents/{expected}/invocation.json"
    path = project / relative
    if any(
        part.is_symlink()
        for part in (
            project / ".exo",
            project / ".exo" / "agents",
            project / ".exo" / "agents" / expected,
            path,
        )
    ):
        inputs[relative] = None
        return _unproven(
            "scope_controller_registration_unreadable",
            expected=expected,
            parent_agent_id=parent_slice.dispatch_agent_id,
        )
    try:
        raw, _mode = read_regular_file(path)
    except (OSError, PlanRevisionError):
        raw = None
    inputs[relative] = raw
    if raw is None:
        return _unproven(
            "scope_controller_registration_missing",
            expected=expected,
            parent_agent_id=parent_slice.dispatch_agent_id,
        )
    record = _decode_invocation(raw)
    if record is None:
        return _unproven(
            "scope_controller_registration_malformed",
            expected=expected,
            registration_sha256=hashlib.sha256(raw).hexdigest(),
        )
    reason = _invocation_registration_failure(
        record,
        expected,
        child,
        parent_slice,
    )
    if reason is not None:
        return _unproven(
            reason,
            expected=expected,
            observed=record.get("runtime_agent_id"),
            invocation_id=record.get("invocation_id"),
            parent_invocation_id=parent_slice.dispatch_invocation_id,
            registration_sha256=hashlib.sha256(raw).hexdigest(),
        )
    invocation_id = record["invocation_id"]
    evidence = (
        "accepted_sub_tl_controller_declaration",
        "parent_scope_dispatch_record",
        "server_invocation_registration_matches_scope",
        "parent_agent_relationship_matches_parent_scope",
    )
    correlation = {
        "scope_id": _safe_text(child.current.scope_id),
        "scope_run_id": _safe_text(child.state.run_id),
        "scope_controller_agent_id": _safe_text(expected),
        "scope_controller_invocation_id": _safe_text(invocation_id),
        "scope_controller_invocation_sha256": hashlib.sha256(raw).hexdigest(),
        "scope_controller_invocation_trigger": _safe_text(record.get("trigger")),
        "scope_controller_branch_sha256": _text_digest(child.state.owner_branch),
        "scope_controller_worktree_sha256": _text_digest(child.state.owner_worktree),
        "parent_scope_run_id": _safe_text(parent.state.run_id),
        "parent_scope_controller_agent_id": _safe_text(parent.scope_emitter_proof.agent_id),
        "parent_dispatch_intent_id": _safe_text(parent_slice.dispatch_intent_id),
        "parent_dispatch_invocation_id": _safe_text(parent_slice.dispatch_invocation_id),
        "parent_dispatch_event_seq": parent_slice.dispatch_authoritative_event_seq,
        "parent_dispatch_agent_id": _safe_text(parent_slice.dispatch_agent_id),
        "parent_dispatch_owner_record": owner_record,
    }
    return ScopeEmitterProof(
        expected,
        "proven",
        "proven",
        evidence,
        tuple(sorted(correlation.items())),
    )


def _declared_controller_id(
    declaration: Mapping[str, object],
    scope_run_id: str,
) -> tuple[str | None, str | None]:
    if declaration.get("name") != scope_run_id:
        return None, "scope_controller_declaration_scope_mismatch"
    value = declaration.get("agent_id", scope_run_id)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return None, "scope_controller_declaration_invalid"
    return value, None


def _valid_agent_segment(value: str) -> bool:
    return (
        value not in {".", ".."} and "/" not in value and "\\" not in value and "\x00" not in value
    )


def _parent_dispatch_proof(
    parent: ReconciliationScope,
    child: ReconciliationScope,
    expected_controller: str,
) -> tuple[str | None, SliceState | None, str]:
    parent_proof = parent.scope_emitter_proof
    if parent_proof is None or parent_proof.agent_id is None:
        return "scope_controller_parent_identity_unproven", None, "missing"
    if (
        child.state.parent_run_id != parent.state.run_id
        or child.state.parent_agent_id != parent_proof.agent_id
        or child.state.parent_branch != parent.state.owner_branch
        or child.current.parent_scope_id != parent.current.scope_id
    ):
        return "scope_controller_parent_relationship_mismatch", None, "conflict"
    accepted_parent = parent.accepted
    accepted_node = _accepted_sub_tl_node(accepted_parent, child.state.run_id)
    if accepted_node is None or accepted_parent is None:
        return "scope_controller_accepted_declaration_mismatch", None, "missing"
    parent_slice = parent.state.slices.get(child.state.run_id)
    if parent_slice is None:
        return "scope_controller_parent_dispatch_missing", None, "missing"
    if not (
        parent_slice.attempts > 0
        and isinstance(parent_slice.dispatch_intent_id, str)
        and bool(parent_slice.dispatch_intent_id)
        and isinstance(parent_slice.dispatch_invocation_id, str)
        and bool(parent_slice.dispatch_invocation_id)
        and type(parent_slice.dispatch_authoritative_event_seq) is int
        and parent_slice.dispatch_authoritative_event_seq > 0
        and parent_slice.branch == child.state.owner_branch
        and parent_slice.worktree == child.state.owner_worktree
        and parent_slice.base_ref == child.state.parent_branch
    ):
        return "scope_controller_parent_dispatch_unproven", parent_slice, "incomplete"
    matching = [
        record
        for record in _records_for(parent.state.recursive_fsm)
        if record.child_id == child.state.run_id
    ]
    if len(matching) != 1:
        return "scope_controller_parent_dispatch_ambiguous", parent_slice, "ambiguous"
    record = matching[0]
    if not (
        record.kind is ChildKind.SUB_TL
        and record.manifest_node_id == accepted_node.node_id
        and record.manifest_revision == accepted_parent.manifest_revision
        and record.dispatch_intent_id == parent_slice.dispatch_intent_id
        and record.invocation_id == parent_slice.dispatch_invocation_id
    ):
        return "scope_controller_parent_dispatch_conflict", parent_slice, "conflict"
    owner = parent_slice.dispatch_agent_id
    integration_owner_id = f"{parent.state.run_id}:{child.state.run_id}:integration"
    if owner == expected_controller:
        return None, parent_slice, "dispatch_owner"
    if owner != integration_owner_id or not _integration_owner_matches(
        parent, child, integration_owner_id
    ):
        return "scope_controller_parent_owner_conflict", parent_slice, "conflict"
    return None, parent_slice, "integration_owner_with_verified_registration"


def _accepted_sub_tl_node(manifest: object | None, name: str) -> ManifestNode | None:
    nodes = getattr(manifest, "nodes", ())
    matches = [node for node in nodes if node.kind == "sub_tl" and node.name == name]
    return matches[0] if len(matches) == 1 else None


def _integration_owner_matches(
    parent: ReconciliationScope,
    child: ReconciliationScope,
    owner_id: str,
) -> bool:
    candidate = parent.state.integration.candidates.get(child.state.run_id)
    return bool(
        candidate is not None
        and candidate.integration_owner_id == owner_id
        and candidate.integration_owner_run_id == child.state.run_id
        and candidate.integration_owner_branch == child.state.owner_branch
        and candidate.integration_owner_worktree == child.state.owner_worktree
    )


def _decode_invocation(raw: bytes) -> Mapping[str, object] | None:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _invocation_registration_failure(
    record: Mapping[str, object],
    expected_controller: str,
    child: ReconciliationScope,
    parent_slice: SliceState,
) -> str | None:
    invocation_id = record.get("invocation_id")
    trigger = record.get("trigger")
    prior = record.get("prior_invocation_id")
    dispatch_invocation = getattr(parent_slice, "dispatch_invocation_id", None)
    lineage_matches = invocation_id == dispatch_invocation or (
        trigger == "resume_pr" and prior == dispatch_invocation
    )
    if not isinstance(invocation_id, str) or not invocation_id or not lineage_matches:
        return "scope_controller_invocation_lineage_mismatch"
    if trigger not in {"spawn", "resume_pr"}:
        return "scope_controller_invocation_trigger_mismatch"
    if record.get("runtime_agent_id") != expected_controller:
        return "scope_controller_invocation_owner_mismatch"
    if record.get("slice_id") != child.state.run_id:
        return "scope_controller_invocation_scope_mismatch"
    if (
        not isinstance(child.state.owner_branch, str)
        or not child.state.owner_branch
        or record.get("branch") != child.state.owner_branch
    ):
        return "scope_controller_invocation_branch_mismatch"
    if (
        not isinstance(child.state.owner_worktree, str)
        or not child.state.owner_worktree
        or record.get("worktree") != child.state.owner_worktree
    ):
        return "scope_controller_invocation_worktree_mismatch"
    return None


def _unproven(reason: str, **correlation: object) -> ScopeEmitterProof:
    bounded = tuple(
        sorted(
            (key, _safe_text(value) if isinstance(value, str) else value)
            for key, value in correlation.items()
        )
    )
    return ScopeEmitterProof(None, "unproven", reason, (), bounded)


def _text_digest(value: object) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if isinstance(value, str) else None


__all__ = ["ScopeEmitterProof", "child_scope_emitter", "root_scope_emitter"]
