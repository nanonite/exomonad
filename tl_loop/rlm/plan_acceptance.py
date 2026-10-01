"""Human acceptance gate that turns an authored plan into execution authority.

An :class:`~tl_loop.rlm.plan_authoring.AuthoredPlan` is a proposal. This module
is the only path that makes it authority, and it refuses until an explicit
human answer exists for the proposal's exact digest:

* the gate name is derived from the plan digest, so approving one proposal can
  never approve a different one;
* the proposal's validated plan identity and its bounded judgment audit record
  are durable, so a second process can finish the flow without re-running the
  judgment, and a restart before acceptance still has no authority;
* installation goes through the same ``set_plan_manifest`` path the controller
  uses, with dispatched ownership protected, so a continuation cannot rewrite
  completed history.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from tl_loop.client.transport import JsonObject, JsonValue
from tl_loop.state.plan_manifest import ManifestError
from tl_loop.state.schema import GateStatus, RunState, SliceStatus
from tl_loop.state.store import CorruptCheckpoint, RunStore

from .plan_authoring import (
    AUTHORING_AUDIT_KEYS,
    MAX_AUDIT_VIOLATION_CHARS,
    MAX_AUDIT_VIOLATIONS,
    AuthoredPlan,
    PlanAuthoringError,
    build_authoring_manifest,
)

ACCEPTANCE_GATE_PREFIX = "plan-acceptance"
ACCEPTANCE_PENDING = "pending"
ACCEPTANCE_APPROVED = "accepted"
AUTHORING_RECORD_SCHEMA_VERSION = 1
_AUTHORING_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "plan_digest",
        "gate_name",
        "status",
        "owned_branch",
        "document",
        "audit",
    }
)
_AUTHORING_RECORD_STATUSES = frozenset({ACCEPTANCE_PENDING, ACCEPTANCE_APPROVED})


class PlanAcceptanceError(RuntimeError):
    """The authored plan cannot become execution authority right now."""


class PlanAcceptanceRequired(PlanAcceptanceError):
    """No explicit human acceptance exists for this exact proposal."""


class PlanRejected(PlanAcceptanceError):
    """A human rejected this exact proposal."""


class PlanAuthoringRecordError(PlanAcceptanceError):
    """The durable plan-authoring record is missing, malformed, or edited."""


def acceptance_gate_name(plan: AuthoredPlan | str) -> str:
    """Return the durable gate name bound to one proposal's digest."""
    digest = plan if isinstance(plan, str) else plan.digest
    return f"{ACCEPTANCE_GATE_PREFIX}-{digest[:16]}"


def acceptance_status(state: RunState, plan: AuthoredPlan | str) -> GateStatus | None:
    """Return the recorded answer for this proposal, if any."""
    name = acceptance_gate_name(plan)
    return next((gate.status for gate in state.gates if gate.name == name), None)


def open_acceptance_gate(
    store: RunStore,
    plan: AuthoredPlan,
    *,
    owned_branch: str = "main",
) -> RunState:
    """Record the proposal durably and open its human gate.

    The record is written before the gate so that a failure between the two
    leaves a proposal nobody has approved, which is the safe direction.
    """
    record_authored_plan(store, plan, status=ACCEPTANCE_PENDING, owned_branch=owned_branch)
    return store.set_gate(acceptance_gate_name(plan), GateStatus.PENDING)


def require_acceptance(state: RunState, plan: AuthoredPlan | str) -> None:
    """Fail closed unless a human approved this exact proposal."""
    status = acceptance_status(state, plan)
    if status is GateStatus.APPROVED:
        return
    if status is GateStatus.REJECTED:
        digest = plan if isinstance(plan, str) else plan.digest
        raise PlanRejected(f"plan {digest[:16]} was rejected; it is not execution authority")
    digest = plan if isinstance(plan, str) else plan.digest
    raise PlanAcceptanceRequired(
        f"plan {digest[:16]} awaits explicit human acceptance: answer the "
        f"{acceptance_gate_name(digest)!r} gate with --approve or --reject"
    )


