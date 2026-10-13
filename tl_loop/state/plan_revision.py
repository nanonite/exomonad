"""Read-only analysis for an operator-approved active plan revision."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from tl_loop.fsm.phase import TLPhase

from .plan_manifest import PlanManifest
from .plan_revision_diff import (
    collect_changed_scope_summaries,
    collect_node_changes,
    unique_blockers,
)
from .plan_revision_manifest import (
    changed_scope_ids,
    increment_changed_revisions,
    retain_scope_revisions,
)
from .plan_revision_plan import (
    build_root_manifest,
    decode_digest,
    decode_plan,
    preserve_run_settings,
    read_required,
    reject_pending_transition,
)
from .plan_revision_protection import check_manifest_revision_rules, collect_blockers
from .plan_revision_state import load_scope_tree, read_checkpoint
from .plan_revision_types import (
    DecodedPlan,
    PlanRevisionAnalysis,
    PlanRevisionError,
    ScopeState,
)
from .schema import RunState


def analyze_plan_revision(
    project_dir: str | Path,
    *,
    run_id: str = "root",
) -> PlanRevisionAnalysis:
    """Compare the edited plan with its snapshot and recursive RunStore tree."""
    project = Path(project_dir)
    state_root = project / ".exo" / "tl-loop"
    _validate_state_directory(project, state_root)
    plan_bytes, snapshot_bytes, snapshot_digest = _read_plan_inputs(state_root)
    root_dir, root_state, previous_manifest = _read_root_manifest(state_root, run_id)
    baseline, candidate = _decode_plan_pair(state_root, snapshot_bytes, plan_bytes)
    _validate_candidate(candidate, root_state)
    _ensure_snapshot_matches_manifest(baseline, previous_manifest)
    current_scopes = load_scope_tree(root_dir, root_state, previous_manifest)
    candidate_manifest = _candidate_manifest(candidate, previous_manifest)
    return _build_analysis(
        snapshot_digest,
        plan_bytes,
        previous_manifest,
        candidate_manifest,
        current_scopes,
    )


def _validate_state_directory(project: Path, state_root: Path) -> None:
    if any(path.is_symlink() for path in (project, project / ".exo", state_root)):
        raise PlanRevisionError("state_path_symlink", "refusing symlinked TL-loop state directory")
    reject_pending_transition(state_root)


def _read_plan_inputs(state_root: Path) -> tuple[bytes, bytes, str]:
    snapshot_path = state_root / "plan.snapshot"
    plan_path = state_root / "plan.json"
    snapshot = read_required(snapshot_path, "plan_snapshot_missing")
    digest_bytes = read_required(
        state_root / "plan.snapshot.sha256",
        "plan_snapshot_digest_missing",
    )
    plan = read_required(plan_path, "candidate_plan_missing")
    digest = hashlib.sha256(snapshot).hexdigest()
    if digest != decode_digest(digest_bytes, state_root / "plan.snapshot.sha256"):
        raise PlanRevisionError(
            "snapshot_digest_mismatch",
            "saved plan snapshot does not match its recorded SHA-256 digest",
        )
    return plan, snapshot, digest


def _read_root_manifest(
    state_root: Path,
    run_id: str,
) -> tuple[Path, RunState, PlanManifest]:
    root_dir = state_root / run_id
    state = read_checkpoint(root_dir / "run.json")
    if state.run_id != run_id:
        raise PlanRevisionError(
            "run_id_mismatch",
            f"root checkpoint identifies run {state.run_id!r}, expected {run_id!r}",
        )
    if state.fsm.phase in {TLPhase.TLDone, TLPhase.TLFailed}:
        raise PlanRevisionError("run_terminal", "plan revision requires a nonterminal run")
    manifest = state.plan_manifest
    if manifest is None:
        raise PlanRevisionError("manifest_missing", "root RunStore has no plan manifest")
    if manifest.scope_id != state.run_id or manifest.role != "root":
        raise PlanRevisionError(
            "root_scope_mismatch",
            "plan revision requires a root RunStore with matching root manifest identity",
        )
    return root_dir, state, manifest


def _decode_plan_pair(
    state_root: Path,
    snapshot: bytes,
    candidate: bytes,
) -> tuple[DecodedPlan, DecodedPlan]:
    baseline = decode_plan(
        snapshot,
        state_root / "plan.snapshot",
        allow_legacy_leaf_bindings=True,
    )
    revised = decode_plan(
        candidate,
        state_root / "plan.json",
        allow_legacy_leaf_bindings=False,
    )
    preserve_run_settings(baseline.document, revised.document)
    return baseline, revised


def _validate_candidate(candidate: DecodedPlan, root_state: RunState) -> None:
    if candidate.document.get("run_id") not in (None, root_state.run_id):
        raise PlanRevisionError(
            "run_id_changed",
            "candidate plan run_id differs from the active RunStore",
        )


def _ensure_snapshot_matches_manifest(
    baseline: DecodedPlan,
    previous: PlanManifest,
) -> None:
    expected = build_root_manifest(baseline.plan, previous)
    expected = retain_scope_revisions(expected, previous)
    if expected != previous:
        raise PlanRevisionError(
            "unversioned_plan_drift",
            "saved plan snapshot does not match the current root manifest; "
            "use a separately verified reconciliation path",
        )


def _candidate_manifest(
    candidate: DecodedPlan,
    previous: PlanManifest,
) -> PlanManifest:
    proposed = build_root_manifest(candidate.plan, previous)
    proposed = retain_scope_revisions(proposed, previous)
    changed_ids = changed_scope_ids(previous, proposed)
    return increment_changed_revisions(proposed, previous, changed_ids)


def _build_analysis(
    snapshot_digest: str,
    plan_bytes: bytes,
    previous: PlanManifest,
    candidate: PlanManifest,
    current_scopes: Mapping[str, ScopeState],
) -> PlanRevisionAnalysis:
    blockers = collect_blockers(previous, candidate, current_scopes)
    blockers.extend(check_manifest_revision_rules(previous, candidate, current_scopes))
    return PlanRevisionAnalysis(
        previous_plan_sha256=snapshot_digest,
        candidate_plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
        scopes=collect_changed_scope_summaries(previous, candidate),
        changes=collect_node_changes(previous, candidate),
        blockers=unique_blockers(blockers),
        candidate_plan_bytes=plan_bytes,
        previous_manifest=previous,
        candidate_manifest=candidate,
    )


__all__ = ["PlanRevisionAnalysis", "PlanRevisionError", "analyze_plan_revision"]
