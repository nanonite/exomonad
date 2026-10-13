"""Audited apply orchestration for validated plan revisions."""

from __future__ import annotations

import getpass
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .lock import RunLock
from .plan_manifest import PlanManifest
from .plan_revision import analyze_plan_revision
from .plan_revision_files import (
    controller_lifecycle_lock_path,
    read_regular_file,
    safe_target,
)
from .plan_revision_state_update import revise_scope_checkpoint
from .plan_revision_transaction import (
    PreparedRevision,
    _read_journal,
    _recover_locked,
    commit_prepared_revision,
    prepare_revision_transaction,
    recover_plan_revision,
)
from .plan_revision_types import PlanRevisionAnalysis, PlanRevisionError

_SECRET_MARKER = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/-]{8,}|(?:token|api[_-]?key|password|secret)\s*[:=]"
    r"|\b(?:gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|xox[baprs]-[a-z0-9-]{10,})\b"
    r"|\beyj[a-z0-9_-]+\.eyj[a-z0-9_-]+\.[a-z0-9_-]+\b)"
)


@dataclass(frozen=True)
class PlanRevisionResult:
    """Outcome of a revision apply or no-op request."""

    analysis: PlanRevisionAnalysis
    audit_record: Mapping[str, object] | None
    applied: bool
    recovered_during_apply: bool = False

    def to_document(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "recovered_during_apply": self.recovered_during_apply,
            "analysis": self.analysis.to_document(),
            "audit": dict(self.audit_record) if self.audit_record is not None else None,
        }


def apply_plan_revision(
    project_dir: str | Path,
    reason: str,
    *,
    actor: str | None = None,
    lock_timeout: float = 5.0,
    after_target: Callable[[int, str], None] | None = None,
) -> PlanRevisionResult:
    """Apply the candidate while excluding active controllers and lifecycle changes."""
    project_root = Path(project_dir).resolve()
    controller_lock = controller_lifecycle_lock_path(project_root)
    with RunLock(controller_lock, timeout=lock_timeout):
        recover_plan_revision(
            project_root,
            lock_timeout=lock_timeout,
            controller_locked=True,
        )
        return _apply_plan_revision_locked(
            project_root,
            reason,
            actor,
            lock_timeout,
            after_target,
        )


def _apply_plan_revision_locked(
    project_root: Path,
    reason: str,
    actor: str | None,
    lock_timeout: float,
    after_target: Callable[[int, str], None] | None,
) -> PlanRevisionResult:
    normalized_reason = _validate_reason(reason)
    normalized_actor = _resolve_actor(actor)
    state_root = project_root / ".exo" / "tl-loop"
    revision_lock = state_root / "plan-revision.lock"
    if revision_lock.is_symlink():
        raise PlanRevisionError("transition_lock_symlink", "plan revision lock is a symlink")

    with RunLock(revision_lock, timeout=lock_timeout):
        return _apply_with_scope_locks(
            project_root,
            state_root,
            normalized_reason,
            normalized_actor,
            lock_timeout,
            after_target,
        )


def _apply_with_scope_locks(
    project_root: Path,
    state_root: Path,
    reason: str,
    actor: str,
    timeout: float,
    after_target: Callable[[int, str], None] | None,
) -> PlanRevisionResult:
    initial = analyze_plan_revision(project_root)
    _require_applicable(initial)
    locks = _materialized_scope_locks(state_root / "root", initial.previous_manifest)
    with ExitStack() as stack:
        _acquire_run_locks(stack, locks, timeout)
        analysis = analyze_plan_revision(project_root)
        _require_applicable(analysis)
        _require_same_candidate(initial, analysis)
        return _apply_locked(project_root, analysis, reason, actor, after_target=after_target)


def _apply_locked(
    project_root: Path,
    analysis: PlanRevisionAnalysis,
    reason: str,
    actor: str,
    *,
    after_target: Callable[[int, str], None] | None,
) -> PlanRevisionResult:
    if analysis.previous_plan_sha256 == analysis.candidate_plan_sha256 and not analysis.scopes:
        return PlanRevisionResult(analysis, None, applied=False)
    audit = _audit_record(analysis, reason, actor)
    changes, expected_before = _collect_file_changes(project_root, analysis, audit)
    _ensure_candidate_unchanged(project_root, analysis)
    return _commit_revision(project_root, analysis, audit, changes, expected_before, after_target)


def _commit_revision(
    project_root: Path,
    analysis: PlanRevisionAnalysis,
    audit: Mapping[str, object],
    changes: Mapping[Path, bytes],
    expected_before: Mapping[Path, str | None],
    after_target: Callable[[int, str], None] | None,
) -> PlanRevisionResult:
    prepared = prepare_revision_transaction(
        project_root,
        changes,
        audit,
        transition_id=cast(str, audit["transition_id"]),
        expected_before=expected_before,
    )
    return _commit_and_recover(project_root, analysis, audit, prepared, after_target)


