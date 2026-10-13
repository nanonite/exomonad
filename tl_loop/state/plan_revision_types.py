"""Data structures shared by the plan revision analyzer and its helpers."""

from __future__ import annotations

from dataclasses import dataclass, field

from .plan_manifest import PlanManifest
from .schema import RunState


class PlanRevisionError(ValueError):
    """A plan revision cannot be analyzed safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlanRevisionBlocker:
    """One plan change that cannot be applied without rewriting history."""

    code: str
    reason: str
    scope_id: str | None = None
    node_id: str | None = None

    def to_document(self) -> dict[str, object]:
        return {
            "code": self.code,
            "reason": self.reason,
            "scope_id": self.scope_id,
            "node_id": self.node_id,
        }


@dataclass(frozen=True)
class PlanRevisionNodeChange:
    """A safe summary of one declaration change, without task text."""

    scope_id: str
    node_id: str
    action: str
    fields: tuple[str, ...]

    def to_document(self) -> dict[str, object]:
        return {
            "scope_id": self.scope_id,
            "node_id": self.node_id,
            "action": self.action,
            "fields": list(self.fields),
        }


@dataclass(frozen=True)
class PlanRevisionScope:
    """Old and proposed manifest identity for one changed scope."""

    scope_id: str
    previous_revision: int | None
    candidate_revision: int | None
    previous_digest: str | None
    candidate_digest: str | None

    def to_document(self) -> dict[str, object]:
        return {
            "scope_id": self.scope_id,
            "previous_revision": self.previous_revision,
            "candidate_revision": self.candidate_revision,
            "previous_digest": self.previous_digest,
            "candidate_digest": self.candidate_digest,
        }


@dataclass(frozen=True)
class PlanRevisionAnalysis:
    """Immutable dry-run result and candidate manifests for the apply phase."""

    previous_plan_sha256: str
    candidate_plan_sha256: str
    scopes: tuple[PlanRevisionScope, ...]
    changes: tuple[PlanRevisionNodeChange, ...]
    blockers: tuple[PlanRevisionBlocker, ...]
    candidate_plan_bytes: bytes = field(repr=False, compare=False)
    previous_manifest: PlanManifest = field(repr=False, compare=False)
    candidate_manifest: PlanManifest = field(repr=False)

    @property
    def can_apply(self) -> bool:
        """Return whether this candidate preserves all protected state."""
        return not self.blockers

    def to_document(self) -> dict[str, object]:
        """Return deterministic JSON data suitable for a CLI dry-run."""
        return {
            "previous_plan_sha256": self.previous_plan_sha256,
            "candidate_plan_sha256": self.candidate_plan_sha256,
            "can_apply": self.can_apply,
            "scopes": [scope.to_document() for scope in self.scopes],
            "changes": [change.to_document() for change in self.changes],
            "blockers": [blocker.to_document() for blocker in self.blockers],
        }


@dataclass(frozen=True)
class DecodedPlan:
    """Validated plan document and its typed executable plan."""

    document: dict[str, object]
    plan: object


@dataclass(frozen=True)
class ScopeState:
    """Validated RunStore view and matching manifest for one scope."""

    state: RunState
    manifest: PlanManifest
