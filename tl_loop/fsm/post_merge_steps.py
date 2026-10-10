"""Normal post-merge bookkeeping steps and cumulative evidence checks."""

from __future__ import annotations

from collections.abc import Mapping

from .evidence import require_positive as _require_positive
from .evidence import require_text as _require_text
from .post_merge import PostMergePhase, PostMergeState
from .post_merge_events import (
    UNLINKED_CHANGELOG_DECISIONS,
    UNLINKED_CHANGELOG_OUTCOMES,
    ChangelogCommitted,
    ChangelogPending,
    IssueCloseAlreadyClosed,
    IssueCloseConfirmed,
    IssueCloseFailed,
    IssueCloseFailureKind,
    IssueClosePending,
    IssueCloseRetryAuthorized,
    MergeAdopted,
    ParentBranchSynced,
    ParentPushPending,
    PostMergeNotApplicable,
    UnlinkedChangelogJudged,
)
from .post_merge_recovery import require_fresh_rebuild_push


def advance_standard_post_merge(state: PostMergeState, event: object) -> PostMergeState:
    """Apply one non-terminal, non-recovery bookkeeping step."""
    if isinstance(event, MergeAdopted):
        return _merge_adopted(state, event)
    if isinstance(event, ParentBranchSynced):
        return _parent_branch_synced(state, event)
    if isinstance(event, UnlinkedChangelogJudged):
        return _unlinked_changelog_judged(state, event)
    if isinstance(event, PostMergeNotApplicable):
        return _post_merge_not_applicable(state, event)
    if isinstance(event, IssueClosePending):
        return _issue_close_pending(state, event)
    if isinstance(event, IssueCloseConfirmed):
        return _issue_close_confirmed(state, event)
    if isinstance(event, IssueCloseAlreadyClosed):
        return _issue_close_already_closed(state, event)
    if isinstance(event, IssueCloseFailed):
        return _issue_close_failed(state, event)
    if isinstance(event, IssueCloseRetryAuthorized):
        return _issue_close_retry_authorized(state, event)
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
        {
            "issue_id": event.issue_id,
            "issue_close_intent_id": event.intent_id,
            "issue_close_attempt": "1",
        },
    )


def _unlinked_changelog_judged(
    state: PostMergeState, event: UnlinkedChangelogJudged
) -> PostMergeState:
    _require_text(event.child_id, "post-merge child ID")
    _require_text(event.intent_id, "changelog judgment intent ID")
    _require_text(event.reason, "changelog judgment reason")
    if event.decision not in UNLINKED_CHANGELOG_DECISIONS:
        raise ValueError(f"unknown unlinked changelog decision {event.decision!r}")
    if event.decision == "add_entry":
        _require_text(event.section, "changelog entry section")
        _require_text(event.entry, "changelog entry")
    elif event.section or event.entry:
        raise ValueError("only an add_entry decision carries a changelog entry")
    _require_phase(state, PostMergePhase.PARENT_BRANCH_SYNCED, "unlinked changelog judgment")
    if "issue_id" in state.evidence:
        raise ValueError("a bound Chainlink issue has no unlinked changelog judgment")
    if event.tokens < 0:
        raise ValueError("changelog judgment tokens must not be negative")
    additions = {
        "changelog_judgment_intent_id": event.intent_id,
        "changelog_decision": event.decision,
        "changelog_decision_reason": event.reason[:500],
        "changelog_judgment_tokens": str(event.tokens),
    }
    if event.model:
        additions["changelog_judgment_model"] = event.model
    if event.decision == "add_entry":
        additions["changelog_entry_section"] = event.section
        additions["changelog_entry"] = event.entry
    if "changelog_decision" in state.evidence:
        for key, value in additions.items():
            _require_evidence(state, key, value)
        return state
    return _with_evidence(state, PostMergePhase.PARENT_BRANCH_SYNCED, additions)


