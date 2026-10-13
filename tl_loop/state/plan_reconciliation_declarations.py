"""Safe manifest declaration diagnostics for plan reconciliation."""

from __future__ import annotations

import hashlib

from .plan_manifest import PlanManifest
from .plan_reconciliation_report import _safe_text, _safe_value
from .plan_revision_diff import changed_node_fields


def _declaration_changes(
    current: PlanManifest,
    accepted: PlanManifest | None,
    protected_node_ids: tuple[str, ...],
    blocked_node_ids: tuple[str, ...] = (),
    *,
    unprotected_disposition: str = "restore",
    evidence_source: str = "verified_plan_snapshot",
) -> list[dict[str, object]]:
    current_nodes = {node.node_id: node for node in current.nodes}
    accepted_nodes = {node.node_id: node for node in accepted.nodes} if accepted else {}
    changes = []
    for node_id in sorted(set(current_nodes) | set(accepted_nodes)):
        observed, proposed = current_nodes.get(node_id), accepted_nodes.get(node_id)
        fields = (
            changed_node_fields(observed, proposed)
            if observed is not None and proposed is not None
            else ()
        )
        if observed is None or proposed is None:
            fields = tuple(
                sorted(
                    set(observed.to_document() if observed else {})
                    | set(proposed.to_document() if proposed else {})
                )
            )
        if not fields:
            continue
        observed_doc = observed.to_document() if observed else {}
        proposed_doc = proposed.to_document() if proposed else {}
        changes.append(
            {
                "node_id": _safe_text(node_id),
                "node_name": _safe_text((observed or proposed).name),
                "protected": node_id in protected_node_ids,
                "disposition": "blocked"
                if node_id in blocked_node_ids
                else unprotected_disposition,
                "evidence_source": evidence_source if proposed else "none",
                "fields": [
                    {
                        "field": _safe_text(name),
                        "observed": _safe_declaration_value(
                            name, _nested_value(observed_doc, name)
                        ),
                        "proposed": _safe_declaration_value(
                            name, _nested_value(proposed_doc, name)
                        ),
                    }
                    for name in fields
                ],
            }
        )
    return changes


def _safe_declaration_value(field: str, value: object) -> object:
    if isinstance(value, str) and any(
        marker in field.casefold() for marker in ("task", "prompt", "body", "transcript", "review")
    ):
        return {"sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(), "length": len(value)}
    return _safe_value(value)


def _manifest_node_summary(manifest: PlanManifest) -> list[dict[str, object]]:
    return [
        {
            "node_id": _safe_text(node.node_id),
            "name": _safe_text(node.name),
            "kind": node.kind,
            "task_sha256": hashlib.sha256(node.task.encode("utf-8")).hexdigest(),
            "task_length": len(node.task),
            "order": node.order,
            "chainlink_issue_id": node.chainlink_issue_id,
            "branch": _safe_text(node.owned_branch),
            "worktree": _safe_text(node.worktree),
            "boundary": [_safe_text(path) for path in node.boundary],
            "depends_on": [_safe_text(name) for name in node.depends_on],
            "child_manifest_sha256": node.child_manifest_digest,
        }
        for node in manifest.nodes
    ]


def _nested_value(document: dict[str, object], path: str) -> object:
    current: object = document
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _recursive_declaration_changes(
    current: PlanManifest,
    accepted: PlanManifest,
    protected_node_ids: tuple[str, ...],
    blocked_node_ids: tuple[str, ...] = (),
    *,
    unprotected_disposition: str = "restore",
    evidence_source: str = "verified_plan_snapshot",
) -> list[dict[str, object]]:
    changes = _declaration_changes(
        current,
        accepted,
        protected_node_ids,
        blocked_node_ids,
        unprotected_disposition=unprotected_disposition,
        evidence_source=evidence_source,
    )
    for change in changes:
        change["scope_id"] = _safe_text(current.scope_id)
    for node_id, current_child in current.child_manifests.items():
        accepted_child = accepted.child_manifests.get(node_id)
        if accepted_child is None:
            continue
        changes.extend(
            _recursive_declaration_changes(
                current_child,
                accepted_child,
                protected_node_ids,
                blocked_node_ids,
                unprotected_disposition=unprotected_disposition,
                evidence_source=evidence_source,
            )
        )
    return changes
