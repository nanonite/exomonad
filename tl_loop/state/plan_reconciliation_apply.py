"""Plan reconciliation support."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .lock import RunLock
from .plan_reconciliation import analyze_plan_reconciliation
from .plan_reconciliation_audit import _append_audit, _audit_record
from .plan_reconciliation_checkpoint import _reconciled_checkpoint, _scope_needs_update
from .plan_reconciliation_models import PlanReconciliationAnalysis, ReconciliationScope
from .plan_revision_apply import _resolve_actor, _validate_reason
from .plan_revision_files import controller_lifecycle_lock_path, safe_target
from .plan_revision_transaction import (
    PreparedRevision,
    _read_journal,
    _recover_locked,
    commit_prepared_revision,
    prepare_revision_transaction,
)
from .plan_revision_types import PlanRevisionError


@dataclass(frozen=True)
class PlanReconciliationResult:
    """Outcome of an audited restoration to the verified accepted plan."""

    analysis: PlanReconciliationAnalysis
    audit_record: Mapping[str, object] | None
    applied: bool
    recovered_during_apply: bool = False

    def to_document(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "recovered_during_apply": self.recovered_during_apply,
            "analysis": self.analysis.to_document(),
            "audit": dict(self.audit_record) if self.audit_record else None,
        }


def apply_plan_reconciliation(
    project_dir: str | Path,
    reason: str,
    expected_fingerprint: str,
    *,
    actor: str | None = None,
    lock_timeout: float = 5.0,
    after_target: Callable[[int, str], None] | None = None,
) -> PlanReconciliationResult:
    """Restore eligible RunStore bindings after comparing the dry-run proof."""
    project = Path(project_dir).absolute()
    _validate_expected_fingerprint(expected_fingerprint)
    normalized_reason = _validate_reason(reason)
    normalized_actor = _resolve_actor(actor)
    lifecycle = controller_lifecycle_lock_path(project)
    with RunLock(lifecycle, timeout=lock_timeout):
        return _apply_with_revision_lock(
            project,
            normalized_reason,
            expected_fingerprint,
            normalized_actor,
            lock_timeout,
            after_target,
        )


def _apply_with_revision_lock(
    project: Path,
    reason: str,
    fingerprint: str,
    actor: str,
    timeout: float,
    after_target: Callable[[int, str], None] | None,
) -> PlanReconciliationResult:
    state_root = project / ".exo" / "tl-loop"
    revision_lock = state_root / "plan-revision.lock"
    if revision_lock.is_symlink():
        raise PlanRevisionError("transition_lock_symlink", "plan revision lock is a symlink")
    with RunLock(revision_lock, timeout=timeout):
        initial = analyze_plan_reconciliation(project)
        _require_fingerprint(initial, fingerprint)
        _require_reconcilable(initial)
        with ExitStack() as stack:
            _acquire_scope_locks(stack, initial.scopes, timeout)
            analysis = analyze_plan_reconciliation(project)
            _require_fingerprint(analysis, fingerprint)
            _require_reconcilable(analysis)
            return _apply_locked(project, analysis, reason, actor, after_target)


def _apply_locked(
    project: Path,
    analysis: PlanReconciliationAnalysis,
    reason: str,
    actor: str,
    after_target: Callable[[int, str], None] | None,
) -> PlanReconciliationResult:
    if not analysis.has_drift:
        return PlanReconciliationResult(analysis, None, applied=False)
    audit = _audit_record(analysis, reason, actor)
    changes, expected_before = _collect_changes(project, analysis, audit)
    prepared = prepare_revision_transaction(
        project,
        changes,
        audit,
        transition_id=cast(str, audit["transition_id"]),
        expected_before=expected_before,
    )
    return _commit_and_recover(project, analysis, audit, prepared, after_target)


def _collect_changes(
    project: Path,
    analysis: PlanReconciliationAnalysis,
    audit: Mapping[str, object],
) -> tuple[dict[Path, bytes], dict[Path, str | None]]:
    changes: dict[Path, bytes] = {}
    expected: dict[Path, str | None] = {}
    for scope in analysis.scopes:
        if not _scope_needs_update(scope):
            continue
        path = safe_target(project, scope.relative_checkpoint)
        raw = scope.raw_checkpoint
        changes[path] = _reconciled_checkpoint(raw, scope)
        expected[path] = hashlib.sha256(raw).hexdigest()
    audit_path = safe_target(project, ".exo/tl-loop/plan-reconciliations.jsonl")
    changes[audit_path] = _append_audit(analysis.audit_bytes, audit)
    expected[audit_path] = (
        hashlib.sha256(analysis.audit_bytes).hexdigest()
        if analysis.audit_bytes is not None
        else None
    )
    return changes, expected


def _commit_and_recover(
    project: Path,
    analysis: PlanReconciliationAnalysis,
    audit: Mapping[str, object],
    prepared: PreparedRevision,
    after_target: Callable[[int, str], None] | None,
) -> PlanReconciliationResult:
    try:
        commit_prepared_revision(prepared, after_target=after_target)
    except Exception:  # noqa: BLE001 - recover ordinary failures from the durable journal
        try:
            journal = _read_journal(prepared.journal_path)
            _recover_locked(project, prepared.journal_path, journal)
        except Exception as error:
            raise PlanRevisionError(
                "transition_recovery_required",
                "plan reconciliation remains journaled for startup recovery",
            ) from error
        return PlanReconciliationResult(analysis, audit, True, recovered_during_apply=True)
    return PlanReconciliationResult(analysis, audit, True)


def _acquire_scope_locks(
    stack: ExitStack,
    scopes: tuple[ReconciliationScope, ...],
    timeout: float,
) -> None:
    paths = {scope.run_dir.parent / "run.lock" for scope in scopes}
    for path in sorted(paths, key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise PlanRevisionError("run_lock_symlink", "RunStore lock path is a symlink")
        stack.enter_context(RunLock(path, timeout=timeout))


def _require_reconcilable(analysis: PlanReconciliationAnalysis) -> None:
    if analysis.blockers:
        codes = ", ".join(sorted({item.code for item in analysis.blockers}))
        raise PlanRevisionError(
            "reconciliation_blocked", f"plan reconciliation is blocked: {codes}"
        )


def _require_fingerprint(analysis: PlanReconciliationAnalysis, expected: str) -> None:
    if analysis.evidence_fingerprint != expected:
        raise PlanRevisionError(
            "reconciliation_evidence_stale",
            "plan reconciliation inputs changed; run a new dry-run before applying",
        )


def _validate_expected_fingerprint(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise PlanRevisionError(
            "reconciliation_fingerprint_invalid",
            "--expected-fingerprint must be a lowercase SHA-256 value from dry-run",
        )


__all__ = ["PlanReconciliationResult", "apply_plan_reconciliation"]
