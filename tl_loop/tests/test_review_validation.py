from dataclasses import replace
from datetime import UTC, datetime

from tl_loop.state.review_validation import (
    review_validation_disposition,
    review_validation_is_fresh,
)
from tl_loop.state.schema import (
    DurableReviewEvidence,
    ReviewValidationDisposition,
    ReviewValidationObservation,
    Verdict,
)

NOW = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)


def _evidence(
    *, validated_at: str | None = "2026-08-28T23:55:00Z", **changes: object
) -> DurableReviewEvidence:
    evidence = DurableReviewEvidence(
        review_id=17,
        pr_number=43,
        head_sha="head-a",
        reviewer_agent_id="review-invocation",
        verdict=Verdict.GO,
        submitted_at="2026-08-27T00:00:00Z",
        validated_at=validated_at,
    )
    return replace(evidence, **changes)


def _observation(**changes: object) -> ReviewValidationObservation:
    values: dict[str, object] = {
        "review_id": 17,
        "pr_number": 43,
        "head_sha": "head-a",
        "reviewer_agent_id": "review-invocation",
        "verdict": Verdict.GO,
        "observed_at": "2026-08-29T00:00:00Z",
    }
    values.update(changes)
    return ReviewValidationObservation(**values)  # type: ignore[arg-type]


def test_same_identity_expired_validation_is_refreshed() -> None:
    assert (
        review_validation_disposition(
            _evidence(),
            _observation(),
            now=NOW,
            freshness_window_secs=60,
        )
        is ReviewValidationDisposition.REFRESHED
    )


def test_same_identity_fresh_validation_is_a_noop() -> None:
    assert (
        review_validation_disposition(
            _evidence(validated_at="2026-08-28T23:59:30Z"),
            _observation(),
            now=NOW,
            freshness_window_secs=60,
        )
        is ReviewValidationDisposition.ALREADY_FRESH
    )


def test_newer_same_head_review_is_superseding() -> None:
    assert (
        review_validation_disposition(
            _evidence(),
            _observation(review_id=18, verdict=Verdict.NO_GO),
        )
        is ReviewValidationDisposition.SUPERSEDED
    )


def test_older_review_is_explicitly_out_of_order() -> None:
    assert (
        review_validation_disposition(_evidence(), _observation(review_id=16))
        is ReviewValidationDisposition.OUT_OF_ORDER
    )


def test_dismissed_or_unauthenticated_observation_is_rejected() -> None:
    assert (
        review_validation_disposition(_evidence(), _observation(dismissed=True))
        is ReviewValidationDisposition.INVALIDATED
    )
    assert (
        review_validation_disposition(
            _evidence(), _observation(reviewer_account_authenticated=False)
        )
        is ReviewValidationDisposition.UNAUTHORIZED
    )


def test_missing_validation_timestamp_is_not_fresh() -> None:
    assert (
        review_validation_is_fresh(
            _evidence(validated_at=None),
            now=NOW,
            freshness_window_secs=60,
        )
        is False
    )


def test_freshness_is_false_for_invalid_or_mismatched_evidence() -> None:
    for changes in (
        {"reviewer_account_authenticated": False},
        {"dismissed": True},
        {"forgejo_stale": True},
        {"reviewer_identity_unresolved": True},
    ):
        evidence = _evidence(**changes)
        assert (
            review_validation_is_fresh(
                evidence,
                now=NOW,
                freshness_window_secs=60,
                expected_pr_number=43,
                expected_head_sha="head-a",
                expected_verdict=Verdict.GO,
                expected_reviewer_agent_id="review-invocation",
            )
            is False
        )
    assert (
        review_validation_is_fresh(
            _evidence(),
            now=NOW,
            freshness_window_secs=60,
            expected_pr_number=44,
            expected_head_sha="head-a",
            expected_verdict=Verdict.GO,
            expected_reviewer_agent_id="review-invocation",
        )
        is False
    )
