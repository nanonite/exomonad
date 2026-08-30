"""Typed events for recursive TL scope transitions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .child import TLOrchestrationEvent


class ScopeRole(str, Enum):
    """Whether finalization belongs to the root or a direct parent."""

    ROOT = "root"
    NON_ROOT = "non_root"


@dataclass(frozen=True)
class StageReleased(TLOrchestrationEvent):
    """Release direct work or the first ordered sub-TL stage."""

    order: int
    child_ids: tuple[str, ...]


@dataclass(frozen=True)
class WorkerCompleted(TLOrchestrationEvent):
    """Complete a typed worker without a PR/post-merge sequence."""

    child_id: str
    result_digest: str


@dataclass(frozen=True)
class FinalizationRequested(TLOrchestrationEvent):
    """Enter root checkout or non-root publication finalization."""

    role: ScopeRole


@dataclass(frozen=True)
class FinalizationComplete(TLOrchestrationEvent):
    """Confirm role-specific finalization evidence."""

    role: ScopeRole
    evidence: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True)
class FailureRecorded(TLOrchestrationEvent):
    """Stop automatic progression with a durable reason."""

    reason: str


@dataclass(frozen=True)
class ParkRequested(TLOrchestrationEvent):
    """Stop automatic progression behind a durable gate."""

    cause: str
    diagnostic: str


__all__ = [
    "FailureRecorded",
    "FinalizationComplete",
    "FinalizationRequested",
    "ParkRequested",
    "ScopeRole",
    "StageReleased",
    "WorkerCompleted",
]
