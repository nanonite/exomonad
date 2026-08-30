"""Typed post-merge recovery events."""

from __future__ import annotations

from dataclasses import dataclass

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
    "ChangelogCommitted",
    "ChangelogPending",
    "IssueCloseConfirmed",
    "IssueClosePending",
    "MergeAdopted",
    "ParentBranchSynced",
    "ParentPushPending",
    "PostMergeComplete",
    "PostMergeRebuildRequested",
]
