"""Corroboration for completed leaves in non-root aggregate handoff phases."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from tl_loop.fsm.scope import TLFinalizing, TLPRFiled
from tl_loop.state.plan_manifest import ManifestNode, PlanManifest
from tl_loop.state.plan_reconciliation_emitter import _integration_owner_matches
from tl_loop.state.schema import (
    IntegrationCandidateState,
    IntegrationRuntimeState,
    RunState,
    SliceState,
)

if TYPE_CHECKING:
    from .plan_reconciliation_models import ReconciliationScope


def terminal_aggregate_handoff_matches(
    scope: ReconciliationScope,
    phase: TLFinalizing | TLPRFiled,
) -> bool:
    """Require the filed handoff to match both manifests and the parent owner."""
    evidence = _handoff_coordinates(phase)
    parent = scope.parent_scope
    if evidence is None or parent is None:
        return False
    if evidence["parent_branch"] != scope.state.parent_branch:
        return False
    if phase.scope_path != (scope.current.scope_id,):
        return False
    if not _declared_child_scope_matches(scope, parent.current, parent.accepted):
        return False
    return _parent_owner_matches(scope, evidence) and _publication_coordinates_match(
        scope, evidence
    )


def _declared_child_scope_matches(
    scope: ReconciliationScope,
    parent_manifest: PlanManifest,
    accepted_parent: PlanManifest | None,
) -> bool:
    parent = scope.parent_scope
    state = scope.state
    manifest = scope.current
    if parent is None or accepted_parent is None:
        return False
    if not _declared_parent_coordinates_match(scope, parent_manifest):
        return False
    current_node = _unique_sub_tl_node(parent_manifest, state.run_id)
    accepted_node = _unique_sub_tl_node(accepted_parent, state.run_id)
    return bool(
        current_node is not None
        and accepted_node is not None
        and current_node.node_id == accepted_node.node_id
        and parent_manifest.child_manifests.get(current_node.node_id) == manifest
        and accepted_parent.child_manifests.get(accepted_node.node_id) == scope.accepted
    )


def _declared_parent_coordinates_match(
    scope: ReconciliationScope,
    parent_manifest: PlanManifest,
) -> bool:
    parent = scope.parent_scope
    state = scope.state
    manifest = scope.current
    return bool(
        parent is not None
        and manifest.role == "non_root"
        and manifest.parent_scope_id == parent_manifest.scope_id
        and state.run_id == manifest.scope_id.rsplit("/", 1)[-1]
        and state.parent_run_id == parent.state.run_id
        and state.parent_agent_id == getattr(parent.scope_emitter_proof, "agent_id", None)
        and state.parent_branch == parent.state.owner_branch
        and manifest.parent_integration_target == state.parent_branch
    )


def _parent_owner_matches(
    scope: ReconciliationScope,
    evidence: Mapping[str, str],
) -> bool:
    parent = scope.parent_scope
    if parent is None:
        return False
    state = scope.state
    parent_slice = parent.state.slices.get(state.run_id)
    candidate = parent.state.integration.candidates.get(state.run_id)
    owner_id = f"{parent.state.run_id}:{state.run_id}:integration"
    return bool(
        parent_slice is not None
        and candidate is not None
        and parent_slice.dispatch_agent_id == owner_id
        and parent_slice.branch == state.owner_branch
        and parent_slice.worktree == state.owner_worktree
        and parent_slice.base_ref == state.parent_branch
        and evidence["handoff"] == owner_id
        and _integration_owner_matches(parent, scope, owner_id)
    )


def _publication_coordinates_match(
    scope: ReconciliationScope,
    evidence: Mapping[str, str],
) -> bool:
    state = scope.state
    child_integration = state.integration
    parent = scope.parent_scope
    parent_slice = parent.state.slices.get(state.run_id) if parent is not None else None
    candidate = (
        parent.state.integration.candidates.get(state.run_id) if parent is not None else None
    )
    patch_digest = child_integration.aggregate_patch_digest
    return bool(
        parent is not None
        and parent_slice is not None
        and candidate is not None
        and _child_coordinates_match(child_integration, evidence, state, patch_digest)
        and _parent_coordinates_match(parent_slice, candidate, evidence, patch_digest)
    )


def _child_coordinates_match(
    integration: IntegrationRuntimeState,
    evidence: Mapping[str, str],
    state: RunState,
    patch_digest: object,
) -> bool:
    aggregate_pr = integration.aggregate_pr_number
    aggregate_head = integration.aggregate_head_sha
    aggregate_base = integration.aggregate_original_base_sha
    return (
        type(aggregate_pr) is int
        and aggregate_pr > 0
        and evidence["aggregate_pr"] == str(aggregate_pr)
        and _is_commit_sha(evidence["head_sha"])
        and aggregate_head == evidence["head_sha"]
        and _is_commit_sha(evidence["base_sha"])
        and aggregate_base == evidence["base_sha"]
        and isinstance(patch_digest, str)
        and patch_digest
        and _child_owner_coordinates_match(integration, evidence, state, patch_digest)
    )


def _child_owner_coordinates_match(
    integration: IntegrationRuntimeState,
    evidence: Mapping[str, str],
    state: RunState,
    patch_digest: str,
) -> bool:
    return (
        integration.head_sha == integration.aggregate_head_sha
        and integration.patch_digest == patch_digest
        and integration.integration_owner_id == evidence["handoff"]
        and integration.integration_owner_run_id == state.run_id
        and integration.integration_owner_branch == state.owner_branch
        and integration.integration_owner_worktree == state.owner_worktree
    )


def _parent_coordinates_match(
    parent_slice: SliceState,
    candidate: IntegrationCandidateState,
    evidence: Mapping[str, str],
    child_patch_digest: object,
) -> bool:
    expected = (
        int(evidence["aggregate_pr"]),
        evidence["head_sha"],
        evidence["base_sha"],
    )
    observed = (
        candidate.aggregate_pr_number,
        candidate.aggregate_head_sha,
        candidate.aggregate_original_base_sha,
    )
    if any(value is not None for value in observed) and observed != expected:
        return False
    return (
        parent_slice.pr_number in (None, expected[0])
        and _optional_equal(candidate.aggregate_patch_digest, child_patch_digest)
        and _optional_equal(candidate.head_sha, evidence["head_sha"])
        and _optional_equal(candidate.patch_digest, child_patch_digest)
    )


def _optional_equal(observed: object, expected: object) -> bool:
    return observed is None or observed == expected


def _handoff_coordinates(phase: TLFinalizing | TLPRFiled) -> dict[str, str] | None:
    if isinstance(phase, TLPRFiled):
        return {
            "aggregate_pr": phase.aggregate_pr,
            "head_sha": phase.head_sha,
            "base_sha": phase.base_sha,
            "parent_branch": phase.parent_branch,
            "handoff": phase.handoff,
        }
    if phase.role.value != "non_root":
        return None
    fields = ("aggregate_pr", "head_sha", "base_sha", "parent_branch", "handoff")
    evidence = phase.evidence
    if any(not isinstance(evidence.get(field), str) or not evidence.get(field) for field in fields):
        return None
    return {field: evidence[field] for field in fields}


def _unique_sub_tl_node(manifest: PlanManifest, name: str) -> ManifestNode | None:
    matches = [node for node in manifest.nodes if node.kind == "sub_tl" and node.name == name]
    return matches[0] if len(matches) == 1 else None


def _is_commit_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )
