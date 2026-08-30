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

    if isinstance(
        phase,
        (
            RecursiveTLPlanning,
            RecursiveTLRunning,
            RecursiveTLAllMerged,
            RecursiveTLFinalizing,
            RecursiveTLDone,
            RecursiveTLPRFiled,
            RecursiveTLFailed,
            RecursiveTLParked,
        ),
    ):
        from .orchestration import IllegalTransition as RecursiveIllegalTransition
        from .orchestration import transition as recursive_transition

        try:
            return recursive_transition(phase, to_orchestration_event(phase, event))  # type: ignore[arg-type]
        except RecursiveIllegalTransition as error:
            raise IllegalTransition(phase, event) from error
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


def to_orchestration_event(phase: object, event: TLEvent) -> object:
    """Map compatibility wire events into the canonical scope union."""
    from .scope import TLRunning
    from .scope_events import ChildSpawned as RecursiveChildSpawned
    from .scope_events import (
        ChildTerminal,
        FailureRecorded,
        Heartbeat,
        LeafCompleted,
        ParkRequested,
        PublicationFiled,
    )

    if isinstance(event, ChildSpawned):
        return RecursiveChildSpawned(
            event.handle.slug,
            "legacy-invocation",
            1,
            event.handle.branch,
            "legacy-worktree",
        )
    if isinstance(event, ChildCompleted):
        if isinstance(phase, TLRunning):
            records = (
                *phase.parallel_pending,
                *phase.pending_by_order.get(phase.current_order, ()),
            )
            record = next((item for item in records if item.child_id == event.slug), None)
            if record is not None and record.kind.value == "worker":
                return ChildTerminal(
                    event.slug,
                    "completed",
                    {"result_digest": event.result_digest},
                )
            if record is not None and record.kind.value == "leaf":
                return LeafCompleted(event.slug, event.result_digest)
        return PublicationFiled(event.slug, 1, "legacy-head", "legacy-base", "legacy-evidence")
    if isinstance(event, ChildFailed):
        return FailureRecorded(f"{event.slug}: {event.reason}")
    if isinstance(event, ChildBlocked):
        return ParkRequested(event.cause, event.recovery_action)
    if isinstance(event, AllChildrenDone):
        return Heartbeat("legacy-all-children-done")
    if isinstance(event, (PRFiled, PRUpdated, PRMerged)):
        return PublicationFiled(
            getattr(event, "slice_id", None) or getattr(event, "slug", "child"),
            getattr(event, "pr_number", 1),
            getattr(event, "head_sha", None) or "legacy-head",
            "legacy-base",
            "legacy-evidence",
        )
    raise TypeError(f"legacy event {type(event).__name__} has no canonical adapter")


_adapt_legacy_event = to_orchestration_event


__all__ = ["IllegalTransition", "to_orchestration_event", "transition"]


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
