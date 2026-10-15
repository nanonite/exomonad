"""Durable, scope-bound approval records for aggregate integration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class AggregateApprovalError(ValueError):
    """An approval policy or receipt is not safe to use."""


class UnsupportedAggregateApprovalMode(AggregateApprovalError):
    """A reserved aggregate reviewer policy was requested before support."""


_PARENT_APPROVAL_COORDINATES = frozenset(
    {
        "parent_run_id",
        "parent_scope_path",
        "child_scope_id",
        "parent_agent_id",
        "parent_branch",
        "repository_url",
        "plan_manifest_digest",
        "plan_manifest_revision",
        "approval_policy_digest",
        "child_manifest_digest",
        "pr_number",
        "head_sha",
        "head_tree_sha",
        "base_sha",
        "patch_digest",
        "merge_tree_sha",
        "ci_status",
        "readiness_receipt_digest",
        "lineage_proof_digest",
        "lineage_commit_shas",
        "reviewed_leaf_ids",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "receipt_id",
        "policy_mode",
        "parent_run_id",
        "parent_scope_path",
        "child_scope_id",
        "parent_agent_id",
        "parent_branch",
        "repository_url",
        "plan_manifest_digest",
        "plan_manifest_revision",
        "approval_policy_digest",
        "child_manifest_digest",
        "pr_number",
        "head_sha",
        "head_tree_sha",
        "base_sha",
        "patch_digest",
        "merge_tree_sha",
        "ci_status",
        "readiness_receipt_digest",
        "lineage_proof_digest",
        "lineage_commit_shas",
        "reviewed_leaf_ids",
        "created_at",
        "supersedes_receipt_id",
    }
)
_RECEIPT_NON_TEXT_FIELDS = frozenset(
    {
        "policy_mode",
        "parent_scope_path",
        "lineage_commit_shas",
        "reviewed_leaf_ids",
        "plan_manifest_revision",
        "pr_number",
        "supersedes_receipt_id",
    }
)


def parse_aggregate_approval_mode(value: object = None) -> str:
    """Return current parent mode or reject unsupported/unknown plan values."""
    if value is None or value == "parent":
        return "parent"
    if value == "reviewer":
        raise UnsupportedAggregateApprovalMode(
            "aggregate_approval_mode='reviewer' is reserved and not supported yet"
        )
    raise AggregateApprovalError("aggregate_approval_mode must be 'parent'")


def canonical_digest(value: Mapping[str, object]) -> str:
    """Hash one canonical JSON object with no caller-controlled encoding."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_parent_aggregate_approval(
    existing: Sequence[AggregateApprovalReceipt],
    coordinates: Mapping[str, object],
    *,
    created_at: str,
) -> tuple[tuple[AggregateApprovalReceipt, ...], AggregateApprovalReceipt]:
    """Return the existing decision or append an immutable superseding one."""
    _validate_coordinate_keys(coordinates)
    current_policy = {**dict(coordinates), "policy_mode": "parent"}
    prior = _matching_parent_approval(existing, current_policy)
    if prior is not None:
        return tuple(existing), prior
    receipt = _new_parent_approval(existing, coordinates, created_at)
    return (*existing, receipt), receipt


def _validate_coordinate_keys(coordinates: Mapping[str, object]) -> None:
    """Reject omitted and unrecognized coordinates before receipt creation."""
    if set(coordinates) != _PARENT_APPROVAL_COORDINATES:
        raise AggregateApprovalError("parent approval coordinates are incomplete or unknown")


def _matching_parent_approval(
    existing: Sequence[AggregateApprovalReceipt],
    current_policy: Mapping[str, object],
) -> AggregateApprovalReceipt | None:
    """Reuse a prior decision only when its complete coordinates match."""
    for receipt in existing:
        prior = receipt.to_document()
        for key in ("receipt_id", "created_at", "supersedes_receipt_id"):
            prior.pop(key)
        if prior == current_policy:
            return receipt
    return None


