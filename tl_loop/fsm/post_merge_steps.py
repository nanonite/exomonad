"""Normal post-merge bookkeeping steps and cumulative evidence checks."""

from __future__ import annotations

from collections.abc import Mapping

from .evidence import require_positive as _require_positive
from .evidence import require_text as _require_text
from .post_merge import PostMergePhase, PostMergeState
from .post_merge_events import (
    ChangelogCommitted,
    ChangelogPending,
    IssueCloseConfirmed,
    IssueClosePending,
    MergeAdopted,
    ParentBranchSynced,
    ParentPushPending,
)
from .post_merge_recovery import require_fresh_rebuild_push


def advance_standard_post_merge(state: PostMergeState, event: object) -> PostMergeState:
    """Apply one non-terminal, non-recovery bookkeeping step."""
    if isinstance(event, MergeAdopted):
        return _merge_adopted(state, event)
    if isinstance(event, ParentBranchSynced):
        return _parent_branch_synced(state, event)
    if isinstance(event, IssueClosePending):
        return _issue_close_pending(state, event)
    if isinstance(event, IssueCloseConfirmed):
        return _issue_close_confirmed(state, event)
    if isinstance(event, ChangelogPending):
        return _changelog_pending(state, event)
    if isinstance(event, ChangelogCommitted):
        return _changelog_committed(state, event)
    if isinstance(event, ParentPushPending):
        return _parent_push_pending(state, event)
    raise TypeError(f"unsupported standard post-merge event {type(event).__name__}")


def _merge_adopted(state: PostMergeState, event: MergeAdopted) -> PostMergeState:
    _require_positive(event.pr_number, "merge PR number")
    _require_positive(event.lane_epoch, "merge lane epoch")
    for value, field in (
        (event.child_id, "merge child ID"),
        (event.repository, "merge repository"),
        (event.parent_branch, "merge parent branch"),
        (event.head_sha, "merge head SHA"),
        (event.journal_id, "merge journal ID"),
    ):
        _require_text(value, field)
    if state.phase is PostMergePhase.REMOTE_MERGE_ADOPTED:
        _require_same(
            state,
            event,
            (
                "repository",
                "parent_branch",
                "pr_number",
                "head_sha",
                "merge_journal_id",
                "lane_epoch",
            ),
        )
        return state
    _require_phase(state, PostMergePhase.NOT_STARTED, "merge adoption")
    return PostMergeState(
        PostMergePhase.REMOTE_MERGE_ADOPTED,
        {
            "child_id": event.child_id,
            "repository": event.repository,
            "parent_branch": event.parent_branch,
            "pr_number": str(event.pr_number),
            "head_sha": event.head_sha,
            "merge_journal_id": event.journal_id,
            "lane_epoch": str(event.lane_epoch),
        },
    )


def _parent_branch_synced(state: PostMergeState, event: ParentBranchSynced) -> PostMergeState:
    for value, field in (
        (event.branch, "parent branch"),
        (event.commit_sha, "parent branch commit SHA"),
    ):
        _require_text(value, field)
    if state.phase is PostMergePhase.PARENT_BRANCH_SYNCED:
        _require_same(state, event, ("parent_branch", "parent_commit_sha"))
        return state
    _require_phase(state, PostMergePhase.REMOTE_MERGE_ADOPTED, "parent branch sync")
    _require_evidence(state, "parent_branch", event.branch)
    return _with_evidence(
        state, PostMergePhase.PARENT_BRANCH_SYNCED, {"parent_commit_sha": event.commit_sha}
    )


def _issue_close_pending(state: PostMergeState, event: IssueClosePending) -> PostMergeState:
    _require_text(event.issue_id, "Chainlink issue ID")
    _require_text(event.intent_id, "issue-close intent ID")
    if state.phase is PostMergePhase.ISSUE_CLOSE_PENDING:
        _require_same(state, event, ("issue_id", "issue_close_intent_id"))
        return state
    _require_phase(state, PostMergePhase.PARENT_BRANCH_SYNCED, "issue close pending")
    return _with_evidence(
        state,
        PostMergePhase.ISSUE_CLOSE_PENDING,
        {"issue_id": event.issue_id, "issue_close_intent_id": event.intent_id},
    )


def _issue_close_confirmed(state: PostMergeState, event: IssueCloseConfirmed) -> PostMergeState:
    for value, field in (
        (event.issue_id, "Chainlink issue ID"),
        (event.intent_id, "issue-close intent ID"),
        (event.journal_id, "issue-close journal ID"),
    ):
        _require_text(value, field)
    if state.phase is PostMergePhase.ISSUE_CLOSE_CONFIRMED:
        _require_same(state, event, ("issue_id", "issue_close_intent_id", "issue_close_journal_id"))
        return state
    _require_phase(state, PostMergePhase.ISSUE_CLOSE_PENDING, "issue close confirmation")
    _require_same(state, event, ("issue_id", "issue_close_intent_id"))
    return _with_evidence(
        state,
        PostMergePhase.ISSUE_CLOSE_CONFIRMED,
        {"issue_close_journal_id": event.journal_id},
    )


