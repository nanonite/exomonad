"""Typed projection of the host watcher response.

Watcher responses cross a protobuf/JSON boundary where default values may be
omitted.  This projection keeps absence distinct from explicit ``False`` and
empty strings so loop decisions have one contract to consume.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_bool(value: object) -> bool | None:
    return value if type(value) is bool else None


@dataclass(frozen=True)
class PublicationRecord:
    """Host-verified publication identity carried by a watcher snapshot."""

    invocation_id: str | None
    slice_id: str | None
    author_agent: str | None
    succession_invocation_ids: tuple[str, ...]

    @classmethod
    def from_value(cls, value: object) -> PublicationRecord | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            return None
        succession = value.get("succession_invocation_ids")
        if not isinstance(succession, (list, tuple)):
            succession_ids: tuple[str, ...] = ()
        else:
            succession_ids = tuple(item for item in succession if isinstance(item, str))
        return cls(
            invocation_id=_optional_text(value.get("invocation_id")),
            slice_id=_optional_text(value.get("slice_id")),
            author_agent=_optional_text(value.get("author_agent")),
            succession_invocation_ids=succession_ids,
        )


@dataclass(frozen=True)
class WatcherObservation:
    """Typed watcher response; absent values remain ``None``."""

    pr_number: int | None
    found: bool | None
    pr_state: str | None
    merged: bool | None
    head_sha: str | None
    base_sha: str | None
    ci_status: str | None
    review_state: str | None
    patch_digest: str | None
    merge_tree_sha: str | None
    head_branch: str | None
    base_branch: str | None
    head_reachable: bool | None
    evidence_error: str | None
    publication_ownership_verified: bool | None
    publication_ownership_error: str | None
    publication: PublicationRecord | None
    ownership_verified_present: bool
    ownership_error_present: bool

    def get(self, name: str, default: object = None) -> object:
        """Compatibility accessor for non-migrated observation consumers."""
        return getattr(self, name, default)

    def __contains__(self, name: str) -> bool:
        return hasattr(self, name)

    @classmethod
    def from_response(cls, raw: Mapping[str, object]) -> WatcherObservation:
        """Project one raw effect result without applying defaults."""
        pr_number = raw.get("pr_number")
        return cls(
            pr_number=pr_number if type(pr_number) is int else None,
            found=_optional_bool(raw.get("found")),
            pr_state=_optional_text(raw.get("pr_state")),
            merged=_optional_bool(raw.get("merged")),
            head_sha=_optional_text(raw.get("head_sha")),
            base_sha=_optional_text(raw.get("base_sha")),
            ci_status=_optional_text(raw.get("ci_status")),
            review_state=_optional_text(raw.get("review_state")),
            patch_digest=_optional_text(raw.get("patch_digest")),
            merge_tree_sha=_optional_text(raw.get("merge_tree_sha")),
            head_branch=_optional_text(raw.get("head_branch")),
            base_branch=_optional_text(raw.get("base_branch")),
            head_reachable=_optional_bool(raw.get("head_reachable")),
            evidence_error=_optional_text(raw.get("evidence_error")),
            publication_ownership_verified=_optional_bool(
                raw.get("publication_ownership_verified")
            ),
            publication_ownership_error=_optional_text(raw.get("publication_ownership_error")),
            publication=PublicationRecord.from_value(raw.get("publication")),
            ownership_verified_present="publication_ownership_verified" in raw,
            ownership_error_present="publication_ownership_error" in raw,
        )

    def ownership_status(self) -> tuple[bool, str | None]:
        """Return the fail-closed ownership verdict and an operator reason."""
        if not self.ownership_verified_present:
            return False, "watcher_pr_state omitted publication_ownership_verified"
        if type(self.publication_ownership_verified) is not bool:
            return False, "watcher_pr_state returned a non-boolean ownership verdict"
        if not self.ownership_error_present:
            return False, "watcher_pr_state omitted publication_ownership_error"
        if not isinstance(self.publication_ownership_error, str):
            return False, "watcher_pr_state returned a non-string ownership error"
        if self.publication_ownership_verified and self.publication_ownership_error:
            return False, "watcher_pr_state returned contradictory ownership evidence"
        if not self.publication_ownership_verified:
            return False, self.publication_ownership_error or "publication ownership is unverified"
        return True, None

    def to_payload(self) -> dict[str, object]:
        """Serialize observed fields for telemetry without inventing defaults."""
        payload: dict[str, object] = {}
        for name in (
            "head_sha",
            "review_state",
            "ci_status",
            "pr_state",
            "merged",
            "head_reachable",
            "evidence_error",
            "publication_ownership_verified",
            "publication_ownership_error",
        ):
            value = getattr(self, name)
            if value is not None or (
                name == "publication_ownership_error" and self.ownership_error_present
            ):
                payload[name] = value
        return payload

    def with_publication(self, publication: object) -> WatcherObservation:
        """Return this snapshot with a separately resolved publication record."""
        return WatcherObservation(
            pr_number=self.pr_number,
            found=self.found,
            pr_state=self.pr_state,
            merged=self.merged,
            head_sha=self.head_sha,
            base_sha=self.base_sha,
            ci_status=self.ci_status,
            review_state=self.review_state,
            patch_digest=self.patch_digest,
            merge_tree_sha=self.merge_tree_sha,
            head_branch=self.head_branch,
            base_branch=self.base_branch,
            head_reachable=self.head_reachable,
            evidence_error=self.evidence_error,
            publication_ownership_verified=self.publication_ownership_verified,
            publication_ownership_error=self.publication_ownership_error,
            publication=PublicationRecord.from_value(publication),
            ownership_verified_present=self.ownership_verified_present,
            ownership_error_present=self.ownership_error_present,
        )
