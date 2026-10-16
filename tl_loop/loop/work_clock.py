"""Invocation-scoped active-work timing for the TL controller.

The task budget bounds *work*, not the passage of wall time. A controller
invocation that is replaying history, waiting between invocations, or simply
idle must charge no work budget, while an invocation that is genuinely
executing a worker must still time out. This clock is the only place that
answers "how long has this slice's proven work been running *in this
invocation*".

It is deliberately in-memory and per-invocation:

* Between-invocation downtime is never charged because a new process builds a
  new clock with an empty start table.
* Replay/catch-up never charges budget because nothing marks a slice active
  until live operation is running.
* Timers use a monotonic source, so a wall-clock jump cannot move a deadline.
* Historical ``dispatch_started_at`` values remain audit facts on the slice;
  they are never subtracted from the current time to manufacture a deadline.

Only slices the controller has proven active in the current invocation are
marked; a completed producer awaiting parent integration is never marked, so
aggregate integration cannot be expired by a worker budget.
"""

from __future__ import annotations

import time
from collections.abc import Callable

ActivityIdentity = tuple[str | int | None, ...]


class ActiveWorkClock:
    """Tracks monotonic starts for proven work and its observed invocation."""

    def __init__(self, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.monotonic
        self._started: dict[str, tuple[ActivityIdentity | None, float]] = {}

    def mark_active(
        self,
        slice_id: str,
        *,
        activity_identity: ActivityIdentity | None = None,
    ) -> None:
        """Start timing this slice if it is not already being timed.

        Repeated observations of one invocation never move its start forward.
        A newly observed invocation gets a fresh active interval. A timer
        seeded without identity can be bound to the first observation without
        losing already measured active time.
        """
        previous = self._started.get(slice_id)
        if previous is not None:
            previous_identity, previous_start = previous
            if previous_identity == activity_identity:
                return
            if previous_identity is None:
                self._started[slice_id] = (activity_identity, previous_start)
                return
        self._started[slice_id] = (activity_identity, self._now())

    def elapsed(self, slice_id: str) -> float | None:
        """Return active-work seconds in this invocation, or None if untimed."""
        entry = self._started.get(slice_id)
        if entry is None:
            return None
        _, start = entry
        return max(0.0, self._now() - start)

    def stop(self, slice_id: str) -> None:
        """Forget timing for a slice that is no longer active work.

        This is a reset, not a pause: the next interval in which the slice is
        proven active work starts its own clock. That is deliberate -- an
        inactive interval must not consume budget, and each fresh active
        interval is bounded on its own rather than by accumulated wall time.
        """
        self._started.pop(slice_id, None)

    def snapshot(self) -> dict[str, float]:
        """Return a defensive copy for diagnostics/tests."""
        return {slice_id: start for slice_id, (_, start) in self._started.items()}