def _changelog_pending(state: PostMergeState, event: ChangelogPending) -> PostMergeState:
    _require_text(event.intent_id, "changelog intent ID")
    if event.generation < 0:
        raise ValueError("changelog generation must be non-negative")
    if state.phase is PostMergePhase.CHANGELOG_PENDING:
        if (
            state.evidence.get("rebuild_reason")
            and state.evidence.get("changelog_generation") == str(event.generation)
            and state.evidence.get("changelog_intent_id") != event.intent_id
        ):
            return _with_evidence(
                state,
                PostMergePhase.CHANGELOG_PENDING,
                {"changelog_intent_id": event.intent_id, "rebuild_applied": "true"},
            )
        _require_same(state, event, ("changelog_intent_id", "changelog_generation"))
        return state
    _require_phase(state, PostMergePhase.ISSUE_CLOSE_CONFIRMED, "changelog pending")
    return _with_evidence(
        state,
        PostMergePhase.CHANGELOG_PENDING,
        {
            "changelog_intent_id": event.intent_id,
            "changelog_generation": str(event.generation),
        },
    )


def _changelog_committed(state: PostMergeState, event: ChangelogCommitted) -> PostMergeState:
    _require_text(event.intent_id, "changelog intent ID")
    _require_text(event.commit_sha, "changelog commit SHA")
    if state.phase is PostMergePhase.CHANGELOG_COMMITTED:
        _require_same(state, event, ("changelog_intent_id", "changelog_commit_sha"))
        return state
    if state.evidence.get("rebuild_reason") and not state.evidence.get("rebuild_applied"):
        raise ValueError("post-merge rebuild requires a new changelog intent")
    _require_phase(state, PostMergePhase.CHANGELOG_PENDING, "changelog commit")
    _require_evidence(state, "changelog_intent_id", event.intent_id)
    return _with_evidence(
        state,
        PostMergePhase.CHANGELOG_COMMITTED,
        {"changelog_commit_sha": event.commit_sha},
    )


def _parent_push_pending(state: PostMergeState, event: ParentPushPending) -> PostMergeState:
    _require_text(event.intent_id, "parent push intent ID")
    _require_text(event.expected_base_sha, "parent push expected base SHA")
    _require_text(event.journal_id, "parent push journal ID")
    if state.phase is PostMergePhase.PARENT_PUSH_PENDING:
        _require_same(
            state, event, ("parent_push_intent_id", "push_journal_id", "expected_base_sha")
        )
        return state
    _require_phase(state, PostMergePhase.CHANGELOG_COMMITTED, "parent push pending")
    require_fresh_rebuild_push(state, event.intent_id, event.journal_id)
    return _with_evidence(
        state,
        PostMergePhase.PARENT_PUSH_PENDING,
        {
            "parent_push_intent_id": event.intent_id,
            "push_journal_id": event.journal_id,
            "expected_base_sha": event.expected_base_sha,
        },
    )


def _with_evidence(
    state: PostMergeState,
    phase: PostMergePhase,
    additions: Mapping[str, str],
) -> PostMergeState:
    return PostMergeState(phase, {**state.evidence, **additions})


def _require_phase(state: PostMergeState, expected: PostMergePhase, operation: str) -> None:
    if state.phase is not expected:
        raise ValueError(f"{operation} requires {expected.value}, got {state.phase.value}")


def _require_evidence(state: PostMergeState, key: str, value: str) -> None:
    if state.evidence.get(key) != value:
        raise ValueError(f"post-merge evidence mismatch for {key}")


def _require_same(state: PostMergeState, event: object, keys: tuple[str, ...]) -> None:
    values = {
        "pr_number": str(getattr(event, "pr_number", "")),
        "head_sha": getattr(event, "head_sha", ""),
        "merge_journal_id": getattr(event, "journal_id", ""),
        "lane_epoch": str(getattr(event, "lane_epoch", "")),
        "repository": getattr(event, "repository", ""),
        "parent_branch": getattr(event, "parent_branch", getattr(event, "branch", "")),
        "parent_commit_sha": getattr(event, "commit_sha", ""),
        "issue_id": getattr(event, "issue_id", ""),
        "issue_close_intent_id": getattr(event, "intent_id", ""),
        "issue_close_journal_id": getattr(event, "journal_id", ""),
        "changelog_intent_id": getattr(event, "intent_id", ""),
        "changelog_generation": str(getattr(event, "generation", "")),
        "changelog_commit_sha": getattr(event, "commit_sha", ""),
        "parent_push_intent_id": getattr(event, "intent_id", ""),
        "push_journal_id": getattr(event, "journal_id", ""),
        "expected_base_sha": getattr(event, "expected_base_sha", ""),
    }
    for key in keys:
        _require_evidence(state, key, values[key])


__all__ = ["advance_standard_post_merge"]
