"""Pure comparison of durable review evidence and authoritative observations."""

from __future__ import annotations

from datetime import UTC, datetime

from tl_loop.state.schema import (
    DurableReviewEvidence,
    ReviewValidationDisposition,
    ReviewValidationObservation,
    Verdict,
)


def review_validation_is_fresh(
    evidence: DurableReviewEvidence | None,
    *,
    now: datetime | None,
    freshness_window_secs: int | None,
    expected_pr_number: int | None = None,
    expected_head_sha: str | None = None,
    expected_verdict: Verdict | None = None,
    expected_reviewer_agent_id: str | None = None,
) -> bool:
    """Return whether the latest validation is recent enough for a merge."""
    if (
        evidence is None
        or evidence.validated_at is None
        or not evidence.reviewer_account_authenticated
        or evidence.dismissed
        or evidence.forgejo_stale
        or evidence.reviewer_identity_unresolved
        or not evidence.reviewer_agent_id
    ):
        return False
    if (
        (expected_pr_number is not None and evidence.pr_number != expected_pr_number)
        or (expected_head_sha is not None and evidence.head_sha != expected_head_sha)
        or (expected_verdict is not None and evidence.verdict != expected_verdict)
        or (
            expected_reviewer_agent_id is not None
            and evidence.reviewer_agent_id != expected_reviewer_agent_id
        )
    ):
        return False
    if freshness_window_secs is None:
        return True
    if evidence.validated_at is None:
        return False
    try:
        validated_at = _parse_timestamp(evidence.validated_at)
    except ValueError:
        return False
    current = now or datetime.now(UTC)
    return max(0.0, (current - validated_at).total_seconds()) <= freshness_window_secs


def review_validation_disposition(
    evidence: DurableReviewEvidence | None,
    observation: ReviewValidationObservation,
    *,
    now: datetime | None = None,
    freshness_window_secs: int | None = None,
) -> ReviewValidationDisposition:
    """Classify one snapshot without mutating or re-adjudicating review state."""
    if not observation.reviewer_account_authenticated or observation.reviewer_identity_unresolved:
        return ReviewValidationDisposition.UNAUTHORIZED
    if observation.dismissed or observation.forgejo_stale:
        return ReviewValidationDisposition.INVALIDATED
    if evidence is None:
        return ReviewValidationDisposition.REFRESHED
    if observation.head_sha != evidence.head_sha or observation.pr_number != evidence.pr_number:
        return ReviewValidationDisposition.INVALIDATED
    if observation.review_id < evidence.review_id:
        return ReviewValidationDisposition.OUT_OF_ORDER
    if observation.review_id > evidence.review_id or observation.identity() != evidence.identity():
        return ReviewValidationDisposition.SUPERSEDED
    if review_validation_is_fresh(
        evidence,
        now=now,
        freshness_window_secs=freshness_window_secs,
        expected_pr_number=evidence.pr_number,
        expected_head_sha=evidence.head_sha,
        expected_verdict=evidence.verdict,
        expected_reviewer_agent_id=evidence.reviewer_agent_id,
    ):
        return ReviewValidationDisposition.ALREADY_FRESH
    return ReviewValidationDisposition.REFRESHED


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