def _post_merge_not_applicable(
    state: PostMergeState, event: PostMergeNotApplicable
) -> PostMergeState:
    _require_text(event.child_id, "post-merge child ID")
    outcome = event.changelog_outcome
    if outcome not in UNLINKED_CHANGELOG_OUTCOMES:
        raise ValueError(f"unknown unlinked changelog outcome {outcome!r}")
    if state.phase is PostMergePhase.COMPLETE:
        if (
            state.evidence.get("issue_close_outcome") == "not_applicable"
            and state.evidence.get("changelog_outcome") == outcome
        ):
            _require_evidence(state, "child_id", event.child_id)
            return state
        raise ValueError("completed post-merge state has a different outcome")
    _require_phase(state, PostMergePhase.PARENT_BRANCH_SYNCED, "unlinked post-merge completion")
    if "issue_id" in state.evidence:
        raise ValueError("a bound Chainlink issue cannot be marked not applicable")
    decision = state.evidence.get("changelog_decision")
    if decision is None:
        raise ValueError("unlinked completion requires a recorded changelog decision")
    expected_decision = "add_entry" if outcome.startswith("entry_") else outcome
    if decision != expected_decision:
        raise ValueError(f"changelog outcome {outcome!r} contradicts decision {decision!r}")
    additions = {
        "issue_close_outcome": "not_applicable",
        "changelog_outcome": outcome,
    }
    if event.changelog_detail:
        additions["changelog_outcome_detail"] = event.changelog_detail[:500]
    if outcome == "entry_added":
        _require_text(event.changelog_commit_sha, "changelog entry commit SHA")
        _require_text(event.observed_remote_head, "changelog entry remote head")
        additions["changelog_commit_sha"] = event.changelog_commit_sha
        additions["observed_remote_head"] = event.observed_remote_head
    elif event.changelog_commit_sha or event.observed_remote_head:
        raise ValueError("only entry_added records a changelog commit")
    return _with_evidence(state, PostMergePhase.COMPLETE, additions)


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
        {
            "issue_close_journal_id": event.journal_id,
            "issue_close_outcome": "closed",
        },
    )


def _issue_close_already_closed(
    state: PostMergeState, event: IssueCloseAlreadyClosed
) -> PostMergeState:
    _require_issue_close_identity(state, event)
    if event.observed_status.lower() != "closed":
        raise ValueError("already-closed evidence must report closed status")
    if state.phase is PostMergePhase.ISSUE_CLOSE_CONFIRMED:
        _require_evidence(state, "issue_close_outcome", "already_closed")
        _require_evidence(state, "issue_close_observed_issue_id", event.issue_id)
        _require_evidence(state, "issue_close_observed_status", "closed")
        return state
    _require_phase(state, PostMergePhase.ISSUE_CLOSE_PENDING, "already-closed issue adoption")
    return _with_evidence(
        state,
        PostMergePhase.ISSUE_CLOSE_CONFIRMED,
        {
            "issue_close_outcome": "already_closed",
            "issue_close_observed_issue_id": event.issue_id,
            "issue_close_observed_status": "closed",
        },
    )


def _issue_close_failed(state: PostMergeState, event: IssueCloseFailed) -> PostMergeState:
    _require_issue_close_identity(state, event)
    _require_phase(state, PostMergePhase.ISSUE_CLOSE_PENDING, "issue-close failure")
    if not isinstance(event.failure_kind, IssueCloseFailureKind):
        raise TypeError("issue-close failure kind must be typed")
    if event.failure_generation <= 0:
        raise ValueError("issue-close failure generation must be positive")
    _require_text(event.detail, "issue-close failure detail")
    prior_generation = _evidence_integer(state, "issue_close_failure_generation", 0)
    generation_key = str(event.failure_generation)
    if event.failure_generation != prior_generation + 1:
        if event.failure_generation == prior_generation:
            _require_evidence(
                state,
                f"issue_close_failure_{generation_key}_kind",
                event.failure_kind.value,
            )
            _require_evidence(
                state,
                f"issue_close_failure_{generation_key}_detail",
                event.detail[:500],
            )
            return state
        raise ValueError("issue-close failure generation must advance by one")
    attempt = _evidence_integer(state, "issue_close_attempt", 1)
    additions = {
        "issue_close_failure_generation": generation_key,
        "issue_close_failure_attempt": str(attempt),
        f"issue_close_failure_{generation_key}_kind": event.failure_kind.value,
        f"issue_close_failure_{generation_key}_detail": event.detail[:500],
    }
    if event.observed_status is not None:
        additions[f"issue_close_failure_{generation_key}_observed_status"] = event.observed_status
    return _with_evidence(state, PostMergePhase.ISSUE_CLOSE_PENDING, additions)


