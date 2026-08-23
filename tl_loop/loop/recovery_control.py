"""Authenticated, compare-and-set recovery commands for the TL control plane."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import TransportClient
from tl_loop.fsm.recovery import RecoveryPhase, RecoveryTransitionError, transition_recovery
from tl_loop.loop.abandon import abandon_slice
from tl_loop.loop.journal import EffectJournal
from tl_loop.loop.observability import emit_controller_event
from tl_loop.loop.recovery_policy import policy_for_cause
from tl_loop.state.schema import RunState, SliceState, SliceStatus
from tl_loop.state.store import RunStore

RecoveryAction = Literal["inspect", "retry", "wait", "approve_scope", "abandon"]


@dataclass(frozen=True)
class PolicyAuthorization:
    policy_id: str
    recovery_round: int


@dataclass(frozen=True)
class HumanAuthorization:
    gate_name: str
    decision_revision: int


@dataclass(frozen=True)
class ResumeRecoveryRequest:
    run_id: str
    slice_id: str
    expected_invocation_id: str
    expected_generation: int
    expected_worktree_fingerprint: str
    action: RecoveryAction
    authorization: PolicyAuthorization | HumanAuthorization
    idempotency_key: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ResumeRecoveryRequest:
        allowed = {
            "run_id",
            "slice_id",
            "expected_invocation_id",
            "expected_generation",
            "expected_worktree_fingerprint",
            "action",
            "authorization",
            "idempotency_key",
        }
        unknown = set(value) - allowed
        if unknown:
            raise RecoveryCommandError(f"unknown recovery command fields: {sorted(unknown)}")
        authorization = value.get("authorization")
        if not isinstance(authorization, Mapping):
            raise RecoveryCommandError("authorization must be an object")
        kind = authorization.get("kind")
        if kind == "policy":
            auth: PolicyAuthorization | HumanAuthorization = PolicyAuthorization(
                _required_text(authorization, "policy_id"),
                _non_negative_int(authorization, "recovery_round"),
            )
        elif kind == "human":
            auth = HumanAuthorization(
                _required_text(authorization, "gate_name"),
                _non_negative_int(authorization, "decision_revision"),
            )
        else:
            raise RecoveryCommandError("authorization.kind must be policy or human")
        action = value.get("action")
        if action not in {"inspect", "retry", "wait", "approve_scope", "abandon"}:
            raise RecoveryCommandError(f"unsupported recovery action {action!r}")
        return cls(
            run_id=_required_text(value, "run_id"),
            slice_id=_required_text(value, "slice_id"),
            expected_invocation_id=_required_text(value, "expected_invocation_id"),
            expected_generation=_non_negative_int(value, "expected_generation"),
            expected_worktree_fingerprint=_required_text(value, "expected_worktree_fingerprint"),
            action=cast(RecoveryAction, action),
            authorization=auth,
            idempotency_key=_required_text(value, "idempotency_key"),
        )


class RecoveryCommandError(RuntimeError):
    """A recovery command failed closed at the workflow boundary."""


@dataclass(frozen=True)
class _CommandIntent:
    operation: str
    target: str
    arguments: Mapping[str, object]
    active: bool = True


def execute_recovery_command(
    project_root: Path,
    request: ResumeRecoveryRequest,
    *,
    effects: EffectClient | None = None,
) -> dict[str, object]:
    """Validate and apply one authenticated recovery command idempotently."""
    if request.run_id != request.run_id.strip():
        raise RecoveryCommandError("run_id must not contain surrounding whitespace")
    store = RunStore(request.run_id, project_root / ".exo" / "tl-loop")
    journal = EffectJournal(request.run_id, store.run_dir / "action-journal.json")
    intent = _intent(request)
    existing = journal.existing(intent)
    if existing is not None:
        status = existing.get("status")
        if status == "confirmed" and isinstance(existing.get("result"), Mapping):
            return dict(cast(Mapping[str, object], existing["result"]))
        if status in {"intended", "unknown"}:
            raise RecoveryCommandError(
                f"recovery command {request.idempotency_key!r} has unresolved journal status {status!r}"
            )
        raise RecoveryCommandError(f"recovery command has journal status {status!r}")

    state = store.load()
    current = state.slices.get(request.slice_id)
    if current is None or current.recovery is None:
        raise RecoveryCommandError(f"slice {request.slice_id!r} has no active recovery")
    _validate_identity(current, request)
    _validate_authorization(state, current, request)
    if request.action == "inspect":
        return _inspection(state, current)
    _validate_next_action(current.recovery.next_action, request.action)

    journal.append(intent)
    try:
        result = _apply_command(project_root, store, state, current, request, effects)
    except BaseException as error:
        journal.resolve_by_key(journal.key_for(intent), status="unknown", error=str(error))
        raise
    journal.resolve_by_key(journal.key_for(intent), status="confirmed", result=result)
    return result


def _apply_command(
    project_root: Path,
    store: RunStore,
    state: RunState,
    current: SliceState,
    request: ResumeRecoveryRequest,
    effects: EffectClient | None,
) -> dict[str, object]:
    recovery = current.recovery
    evidence = {**dict(recovery.evidence), "authorization": _authorization_document(request)}
    try:
        if request.action == "abandon":
            client = effects or EffectClient(
                TransportClient(project_root=project_root), role="tl", name="root"
            )
            if current.status not in {
                SliceStatus.DISPATCHING,
                SliceStatus.DISPATCH_UNCONFIRMED,
                SliceStatus.SPAWNED,
                SliceStatus.IN_REVIEW,
                SliceStatus.REPAIRING,
            }:
                raise RecoveryCommandError("abandon requires a live invocation")
            result = abandon_slice(project_root, request.run_id, request.slice_id, effects=client)
            emit_controller_event(
                client,
                "agent.recovery.outcome",
                {
                    "slice_id": request.slice_id,
                    "invocation_id": request.expected_invocation_id,
                    "generation": request.expected_generation,
                    "cause": recovery.cause,
                    "slice_attempt": recovery.slice_attempt,
                    "invocation_generation": recovery.invocation_generation,
                    "recovery_round": recovery.recovery_round,
                    "authorization_source": "human",
                    "outcome": "abandoned",
                    "recursive_depth": state.depth,
                    "parallel_impact": "none",
                    "policy_decision": "abandon",
                    "execution_seconds": None,
                    "recovery_wait_seconds": None,
                    "human_wait_seconds": None,
                    "review_seconds": None,
                    "declared_difficulty": recovery.evidence.get(
                        "declared_difficulty", "standard"
                    ),
                    "matched_difficulty_rule": recovery.evidence.get(
                        "matched_difficulty_rule", "recovery"
                    ),
                },
            )
            return result
        phase = {
            "retry": RecoveryPhase.RESUME_INTENDED,
            "wait": RecoveryPhase.WAITING_SIGNAL,
            "approve_scope": RecoveryPhase.RESUME_INTENDED,
        }[request.action]
        next_action = {
            "retry": "resume_same_owner",
            "wait": "wait_for_signal",
            "approve_scope": "resume_same_owner",
        }[request.action]
        updated_recovery = transition_recovery(
            recovery,
            phase,
            next_action=next_action,
            evidence=evidence,
        )
    except RecoveryTransitionError as error:
        raise RecoveryCommandError(str(error)) from error
    updated = store.checkpoint(
        state.fsm,
        {
            **state.slices,
            request.slice_id: replace(current, recovery=updated_recovery),
        },
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )
    return {
        "status": "accepted",
        "action": request.action,
        "run_id": request.run_id,
        "slice_id": request.slice_id,
        "recovery_phase": updated.slices[request.slice_id].recovery.phase.value,
        "recovery_round": updated.slices[request.slice_id].recovery.recovery_round,
        "idempotency_key": request.idempotency_key,
    }


def _validate_identity(current: SliceState, request: ResumeRecoveryRequest) -> None:
    recovery = current.recovery
    if recovery.owner_run_id != request.run_id:
        raise RecoveryCommandError("recovery owner run changed")
    if (
        recovery.owner_agent_id is not None
        and current.dispatch_agent_id is not None
        and recovery.owner_agent_id != current.dispatch_agent_id
    ):
        raise RecoveryCommandError("recovery owner agent changed")
    invocation_id = recovery.evidence.get("invocation_id") or current.dispatch_invocation_id
    if invocation_id != request.expected_invocation_id:
        raise RecoveryCommandError("recovery invocation identity changed")
    if recovery.invocation_generation != request.expected_generation:
        raise RecoveryCommandError("recovery invocation generation changed")
    fingerprint = recovery.evidence.get("worktree_fingerprint")
    if fingerprint != request.expected_worktree_fingerprint:
        raise RecoveryCommandError("recovery worktree fingerprint changed")


def _validate_authorization(
    state: RunState, current: SliceState, request: ResumeRecoveryRequest
) -> None:
    if request.action == "inspect":
        return
    recovery = current.recovery
    if isinstance(request.authorization, PolicyAuthorization):
        policy = policy_for_cause(recovery.cause)
        if request.authorization.policy_id != recovery.cause:
            raise RecoveryCommandError("policy authorization does not match recovery cause")
        if request.authorization.recovery_round != recovery.recovery_round:
            raise RecoveryCommandError("policy authorization recovery round is stale")
        if not policy.automatic_resume or request.action not in {"retry", "wait"}:
            raise RecoveryCommandError("recovery policy cannot authorize this action")
    else:
        if request.authorization.decision_revision != state.revision:
            raise RecoveryCommandError("human recovery decision revision is stale")
        if request.action not in {"retry", "wait", "approve_scope", "abandon"}:
            raise RecoveryCommandError("human authorization cannot authorize this action")
        gate_names = {gate.name for gate in state.gates if gate.status.value == "pending"}
        expected_gate = current.park_audit.get("gate_name") if current.park_audit else None
        if (
            request.authorization.gate_name not in gate_names
            and request.authorization.gate_name != expected_gate
        ):
            raise RecoveryCommandError("human authorization names no pending recovery gate")


def _validate_next_action(next_action: str, action: RecoveryAction) -> None:
    allowed = {
        "retry": {"resume_same_owner", "retry", "diagnose"},
        "wait": {"wait_for_signal", "wait", "probe", "diagnose"},
        "approve_scope": {"open_human_gate"},
        "abandon": {"diagnose", "wait_for_signal", "resume_same_owner", "open_human_gate"},
    }
    if action != "inspect" and next_action not in allowed[action]:
        raise RecoveryCommandError(
            f"recovery action {action!r} is not authorized by next_action {next_action!r}"
        )


def _inspection(state: RunState, current: SliceState) -> dict[str, object]:
    return {
        "status": "observed",
        "run_id": state.run_id,
        "slice_id": current.id,
        "state_revision": state.revision,
        "recovery": {
            "phase": current.recovery.phase.value,
            "cause": current.recovery.cause,
            "next_action": current.recovery.next_action,
            "recovery_round": current.recovery.recovery_round,
            "invocation_generation": current.recovery.invocation_generation,
        },
    }


def _intent(request: ResumeRecoveryRequest) -> _CommandIntent:
    return _CommandIntent(
        "recovery_command",
        f"{request.run_id}:{request.slice_id}",
        {
            "idempotency_key": request.idempotency_key,
            "action": request.action,
            "expected_invocation_id": request.expected_invocation_id,
            "expected_generation": request.expected_generation,
            "expected_worktree_fingerprint": request.expected_worktree_fingerprint,
            "authorization": _authorization_document(request),
        },
    )


def _authorization_document(request: ResumeRecoveryRequest) -> dict[str, object]:
    if isinstance(request.authorization, PolicyAuthorization):
        return {
            "kind": "policy",
            "policy_id": request.authorization.policy_id,
            "recovery_round": request.authorization.recovery_round,
        }
    return {
        "kind": "human",
        "gate_name": request.authorization.gate_name,
        "decision_revision": request.authorization.decision_revision,
    }


def _required_text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise RecoveryCommandError(f"{key} must be non-empty text")
    return item.strip()


def _non_negative_int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if type(item) is not int or item < 0:
        raise RecoveryCommandError(f"{key} must be a non-negative integer")
    return item