def record_authored_plan(
    store: RunStore,
    plan: AuthoredPlan,
    *,
    status: str,
    owned_branch: str = "main",
) -> Mapping[str, object]:
    """Persist one proposal's validated plan identity and bounded audit."""
    if status not in _AUTHORING_RECORD_STATUSES:
        raise PlanAuthoringRecordError(f"plan-authoring status {status!r} is not recognized")
    _validate_audit(plan.audit)
    record = {
        "schema_version": AUTHORING_RECORD_SCHEMA_VERSION,
        "run_id": plan.run_id,
        "plan_digest": plan.digest,
        "gate_name": acceptance_gate_name(plan),
        "status": status,
        "owned_branch": owned_branch,
        "document": copy.deepcopy(dict(plan.document)),
        "audit": copy.deepcopy(dict(plan.audit)),
    }
    store.record_plan_authoring(record)
    return record


def recorded_plan(store: RunStore, run_id: str | None = None) -> AuthoredPlan | None:
    """Return the validated proposal this run recorded, if there is one."""
    try:
        record = store.plan_authoring_record()
    except CorruptCheckpoint as error:
        raise PlanAuthoringRecordError(str(error)) from error
    if record is None:
        return None
    return plan_from_record(record, expected_run_id=run_id)


def plan_from_record(
    record: Mapping[str, object],
    *,
    expected_run_id: str | None = None,
) -> AuthoredPlan:
    """Validate a durable record and rebuild its digest-bound proposal.

    The stored document is re-validated and re-hashed, so a hand-edited record
    cannot present one plan while carrying another plan's digest — and the
    gate is named after that digest.
    """
    unknown = sorted(set(record) - _AUTHORING_RECORD_KEYS)
    if unknown:
        raise PlanAuthoringRecordError(
            "plan-authoring record contains unknown keys: " + ", ".join(unknown)
        )
    if record.get("schema_version") != AUTHORING_RECORD_SCHEMA_VERSION:
        raise PlanAuthoringRecordError("plan-authoring record schema version is unsupported")
    digest = _require_digest(record.get("plan_digest"))
    status = record.get("status")
    if status not in _AUTHORING_RECORD_STATUSES:
        raise PlanAuthoringRecordError("plan-authoring record has no valid status")
    if record.get("gate_name") != acceptance_gate_name(digest):
        raise PlanAuthoringRecordError("plan-authoring record gate does not match its digest")
    run_id = _require_text(record.get("run_id"), "plan-authoring record run_id")
    if expected_run_id is not None and run_id != expected_run_id:
        raise PlanAuthoringRecordError(
            f"plan-authoring record belongs to run {run_id!r}, not {expected_run_id!r}"
        )
    owned_branch = _require_text(record.get("owned_branch"), "plan-authoring record owned_branch")
    document = record.get("document")
    if not isinstance(document, Mapping):
        raise PlanAuthoringRecordError("plan-authoring record document must be an object")
    audit = record.get("audit")
    if not isinstance(audit, Mapping):
        raise PlanAuthoringRecordError("plan-authoring record audit must be an object")
    _validate_audit(audit)
    try:
        manifest = build_authoring_manifest(
            document, run_id=run_id, owned_branch=owned_branch
        )
    except PlanAuthoringError as error:
        raise PlanAuthoringRecordError(f"recorded plan is invalid: {error}") from error
    if manifest.digest != digest:
        raise PlanAuthoringRecordError(
            "recorded plan document does not hash to its recorded digest"
        )
    return AuthoredPlan(run_id=run_id, document=document, manifest=manifest, audit=audit)


def install_accepted_plan(
    store: RunStore,
    plan: AuthoredPlan | None = None,
    *,
    run_id: str | None = None,
) -> RunState:
    """Install one accepted plan as the run's immutable manifest declaration.

    With no explicit ``plan`` the durable record is re-validated and used, so
    a second process can complete the flow after the human answered the gate.
    """
    state = _load(store)
    proposal = plan if plan is not None else recorded_plan(store, run_id or store.run_id)
    if proposal is None:
        raise PlanAuthoringRecordError("no plan-authoring proposal is recorded for this run")
    require_acceptance(state, proposal)
    try:
        installed = store.set_plan_manifest(
            proposal.manifest,
            protected_node_ids=_protected_node_ids(state),
        )
    except ManifestError as error:
        raise PlanAcceptanceError(
            f"accepted plan {proposal.digest[:16]} cannot replace the persisted "
            f"declaration: {error}"
        ) from error
    record_authored_plan(
        store,
        proposal,
        status=ACCEPTANCE_APPROVED,
        owned_branch=proposal.manifest.owned_branch,
    )
    return installed


