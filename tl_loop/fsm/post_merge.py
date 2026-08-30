"""Durable post-merge phases and cumulative checkpoint validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from .evidence import require_fields as _require_fields
from .post_merge_events import (
    ChangelogCommitted,
    ChangelogPending,
    IssueCloseConfirmed,
    IssueClosePending,
    MergeAdopted,
    ParentBranchSynced,
    ParentPushPending,
    PostMergeComplete,
    PostMergeRebuildRequested,
)
from .post_merge_evidence import PushReceipt


class PostMergePhase(str, Enum):
    """Strict per-child post-merge progression."""

    NOT_STARTED = "not_started"
    NOT_REQUIRED = "not_required"
    REMOTE_MERGE_ADOPTED = "remote_merge_adopted"
    PARENT_BRANCH_SYNCED = "parent_branch_synced"
    ISSUE_CLOSE_PENDING = "issue_close_pending"
    ISSUE_CLOSE_CONFIRMED = "issue_close_confirmed"
    CHANGELOG_PENDING = "changelog_pending"
    CHANGELOG_COMMITTED = "changelog_committed"
    PARENT_PUSH_PENDING = "parent_push_pending"
    COMPLETE = "complete"


def _cumulative(*fields: str) -> tuple[str, ...]:
    return ("child_id", *fields)


_REQUIRED_EVIDENCE: dict[PostMergePhase, tuple[str, ...]] = {
    PostMergePhase.REMOTE_MERGE_ADOPTED: _cumulative(
        "repository",
        "parent_branch",
        "pr_number",
        "head_sha",
        "merge_journal_id",
        "lane_epoch",
    ),
    PostMergePhase.PARENT_BRANCH_SYNCED: _cumulative(
        "repository",
        "parent_branch",
        "pr_number",
        "head_sha",
        "merge_journal_id",
        "lane_epoch",
        "parent_commit_sha",
    ),
    PostMergePhase.ISSUE_CLOSE_PENDING: _cumulative(
        "repository",
        "parent_branch",
        "pr_number",
        "head_sha",
        "merge_journal_id",
        "lane_epoch",
        "parent_commit_sha",
        "issue_id",
        "issue_close_intent_id",
    ),
    PostMergePhase.ISSUE_CLOSE_CONFIRMED: _cumulative(
        "repository",
        "parent_branch",
        "pr_number",
        "head_sha",
        "merge_journal_id",
        "lane_epoch",
        "parent_commit_sha",
        "issue_id",
        "issue_close_intent_id",
        "issue_close_journal_id",
    ),
    PostMergePhase.CHANGELOG_PENDING: _cumulative(
        "repository",
        "parent_branch",
        "pr_number",
        "head_sha",
        "merge_journal_id",
        "lane_epoch",
        "parent_commit_sha",
        "issue_id",
        "issue_close_intent_id",
        "issue_close_journal_id",
        "changelog_intent_id",
        "changelog_generation",
    ),
    PostMergePhase.CHANGELOG_COMMITTED: _cumulative(
        "repository",
        "parent_branch",
        "pr_number",
        "head_sha",
        "merge_journal_id",
        "lane_epoch",
        "parent_commit_sha",
        "issue_id",
        "issue_close_intent_id",
        "issue_close_journal_id",
        "changelog_intent_id",
        "changelog_generation",
        "changelog_commit_sha",
    ),
    PostMergePhase.PARENT_PUSH_PENDING: _cumulative(
        "repository",
        "parent_branch",
        "pr_number",
        "head_sha",
        "merge_journal_id",
        "lane_epoch",
        "parent_commit_sha",
        "issue_id",
        "issue_close_intent_id",
        "issue_close_journal_id",
        "changelog_intent_id",
        "changelog_generation",
        "changelog_commit_sha",
        "parent_push_intent_id",
        "push_journal_id",
        "expected_base_sha",
    ),
    PostMergePhase.COMPLETE: _cumulative(
        "repository",
        "parent_branch",
        "pr_number",
        "head_sha",
        "merge_journal_id",
        "lane_epoch",
        "parent_commit_sha",
        "issue_id",
        "issue_close_intent_id",
        "issue_close_journal_id",
        "changelog_intent_id",
        "changelog_generation",
        "changelog_commit_sha",
        "parent_push_intent_id",
        "push_journal_id",
        "expected_base_sha",
        "push_receipt_id",
        "pushed_commit",
        "bookkeeping_commit",
        "observed_remote_head",
        "ancestry_proof",
    ),
}


@dataclass(frozen=True)
class PostMergeState:
    """One post-merge phase with all predecessor evidence retained."""

    phase: PostMergePhase = PostMergePhase.NOT_STARTED
    evidence: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.phase, PostMergePhase):
            raise TypeError("post-merge phase must be a PostMergePhase")
        evidence = dict(self.evidence)
        _require_fields(evidence, _REQUIRED_EVIDENCE.get(self.phase, ()), self.phase.value)
        object.__setattr__(self, "evidence", MappingProxyType(evidence))


__all__ = [
    "ChangelogCommitted",
    "ChangelogPending",
    "IssueCloseConfirmed",
    "IssueClosePending",
    "MergeAdopted",
    "ParentBranchSynced",
    "ParentPushPending",
    "PostMergeComplete",
    "PostMergePhase",
    "PostMergeRebuildRequested",
    "PostMergeState",
    "PushReceipt",
]