def _commit_and_recover(
    project_root: Path,
    analysis: PlanRevisionAnalysis,
    audit: Mapping[str, object],
    prepared: PreparedRevision,
    after_target: Callable[[int, str], None] | None,
) -> PlanRevisionResult:
    try:
        commit_prepared_revision(prepared, after_target=after_target)
    except Exception:  # noqa: BLE001 - recover every ordinary failure from the journal
        try:
            journal = _read_journal(prepared.journal_path)
            _recover_locked(project_root, prepared.journal_path, journal)
        except Exception as recovery_error:
            raise PlanRevisionError(
                "transition_recovery_required",
                "plan revision was interrupted and remains journaled for startup recovery",
            ) from recovery_error
        return PlanRevisionResult(analysis, audit, applied=True, recovered_during_apply=True)
    return PlanRevisionResult(analysis, audit, applied=True)


def _collect_file_changes(
    project_root: Path,
    analysis: PlanRevisionAnalysis,
    audit: Mapping[str, object],
) -> tuple[dict[Path, bytes], dict[Path, str | None]]:
    state_root = project_root / ".exo" / "tl-loop"
    changes, expected_before = _snapshot_file_changes(state_root, analysis)
    scope_changes, scope_expected = _scope_checkpoint_changes(state_root, analysis)
    changes.update(scope_changes)
    expected_before.update(scope_expected)
    audit_path = state_root / "plan-revisions.jsonl"
    prior_audit, _ = read_regular_file(audit_path)
    changes[audit_path] = _append_audit(prior_audit, audit)
    expected_before[audit_path] = (
        hashlib.sha256(prior_audit).hexdigest() if prior_audit is not None else None
    )
    return changes, expected_before


def _snapshot_file_changes(
    state_root: Path,
    analysis: PlanRevisionAnalysis,
) -> tuple[dict[Path, bytes], dict[Path, str | None]]:
    candidate_digest = analysis.candidate_plan_sha256
    plan_path = state_root / "plan.json"
    snapshot_path = state_root / "plan.snapshot"
    digest_path = state_root / "plan.snapshot.sha256"
    plan, _ = read_regular_file(plan_path)
    snapshot, _ = read_regular_file(snapshot_path)
    snapshot_digest, _ = read_regular_file(digest_path)
    _validate_plan_candidate(plan, candidate_digest)
    _validate_saved_snapshot(snapshot, snapshot_digest, analysis.previous_plan_sha256)
    snapshot_digest = cast(bytes, snapshot_digest)
    changes = {
        plan_path: analysis.candidate_plan_bytes,
        snapshot_path: analysis.candidate_plan_bytes,
        digest_path: f"{candidate_digest}\n".encode("ascii"),
    }
    expected_before = {
        plan_path: candidate_digest,
        snapshot_path: analysis.previous_plan_sha256,
        digest_path: hashlib.sha256(snapshot_digest).hexdigest(),
    }
    return changes, expected_before


def _validate_plan_candidate(plan: bytes | None, candidate_digest: str) -> None:
    if plan is None or hashlib.sha256(plan).hexdigest() != candidate_digest:
        raise PlanRevisionError(
            "candidate_changed_during_apply", "candidate plan changed during apply"
        )


def _validate_saved_snapshot(
    snapshot: bytes | None,
    snapshot_digest: bytes | None,
    expected_digest: str,
) -> None:
    if snapshot is None or hashlib.sha256(snapshot).hexdigest() != expected_digest:
        raise PlanRevisionError(
            "snapshot_changed_during_apply", "saved plan snapshot changed during apply"
        )
    if snapshot_digest is None:
        raise PlanRevisionError(
            "snapshot_digest_missing",
            "saved plan snapshot digest disappeared during apply",
        )
    _validate_snapshot_digest(snapshot_digest, expected_digest)


def _validate_snapshot_digest(snapshot_digest: bytes, expected_digest: str) -> None:
    try:
        decoded_snapshot_digest = snapshot_digest.decode("ascii").strip()
    except UnicodeError as error:
        raise PlanRevisionError(
            "snapshot_digest_invalid",
            "saved plan snapshot digest changed to non-ASCII data during apply",
        ) from error
    if decoded_snapshot_digest != expected_digest:
        raise PlanRevisionError(
            "snapshot_digest_changed_during_apply",
            "saved plan snapshot digest changed during apply",
        )


def _scope_checkpoint_changes(
    state_root: Path,
    analysis: PlanRevisionAnalysis,
) -> tuple[dict[Path, bytes], dict[Path, str | None]]:
    changes: dict[Path, bytes] = {}
    expected_before: dict[Path, str | None] = {}
    old_scopes = _manifest_scope_paths(analysis.previous_manifest, state_root / "root")
    new_scopes = _manifest_scope_paths(analysis.candidate_manifest, state_root / "root")
    for scope in analysis.scopes:
        current = new_scopes.get(scope.scope_id)
        previous = old_scopes.get(scope.scope_id)
        if current is None or previous is None:
            continue
        entry = _updated_scope_checkpoint(previous[0], current[0], current[1])
        if entry is not None:
            target, updated, digest = entry
            changes[target] = updated
            expected_before[target] = digest
    return changes, expected_before