def _new_parent_approval(
    existing: Sequence[AggregateApprovalReceipt],
    coordinates: Mapping[str, object],
    created_at: str,
) -> AggregateApprovalReceipt:
    """Create the next content-addressed decision linked to retained history."""
    document = {
        **dict(coordinates),
        "receipt_id": "",
        "policy_mode": "parent",
        "created_at": created_at,
        "supersedes_receipt_id": existing[-1].receipt_id if existing else None,
    }
    document["receipt_id"] = canonical_digest(approval_identity(document))
    return validate_receipt_document(document)


def approval_reference(
    receipt: AggregateApprovalReceipt, controller_epoch: str
) -> dict[str, object]:
    """Build the non-authoritative lookup reference checked by the host."""
    if not isinstance(controller_epoch, str) or not controller_epoch.strip():
        raise AggregateApprovalError("current controller epoch is required for merge authorization")
    return {
        "receipt_id": receipt.receipt_id,
        "parent_run_id": receipt.parent_run_id,
        "parent_scope_path": list(receipt.parent_scope_path),
        "child_scope_id": receipt.child_scope_id,
        "controller_epoch": controller_epoch,
    }


@dataclass(frozen=True)
class AggregateApprovalReceipt:
    """Immutable parent decision bound to one accepted aggregate compare."""

    receipt_id: str
    parent_run_id: str
    parent_scope_path: tuple[str, ...]
    child_scope_id: str
    parent_agent_id: str
    parent_branch: str
    repository_url: str
    plan_manifest_digest: str
    plan_manifest_revision: int
    approval_policy_digest: str
    child_manifest_digest: str
    pr_number: int
    head_sha: str
    head_tree_sha: str
    base_sha: str
    patch_digest: str
    merge_tree_sha: str
    ci_status: str
    readiness_receipt_digest: str
    lineage_proof_digest: str
    lineage_commit_shas: tuple[str, ...]
    reviewed_leaf_ids: tuple[str, ...]
    created_at: str
    supersedes_receipt_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "receipt_id",
            "parent_run_id",
            "child_scope_id",
            "parent_agent_id",
            "parent_branch",
            "repository_url",
            "plan_manifest_digest",
            "approval_policy_digest",
            "child_manifest_digest",
            "head_sha",
            "head_tree_sha",
            "base_sha",
            "patch_digest",
            "merge_tree_sha",
            "readiness_receipt_digest",
            "lineage_proof_digest",
            "created_at",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise AggregateApprovalError(f"{name} must be non-empty")
        if not self.parent_scope_path or self.parent_scope_path[-1] != self.parent_run_id:
            raise AggregateApprovalError("parent_scope_path must end at parent_run_id")
        if type(self.plan_manifest_revision) is not int or self.plan_manifest_revision < 1:
            raise AggregateApprovalError("plan_manifest_revision must be positive")
        if type(self.pr_number) is not int or self.pr_number <= 0:
            raise AggregateApprovalError("pr_number must be positive")
        if self.ci_status not in {"success", "neutral"}:
            raise AggregateApprovalError("CI must be success or neutral for parent approval")
        if not self.lineage_commit_shas or any(not item for item in self.lineage_commit_shas):
            raise AggregateApprovalError("lineage_commit_shas must contain proven commits")
        if any(not item for item in self.reviewed_leaf_ids):
            raise AggregateApprovalError("reviewed_leaf_ids cannot contain empty values")
        if self.supersedes_receipt_id == self.receipt_id:
            raise AggregateApprovalError("an approval receipt cannot supersede itself")

    def to_document(self) -> dict[str, object]:
        """Return the closed JSON projection used by RunStore and MCP refs."""
        return {
            "receipt_id": self.receipt_id,
            "policy_mode": "parent",
            "parent_run_id": self.parent_run_id,
            "parent_scope_path": list(self.parent_scope_path),
            "child_scope_id": self.child_scope_id,
            "parent_agent_id": self.parent_agent_id,
            "parent_branch": self.parent_branch,
            "repository_url": self.repository_url,
            "plan_manifest_digest": self.plan_manifest_digest,
            "plan_manifest_revision": self.plan_manifest_revision,
            "approval_policy_digest": self.approval_policy_digest,
            "child_manifest_digest": self.child_manifest_digest,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "head_tree_sha": self.head_tree_sha,
            "base_sha": self.base_sha,
            "patch_digest": self.patch_digest,
            "merge_tree_sha": self.merge_tree_sha,
            "ci_status": self.ci_status,
            "readiness_receipt_digest": self.readiness_receipt_digest,
            "lineage_proof_digest": self.lineage_proof_digest,
            "lineage_commit_shas": list(self.lineage_commit_shas),
            "reviewed_leaf_ids": list(self.reviewed_leaf_ids),
            "created_at": self.created_at,
            "supersedes_receipt_id": self.supersedes_receipt_id,
        }


