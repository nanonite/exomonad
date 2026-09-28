"""Bounded waits on durable boundaries for real-server acceptances.

Every wait here polls a durable artifact until a boundary is reached, with an
explicit deadline and a diagnostic that names the last state it saw, so a
failure reports what the system looked like rather than only how long the
harness waited. No wait returns success because time passed: each one names the
boundary it proved.

There is deliberately no "wait for the count to stop changing" wait here. A
count that has not moved for a few seconds is not evidence that nothing further
is coming, so a duplicate that lands after such a window would pass silently.
Every count in these acceptances is read after a terminal boundary instead: the
deterministic agent's own durable record that its invocation finished.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

#: The default bound for a durable boundary. A real server provisioning a
#: worktree, pushing to a real forge, and waiting for a tmux window is slow, so
#: the default is generous; every call site may pass a tighter one.
DEFAULT_TIMEOUT_SECONDS = 120.0

#: Poll interval. Short enough that a boundary is observed promptly, long
#: enough that a full run does not spend its time polling.
POLL_INTERVAL_SECONDS = 0.2

T = TypeVar("T")


class Timeout(RuntimeError):
    """Raised when a durable boundary did not arrive before its deadline."""


def await_boundary(
    probe: Callable[[], T | None],
    *,
    description: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> T:
    """Poll ``probe`` until it returns a value, or fail with its last state.

    ``probe`` returns ``None`` while the boundary has not been reached, and a
    value once it has. The value is returned unchanged, so a caller can assert
    on the evidence rather than on the fact that a wait ended.
    """
    deadline = time.monotonic() + timeout
    last: T | None = None
    while time.monotonic() < deadline:
        last = probe()
        if last is not None:
            return last
        time.sleep(poll_interval)
    raise Timeout(
        f"timed out after {timeout:.0f}s waiting for {description}; "
        f"last observed state: {last!r}"
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "Timeout",
    "await_boundary",
]
