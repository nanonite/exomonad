"""Plan reconciliation support."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .plan_manifest import PlanManifest
from .plan_reconciliation_declarations import _declaration_changes, _manifest_node_summary
from .plan_reconciliation_execution import (
    ProtectedDeclarationProof,
    evaluate_protected_declaration_restore,
)
from .plan_reconciliation_legacy import proven_legacy_default_fields
from .plan_reconciliation_ownership import _runtime_record_summary, _slice_ownership_summary
from .plan_reconciliation_report import (
    _persisted_stages,
    _retained_state_summary,
    _safe_blocker,
    _safe_stages,
    _safe_text,
    _stage_projection,
)
from .plan_revision_types import PlanRevisionBlocker
from .schema import RunState


def _protected_declaration_proofs(scope: ReconciliationScope) -> list[ProtectedDeclarationProof]:
    if scope.accepted is None:
        return []
    accepted = {node.node_id: node for node in scope.accepted.nodes}
    return [
        proof
        for node in scope.current.nodes
        if node.node_id in accepted
        for proof in (evaluate_protected_declaration_restore(scope, node, accepted[node.node_id]),)
        if proof is not None
    ]


@dataclass(frozen=True)
class ReconciliationScope:
    """One captured RunStore and its accepted-plan reconstruction target."""

    run_dir: Path = field(repr=False)
    relative_checkpoint: str
    raw_checkpoint: bytes = field(repr=False)
    state: RunState = field(repr=False)
    current: PlanManifest = field(repr=False)
    accepted: PlanManifest | None = field(repr=False)
    parent_manifest_matches: bool
    run_identity_matches: bool
    action_journal_entries: tuple[Mapping[str, object], ...] | None = field(
        default=None, repr=False, compare=False
    )
    action_journal_status: str = "missing"
    action_journal_sha256: str | None = None
    protected_node_ids: tuple[str, ...] = ()

    def to_document(self, blocked_node_ids: tuple[str, ...] = ()) -> dict[str, object]:
        phase = self.state.recursive_fsm
        phase_digest = getattr(phase, "plan_digest", None)
        accepted_digest = self.accepted.digest if self.accepted else None
        legacy_equivalences = (
            proven_legacy_default_fields(self.current, self.accepted, self.state)
            if self.accepted is not None
            else {}
        )
        declaration_proofs = _protected_declaration_proofs(self)
        current_order = getattr(phase, "current_order", None)
        expected_stages = _stage_projection(self.accepted) if self.accepted else ()
        persisted_stages = _persisted_stages(self.state)
        return {
            "scope_id": _safe_text(self.current.scope_id),
            "checkpoint_revision": self.state.revision,
            "manifest_revision": self.current.manifest_revision,
            "accepted_manifest_revision": (
                self.accepted.manifest_revision if self.accepted is not None else None
            ),
            "current_manifest_sha256": self.current.digest,
            "accepted_manifest_sha256": accepted_digest,
            "fsm_plan_sha256": _safe_text(phase_digest),
            "fsm_matches_current": phase_digest == self.current.digest,
            "fsm_matches_accepted": phase_digest == accepted_digest,
            "parent_manifest_matches": self.parent_manifest_matches,
            "run_identity_matches": self.run_identity_matches,
            "current_order": current_order,
            "persisted_stages": _safe_stages(persisted_stages),
            "accepted_stages": _safe_stages(expected_stages),
            "checkpoint_sha256": hashlib.sha256(self.raw_checkpoint).hexdigest(),
            "retained_state": _retained_state_summary(self.state),
            "slice_ownership": _slice_ownership_summary(self.state),
            "runtime_records": _runtime_record_summary(self.state),
            "action_journal": {
                "status": self.action_journal_status,
                "sha256": self.action_journal_sha256,
                "entry_count": len(self.action_journal_entries or ()),
            },
            "current_manifest_nodes": _manifest_node_summary(self.current),
            "accepted_manifest_nodes": (
                _manifest_node_summary(self.accepted) if self.accepted is not None else []
            ),
            "declaration_changes": _declaration_changes(
                self.current,
                self.accepted,
                self.protected_node_ids,
                blocked_node_ids,
            ),
            "legacy_default_equivalences": [
                {
                    "node_id": _safe_text(node_id),
                    "fields": list(fields),
                    "manifest_schema_version": self.current.schema_version,
                    "accepted_manifest_sha256": accepted_digest,
                    "evidence_source": "verified_snapshot_and_matching_fsm_dispatch_identity",
                }
                for node_id, fields in sorted(legacy_equivalences.items())
            ],
            "protected_declaration_proofs": [
                {
                    "node_id": _safe_text(proof.node_id),
                    "fields": list(proof.fields),
                    "disposition": "proven" if proof.proven else "blocked",
                    "reason_code": proof.reason_code,
                    "evidence": list(proof.evidence),
                }
                for proof in declaration_proofs
            ],
        }


@dataclass(frozen=True)
class PlanReconciliationAnalysis:
    """Immutable report plus captured participants for the apply boundary."""

    evidence_fingerprint: str
    snapshot_sha256: str
    candidate_sha256: str
    scopes: tuple[ReconciliationScope, ...]
    changes: tuple[dict[str, object], ...]
    candidate_changes: tuple[dict[str, object], ...]
    candidate_declaration_changes: tuple[dict[str, object], ...]
    blockers: tuple[PlanRevisionBlocker, ...]
    input_hashes: tuple[tuple[str, str | None], ...]
    audit_bytes: bytes | None = field(repr=False, compare=False)

    @property
    def can_apply(self) -> bool:
        return not self.blockers and self.has_drift

    @property
    def has_drift(self) -> bool:
        return any(
            scope.accepted is None
            or scope.current != scope.accepted
            or getattr(scope.state.recursive_fsm, "plan_digest", None) != scope.accepted.digest
            or not scope.parent_manifest_matches
            or not scope.run_identity_matches
            for scope in self.scopes
        )

    def to_document(self) -> dict[str, object]:
        blocked_node_ids = tuple(
            blocker.node_id for blocker in self.blockers if blocker.node_id is not None
        )
        return {
            "operation": "plan_reconciliation",
            "accepted_plan_authority": "verified plan.snapshot and plan.snapshot.sha256",
            "rejected_alternatives": [
                "adopt the candidate plan as accepted execution history",
                "treat a current manifest as blanket authority",
                "rewrite a protected node without dispatch-time proof",
            ],
            "evidence_fingerprint": self.evidence_fingerprint,
            "snapshot_sha256": self.snapshot_sha256,
            "candidate_sha256": self.candidate_sha256,
            "can_apply": self.can_apply,
            "has_drift": self.has_drift,
            "inputs": [
                {"path": _safe_text(path), "sha256": digest} for path, digest in self.input_hashes
            ],
            "scopes": [scope.to_document(blocked_node_ids) for scope in self.scopes],
            "manifest_changes_to_accepted_plan": list(self.changes),
            "candidate_changes_not_applied": list(self.candidate_changes),
            "candidate_declaration_changes_not_applied": list(self.candidate_declaration_changes),
            "blockers": [_safe_blocker(item) for item in self.blockers],
        }
