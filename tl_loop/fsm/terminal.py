"""Pure phase-level stop predicates matching ``TLPhase.canExit``."""

from __future__ import annotations

from .phase import TLAllMerged, TLDispatching, TLDone, TLFailed, TLMerging, TLPlanning, TLWaiting
from .scope import (
    TLAllMerged as RecursiveTLAllMerged,
)
from .scope import (
    TLDone as RecursiveTLDone,
)
from .scope import (
    TLFailed as RecursiveTLFailed,
)
from .scope import (
    TLFinalizing as RecursiveTLFinalizing,
)
from .scope import (
    TLParked as RecursiveTLParked,
)
from .scope import (
    TLPlanning as RecursiveTLPlanning,
)
from .scope import (
    TLPRFiled as RecursiveTLPRFiled,
)
from .scope import (
    TLRunning as RecursiveTLRunning,
)
from .scope_projection import active_child_ids


def is_waiting(phase: object) -> bool:
    """Return whether the phase still has work that should nudge the loop."""
    if isinstance(phase, RecursiveTLRunning):
        return bool(active_child_ids(phase))
    if isinstance(
        phase,
        (
            RecursiveTLPlanning,
            RecursiveTLAllMerged,
            RecursiveTLFinalizing,
            RecursiveTLPRFiled,
            RecursiveTLDone,
            RecursiveTLFailed,
            RecursiveTLParked,
        ),
    ):
        return False
    return isinstance(phase, (TLDispatching, TLWaiting, TLMerging))


def is_terminal(phase: object) -> bool:
    """Return whether the phase has the Haskell ``Clean`` exit decision."""
    if isinstance(
        phase, (RecursiveTLDone, RecursiveTLPRFiled, RecursiveTLFailed, RecursiveTLParked)
    ):
        return True
    if isinstance(
        phase,
        (RecursiveTLPlanning, RecursiveTLRunning, RecursiveTLAllMerged, RecursiveTLFinalizing),
    ):
        return False
    return isinstance(phase, (TLPlanning, TLAllMerged, TLDone, TLFailed))
