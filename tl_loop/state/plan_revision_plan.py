"""Plan file validation and canonical manifest construction."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from tl_loop.plan_validation import PlanValidationError, validate_plan_document

from .plan_manifest import ManifestError, PlanManifest, build_plan_manifest
from .plan_revision_types import DecodedPlan, PlanRevisionError


def reject_pending_transition(state_root: Path) -> None:
    """Refuse analysis while startup recovery owns a plan transition."""
    if any(
        path.exists()
        for path in (
            state_root / "plan-transition.json",
            state_root / "plan-revision-transition.json",
        )
    ):
        raise PlanRevisionError(
            "transition_pending",
            "an unresolved plan transition journal exists; recover it before revising the plan",
        )


def read_required(path: Path, code: str) -> bytes:
    """Read one required plan identity file with an actionable error."""
    if path.is_symlink():
        raise PlanRevisionError(code, f"refusing symlinked plan identity file: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise PlanRevisionError(code, f"cannot read {path}: {error}") from error


def decode_digest(data: bytes, path: Path) -> str:
    """Decode the exact lowercase SHA-256 format used by the Rust launcher."""
    try:
        value = data.decode("ascii").strip()
    except UnicodeError as error:
        raise PlanRevisionError(
            "snapshot_digest_invalid",
            f"snapshot digest {path} is not ASCII text",
        ) from error
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise PlanRevisionError(
            "snapshot_digest_invalid",
            f"snapshot digest {path} is not a lowercase SHA-256 value",
        )
    return value


def decode_plan(data: bytes, path: Path, *, allow_legacy_leaf_bindings: bool) -> DecodedPlan:
    """Validate one saved or candidate plan without changing its file."""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PlanRevisionError("plan_invalid", f"plan {path} is not valid UTF-8 JSON") from error
    if not isinstance(raw, dict):
        raise PlanRevisionError("plan_invalid", f"plan {path} must be a JSON object")
    document = _add_legacy_null_bindings(raw) if allow_legacy_leaf_bindings else raw
    try:
        validated = validate_plan_document(document)
        from tl_loop.loop.driver import WorkPlan

        plan = WorkPlan.from_mapping(cast(Mapping[str, object], validated["plan"]), path="plan")
    except (PlanValidationError, TypeError, ValueError) as error:
        raise PlanRevisionError("plan_invalid", f"plan {path} is invalid: {error}") from error
    return DecodedPlan(validated, plan)


def _add_legacy_null_bindings(document: dict[str, object]) -> dict[str, object]:
    """Treat a pre-#1185 missing leaf binding as explicit null for comparison."""
    normalized = copy.deepcopy(document)
    plan = normalized.get("plan")
    if plan is None:
        plan = {
            key: normalized[key] for key in ("workers", "leaves", "sub_tls") if key in normalized
        }
    if not isinstance(plan, dict):
        return normalized
    _add_legacy_null_bindings_to_scope(plan)
    if "plan" in normalized:
        normalized["plan"] = plan
    else:
        normalized.update(plan)
    return normalized


def _add_legacy_null_bindings_to_scope(plan: dict[str, object]) -> None:
    leaves = plan.get("leaves", [])
    if isinstance(leaves, list):
        for leaf in leaves:
            if isinstance(leaf, dict):
                leaf.setdefault("chainlink_issue_id", None)
    sub_tls = plan.get("sub_tls", [])
    if not isinstance(sub_tls, list):
        return
    for sub_tl in sub_tls:
        if not isinstance(sub_tl, dict):
            continue
        nested = sub_tl.get("plan")
        if nested is None:
            nested = {key: sub_tl[key] for key in ("workers", "leaves", "sub_tls") if key in sub_tl}
        if not isinstance(nested, dict):
            continue
        _add_legacy_null_bindings_to_scope(nested)
        if "plan" in sub_tl:
            sub_tl["plan"] = nested
        else:
            for key in ("workers", "leaves", "sub_tls"):
                if key in sub_tl:
                    sub_tl[key] = nested.get(key, sub_tl[key])


def preserve_run_settings(previous: Mapping[str, object], candidate: Mapping[str, object]) -> None:
    """Keep the active run identity and budgets outside plan revisions."""
    if previous.get("run_id") != candidate.get("run_id"):
        raise PlanRevisionError("run_id_changed", "plan revision cannot change run_id")
    if previous.get("budgets") != candidate.get("budgets"):
        raise PlanRevisionError("budgets_changed", "plan revision cannot change run budgets")


def build_root_manifest(plan: object, template: PlanManifest) -> PlanManifest:
    """Build a root candidate with the persisted scope ownership identity."""
    from tl_loop.loop.driver import WorkPlan, _manifest_plan

    if not isinstance(plan, WorkPlan):
        raise PlanRevisionError("plan_invalid", "validated plan has an unexpected type")
    try:
        return build_plan_manifest(
            _manifest_plan(plan),
            scope_id=template.scope_id,
            parent_scope_id=template.parent_scope_id,
            role=template.role,
            owned_branch=template.owned_branch,
            parent_integration_target=template.parent_integration_target,
            manifest_revision=template.manifest_revision,
        )
    except (ManifestError, TypeError, ValueError) as error:
        raise PlanRevisionError(
            "manifest_invalid", f"candidate manifest is invalid: {error}"
        ) from error
