"""Plan reconciliation support."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

from .plan_reconciliation_declarations import _recursive_declaration_changes
from .plan_reconciliation_emitter import root_scope_emitter
from .plan_reconciliation_models import PlanReconciliationAnalysis, ReconciliationScope
from .plan_reconciliation_report import (
    _fingerprint,
    _input_hashes,
    _safe_changes,
)
from .plan_reconciliation_scope import _collect_scope_tree, _read_scope, _validate_plan_run_id
from .plan_revision_diff import collect_node_changes, unique_blockers
from .plan_revision_files import read_regular_file, safe_target
from .plan_revision_manifest import retain_scope_revisions
from .plan_revision_plan import (
    build_root_manifest,
    decode_digest,
    decode_plan,
    preserve_run_settings,
    reject_pending_transition,
)
from .plan_revision_types import DecodedPlan, PlanRevisionBlocker, PlanRevisionError


def analyze_plan_reconciliation(
    project_dir: str | Path,
    *,
    run_id: str = "root",
) -> PlanReconciliationAnalysis:
    """Report all recoverable plan/runtime identity drift without writing."""
    project = Path(project_dir).absolute()
    state_root = project / ".exo" / "tl-loop"
    _validate_roots(project, state_root)
    inputs: dict[str, bytes | None] = {}
    snapshot, digest_bytes, candidate, audit = _read_plan_inputs(project, inputs)
    snapshot_hash = hashlib.sha256(snapshot).hexdigest()
    if snapshot_hash != decode_digest(digest_bytes, state_root / "plan.snapshot.sha256"):
        raise PlanRevisionError("snapshot_digest_mismatch", "saved plan snapshot digest is invalid")
    baseline = _decode_plan_safely(snapshot, "accepted snapshot", allow_legacy=True)
    revised = _decode_plan_safely(candidate, "candidate", allow_legacy=False)
    preserve_run_settings(baseline.document, revised.document)
    _validate_plan_run_id(baseline.document, revised.document, run_id)
    root_view = _read_scope(project, run_id, run_id, None, inputs)
    root_view = replace(root_view, scope_emitter_proof=root_scope_emitter(root_view))
    root_current = root_view.current
    root_accepted = retain_scope_revisions(
        build_root_manifest(baseline.plan, root_current), root_current
    )
    candidate_manifest = retain_scope_revisions(
        build_root_manifest(revised.plan, root_accepted), root_accepted
    )
    blockers: list[PlanRevisionBlocker] = []
    scopes: list[ReconciliationScope] = []
    _collect_scope_tree(
        project,
        root_view,
        root_accepted,
        parent_manifest_matches=True,
        inputs=inputs,
        scopes=scopes,
        blockers=blockers,
        accepted_plan=baseline.document,
    )
    changes = _safe_changes(collect_node_changes(root_current, root_accepted))
    candidate_changes = _safe_changes(collect_node_changes(root_accepted, candidate_manifest))
    input_hashes = _input_hashes(inputs)
    _validate_audit(audit)
    return PlanReconciliationAnalysis(
        evidence_fingerprint=_fingerprint(input_hashes),
        snapshot_sha256=snapshot_hash,
        candidate_sha256=hashlib.sha256(candidate).hexdigest(),
        scopes=tuple(scopes),
        changes=changes,
        candidate_changes=candidate_changes,
        candidate_declaration_changes=tuple(
            _recursive_declaration_changes(
                root_accepted,
                candidate_manifest,
                tuple(node_id for scope in scopes for node_id in scope.protected_node_ids),
                tuple(blocker.node_id for blocker in blockers if blocker.node_id is not None),
                unprotected_disposition="not_applied",
                evidence_source="candidate_plan",
            )
        ),
        blockers=unique_blockers(blockers),
        input_hashes=input_hashes,
        audit_bytes=audit,
    )


def _validate_roots(project: Path, state_root: Path) -> None:
    if project.is_symlink() or any(path.is_symlink() for path in (project / ".exo", state_root)):
        raise PlanRevisionError("state_path_symlink", "refusing symlinked plan state directory")
    for name in ("plan-transition.json", "plan-revision-transition.json"):
        safe_target(project, f".exo/tl-loop/{name}")
    reject_pending_transition(state_root)


def _validate_audit(raw: bytes | None) -> None:
    if raw is None:
        return
    if raw and not raw.endswith(b"\n"):
        raise PlanRevisionError("reconciliation_audit_invalid", "audit log is truncated")
    for number, line in enumerate(raw.splitlines(), start=1):
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise PlanRevisionError(
                "reconciliation_audit_invalid", f"audit record {number} is malformed"
            ) from error
        if not isinstance(value, dict):
            raise PlanRevisionError(
                "reconciliation_audit_invalid", f"audit record {number} is malformed"
            )


def _read_plan_inputs(
    project: Path,
    inputs: dict[str, bytes | None],
) -> tuple[bytes, bytes, bytes, bytes | None]:
    snapshot = _read_input(project, ".exo/tl-loop/plan.snapshot", inputs, required=True)
    digest = _read_input(project, ".exo/tl-loop/plan.snapshot.sha256", inputs, required=True)
    candidate = _read_input(project, ".exo/tl-loop/plan.json", inputs, required=True)
    audit = _read_input(project, ".exo/tl-loop/plan-reconciliations.jsonl", inputs)
    return cast(bytes, snapshot), cast(bytes, digest), cast(bytes, candidate), audit


def _read_input(
    project: Path,
    relative: str,
    inputs: dict[str, bytes | None],
    *,
    required: bool = False,
) -> bytes | None:
    path = safe_target(project, relative)
    raw, _mode = read_regular_file(path)
    if raw is None and required:
        raise PlanRevisionError(
            "reconciliation_input_missing", f"required input is missing: {relative}"
        )
    inputs[relative] = raw
    return raw


def _decode_plan_safely(data: bytes, label: str, *, allow_legacy: bool) -> DecodedPlan:
    try:
        return decode_plan(
            data,
            Path(label),
            allow_legacy_leaf_bindings=allow_legacy,
        )
    except PlanRevisionError as error:
        raise PlanRevisionError("plan_invalid", f"{label} plan failed validation") from error


__all__ = ["PlanReconciliationAnalysis", "analyze_plan_reconciliation"]
