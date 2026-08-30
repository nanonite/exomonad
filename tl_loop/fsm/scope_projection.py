"""Compatibility projections for the canonical recursive scope FSM."""

from __future__ import annotations

from .phase import TLPhase
from .scope import (
    PhaseValue,
    TLAllMerged,
    TLDone,
    TLFailed,
    TLFinalizing,
    TLParked,
    TLPlanning,
    TLPRFiled,
    TLRunning,
)


def phase_tag(phase: PhaseValue) -> TLPhase:
    """Project a canonical phase to the stable checkpoint tag."""
    if isinstance(phase, TLPlanning):
        return TLPhase.TLPlanning
    if isinstance(phase, TLRunning):
        # FSMState is a legacy observability projection. The authoritative
        # scheduler state remains recursive_fsm=TLRunning.
        return TLPhase.TLWaiting
    if isinstance(phase, TLAllMerged):
        return TLPhase.TLAllMerged
    if isinstance(phase, TLFinalizing):
        return TLPhase.TLFinalizing
    if isinstance(phase, TLPRFiled):
        return TLPhase.TLPRFiled
    if isinstance(phase, TLDone):
        return TLPhase.TLDone
    if isinstance(phase, TLParked):
        return TLPhase.TLParked
    if isinstance(phase, TLFailed):
        return TLPhase.TLFailed
    raise TypeError(f"unsupported canonical phase: {type(phase).__name__}")


def active_child_ids(phase: PhaseValue) -> tuple[str, ...]:
    """Project only active children for observability, never scheduling."""
    if not isinstance(phase, TLRunning):
        return ()
    parallel = tuple(record.child_id for record in phase.parallel_pending)
    ordered = tuple(
        record.child_id for record in phase.pending_by_order.get(phase.current_order, ())
    )
    return parallel + ordered


__all__ = ["active_child_ids", "phase_tag"]
