"""Typed direct-child records shared by the recursive TL reducers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from .evidence import require_text as _require_text


class TLOrchestrationEvent:
    """Base type for events accepted by the orchestration reducers."""


class ChildKind(str, Enum):
    """The completion contract owned by one direct child."""

    WORKER = "worker"
    LEAF = "leaf"
    SUB_TL = "sub_tl"


@dataclass(frozen=True)
class ChildRecord:
    """Typed direct-child identity and durable dispatch evidence."""

    child_id: str
    kind: ChildKind
    dispatch_intent_id: str | None = None
    invocation_id: str | None = None
    evidence: Mapping[str, str] = field(default_factory=dict)
    lane_id: str | None = None
    manifest_node_id: str | None = None
    manifest_revision: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.child_id, "child ID")
        if not isinstance(self.kind, ChildKind):
            raise TypeError("child kind must be a ChildKind")
        for name in ("dispatch_intent_id", "invocation_id", "lane_id", "manifest_node_id"):
            value = getattr(self, name)
            if value is not None:
                _require_text(value, name)
        if self.manifest_revision is not None:
            if type(self.manifest_revision) is not int or self.manifest_revision < 1:
                raise ValueError("manifest revision must be a positive integer")
            if self.manifest_node_id is None:
                raise ValueError("manifest revision requires a manifest node ID")
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


__all__ = ["ChildKind", "ChildRecord", "TLOrchestrationEvent"]