def _issue_close_retry_authorized(
    state: PostMergeState, event: IssueCloseRetryAuthorized
) -> PostMergeState:
    _require_issue_close_identity(state, event)
    _require_phase(state, PostMergePhase.ISSUE_CLOSE_PENDING, "issue-close retry")
    _require_positive(event.failure_generation, "issue-close failure generation")
    _require_positive(event.next_attempt, "issue-close retry attempt")
    observed_status = event.observed_status.lower()
    if observed_status not in {"open", "in_progress"}:
        raise ValueError("issue-close retry requires an open issue status")
    _require_evidence(state, "issue_close_failure_generation", str(event.failure_generation))
    current_attempt = _evidence_integer(state, "issue_close_attempt", 1)
    if event.next_attempt != current_attempt + 1:
        raise ValueError("issue-close retry attempt must advance by one")
    return _with_evidence(
        state,
        PostMergePhase.ISSUE_CLOSE_PENDING,
        {
            "issue_close_attempt": str(event.next_attempt),
            "issue_close_retry_authorized_generation": str(event.failure_generation),
            "issue_close_retry_observed_issue_id": event.issue_id,
            "issue_close_retry_observed_status": observed_status,
        },
    )


def _require_issue_close_identity(state: PostMergeState, event: object) -> None:
    issue_id = getattr(event, "issue_id", "")
    intent_id = getattr(event, "intent_id", "")
    _require_text(issue_id, "Chainlink issue ID")
    _require_text(intent_id, "issue-close intent ID")
    _require_evidence(state, "issue_id", issue_id)
    _require_evidence(state, "issue_close_intent_id", intent_id)


def _evidence_integer(state: PostMergeState, key: str, default: int) -> int:
    value = state.evidence.get(key)
    if value is None:
        return default
    if not value.isdigit():
        raise ValueError(f"{key} evidence must be a non-negative integer")
    return int(value)


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
    _require_phase(state, PostMergePhase.CHANGELOG_PENDING, "changelog commit")
    if state.evidence.get("rebuild_reason") and not state.evidence.get("rebuild_applied"):
        if state.evidence.get("changelog_intent_id") == event.intent_id:
            raise ValueError("post-merge rebuild requires a fresh changelog intent")
        return _with_evidence(
            state,
            PostMergePhase.CHANGELOG_COMMITTED,
            {
                "changelog_intent_id": event.intent_id,
                "changelog_commit_sha": event.commit_sha,
                "rebuild_applied": "true",
                "rebuild_commit_sha": event.commit_sha,
            },
        )
    _require_evidence(state, "changelog_intent_id", event.intent_id)
    additions = {"changelog_commit_sha": event.commit_sha}
    if state.evidence.get("rebuild_reason"):
        additions["rebuild_commit_sha"] = event.commit_sha
    return _with_evidence(state, PostMergePhase.CHANGELOG_COMMITTED, additions)


def _parent_push_pending(state: PostMergeState, event: ParentPushPending) -> PostMergeState:
    _require_text(event.intent_id, "parent push intent ID")
    _require_text(event.expected_base_sha, "parent push expected base SHA")
    _require_text(event.journal_id, "parent push journal ID")
    if state.phase is PostMergePhase.PARENT_PUSH_PENDING:
        keys = ("parent_push_intent_id", "push_journal_id", "expected_base_sha")
        if event.lane_epoch is not None:
            keys = (*keys, "lane_epoch")
        _require_same(state, event, keys)
        return state
    _require_phase(state, PostMergePhase.CHANGELOG_COMMITTED, "parent push pending")
    require_fresh_rebuild_push(state, event.intent_id, event.journal_id)
    additions = {
        "parent_push_intent_id": event.intent_id,
        "push_journal_id": event.journal_id,
        "expected_base_sha": event.expected_base_sha,
    }
    if event.lane_epoch is not None:
        _require_positive(event.lane_epoch, "parent push lane epoch")
        additions["lane_epoch"] = str(event.lane_epoch)
    return _with_evidence(state, PostMergePhase.PARENT_PUSH_PENDING, additions)


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
