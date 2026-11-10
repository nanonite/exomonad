"""Required persisted PR identity accessors for TL loop operations."""

from tl_loop.loop.errors import TLLoopError
from tl_loop.state.schema import SliceState


def _require_pr_number(current: SliceState, operation: str) -> int:
    """Return the slice's persisted PR number or fail closed before a PR effect."""
    if current.pr_number is None:
        raise TLLoopError(f"{operation} requires a persisted PR number for slice {current.id!r}")
    return current.pr_number
