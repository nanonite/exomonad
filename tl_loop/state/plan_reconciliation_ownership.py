"""RunStore dispatch, publication, and receipt diagnostics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from tl_loop.fsm.child import ChildRecord
from tl_loop.fsm.scope import TLPlanning, TLRunning

from .plan_reconciliation_report import _safe_text
from .schema import RunState, SliceState


def _slice_ownership_summary(state: RunState) -> list[dict[str, object]]:
    return [
        _slice_ownership_record(record)
        for record in sorted(state.slices.values(), key=lambda item: item.id)
    ]


def _slice_ownership_record(record: SliceState) -> dict[str, object]:
    publication = record.publication
    post_merge = record.post_merge
    evidence = post_merge.evidence if post_merge is not None else {}
    evidence_bytes = json.dumps(dict(evidence), sort_keys=True, separators=(",", ":")).encode()
    return {
        "slice_id": _safe_text(record.id),
        "status": record.status.value,
        "manifest_node_id": _safe_text(record.manifest_node_id),
        "manifest_revision": record.manifest_revision,
        "chainlink_issue_id": record.chainlink_issue_id,
        "branch": _safe_text(record.branch),
        "worktree": _safe_text(record.worktree),
        "pr_number": record.pr_number,
        "reviewed_head": record.reviewed_head,
        "dispatch_intent_id": _safe_text(record.dispatch_intent_id),
        "dispatch_agent_id": _safe_text(record.dispatch_agent_id),
        "dispatch_invocation_id": _safe_text(record.dispatch_invocation_id),
        "publication": (
            {
                "pr_number": publication.pr_number,
                "head_sha": publication.head_sha,
                "head_branch": _safe_text(publication.head_branch),
                "base_branch": _safe_text(publication.base_branch),
                "attempt": publication.attempt,
                "invocation_id": _safe_text(publication.invocation_id),
            }
            if publication is not None
            else None
        ),
        "post_merge_phase": post_merge.phase.value if post_merge is not None else None,
        "post_merge_evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest()
        if evidence
        else None,
        "park_cause": record.park_cause.value if record.park_cause is not None else None,
        "park_issue_id": record.park_issue_id,
        "park_audit": _park_audit_summary(record.park_audit),
    }


def _runtime_record_summary(state: RunState) -> list[dict[str, object]]:
    return [_runtime_record_record(record) for record in _records_for(state.recursive_fsm)]


def _park_audit_summary(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    summary: dict[str, object] = {}
    for key in ("error_kind", "reason_code"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            summary[key] = _safe_text(candidate)
    missing = value.get("missing_evidence")
    if isinstance(missing, list) and all(isinstance(item, str) for item in missing):
        summary["missing_evidence"] = [_safe_text(item) for item in missing[:50]]
    retryable = value.get("retryable")
    if isinstance(retryable, bool):
        summary["retryable"] = retryable
    return summary


def _runtime_record_record(record: ChildRecord) -> dict[str, object]:
    evidence = dict(record.evidence)
    evidence_bytes = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    return {
        "child_id": _safe_text(record.child_id),
        "kind": record.kind.value,
        "manifest_node_id": _safe_text(record.manifest_node_id),
        "manifest_revision": record.manifest_revision,
        "dispatch_intent_id": _safe_text(record.dispatch_intent_id),
        "invocation_id": _safe_text(record.invocation_id),
        "lane_id": _safe_text(record.lane_id),
        "evidence_keys": sorted(_safe_text(key) for key in evidence),
        "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest() if evidence else None,
    }


def _records_for(phase: object) -> tuple[ChildRecord, ...]:
    if isinstance(phase, TLPlanning):
        ordered = tuple(record for _order, records in phase.ordered_children for record in records)
        return (*phase.parallel_children, *ordered)
    if not isinstance(phase, TLRunning):
        return ()
    pending = tuple(record for records in phase.pending_by_order.values() for record in records)
    return (*phase.parallel_pending, *pending, *phase.completed_children.values())