def _updated_scope_checkpoint(
    previous: PlanManifest,
    candidate: PlanManifest,
    run_dir: Path,
) -> tuple[Path, bytes, str] | None:
    target = run_dir / "run.json"
    raw, _ = read_regular_file(target)
    if raw is None:
        return None
    updated = revise_scope_checkpoint(raw, previous, candidate, target)
    return target, updated, hashlib.sha256(raw).hexdigest()


def _manifest_scope_paths(
    manifest: PlanManifest,
    run_dir: Path,
) -> dict[str, tuple[PlanManifest, Path]]:
    result = {manifest.scope_id: (manifest, run_dir)}
    for node in manifest.nodes:
        if node.kind != "sub_tl":
            continue
        child = manifest.child_manifests[node.node_id]
        result.update(_manifest_scope_paths(child, run_dir / node.name))
    return result


def _materialized_scope_locks(
    root_dir: Path,
    manifest: PlanManifest,
) -> tuple[Path, ...]:
    locks: set[Path] = {root_dir.parent / "run.lock"}
    for _child, child_dir in _manifest_scope_paths(manifest, root_dir).values():
        if child_dir != root_dir and (child_dir / "run.json").is_file():
            locks.add(child_dir.parent / "run.lock")
    return tuple(sorted(locks, key=lambda path: path.as_posix()))


def _acquire_run_locks(stack: ExitStack, locks: tuple[Path, ...], timeout: float) -> None:
    for lock_path in locks:
        if lock_path.is_symlink():
            raise PlanRevisionError("run_lock_symlink", f"RunStore lock is a symlink: {lock_path}")
        stack.enter_context(RunLock(lock_path, timeout=timeout))


def _audit_record(
    analysis: PlanRevisionAnalysis,
    reason: str,
    actor: str,
) -> dict[str, object]:
    transition_id = uuid.uuid4().hex
    return {
        "schema_version": 1,
        "transition_id": transition_id,
        "timestamp": datetime.now(UTC).isoformat(timespec="microseconds"),
        "actor": actor,
        "reason": reason,
        "previous_plan_sha256": analysis.previous_plan_sha256,
        "candidate_plan_sha256": analysis.candidate_plan_sha256,
        "scopes": [scope.to_document() for scope in analysis.scopes],
        "changed_node_ids": [change.to_document() for change in analysis.changes],
    }


def _append_audit(previous: bytes | None, record: Mapping[str, object]) -> bytes:
    if previous is None:
        prefix = b""
    else:
        if previous and not previous.endswith(b"\n"):
            raise PlanRevisionError("audit_invalid", "plan revision audit log is truncated")
        for line_number, line in enumerate(previous.splitlines(), start=1):
            try:
                decoded = json.loads(line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as error:
                raise PlanRevisionError(
                    "audit_invalid",
                    f"plan revision audit line {line_number} is invalid",
                ) from error
            if not isinstance(decoded, dict):
                raise PlanRevisionError(
                    "audit_invalid", f"plan revision audit line {line_number} is invalid"
                )
        prefix = previous
    line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return prefix + line


def _ensure_candidate_unchanged(project_root: Path, analysis: PlanRevisionAnalysis) -> None:
    plan_path = safe_target(project_root, ".exo/tl-loop/plan.json")
    raw, _ = read_regular_file(plan_path)
    if raw is None or hashlib.sha256(raw).hexdigest() != analysis.candidate_plan_sha256:
        raise PlanRevisionError(
            "candidate_changed_during_apply",
            "plan.json changed while the revision was waiting for RunStore locks",
        )


def _require_applicable(analysis: PlanRevisionAnalysis) -> None:
    if analysis.can_apply:
        return
    details = "; ".join(
        f"{blocker.code} {blocker.node_id or blocker.scope_id or 'plan'}: {blocker.reason}"
        for blocker in analysis.blockers
    )
    raise PlanRevisionError("protected_changes", f"plan revision is blocked: {details}")


def _require_same_candidate(
    initial: PlanRevisionAnalysis,
    locked: PlanRevisionAnalysis,
) -> None:
    if (
        initial.candidate_plan_sha256 != locked.candidate_plan_sha256
        or initial.previous_plan_sha256 != locked.previous_plan_sha256
    ):
        raise PlanRevisionError(
            "candidate_changed_during_apply",
            "plan identity changed while the revision was waiting for RunStore locks",
        )


def _validate_reason(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanRevisionError("reason_required", "plan revision requires a non-empty reason")
    normalized = value.strip()
    if len(normalized) > 4096 or _SECRET_MARKER.search(normalized):
        raise PlanRevisionError(
            "reason_rejected",
            "reason is too long or contains credential-like text",
        )
    return normalized


def _resolve_actor(actor: str | None) -> str:
    value = actor if actor is not None else getpass.getuser()
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 255
        or _SECRET_MARKER.search(value)
    ):
        raise PlanRevisionError("actor_invalid", "operator identity is unavailable")
    return value.strip()
