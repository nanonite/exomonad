"""Receipt completion and explicit rebuild recovery for post-merge state."""

from __future__ import annotations

from collections.abc import Mapping

from .evidence import require_positive as _require_positive
from .evidence import require_text as _require_text
from .post_merge import PostMergePhase, PostMergeState
from .post_merge_events import PostMergeComplete, PostMergeRebuildRequested


def rebuild_post_merge(state: PostMergeState, event: PostMergeRebuildRequested) -> PostMergeState:
    """Start a new bookkeeping generation only with authoritative failure evidence."""
    _require_positive(event.generation, "post-merge rebuild generation")
    for value, field in (
        (event.failed_push_intent_id, "failed push intent ID"),
        (event.failed_push_journal_id, "failed push journal ID"),
        (event.failed_push_result, "failed push result"),
        (event.observed_remote_head, "observed remote head"),
        (event.new_base_sha, "new base SHA"),
        (event.merge_ancestor_proof, "merge ancestor proof"),
        (event.reason, "post-merge rebuild reason"),
    ):
        _require_text(value, field)
    _require_phase(state, PostMergePhase.PARENT_PUSH_PENDING, "post-merge rebuild")
    _require_evidence(state, "parent_push_intent_id", event.failed_push_intent_id)
    _require_evidence(state, "push_journal_id", event.failed_push_journal_id)
    previous = int(state.evidence.get("changelog_generation", "0"))
    if event.generation <= previous:
        raise ValueError("post-merge rebuild generation must advance")
    return _with_evidence(
        state,
        PostMergePhase.CHANGELOG_PENDING,
        {
            "changelog_generation": str(event.generation),
            "expected_base_sha": event.new_base_sha,
            "rebuild_failed_push_intent_id": event.failed_push_intent_id,
            "rebuild_failed_push_journal_id": event.failed_push_journal_id,
            "rebuild_failed_push_result": event.failed_push_result,
            "rebuild_observed_remote_head": event.observed_remote_head,
            "rebuild_new_base_sha": event.new_base_sha,
            "rebuild_merge_ancestor_proof": event.merge_ancestor_proof,
            "rebuild_reason": event.reason,
        },
    )


def complete_post_merge(state: PostMergeState, event: PostMergeComplete) -> PostMergeState:
    """Complete a post-merge sequence only with a matching push receipt."""
    _require_phase(state, PostMergePhase.PARENT_PUSH_PENDING, "post-merge completion")
    for value, field in (
        (event.journal_id, "post-merge journal ID"),
        (event.push_intent_id, "parent push intent ID"),
        (event.bookkeeping_commit, "bookkeeping commit"),
    ):
        _require_text(value, field)
    receipt = event.receipt
    for key, value in (
        ("merge_journal_id", event.journal_id),
        ("parent_push_intent_id", event.push_intent_id),
        ("push_journal_id", receipt.push_journal_id),
        ("expected_base_sha", receipt.expected_base_sha),
        ("lane_epoch", str(receipt.lane_epoch)),
        ("repository", receipt.repository),
        ("parent_branch", receipt.parent_branch),
        ("changelog_commit_sha", event.bookkeeping_commit),
    ):
        _require_evidence(state, key, value)
    if receipt.child_id != event.child_id:
        raise ValueError("push receipt child does not match post-merge child")
    if receipt.push_intent_id != event.push_intent_id:
        raise ValueError("push receipt intent does not match post-merge intent")
    if receipt.pushed_commit != event.bookkeeping_commit:
        raise ValueError("push receipt commit does not match bookkeeping commit")
    return _with_evidence(
        state,
        PostMergePhase.COMPLETE,
        {
            "push_receipt_id": receipt.push_receipt_id,
            "pushed_commit": receipt.pushed_commit,
            "bookkeeping_commit": event.bookkeeping_commit,
            "observed_remote_head": receipt.observed_remote_head,
            "ancestry_proof": receipt.ancestry_proof,
        },
    )


def receipt_matches(state: PostMergeState, event: PostMergeComplete) -> bool:
    """Recognize an exact replay of an already completed receipt."""
    return all(
        state.evidence.get(key) == value
        for key, value in {
            "child_id": event.child_id,
            "merge_journal_id": event.journal_id,
            "parent_push_intent_id": event.push_intent_id,
            "push_journal_id": event.receipt.push_journal_id,
            "push_receipt_id": event.receipt.push_receipt_id,
            "pushed_commit": event.receipt.pushed_commit,
            "bookkeeping_commit": event.bookkeeping_commit,
            "observed_remote_head": event.receipt.observed_remote_head,
            "ancestry_proof": event.receipt.ancestry_proof,
        }.items()
    )


def require_fresh_rebuild_push(
    state: PostMergeState,
    push_intent_id: str,
    push_journal_id: str,
) -> None:
    """Reject a rebuilt push that could be deduplicated as its failed attempt."""
    failed_intent = state.evidence.get("rebuild_failed_push_intent_id")
    failed_journal = state.evidence.get("rebuild_failed_push_journal_id")
    if failed_intent is None and failed_journal is None:
        return
    if failed_intent is None or failed_journal is None:
        raise ValueError("rebuilt push is missing failed-attempt identity evidence")
    if push_intent_id == failed_intent:
        raise ValueError("rebuilt push intent must be fresh")
    if push_journal_id == failed_journal:
        raise ValueError("rebuilt push journal must be fresh")


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


__all__ = [
    "complete_post_merge",
    "rebuild_post_merge",
    "receipt_matches",
    "require_fresh_rebuild_push",
]
