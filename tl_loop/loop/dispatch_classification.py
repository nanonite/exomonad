"""The single classification table for leaf-dispatch failures.

A dispatch failure is classified only by its stable machine code. The
operator-facing ``dispatch_error`` prose is never inspected: it is written for
a human reading a parked run, and two different codes can share a sentence.

The machine code for one failure is the ``code`` field of the correlated
durable ``agent.spawn_failed`` ledger event, which the runtime writes from the
typed ``EffectError`` it already returns. A rejected tool result with no such
event carries no code, and an absent code is terminal: the controller fails
closed rather than guessing that an untyped rejection is transient.

Unknown and absent codes are terminal by default. Adding a code to
``RETRYABLE_CODES`` is a deliberate, reviewed act: it asserts that re-driving
the same dispatch is safe *and* that the underlying condition is transient.
"""

from __future__ import annotations

from enum import Enum

#: Bounded exponential backoff for a scheduled dispatch retry.
#:
#: The delay is a durable scheduled boundary (``dispatch_next_attempt_at``),
#: never an in-memory sleep: a restart inside the window resumes the same
#: boundary instead of re-issuing the effect.
DEFAULT_RETRY_LIMIT = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 5.0
DEFAULT_RETRY_MAX_DELAY_SECONDS = 60.0


class DispatchFailureClass(str, Enum):
    """Whether re-driving the same dispatch is safe and can succeed later."""

    RETRYABLE = "retryable"
    TERMINAL = "terminal"


#: The complete retryable set, with the reason each code is safe to retry.
#:
#: Every entry names a condition that another actor can clear on its own. No
#: entry is a heuristic, and no code is retryable merely because its prose
#: looks temporary.
RETRYABLE_CODES: dict[str, str] = {
    # Another creator made the deterministic birth branch first. The branch
    # exists, so a re-drive attaches to it instead of racing to create it.
    "worktree.branch_exists": "creation race: another creator won the birth branch",
    # The shared lifecycle lock was held by a concurrent create/attach
    # decision for longer than the bounded decision timeout. Nothing about the
    # slice or the repository changed; the decision was never made.
    "worktree.lifecycle_lock_timeout": "shared lifecycle lock busy: the decision never ran",
    # An explicit transport rejection that carries a timeout code. The
    # boundary is known not to have completed, so re-driving is safe.
    "dispatch.transport_timeout": "explicit transport timeout rejection",
}

#: Terminal codes named here exist to document intent. Every code absent from
#: ``RETRYABLE_CODES`` is terminal too, so this mapping is never consulted to
#: widen the retryable set.
TERMINAL_CODES: dict[str, str] = {
    # The branch is checked out in a worktree this owner does not own. No
    # retry can change that, and retrying could attach a second agent to
    # another owner's checkout.
    "worktree.branch_ownership_conflict": "the birth branch belongs to another worktree owner",
    # The forge could not be read for this resume. The operator must fix the
    # forge configuration or the pull request, then re-drive deliberately.
    "worktree.pr_context_unavailable": "the forge could not supply the resume context",
}


def classify_dispatch_failure(code: str | None) -> DispatchFailureClass:
    """Classify one dispatch failure by its machine code alone."""
    if not isinstance(code, str) or not code:
        return DispatchFailureClass.TERMINAL
    if code in RETRYABLE_CODES:
        return DispatchFailureClass.RETRYABLE
    return DispatchFailureClass.TERMINAL


def dispatch_retry_delay(
    retry_attempt: int,
    base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_RETRY_MAX_DELAY_SECONDS,
) -> float:
    """Return the bounded exponential delay before retry number ``retry_attempt``.

    ``retry_attempt`` is 1 for the first scheduled retry, so the delay doubles
    once per scheduled retry and is capped at ``max_delay_seconds``.
    """
    if retry_attempt < 1:
        raise ValueError("retry_attempt must be at least 1")
    if base_delay_seconds < 0 or max_delay_seconds < 0:
        raise ValueError("retry delays must be non-negative")
    exponent = retry_attempt - 1
    return min(base_delay_seconds * (2.0**exponent), max_delay_seconds)
