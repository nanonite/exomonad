"""Dispatcher for the guarded post-merge recovery FSM."""

from __future__ import annotations

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
    PostMergeComplete,
    PostMergeRebuildRequested,
)
from .post_merge_recovery import (
    complete_post_merge,
    rebuild_post_merge,
    receipt_matches,
)
from .post_merge_steps import advance_standard_post_merge


def advance_post_merge(state: PostMergeState, event: object) -> PostMergeState:
    """Apply exactly one post-merge edge or reject the checkpoint."""
    if state.phase is PostMergePhase.COMPLETE:
        if isinstance(event, PostMergeComplete) and receipt_matches(state, event):
            return state
        raise ValueError("completed post-merge state rejects new evidence")
    supported = (
        MergeAdopted,
        ParentBranchSynced,
        IssueClosePending,
        IssueCloseConfirmed,
        ChangelogPending,
        ChangelogCommitted,
        ParentPushPending,
        PostMergeComplete,
        PostMergeRebuildRequested,
    )
    if not isinstance(event, supported):
        raise TypeError(f"unsupported post-merge event {type(event).__name__}")
    if state.phase is not PostMergePhase.NOT_STARTED:
        _require_text(event.child_id, "post-merge child ID")
        if state.evidence.get("child_id") != event.child_id:
            raise ValueError("post-merge evidence mismatch for child_id")
    if isinstance(event, PostMergeRebuildRequested):
        return rebuild_post_merge(state, event)
    if isinstance(event, PostMergeComplete):
        return complete_post_merge(state, event)
    return advance_standard_post_merge(state, event)


__all__ = ["advance_post_merge"]
