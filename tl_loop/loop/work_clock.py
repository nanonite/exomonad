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


class ActiveWorkClock:
    """Tracks the monotonic start of proven active work per slice."""

    def __init__(self, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.monotonic
        self._started: dict[str, float] = {}

    def mark_active(self, slice_id: str) -> None:
        """Start timing this slice if it is not already being timed.

        Repeated observations never move the start forward: the first proof of
        active work in this invocation fixes the deadline.
        """
        self._started.setdefault(slice_id, self._now())

    def elapsed(self, slice_id: str) -> float | None:
        """Return active-work seconds in this invocation, or None if untimed."""
        start = self._started.get(slice_id)
        if start is None:
            return None
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
        return dict(self._started)
