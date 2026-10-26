"""Validate durable nested recovery waits against accepted scope ownership."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from tl_loop.fsm.recovery import RecoveryPhase
from tl_loop.ordered import ChildRecoverySummary, SubTLLifecycle
from tl_loop.state.plan_manifest import ManifestNode, PlanManifest
from tl_loop.state.schema import RunState, SliceState
from tl_loop.state.store import RunStore


@dataclass(frozen=True)
class NestedRecoveryAssessment:
    """Evidence result for a parent checkpoint that may declare nested recovery."""

    asserted: bool
    verified: bool
    reason_code: str
    reason: str


def assess_nested_recovery_wait(
    parent: RunState,
    parent_slice: SliceState,
    parent_store: RunStore,
    *,
    parent_controller_agent_id: str | None = None,
    project_root: Path | str | None = None,
) -> NestedRecoveryAssessment:
    """Distinguish no declared recovery from a declaration lacking proof."""
    lifecycle = parent.integration.sub_tl_states.get(parent_slice.id)
    has_summary = parent_slice.id in parent.integration.sub_tl_recovery
    asserted = has_summary or lifecycle in {
        SubTLLifecycle.RECOVERING,
        SubTLLifecycle.HUMAN_GATE,
    }
    if not asserted:
        return NestedRecoveryAssessment(False, False, "not_asserted", "no nested recovery declared")
    if not isinstance(lifecycle, SubTLLifecycle) or not isinstance(
        parent.integration.sub_tl_recovery.get(parent_slice.id), ChildRecoverySummary
    ):
        return NestedRecoveryAssessment(
            True, False, "summary_malformed", "parent nested recovery projection is malformed"
        )
    if parent.plan_manifest is None:
        return NestedRecoveryAssessment(
            True, False, "manifest_unavailable", "accepted parent manifest is unavailable"
        )
    nodes = [
        node
        for node in parent.plan_manifest.nodes
        if node.kind == "sub_tl" and node.name == parent_slice.id
    ]
    if len(nodes) != 1:
        return NestedRecoveryAssessment(
            True,
            False,
            "declaration_missing",
            "accepted parent declaration is missing or ambiguous",
        )
    node = nodes[0]
    child_manifest = parent.plan_manifest.child_manifests.get(node.node_id)
    if child_manifest is None or parent_slice.manifest_node_id != node.node_id:
        return NestedRecoveryAssessment(
            True,
            False,
            "manifest_binding_conflict",
            "accepted child manifest binding is missing or conflicting",
        )
    if not _valid_identity(parent_controller_agent_id):
        return NestedRecoveryAssessment(
            True,
            False,
            "parent_controller_identity_unavailable",
            "current parent controller identity is unavailable",
        )
    child_store = RunStore(node.name, parent_store.run_dir)
    child, failure = _load_declared_child(
        parent,
        parent_slice,
        node,
        child_manifest,
        child_store,
        expected_parent_controller_agent_id=parent_controller_agent_id,
        project_root=project_root,
    )
    if child is None:
        assert failure is not None
        return NestedRecoveryAssessment(True, False, failure.code, failure.reason)
    projection, failure = _scope_recovery_projection(
        child,
        parent.run_id,
        child_store.run_dir,
        frozenset({parent.run_id}),
        controller_agent_id=parent_slice.dispatch_agent_id,
        project_root=project_root,
    )
    if projection is None:
        assert failure is not None
        return NestedRecoveryAssessment(True, False, failure.code, failure.reason)
    observed_lifecycle, observed_summary = projection
    expected_summary = parent.integration.sub_tl_recovery[parent_slice.id]
    if observed_lifecycle is not lifecycle or observed_summary != expected_summary:
        return NestedRecoveryAssessment(
            True,
            False,
            "summary_conflict",
            "parent recovery summary differs from the current child checkpoint",
        )
    return NestedRecoveryAssessment(True, True, "verified", "verified nested recovery wait")


def verified_nested_recovery_wait(
    parent: RunState,
    parent_slice: SliceState,
    parent_store: RunStore,
    *,
    parent_controller_agent_id: str | None = None,
    project_root: Path | str | None = None,
) -> tuple[bool, str]:
    """Verify the saved parent recovery projection against nested checkpoints."""
    result = assess_nested_recovery_wait(
        parent,
        parent_slice,
        parent_store,
        parent_controller_agent_id=parent_controller_agent_id,
        project_root=project_root,
    )
    return result.verified, result.reason


@dataclass(frozen=True)
class _ProofFailure:
    code: str
    reason: str


def _load_declared_child(
    parent: RunState,
    parent_slice: SliceState,
    node: ManifestNode,
    child_manifest: PlanManifest,
    child_store: RunStore,
    *,
    expected_parent_controller_agent_id: str,
    project_root: Path | str | None,
) -> tuple[RunState | None, _ProofFailure | None]:
    if not child_store.path.exists():
        return None, _ProofFailure("checkpoint_missing", "declared child checkpoint is missing")
    try:
        child = child_store.load()
    except (OSError, ValueError) as error:
        return None, _ProofFailure(
            "checkpoint_unavailable",
            f"declared child checkpoint is unavailable: {type(error).__name__}",
        )
    if child.run_id != node.name or child.parent_run_id != parent.run_id:
        return None, _ProofFailure(
            "child_identity_conflict", "child run or parent run identity conflicts"
        )
    if child.parent_agent_id != expected_parent_controller_agent_id:
        return None, _ProofFailure(
            "parent_controller_identity_conflict",
            "child checkpoint names a different parent controller identity",
        )
    if child.plan_manifest is None or child.plan_manifest.digest != child_manifest.digest:
        return None, _ProofFailure(
            "child_manifest_conflict", "child checkpoint manifest conflicts with its declaration"
        )
    expected_parent_branch = parent.owner_branch
    if (
        not expected_parent_branch
        or node.parent_integration_target != expected_parent_branch
        or child.parent_branch != expected_parent_branch
        or child.owner_branch != node.owned_branch
        or parent_slice.branch != node.owned_branch
        or not parent_slice.worktree
        or child.owner_worktree != parent_slice.worktree
    ):
        return None, _ProofFailure(
            "child_ownership_conflict",
            "child branch or worktree conflicts with accepted parent ownership",
        )
    if (
        not parent_slice.dispatch_agent_id
        or not parent_slice.dispatch_intent_id
        or not parent_slice.dispatch_invocation_id
        or parent_slice.dispatch_authoritative_event_seq is None
        or parent_slice.dispatch_last_boundary not in {"sub_tl_started", "sub_tl_recovered"}
    ):
        return None, _ProofFailure(
            "dispatch_identity_incomplete",
            "parent child dispatch identity is incomplete or unresolved",
        )
    registration_failure = _child_dispatch_registration_failure(
        parent_slice,
        node,
        child,
        project_root,
    )
    if registration_failure is not None:
        return None, registration_failure
    return child, None


def _child_dispatch_registration_failure(
    parent_slice: SliceState,
    node: ManifestNode,
    child: RunState,
    project_root: Path | str | None,
) -> _ProofFailure | None:
    """Bind the child-dispatch identity to the host registration and accepted scope."""
    agent_id = parent_slice.dispatch_agent_id
    if not _valid_identity(agent_id):
        return _ProofFailure(
            "child_dispatch_identity_invalid", "child dispatcher identity is invalid"
        )
    if project_root is None:
        return _ProofFailure(
            "child_dispatch_registration_unavailable",
            "child controller registration cannot be checked without the project root",
        )
    registration = _read_controller_registration(Path(project_root), agent_id)
    if registration is None:
        return _ProofFailure(
            "child_dispatch_registration_unavailable",
            "durable child controller registration is missing or malformed",
        )
    expected = {
        "runtime_agent_id": agent_id,
        "invocation_id": parent_slice.dispatch_invocation_id,
        "slice_id": node.name,
        "branch": child.owner_branch,
        "worktree": child.owner_worktree,
    }
    if any(registration.get(key) != value for key, value in expected.items()):
        return _ProofFailure(
            "child_dispatch_registration_conflict",
            "registered child controller does not match the accepted dispatch binding",
        )
    if parent_slice.branch != child.owner_branch or parent_slice.worktree != child.owner_worktree:
        return _ProofFailure(
            "child_dispatch_coordinates_conflict",
            "parent dispatch branch or worktree conflicts with the child checkpoint",
        )
    return None


def _read_controller_registration(
    project_root: Path,
    agent_id: str,
) -> Mapping[str, object] | None:
    """Read a regular host invocation record without following path symlinks."""
    path = project_root / ".exo" / "agents" / agent_id / "invocation.json"
    if any(
        part.is_symlink()
        for part in (project_root / ".exo", project_root / ".exo" / "agents", path.parent, path)
    ):
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _valid_identity(value: str | None) -> bool:
    return bool(
        isinstance(value, str)
        and value.strip() == value
        and value
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
    )


def _scope_recovery_projection(
    state: RunState,
    parent_run_id: str,
    root_dir: Path,
    visited: frozenset[str],
    *,
    controller_agent_id: str | None,
    project_root: Path | str | None,
) -> tuple[tuple[SubTLLifecycle, ChildRecoverySummary] | None, _ProofFailure | None]:
    if state.run_id in visited:
        return None, _ProofFailure("ownership_cycle", "nested recovery ownership contains a cycle")
    if not _valid_identity(controller_agent_id):
        return None, _ProofFailure(
            "parent_controller_identity_unavailable",
            "current nested controller identity is unavailable",
        )
    direct = [
        (slice_id, slice_state)
        for slice_id, slice_state in state.slices.items()
        if slice_state.recovery is not None
    ]
    nested = state.integration.sub_tl_recovery
    if len(direct) + len(nested) != 1:
        return None, _ProofFailure(
            "recovery_missing_or_ambiguous", "nested recovery proof is missing or ambiguous"
        )
    if direct:
        slice_id, slice_state = direct[0]
        recovery = slice_state.recovery
        error = _direct_recovery_identity_error(state, slice_id, slice_state)
        if error is not None:
            return None, _ProofFailure("child_identity_conflict", error)
        assert recovery is not None
        lifecycle = (
            SubTLLifecycle.HUMAN_GATE
            if recovery.phase is RecoveryPhase.HUMAN_GATE
            else SubTLLifecycle.RECOVERING
        )
        return (
            lifecycle,
            ChildRecoverySummary(
                owner_run_id=state.run_id,
                child_path=(parent_run_id, state.run_id, slice_id),
                slice_id=slice_id,
                cause=recovery.cause,
                recovery_round=recovery.recovery_round,
                next_probe_at=recovery.next_probe_at,
            ),
        ), None
    name, expected_summary = next(iter(nested.items()))
    lifecycle = state.integration.sub_tl_states.get(name)
    if not isinstance(lifecycle, SubTLLifecycle) or not isinstance(
        expected_summary, ChildRecoverySummary
    ):
        return None, _ProofFailure(
            "summary_malformed", "nested child recovery summary is malformed"
        )
    if state.plan_manifest is None:
        return None, _ProofFailure(
            "manifest_unavailable", "nested accepted manifest is unavailable"
        )
    nodes = [
        node for node in state.plan_manifest.nodes if node.kind == "sub_tl" and node.name == name
    ]
    if len(nodes) != 1:
        return None, _ProofFailure(
            "declaration_missing", "nested accepted child declaration is missing or ambiguous"
        )
    node = nodes[0]
    child_manifest = state.plan_manifest.child_manifests.get(node.node_id)
    parent_slice = state.slices.get(name)
    if child_manifest is None or parent_slice is None:
        return None, _ProofFailure(
            "declaration_missing", "nested child declaration or slice is missing"
        )
    child_store = RunStore(name, root_dir)
    child, failure = _load_declared_child(
        state,
        parent_slice,
        node,
        child_manifest,
        child_store,
        expected_parent_controller_agent_id=controller_agent_id,
        project_root=project_root,
    )
    if child is None:
        return None, failure
    projection, failure = _scope_recovery_projection(
        child,
        state.run_id,
        child_store.run_dir,
        visited | {state.run_id},
        controller_agent_id=parent_slice.dispatch_agent_id,
        project_root=project_root,
    )
    if projection is None:
        return None, failure
    observed_lifecycle, observed_summary = projection
    if observed_lifecycle is not lifecycle or observed_summary != expected_summary:
        return None, _ProofFailure(
            "summary_conflict", "nested recovery summary differs from its child checkpoint"
        )
    if observed_summary.child_path[0] != parent_run_id:
        observed_summary = replace(
            observed_summary,
            child_path=(parent_run_id, *observed_summary.child_path),
        )
    return (observed_lifecycle, observed_summary), None


def _direct_recovery_identity_error(
    state: RunState,
    slice_id: str,
    slice_state: SliceState,
) -> str | None:
    """Bind a direct recovery wait to one accepted leaf declaration and attempt."""
    recovery = slice_state.recovery
    if recovery is None or recovery.owner_run_id != state.run_id:
        return "leaf recovery owner does not match its checkpoint"
    if state.plan_manifest is None:
        return "leaf recovery checkpoint has no accepted child manifest"
    nodes = [
        node
        for node in state.plan_manifest.nodes
        if node.name == slice_id and node.kind in {"leaf", "worker"}
    ]
    if len(nodes) != 1 or slice_state.manifest_node_id != nodes[0].node_id:
        return "leaf recovery does not match one accepted manifest declaration"
    evidence = recovery.evidence
    if (
        recovery.slice_attempt != slice_state.attempts
        or evidence.get("slice_id") != slice_id
        or evidence.get("attempt") != slice_state.attempts
    ):
        return "leaf recovery attempt or subject conflicts with its checkpoint"
    evidence_invocation = evidence.get("invocation_id")
    if (
        evidence_invocation is not None
        and evidence_invocation != slice_state.dispatch_invocation_id
    ):
        return "leaf recovery invocation conflicts with its accepted dispatch"
    if slice_state.dispatch_invocation_id is not None and (
        not slice_state.dispatch_agent_id
        or recovery.owner_agent_id != slice_state.dispatch_agent_id
        or evidence_invocation != slice_state.dispatch_invocation_id
    ):
        return "leaf recovery owner or invocation conflicts with its accepted dispatch"
    return None


__all__ = [
    "NestedRecoveryAssessment",
    "assess_nested_recovery_wait",
    "verified_nested_recovery_wait",
]
