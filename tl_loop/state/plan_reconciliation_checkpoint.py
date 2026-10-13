"""Plan reconciliation support."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping

from .plan_reconciliation_models import ReconciliationScope
from .plan_revision_state import decode_checkpoint_bytes
from .plan_revision_state_update import _update_changed_declaration_fields
from .plan_revision_types import PlanRevisionError
from .schema import validate


def _scope_needs_update(scope: ReconciliationScope) -> bool:
    target = scope.accepted
    return target is not None and (
        scope.current != target
        or getattr(scope.state.recursive_fsm, "plan_digest", None) != target.digest
    )


def _reconciled_checkpoint(raw: bytes, scope: ReconciliationScope) -> bytes:
    target = scope.accepted
    if target is None:
        raise PlanRevisionError("accepted_scope_missing", "cannot restore an unknown scope")
    document = _decode_document(raw, scope.relative_checkpoint)
    state = decode_checkpoint_bytes(scope.run_dir / "run.json", raw)
    _reconcile_slice_declarations(document, scope.current, target)
    document["plan_manifest"] = target.to_document()
    _rebind_fsm_digest(document, target.digest)
    document["revision"] = state.revision + 1
    _validate_preserved_fields(document, raw, scope.relative_checkpoint)
    try:
        validate(document)
        decode_checkpoint_bytes(scope.run_dir / "run.json", _json_bytes(document))
    except (TypeError, ValueError) as error:
        raise PlanRevisionError(
            "reconciliation_checkpoint_invalid",
            "restored RunStore failed schema or identity validation",
        ) from error
    return _json_bytes(document)


def _decode_document(raw: bytes, checkpoint: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PlanRevisionError("runstore_unreadable", f"cannot decode {checkpoint}") from error
    if not isinstance(value, dict):
        raise PlanRevisionError("runstore_invalid", f"{checkpoint} must contain an object")
    return value


def _reconcile_slice_declarations(
    document: dict[str, object],
    current: object,
    target: object,
) -> None:
    slices = document.get("slices")
    if not isinstance(slices, dict):
        raise PlanRevisionError("runstore_invalid", "RunStore slice map is malformed")
    current_nodes = {node.node_id: node for node in current.nodes}
    for node in target.nodes:
        record = slices.get(node.name)
        previous = current_nodes.get(node.node_id)
        if previous is None or not isinstance(record, dict):
            raise PlanRevisionError("node_topology_drift", "RunStore node topology is incomplete")
        _update_changed_declaration_fields(record, previous, node)
        record["manifest_node_id"] = node.node_id
        record["manifest_revision"] = target.manifest_revision


def _rebind_fsm_digest(document: dict[str, object], digest: str | None) -> None:
    fsm = document.get("fsm")
    payload = fsm.get("payload") if isinstance(fsm, dict) else None
    if not isinstance(payload, dict) or not isinstance(digest, str):
        raise PlanRevisionError("runtime_digest_unproven", "RunStore lacks a rebindable FSM digest")
    payload["plan_digest"] = digest


def _validate_preserved_fields(
    updated: dict[str, object],
    before: bytes,
    checkpoint: str,
) -> None:
    original = _decode_document(before, checkpoint)
    updated_copy = copy.deepcopy(updated)
    original_copy = copy.deepcopy(original)
    _remove_mutable_reconciliation_fields(updated_copy)
    _remove_mutable_reconciliation_fields(original_copy)
    if updated_copy != original_copy:
        raise PlanRevisionError(
            "reconciliation_mutated_receipts",
            "reconciliation attempted to alter execution evidence outside plan bindings",
        )


def _remove_mutable_reconciliation_fields(document: dict[str, object]) -> None:
    document.pop("plan_manifest", None)
    document.pop("revision", None)
    slices = document.get("slices")
    if isinstance(slices, dict):
        mutable = {
            "paths",
            "depends_on",
            "branch",
            "worktree",
            "requested_harness",
            "chainlink_issue_id",
            "manifest_node_id",
            "manifest_revision",
        }
        for record in slices.values():
            if isinstance(record, dict):
                for key in mutable:
                    record.pop(key, None)
    fsm = document.get("fsm")
    payload = fsm.get("payload") if isinstance(fsm, dict) else None
    if isinstance(payload, dict):
        payload.pop("plan_digest", None)


def _json_bytes(document: Mapping[str, object]) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
