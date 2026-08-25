"""Deterministic post-reduction convergence and action telemetry."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import TypeAlias

from tl_loop.loop.reconcile import (
    ExternalIntent,
    InternalTransition,
    MergeDecision,
    Quiescent,
    derive_next_action,
)
from tl_loop.state.schema import RunState, SliceState
from tl_loop.state.serialization import dumps as dumps_json

PersistedState: TypeAlias = RunState | SliceState


class ConvergenceInvariantError(RuntimeError):
    """A state version attempted the same transition or action twice."""

    def __init__(self, invariant: str, key: str, events: tuple[ConvergenceEvent, ...] = ()) -> None:
        self.invariant = invariant
        self.key = key
        self.events = events
        super().__init__(f"{invariant} violated for {key}")


@dataclass(frozen=True)
class ConvergenceEvent:
    """One bounded telemetry event emitted by convergence."""

    event_type: str
    payload: dict[str, object]


@dataclass(frozen=True)
class ConvergenceResult:
    """Reduced action, updated state, and telemetry emitted for one call."""

    state: PersistedState
    decision: MergeDecision
    events: tuple[ConvergenceEvent, ...]


class ConvergenceTracker:
    """In-memory per-invocation guard for deterministic action reduction."""

    def __init__(self, *, reviewer_max_rounds: int | None = None) -> None:
        self.reviewer_max_rounds = reviewer_max_rounds
        self._seen: set[tuple[str, int, str]] = set()
        self._wait_reasons: dict[str, str] = {}
        self.last_decision: MergeDecision | None = None
        self._invariant_events: set[tuple[str, str, int, str]] = set()

    def reduce(self, state: PersistedState) -> ConvergenceResult:
        """Reduce persisted state once and return a stable intent or wait."""
        decision = derive_next_action(
            state,
            reviewer_max_rounds=self.reviewer_max_rounds,
        )
        self.last_decision = decision
        run_id = state.run_id if isinstance(state, RunState) else state.id
        version = getattr(state, "state_version", 0)
        target = _target(decision, run_id)
        action_key = _action_key(decision)
        events: list[ConvergenceEvent] = []
        if isinstance(decision, Quiescent):
            prior = self._wait_reasons.get(target)
            self._wait_reasons[target] = decision.reason
            if prior != decision.reason:
                events.append(
                    _event(
                        "tl.wait_reason_changed",
                        run_id,
                        target,
                        version,
                        reason=decision.reason,
                        previous_reason=prior,
                    )
                )
            return ConvergenceResult(state, decision, tuple(events))

        key = (target, version, action_key)
        if key in self._seen:
            invariant = "repeated_state_version_action"
            events.extend(self._invariant_event(run_id, target, version, invariant, action_key))
            raise ConvergenceInvariantError(invariant, action_key, tuple(events))
        self._seen.add(key)
        if isinstance(decision, ExternalIntent):
            events.append(
                _event(
                    "tl.action_queued",
                    run_id,
                    target,
                    version,
                    action=decision.operation,
                    action_key=action_key,
                    arguments=dict(decision.arguments),
                )
            )
            return ConvergenceResult(state, decision, tuple(events))

        next_state = state
        if isinstance(state, RunState):
            next_state = replace(state, state_version=version + 1)
        events.append(
            _event(
                "tl.transition_applied",
                run_id,
                target,
                version + 1,
                transition=decision.transition,
                reason=decision.reason,
            )
        )
        return ConvergenceResult(next_state, decision, tuple(events))

    def action_started(
        self,
        state: PersistedState,
        intent: ExternalIntent,
    ) -> ConvergenceEvent:
        """Record the dispatch boundary for a previously queued intent."""
        return _event(
            "tl.action_started",
            state.run_id if isinstance(state, RunState) else state.id,
            intent.target_id,
            getattr(state, "state_version", 0),
            action=intent.operation,
            action_key=_action_key(intent),
        )

    def action_outcome(
        self,
        state: PersistedState,
        intent: ExternalIntent,
        *,
        outcome: str,
        error: str | None = None,
    ) -> ConvergenceEvent:
        """Record an unknown or reconciled effect outcome."""
        if outcome not in {"unknown", "reconciled"}:
            raise ValueError("convergence outcome must be unknown or reconciled")
        payload: dict[str, object] = {
            "action": intent.operation,
            "action_key": _action_key(intent),
            "outcome": outcome,
        }
        if error:
            payload["error"] = error[:500]
        return _event(
            f"tl.action_{outcome}",
            state.run_id if isinstance(state, RunState) else state.id,
            intent.target_id,
            getattr(state, "state_version", 0),
            **payload,
        )

    def _invariant_event(
        self,
        run_id: str,
        target: str,
        version: int,
        invariant: str,
        action_key: str,
    ) -> tuple[ConvergenceEvent, ...]:
        key = (run_id, target, version, invariant)
        if key in self._invariant_events:
            return ()
        self._invariant_events.add(key)
        return (
            _event(
                "tl.transition_invariant_failed",
                run_id,
                target,
                version,
                invariant=invariant,
                action_key=action_key,
            ),
        )


def _target(decision: MergeDecision, fallback: str) -> str:
    if isinstance(decision, ExternalIntent):
        return decision.target_id
    return fallback


def _action_key(decision: MergeDecision) -> str:
    if isinstance(decision, ExternalIntent):
        payload = {
            "operation": decision.operation,
            "target": decision.target_id,
            "arguments": dict(decision.arguments),
        }
    elif isinstance(decision, InternalTransition):
        payload = {"transition": decision.transition, "reason": decision.reason}
    else:
        payload = {"wait": decision.reason}
    return hashlib.sha256(
        dumps_json(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]


def _event(
    event_type: str, run_id: str, target: str, state_version: int, **fields: object
) -> ConvergenceEvent:
    return ConvergenceEvent(
        event_type,
        {"run_id": run_id, "target_id": target, "state_version": state_version, **fields},
    )