def proposal_summary(plan: AuthoredPlan, *, status: str) -> JsonObject:
    """Return the bounded operator-facing projection of one proposal."""
    return {
        "run_id": plan.run_id,
        "plan_digest": plan.digest,
        "gate_name": acceptance_gate_name(plan),
        "status": status,
        "requires_acceptance": plan.requires_acceptance,
        "slices": cast(JsonValue, _slice_names(plan)),
        "audit": cast(JsonValue, copy.deepcopy(dict(plan.audit))),
    }


def gate_status_of(state: RunState, plan: AuthoredPlan) -> str:
    """Return the durable gate answer as a plain record status."""
    status = acceptance_status(state, plan)
    if status is GateStatus.APPROVED:
        return ACCEPTANCE_APPROVED
    if status is GateStatus.REJECTED:
        return GateStatus.REJECTED.value
    return ACCEPTANCE_PENDING


def _slice_names(plan: AuthoredPlan) -> list[str]:
    leaves = plan.plan_mapping().get("leaves")
    if not isinstance(leaves, list):
        return []
    return [cast(str, leaf["name"]) for leaf in leaves if isinstance(leaf, Mapping)]


def _validate_audit(audit: Mapping[str, object]) -> None:
    unknown = sorted(set(audit) - AUTHORING_AUDIT_KEYS)
    if unknown:
        raise PlanAuthoringRecordError(
            "plan-authoring audit contains unknown keys: " + ", ".join(unknown)
        )
    for key in ("attempts", "tokens", "failures"):
        value = audit.get(key, 0)
        if type(value) is not int or value < 0:
            raise PlanAuthoringRecordError(f"plan-authoring audit {key} must be non-negative")
    violations = audit.get("violations", [])
    if not isinstance(violations, list) or len(violations) > MAX_AUDIT_VIOLATIONS:
        raise PlanAuthoringRecordError(
            f"plan-authoring audit violations must hold at most {MAX_AUDIT_VIOLATIONS} entries"
        )
    if any(
        not isinstance(item, str) or len(item) > MAX_AUDIT_VIOLATION_CHARS
        for item in violations
    ):
        raise PlanAuthoringRecordError(
            f"plan-authoring audit violations must be strings within "
            f"{MAX_AUDIT_VIOLATION_CHARS} characters"
        )
    for key in ("judgment", "model"):
        value = audit.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise PlanAuthoringRecordError(
                f"plan-authoring audit {key} must be null or a non-empty string"
            )


def _require_digest(value: object) -> str:
    digest = _require_text(value, "plan-authoring record plan_digest")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise PlanAuthoringRecordError("plan-authoring record plan_digest must be a sha256 hex")
    return digest


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanAuthoringRecordError(f"{name} must be a non-empty string")
    return value


def _protected_node_ids(state: RunState) -> frozenset[str]:
    """Return nodes whose declaration dispatched work already froze."""
    return frozenset(
        cast(str, slice_state.manifest_node_id)
        for slice_state in state.slices.values()
        if slice_state.manifest_node_id is not None
        and slice_state.status not in {SliceStatus.PENDING, SliceStatus.READY}
    )


def _load(store: RunStore) -> RunState:
    try:
        return store.load()
    except (CorruptCheckpoint, OSError, ValueError) as error:
        raise PlanAcceptanceError(f"run state is unavailable: {error}") from error


def plan_authoring_path(store: RunStore) -> Path:
    """Return the durable record path so operators can inspect it."""
    return store.plan_authoring_path


__all__ = [
    "ACCEPTANCE_APPROVED",
    "ACCEPTANCE_GATE_PREFIX",
    "ACCEPTANCE_PENDING",
    "AUTHORING_RECORD_SCHEMA_VERSION",
    "PlanAcceptanceError",
    "PlanAcceptanceRequired",
    "PlanAuthoringRecordError",
    "PlanRejected",
    "acceptance_gate_name",
    "acceptance_status",
    "gate_status_of",
    "install_accepted_plan",
    "open_acceptance_gate",
    "plan_authoring_path",
    "plan_from_record",
    "proposal_summary",
    "record_authored_plan",
    "recorded_plan",
    "require_acceptance",
]
