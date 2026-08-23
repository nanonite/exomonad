"""Durable escalation and human-parking decisions for the TL loop.

The module keeps cause classification pure and makes the state mutation explicit:
an issue is created through the effect boundary, then one atomic run-state write
parks the slice and blocks its dependents.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, cast

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.observability import emit_controller_event
from tl_loop.select.ledger import LedgerInput
from tl_loop.state.schema import BudgetLedger, ParkCause, SliceState, SliceStatus
from tl_loop.state.serialization import dumps as dumps_json
from tl_loop.state.serialization import to_jsonable
from tl_loop.state.store import RunStore
from tl_loop.state.write import apply

_TERMINAL_STATUSES = frozenset(
    {
        SliceStatus.MERGED.value,
        SliceStatus.FAILED.value,
        SliceStatus.PARKED.value,
        SliceStatus.BLOCKED.value,
    }
)
_AUDIT_FIELDS = frozenset({"from_harness", "to_harness", "reason", "model", "effort"})
_BLOCKED_AUDIT_FIELDS = frozenset(
    {
        "attempt",
        "recovery_action",
        "needs_human",
        "base_sha",
        "head_sha",
        "failed_checks",
        "attribution",
    }
)
_BLOCKED_GATE_CAUSES = frozenset(
    {
        ParkCause.BASE_CI_UNSTABLE,
        ParkCause.EXTERNAL_DEPENDENCY,
        ParkCause.SCOPE_BOUNDARY,
        ParkCause.HUMAN_DECISION_REQUIRED,
        ParkCause.MISSING_HANDOFF,
    }
)


class EscalationError(RuntimeError):
    """An escalation could not be recorded safely."""


class IssueCreationError(EscalationError):
    """The needs-human issue was not created with a usable ID."""


def blocked_gate_name(run_id: str, slice_id: str, attempt: int, cause: str) -> str:
    """Return the stable identity for one externally blocked attempt."""
    if not run_id or not slice_id or type(attempt) is not int or attempt <= 0 or not cause:
        raise ValueError("blocked gate identity requires run, slice, positive attempt, and cause")
    return f"task-blocked:{run_id}:{slice_id}:{attempt}:{cause}"


class IssueCreator(Protocol):
    """The effect capability required to create a needs-human issue."""

    def chainlink_issue_create(
        self,
        *,
        title: str,
        description: str | None = None,
        labels: Sequence[str] | None = None,
        priority: str | None = None,
    ) -> ToolResult:
        """Create one issue through the effect boundary."""


@dataclass(frozen=True)
class ParkResult:
    """The durable result of parking one slice and blocking its dependents."""

    issue_id: int
    parked_slice_id: str
    blocked_slice_ids: tuple[str, ...]


@dataclass(frozen=True)
class HarnessSwitchDecision:
    """An auditable allow-or-park decision for changing harnesses."""

    allowed: bool
    from_harness: str
    to_harness: str
    reason: str
    model: str
    effort: str
    cause: ParkCause | None
    audit: Mapping[str, object]


def park(
    slice: SliceState,
    cause: ParkCause | str,
    *,
    store: RunStore | None = None,
    issue_creator: IssueCreator | EffectClient | Callable[[str, str], int] | None = None,
    ledger: LedgerInput | None = None,
    audit: Mapping[str, object] | None = None,
) -> SliceState | ParkResult:
    """Park a slice for a closed cause and block every pending dependent.

    Without a store this returns the pure parked state for classification tests.
    With a store, issue creation and one state.write.apply call are required;
    no dependent is spawned after the mutation.
    """
    parsed_cause = _cause(cause)
    parked_audit = _build_audit(slice, ledger, audit)
    if store is None:
        return replace(
            slice,
            status=SliceStatus.PARKED,
            park_cause=parsed_cause,
            park_issue_id=None,
            park_audit=parked_audit,
            blocked_by=None,
        )
    gate_name: str | None = None
    gate_created = False
    current_state = store.load()
    current_slice = current_state.slices.get(slice.id)
    if parsed_cause in _BLOCKED_GATE_CAUSES:
        raw_attempt = (audit or {}).get("attempt", slice.attempts)
        attempt = raw_attempt if type(raw_attempt) is int and raw_attempt > 0 else slice.attempts
        gate_name = blocked_gate_name(store.run_id, slice.id, attempt, parsed_cause.value)
        parked_audit = {**dict(parked_audit), "gate_name": gate_name, "attempt": attempt}
        if isinstance(current_slice, SliceState) and (
            current_slice.park_cause is parsed_cause and current_slice.park_issue_id is not None
        ):
            gate_created = _ensure_gate(store, gate_name)
            if gate_created and isinstance(issue_creator, EffectClient):
                _emit_gate_opened(issue_creator, store.run_id, gate_name, parsed_cause)
            return ParkResult(current_slice.park_issue_id, slice.id, ())
    if issue_creator is None:
        raise EscalationError("a needs-human issue creator is required for durable parking")

    issue_id = _create_issue(issue_creator, slice, parsed_cause, parked_audit)
    blocked: list[str] = []
    blocked_statuses: dict[str, str] = {}

    def mutate(document: dict[str, object]) -> dict[str, object]:
        raw_slices = document.get("slices")
        if not isinstance(raw_slices, dict):
            raise EscalationError("run state slices are not an object")
        target = raw_slices.get(slice.id)
        if not isinstance(target, dict):
            raise EscalationError(f"slice {slice.id!r} is missing from run state")

        target["status"] = SliceStatus.PARKED.value
        target["park_cause"] = parsed_cause.value
        target["park_issue_id"] = issue_id
        target["park_audit"] = copy.deepcopy(dict(parked_audit))
        target.pop("blocked_by", None)

        blocked_ids = {slice.id}
        changed = True
        while changed:
            changed = False
            for dependent_id, raw_dependent in raw_slices.items():
                if dependent_id in blocked_ids or not isinstance(raw_dependent, dict):
                    continue
                if raw_dependent.get("status") in _TERMINAL_STATUSES:
                    continue
                dependencies = raw_dependent.get("depends_on")
                if not isinstance(dependencies, list):
                    continue
                if not any(dependency in blocked_ids for dependency in dependencies):
                    continue
                blocked_statuses[dependent_id] = str(raw_dependent.get("status", "pending"))
                raw_dependent["status"] = SliceStatus.BLOCKED.value
                raw_dependent["blocked_by"] = slice.id
                raw_dependent["park_issue_id"] = issue_id
                blocked_ids.add(dependent_id)
                blocked.append(dependent_id)
                changed = True
        raw_fsm = document.get("fsm")
        if isinstance(raw_fsm, dict):
            waiting = raw_fsm.get("waiting")
            if isinstance(waiting, list):
                waiting[:] = [item for item in waiting if item not in blocked_ids]
                if (
                    not waiting
                    and raw_fsm.get("phase") in {TLPhase.TLWaiting.value, TLPhase.TLMerging.value}
                    and parsed_cause not in _BLOCKED_GATE_CAUSES
                ):
                    raw_fsm["phase"] = TLPhase.TLFailed.value
        return document

    prior_phase = store.load().fsm.phase.value
    apply(store.run_dir, mutate)
    if gate_name is not None:
        gate_created = _ensure_gate(store, gate_name)
    if isinstance(issue_creator, EffectClient):
        _emit_park_events(
            issue_creator,
            slice,
            parsed_cause,
            blocked_statuses,
            prior_phase,
            store.load().fsm.phase.value,
            store.run_id,
        )
        if gate_name is not None and gate_created:
            _emit_gate_opened(issue_creator, store.run_id, gate_name, parsed_cause)
    return ParkResult(issue_id, slice.id, tuple(blocked))


def authorize_harness_switch(
    from_harness: str,
    to_harness: str,
    reason: str,
    model: str,
    effort: str,
    allow: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> HarnessSwitchDecision:
    """Authorize a declared harness or require the explicit operator flag."""
    audit: dict[str, object] = {
        "from_harness": from_harness,
        "to_harness": to_harness,
        "reason": reason,
        "model": model,
        "effort": effort,
    }
    source_env = os.environ if env is None else env
    allowed = to_harness in allow or source_env.get("EXOMONAD_ALLOW_HARNESS_SWITCH") == "1"
    return HarnessSwitchDecision(
        allowed=allowed,
        from_harness=from_harness,
        to_harness=to_harness,
        reason=reason,
        model=model,
        effort=effort,
        cause=None if allowed else ParkCause.HARNESS_SWITCH_REQUESTED,
        audit=audit,
    )


def switch_harness(
    slice: SliceState,
    from_harness: str,
    to_harness: str,
    reason: str,
    model: str,
    effort: str,
    allow: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    store: RunStore | None = None,
    issue_creator: IssueCreator | EffectClient | Callable[[str, str], int] | None = None,
    ledger: LedgerInput | None = None,
) -> HarnessSwitchDecision | ParkResult:
    """Return an audited switch decision, parking ungated switches durably."""
    decision = authorize_harness_switch(
        from_harness,
        to_harness,
        reason,
        model,
        effort,
        allow,
        env=env,
    )
    if decision.allowed or store is None:
        return decision
    if issue_creator is None:
        raise EscalationError("a needs-human issue creator is required for an ungated switch")
    return cast(
        ParkResult,
        park(
            slice,
            ParkCause.HARNESS_SWITCH_REQUESTED,
            store=store,
            issue_creator=issue_creator,
            ledger=ledger,
            audit=decision.audit,
        ),
    )


def _cause(value: ParkCause | str) -> ParkCause:
    try:
        return value if isinstance(value, ParkCause) else ParkCause(value)
    except ValueError as error:
        raise EscalationError(f"unsupported parking cause: {value!r}") from error


def _emit_park_events(
    effects: EffectClient,
    slice: SliceState,
    cause: ParkCause,
    blocked_statuses: Mapping[str, str],
    before_phase: str,
    after_phase: str,
    run_id: str,
) -> None:
    """Publish parking and related status changes after durable mutation."""
    if slice.status.value != SliceStatus.PARKED.value:
        emit_controller_event(
            effects,
            "tl.slice_status_changed",
            {
                "slice_id": slice.id,
                "from_status": slice.status.value,
                "to_status": SliceStatus.PARKED.value,
            },
        )
    for slice_id in sorted(blocked_statuses):
        emit_controller_event(
            effects,
            "tl.slice_status_changed",
            {
                "slice_id": slice_id,
                "from_status": blocked_statuses[slice_id],
                "to_status": SliceStatus.BLOCKED.value,
            },
        )
    emit_controller_event(
        effects,
        "tl.slice_parked",
        {
            "slice_id": slice.id,
            "park_cause": cause.value,
            "attempts": slice.attempts,
        },
    )
    if before_phase != after_phase:
        emit_controller_event(
            effects,
            "tl.phase_changed",
            {
                "from_phase": before_phase,
                "to_phase": after_phase,
                "run_id": run_id,
            },
        )


def _build_audit(
    slice: SliceState,
    ledger: LedgerInput | None,
    extra: Mapping[str, object] | None,
) -> Mapping[str, object]:
    result: dict[str, object] = {
        "attempts": slice.attempts,
        "verdict": slice.verdict.value if slice.verdict is not None else None,
        "harness": slice.agent_type,
        "model": slice.model,
        "ledger": _ledger_snapshot(ledger),
    }
    if extra is not None:
        for key in _AUDIT_FIELDS | _BLOCKED_AUDIT_FIELDS:
            if key in extra:
                result[key] = to_jsonable(extra[key])
    return result


def _ledger_snapshot(ledger: LedgerInput | None) -> dict[str, object]:
    if ledger is None:
        return {"tokens": 0, "wall_seconds": 0}
    if isinstance(ledger, BudgetLedger):
        return {
            "tokens": ledger.tokens,
            "wall_seconds": ledger.wall_seconds,
            "role_spent": dict(ledger.role_spent),
            "harness_spent": dict(ledger.harness_spent),
            "role_reserved": dict(ledger.role_reserved),
            "harness_reserved": dict(ledger.harness_reserved),
            "charges": [
                {
                    "slice_id": charge.slice_id,
                    "attempt": charge.attempt,
                    "role": charge.role,
                    "harness": charge.harness,
                    "estimated_tokens": charge.estimated_tokens,
                    "actual": charge.actual,
                    "delta_tokens": charge.delta_tokens,
                    "warning": charge.warning,
                    "reconciled": charge.reconciled,
                }
                for charge in ledger.charges
            ],
        }
    value = to_jsonable(ledger)
    if not isinstance(value, dict):
        raise EscalationError("ledger audit must be an object")
    nested = value.get("ledger")
    if isinstance(nested, Mapping):
        return copy.deepcopy(dict(nested))
    return value


def _ensure_gate(store: RunStore, gate_name: str) -> bool:
    """Create a pending gate only when this identity has not been seen."""
    if any(gate.name == gate_name for gate in store.load().gates):
        return False
    store.set_gate(gate_name)
    return True


def _emit_gate_opened(effects: EffectClient, run_id: str, gate_name: str, cause: ParkCause) -> None:
    emit_controller_event(
        effects,
        "tl.gate_opened",
        {
            "gate_name": gate_name,
            "run_id": run_id,
            "reason": f"externally blocked slice: {cause.value}",
        },
    )


def _create_issue(
    creator: IssueCreator | EffectClient | Callable[[str, str], int],
    slice: SliceState,
    cause: ParkCause,
    audit: Mapping[str, object],
) -> int:
    title = f"Escalate slice {slice.id}: {cause.value}"
    description = (
        f"Slice {slice.id} is parked for human action. "
        f"Cause: {cause.value}. Audit: {dumps_json(audit, sort_keys=True)}"
    )
    if cause in _BLOCKED_GATE_CAUSES:
        description += (
            "\n\nOperator choices: retry same owner; wait for recovery; "
            "authorize scope expansion; or abandon the attempt."
        )
    if callable(creator) and not hasattr(creator, "chainlink_issue_create"):
        value: object = creator(title, description)
    else:
        effect = cast(IssueCreator, creator)
        result = effect.chainlink_issue_create(
            title=title,
            description=description,
            labels=("needs-human",),
            priority="high",
        )
        if result.success is not True:
            raise IssueCreationError(result.error or "chainlink issue creation failed")
        value = result.result
    issue_id = _issue_id(value)
    if issue_id is None:
        raise IssueCreationError(f"chainlink issue result has no positive issue ID: {value!r}")
    return issue_id


def _issue_id(value: object) -> int | None:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, Mapping):
        for key in ("issue_id", "id", "number"):
            candidate = value.get(key)
            if type(candidate) is int and candidate > 0:
                return candidate
    return None


__all__ = [
    "EscalationError",
    "HarnessSwitchDecision",
    "IssueCreationError",
    "ParkCause",
    "ParkResult",
    "authorize_harness_switch",
    "blocked_gate_name",
    "park",
    "switch_harness",
]