def approval_identity(value: Mapping[str, object]) -> dict[str, object]:
    """Select immutable approval coordinates used to derive the receipt ID."""
    return {key: item for key, item in value.items() if key not in {"receipt_id", "created_at"}}


def validate_receipt_document(value: object) -> AggregateApprovalReceipt:
    """Decode a durable receipt and verify its content-derived ID."""
    if not isinstance(value, Mapping):
        raise AggregateApprovalError("aggregate approval receipt must be an object")
    if set(value) != _RECEIPT_FIELDS:
        raise AggregateApprovalError("aggregate approval receipt fields are incomplete or unknown")
    if value.get("policy_mode") != "parent":
        raise UnsupportedAggregateApprovalMode("approval receipt policy is not parent mode")
    receipt = _decode_receipt(value)
    expected_id = canonical_digest(approval_identity(receipt.to_document()))
    if receipt.receipt_id != expected_id:
        raise AggregateApprovalError("aggregate approval receipt ID does not match its contents")
    return receipt


def _decode_receipt(value: Mapping[str, object]) -> AggregateApprovalReceipt:
    """Decode validated fields into the immutable receipt value."""
    path = _string_tuple(value.get("parent_scope_path"), "parent_scope_path")
    commits = _string_tuple(value.get("lineage_commit_shas"), "lineage_commit_shas")
    leaves = _string_tuple(value.get("reviewed_leaf_ids"), "reviewed_leaf_ids", allow_empty=True)
    text_values = _receipt_text_fields(value)
    revision = value.get("plan_manifest_revision")
    pr_number = value.get("pr_number")
    if type(revision) is not int or type(pr_number) is not int:
        raise AggregateApprovalError("receipt revision and PR number must be integers")
    return AggregateApprovalReceipt(
        **text_values,
        parent_scope_path=path,
        lineage_commit_shas=commits,
        reviewed_leaf_ids=leaves,
        plan_manifest_revision=revision,
        pr_number=pr_number,
        supersedes_receipt_id=_nullable_text(value.get("supersedes_receipt_id")),
    )


def _receipt_text_fields(value: Mapping[str, object]) -> dict[str, str]:
    """Validate the receipt's scalar text coordinates."""
    text_fields = _RECEIPT_FIELDS - _RECEIPT_NON_TEXT_FIELDS
    return {key: _required_text(value.get(key), key) for key in text_fields}


def _nullable_text(value: object) -> str | None:
    """Decode the optional predecessor link without coercion."""
    if value is not None and not isinstance(value, str):
        raise AggregateApprovalError("supersedes_receipt_id must be a string or null")
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AggregateApprovalError(f"{field} must be non-empty text")
    return value


def _string_tuple(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AggregateApprovalError(f"{field} must be an array")
    result = tuple(value)
    if (not allow_empty and not result) or any(
        not isinstance(item, str) or not item for item in result
    ):
        raise AggregateApprovalError(f"{field} must contain non-empty strings")
    return result


__all__ = [
    "AggregateApprovalError",
    "AggregateApprovalReceipt",
    "UnsupportedAggregateApprovalMode",
    "approval_identity",
    "canonical_digest",
    "parse_aggregate_approval_mode",
    "validate_receipt_document",
]
