"""Pure Python representation of TL lifecycle events."""

from __future__ import annotations

from dataclasses import dataclass

from .phase import ChildHandle


class TLEvent:
    """Base type for all concrete TL lifecycle events."""


@dataclass(frozen=True)
class ChildSpawned(TLEvent):
    """A child was spawned and can be tracked."""

    handle: ChildHandle


@dataclass(frozen=True)
class ChildCompleted(TLEvent):
    """A child completed its work."""

    slug: str


@dataclass(frozen=True)
class ChildFailed(TLEvent):
    """A child failed with a reason."""

    slug: str
    reason: str


@dataclass(frozen=True)
class PRMerged(TLEvent):
    """A child PR merged."""

    pr_number: int
    slug: str


@dataclass(frozen=True)
class AllChildrenDone(TLEvent):
    """All children have completed their lifecycle."""


@dataclass(frozen=True)
class OwnPRFiled(TLEvent):
    """The TL filed its own PR."""

    pr_number: int
    url: str
    branch: str
