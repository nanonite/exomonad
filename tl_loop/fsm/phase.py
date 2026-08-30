"""Pure Python representation of the TL lifecycle phases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias


class TLPhase(Enum):
    """Stable tags for durable scope projections and legacy checkpoints."""

    TLPlanning = "tl_planning"
    TLRunning = "tl_running"
    TLFinalizing = "tl_finalizing"
    TLDispatching = "tl_dispatching"
    TLWaiting = "tl_waiting"
    TLMerging = "tl_merging"
    TLAllMerged = "tl_all_merged"
    TLPRFiled = "tl_pr_filed"
    TLDone = "tl_done"
    TLFailed = "tl_failed"
    TLParked = "tl_parked"


@dataclass(frozen=True)
class ChildHandle:
    """Handle tracked for one spawned child agent."""

    slug: str
    branch: str
    agent_type: str


class Phase:
    """Base type for all concrete TL phase values."""


@dataclass(frozen=True)
class TLPlanning(Phase):
    """The TL is planning work."""


@dataclass(frozen=True)
class TLDispatching(Phase):
    """The TL is dispatching children."""


@dataclass(frozen=True)
class TLWaiting(Phase):
    """The TL is waiting for the mapped outstanding children."""

    children: Mapping[str, ChildHandle]

    def __post_init__(self) -> None:
        object.__setattr__(self, "children", MappingProxyType(dict(self.children)))


@dataclass(frozen=True)
class TLMerging(Phase):
    """The TL is merging children for one PR."""

    pr_number: int
    children: Mapping[str, ChildHandle]

    def __post_init__(self) -> None:
        object.__setattr__(self, "children", MappingProxyType(dict(self.children)))


@dataclass(frozen=True)
class TLAllMerged(Phase):
    """All tracked children have merged."""


@dataclass(frozen=True)
class TLPRFiled(Phase):
    """The TL filed its own PR."""

    pr_number: int
    url: str


@dataclass(frozen=True)
class TLDone(Phase):
    """The TL lifecycle is complete."""


@dataclass(frozen=True)
class TLFailed(Phase):
    """A child failure stopped the TL lifecycle."""

    message: str


PhaseValue: TypeAlias = (
    TLPlanning | TLDispatching | TLWaiting | TLMerging | TLAllMerged | TLPRFiled | TLDone | TLFailed
)
