"""Pure TL phase transitions faithful to ``.exo/roles/devswarm/TLPhase.hs``."""

from __future__ import annotations

from collections.abc import Mapping

from .event import (
    AllChildrenDone,
    ChildBlocked,
    ChildCompleted,
    ChildFailed,
    ChildSpawned,
    OwnPRFiled,
    PRFiled,
    PRMerged,
    PRUpdated,
    TLEvent,
)
from .phase import (
    ChildHandle,
    Phase,
    PhaseValue,
    TLAllMerged,
    TLDone,
    TLFailed,
    TLMerging,
    TLPRFiled,
    TLWaiting,
)


class IllegalTransition(Exception):
    """Raised when an event has no transition arm for the current phase."""

    def __init__(self, phase: Phase, event: TLEvent) -> None:
        self.phase = phase
        self.event = event
        super().__init__(f"No TL transition for {type(phase).__name__} and {type(event).__name__}")


def transition(phase: PhaseValue, event: TLEvent) -> PhaseValue:
    """Apply one pure, total TL transition or raise ``IllegalTransition``.

    The wildcard arms in the Haskell table are intentional: spawning, failing,
    filing the own PR, and completing all children establish their terminal or
    waiting phase from any current phase.  Haskell's silent no-op arms for
    ``ChildCompleted`` and ``PRMerged`` are represented as explicit errors.
    """
    if isinstance(event, ChildSpawned):
        return _child_spawned(phase, event)
    if isinstance(event, ChildCompleted):
        if isinstance(phase, TLWaiting):
            return _without_child(phase.children, event.slug)
        raise IllegalTransition(phase, event)
    if isinstance(event, ChildFailed):
        return TLFailed(f"{event.slug}: {event.reason}")
    if isinstance(event, ChildBlocked):
        # External blockers enter pre-publication recovery; they are not a
        # controller failure and must leave unrelated siblings runnable.
        if isinstance(phase, TLWaiting):
            children = dict(phase.children)
            children.pop(event.slug, None)
            return TLWaiting(children)
        return phase
    if isinstance(event, PRMerged):
        if isinstance(phase, (TLMerging, TLWaiting)):
            return _without_child(phase.children, event.slug)
        raise IllegalTransition(phase, event)
    if isinstance(event, (PRFiled, PRUpdated)):
        return phase
    if isinstance(event, AllChildrenDone):
        return TLDone()
    if isinstance(event, OwnPRFiled):
        return TLPRFiled(event.pr_number, event.url)
    raise IllegalTransition(phase, event)


def _child_spawned(phase: PhaseValue, event: ChildSpawned) -> TLWaiting:
    if isinstance(phase, TLWaiting):
        children = dict(phase.children)
        children[event.handle.slug] = event.handle
        return TLWaiting(children)
    return TLWaiting({event.handle.slug: event.handle})


def _without_child(
    children: Mapping[str, ChildHandle],
    slug: str,
) -> TLWaiting | TLAllMerged:
    remaining = dict(children)
    remaining.pop(slug, None)
    if not remaining:
        return TLAllMerged()
    return TLWaiting(remaining)
