"""Pure phase-level stop predicates matching ``TLPhase.canExit``."""

from __future__ import annotations

from .phase import (
    PhaseValue,
    TLAllMerged,
    TLDispatching,
    TLFailed,
    TLPlanning,
    TLDone,
    TLMerging,
    TLWaiting,
)


def is_waiting(phase: PhaseValue) -> bool:
    """Return whether the phase still has work that should nudge the loop."""
    return isinstance(phase, (TLDispatching, TLWaiting, TLMerging))


def is_terminal(phase: PhaseValue) -> bool:
    """Return whether the phase has the Haskell ``Clean`` exit decision."""
    return isinstance(phase, (TLPlanning, TLAllMerged, TLDone, TLFailed))
