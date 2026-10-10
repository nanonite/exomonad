"""Typed post-merge recovery events."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .child import TLOrchestrationEvent
from .post_merge_evidence import PushReceipt


@dataclass(frozen=True)
class MergeAdopted(TLOrchestrationEvent):
    """Adopt a remote merge without completing the child."""

    child_id: str
    pr_number: int
    head_sha: str
    journal_id: str
    repository: str
    parent_branch: str
    lane_epoch: int = 1


@dataclass(frozen=True)
class ParentBranchSynced(TLOrchestrationEvent):
    """Confirm parent-branch synchronization after merge adoption."""

    child_id: str
    branch: str
    commit_sha: str


@dataclass(frozen=True)
class IssueClosePending(TLOrchestrationEvent):
    """Persist the Chainlink issue-close intent."""

    child_id: str
    issue_id: str
    intent_id: str


@dataclass(frozen=True)
class IssueCloseConfirmed(TLOrchestrationEvent):
    """Confirm the exact issue-close effect."""

    child_id: str
    issue_id: str
    intent_id: str
    journal_id: str


class IssueCloseFailureKind(str, Enum):
    """Closed categories for a linked issue-close recovery boundary."""

    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IssueCloseFailed(TLOrchestrationEvent):
    """Persist a recoverable issue-close failure without a receipt."""

    child_id: str
    issue_id: str
    intent_id: str
    failure_generation: int
    failure_kind: IssueCloseFailureKind
    detail: str
    observed_status: str | None = None


@dataclass(frozen=True)
class IssueCloseRetryAuthorized(TLOrchestrationEvent):
    """Record an issue-close retry after an approved gate and open status proof."""

    child_id: str
    issue_id: str
    intent_id: str
    failure_generation: int
    next_attempt: int
    observed_status: str


@dataclass(frozen=True)
class IssueCloseAlreadyClosed(TLOrchestrationEvent):
    """Accept an exact issue whose authoritative status is already closed."""

    child_id: str
    issue_id: str
    intent_id: str
    observed_status: str


UNLINKED_CHANGELOG_DECISIONS = ("captured", "add_entry", "no_changelog", "judgment_failed")
"""What the controller decided for an unlinked slice's CHANGELOG coverage.

``captured`` and ``add_entry`` are the roles.tl judgment's own actions;
``no_changelog`` means the parent has no CHANGELOG.md (no model call), and
``judgment_failed`` means the context read or the judgment itself failed.
"""

UNLINKED_CHANGELOG_OUTCOMES = (
    "captured",
    "no_changelog",
    "judgment_failed",
    "entry_added",
    "entry_write_failed",
    "entry_push_failed",
)
"""The durable changelog outcome a completed unlinked slice records."""


@dataclass(frozen=True)
class UnlinkedChangelogJudged(TLOrchestrationEvent):
    """Persist the changelog-coverage decision before any entry is written."""

    child_id: str
    intent_id: str
    decision: str
    reason: str
    section: str = ""
    entry: str = ""
    model: str = ""
    tokens: int = 0


@dataclass(frozen=True)
class PostMergeNotApplicable(TLOrchestrationEvent):
    """Complete an unlinked slice: no issue to close, with its changelog outcome."""

    child_id: str
    changelog_outcome: str
    changelog_detail: str = ""
    changelog_commit_sha: str = ""
    observed_remote_head: str = ""


@dataclass(frozen=True)
class ChangelogPending(TLOrchestrationEvent):
    """Persist the changelog commit intent."""

    child_id: str
    intent_id: str
    generation: int = 0


@dataclass(frozen=True)
class ChangelogCommitted(TLOrchestrationEvent):
    """Confirm the changelog commit."""

    child_id: str
    intent_id: str
    commit_sha: str


@dataclass(frozen=True)
class ParentPushPending(TLOrchestrationEvent):
    """Persist the parent-branch bookkeeping push intent and journal."""

    child_id: str
    intent_id: str
    expected_base_sha: str
    journal_id: str
    lane_epoch: int | None = None


@dataclass(frozen=True)
class PostMergeComplete(TLOrchestrationEvent):
    """Confirm the final bookkeeping push using a verified receipt."""

    child_id: str
    journal_id: str
    push_intent_id: str
    bookkeeping_commit: str
    receipt: PushReceipt


@dataclass(frozen=True)
class PostMergeRebuildRequested(TLOrchestrationEvent):
    """Start a recovery generation with complete failed-push evidence."""

    child_id: str
    generation: int
    failed_push_intent_id: str
    failed_push_journal_id: str
    failed_push_result: str
    observed_remote_head: str
    new_base_sha: str
    merge_ancestor_proof: str
    reason: str


__all__ = [
    "UNLINKED_CHANGELOG_DECISIONS",
    "UNLINKED_CHANGELOG_OUTCOMES",
    "ChangelogCommitted",
    "ChangelogPending",
    "IssueCloseAlreadyClosed",
    "IssueCloseConfirmed",
    "IssueCloseFailed",
    "IssueCloseFailureKind",
    "IssueClosePending",
    "IssueCloseRetryAuthorized",
    "MergeAdopted",
    "ParentBranchSynced",
    "ParentPushPending",
    "PostMergeComplete",
    "PostMergeNotApplicable",
    "PostMergeRebuildRequested",
    "UnlinkedChangelogJudged",
]
