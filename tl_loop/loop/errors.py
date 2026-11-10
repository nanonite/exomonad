"""Shared typed errors for TL loop operations."""


class TLLoopError(RuntimeError):
    """The TL loop cannot continue without operator intervention."""
