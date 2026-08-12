"""Pure TL projection of review-stall classifications from watcher evidence."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum


class ReviewStallClassification(str, Enum):
    """Closed classifications derived from raw review and CI observations."""

    DEV_NOT_PUSHING = "dev_not_pushing"
    REVIEWER_NOT_RESPONDING = "reviewer_not_responding"
    REVIEWER_NEVER_STARTED = "reviewer_never_started"
    CI_FAILED = "ci_failed"


def classify_review_stall(payload: Mapping[str, object]) -> ReviewStallClassification | None:
    """Classify a timeout/stuck observation without trusting producer labels."""
    kind = payload.get("kind")
    ci_status = payload.get("ci_status")
    if kind == "ci_blocked" and ci_status == "failure":
        return ReviewStallClassification.CI_FAILED
    if kind not in {"timeout", "stuck"}:
        return None

    if payload.get("last_review_state") == "changes_requested":
        return ReviewStallClassification.DEV_NOT_PUSHING
    if (
        payload.get("addressed_changes") is True
        and payload.get("forgejo_review_present") is True
    ):
        return ReviewStallClassification.REVIEWER_NOT_RESPONDING
    if (
        payload.get("reviewer_registered") is True
        and payload.get("forgejo_review_present") is False
    ):
        return ReviewStallClassification.REVIEWER_NEVER_STARTED
    return ReviewStallClassification.REVIEWER_NOT_RESPONDING


__all__ = ["ReviewStallClassification", "classify_review_stall"]
