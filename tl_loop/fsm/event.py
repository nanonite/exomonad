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
class PRFiled(TLEvent):
    """A slice PR was filed for a particular head."""

    pr_number: int
    head_sha: str
    slice_id: str | None = None


@dataclass(frozen=True)
class PRUpdated(TLEvent):
    """A slice PR was updated; a new head starts fresh gate state."""

    pr_number: int
    head_sha: str
    slice_id: str | None = None


# Name the semantic transition explicitly for callers that do not care about
# the wire event which reported it.
PRHeadChanged = PRUpdated


@dataclass(frozen=True)
class AllChildrenDone(TLEvent):
    """All children have completed their lifecycle."""


@dataclass(frozen=True)
class OwnPRFiled(TLEvent):
    """The TL filed its own PR."""

    pr_number: int
    url: str
    branch: str
