"""Plan reconciliation support."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from .plan_reconciliation_checkpoint import _reconciled_checkpoint, _scope_needs_update
from .plan_reconciliation_models import (
    PlanReconciliationAnalysis,
    ReconciliationScope,
    _protected_declaration_proofs,
)
from .plan_reconciliation_report import _safe_text
from .plan_revision_types import PlanRevisionError


def _audit_record(
    analysis: PlanReconciliationAnalysis,
    reason: str,
    actor: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "transition_id": uuid.uuid4().hex,
        "operation": "plan_reconciliation",
        "timestamp": datetime.now(UTC).isoformat(timespec="microseconds"),
        "actor": actor,
        "reason": reason,
        "evidence_fingerprint": analysis.evidence_fingerprint,
        "snapshot_sha256": analysis.snapshot_sha256,
        "snapshot_sha256_before": analysis.snapshot_sha256,
        "snapshot_sha256_after": analysis.snapshot_sha256,
        "candidate_sha256": analysis.candidate_sha256,
        "candidate_sha256_before": analysis.candidate_sha256,
        "candidate_sha256_after": analysis.candidate_sha256,
        "scopes": [_audit_scope(scope) for scope in analysis.scopes if _scope_needs_update(scope)],
        "changed_node_ids": list(analysis.changes),
        "protected_declaration_proofs": _audit_declaration_proofs(analysis.scopes),
    }


def _audit_declaration_proofs(
    scopes: tuple[ReconciliationScope, ...],
) -> list[dict[str, object]]:
    return [
        {
            "scope_id": _safe_text(scope.current.scope_id),
            "node_id": _safe_text(proof.node_id),
            "fields": list(proof.fields),
            "reason_code": proof.reason_code,
            "evidence": list(proof.evidence),
            "correlation": dict(proof.correlation),
        }
        for scope in scopes
        for proof in _protected_declaration_proofs(scope)
    ]


def _audit_scope(scope: ReconciliationScope) -> dict[str, object]:
    target = scope.accepted
    restored_checkpoint = _reconciled_checkpoint(scope.raw_checkpoint, scope)
    return {
        "scope_id": _safe_text(scope.current.scope_id),
        "checkpoint_revision_before": scope.state.revision,
        "checkpoint_revision_after": scope.state.revision + 1,
        "manifest_revision": scope.current.manifest_revision,
        "manifest_revision_before": scope.current.manifest_revision,
        "manifest_revision_after": target.manifest_revision if target else None,
        "manifest_sha256_before": scope.current.digest,
        "manifest_sha256_after": target.digest if target else None,
        "checkpoint_sha256_before": hashlib.sha256(scope.raw_checkpoint).hexdigest(),
        "checkpoint_sha256_after": hashlib.sha256(restored_checkpoint).hexdigest(),
        "scope_emitter_proof": (
            scope.scope_emitter_proof.to_document()
            if scope.scope_emitter_proof is not None
            else None
        ),
    }


def _append_audit(previous: bytes | None, record: Mapping[str, object]) -> bytes:
    if previous is None:
        prefix = b""
    else:
        prefix = previous
        if prefix and not prefix.endswith(b"\n"):
            raise PlanRevisionError("reconciliation_audit_invalid", "audit log is truncated")
        _validate_audit_lines(prefix)
    entry = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return prefix + (entry + "\n").encode("utf-8")


def _validate_audit_lines(raw: bytes) -> None:
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
