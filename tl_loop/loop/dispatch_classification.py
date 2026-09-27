"""The single classification table for leaf-dispatch failures.

A dispatch failure is classified only by its stable machine code. The
operator-facing ``dispatch_error`` prose is never inspected: it is written for
a human reading a parked run, and two different codes can share a sentence.

The machine code for one failure is the ``code`` field of the correlated
durable ``agent.spawn_failed`` ledger event, which the runtime writes from the
typed ``EffectError`` it already returned. A rejected tool result with no such
event carries no code, and an absent code is terminal: the controller fails
closed rather than guessing that an untyped rejection is transient.

## The retryable invariant

**A code is retryable only when its refusal proves that no side effect of the
dispatch happened.** Two facts establish that, and a code asserting either is a
reviewed addition to ``RETRYABLE_CODES``:

1. the decision never ran — nothing was created because nothing was attempted;
2. the attempt lost a creation race — the other creator owns the result and
   this attempt created nothing.

Anything else is either terminal or ambiguous. A transport timeout, a lost
response, and a boundary crossed after a worktree, identity, or tmux window
already exists all leave the outcome *unproven*. An unproven dispatch is never
re-driven: a second spawn could launch a second actor onto the same
deterministic branch. It is resolved by evidence and owner reconciliation,
which is what the durable child-dispatch protocol already prescribes for an
accepted request with delayed evidence.

Unknown and absent codes are terminal by default. Adding a code to
``RETRYABLE_CODES`` is a deliberate, reviewed act; adding a code to
``AMBIGUOUS_CODES`` asserts only that the outcome cannot be proven either way.
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
    """What a refused dispatch may do next, decided from its code alone."""

    #: The refusal proves nothing was created, so re-driving is safe.
    RETRYABLE = "retryable"
    #: The outcome is unproven: hold the persisted intent and wait for evidence.
    AMBIGUOUS = "ambiguous"
    #: The refusal is final; the slice parks and an operator decides.
    TERMINAL = "terminal"


#: The complete retryable set, with the invariant each code satisfies.
RETRYABLE_CODES: dict[str, str] = {
    # Another creator made the deterministic birth branch first. This attempt
    # created nothing, so a re-drive attaches to the branch instead of racing
    # to create it. Satisfies "the attempt lost a creation race".
    "worktree.branch_exists": "creation race: another creator won the birth branch",
    # The shared lifecycle lock was held by a concurrent create/attach decision
    # for longer than the bounded decision timeout, so this attempt made no
    # decision and created nothing. Satisfies "the decision never ran".
    "worktree.lifecycle_lock_timeout": "shared lifecycle lock busy: the decision never ran",
}

#: Codes whose outcome cannot be proven either way.
#:
#: The dispatch is held at ``dispatch_unconfirmed`` with its intent intact and
#: is resolved by matching ``agent.spawned`` evidence or verified owner
#: reconciliation, exactly like an accepted request with delayed evidence. It is
#: never re-driven and never opens a gate on its own.
AMBIGUOUS_CODES: dict[str, str] = {
    # The server-side spawn timeout wraps the whole spawn, so it can fire after
    # the worktree, the identity record, and the tmux window already exist. A
    # retry would risk a second actor on the same deterministic branch.
    "dispatch.transport_timeout": "the spawn boundary timed out; a side effect may already exist",
}

#: Terminal codes named here to document intent. Every code absent from
#: ``RETRYABLE_CODES`` and ``AMBIGUOUS_CODES`` is terminal too, so these
#: entries are never consulted to widen either set.
TERMINAL_CODES: dict[str, str] = {
    # The branch is checked out in a worktree this owner does not own. No
    # retry can change that, and attaching a second agent to another owner's
    # checkout would be worse than parking.
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
    if code in AMBIGUOUS_CODES:
        return DispatchFailureClass.AMBIGUOUS
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
