"""Pure TL projection of review-stall classifications from watcher evidence."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum


class CiFailureAttribution(str, Enum):
    """Attribution bound to immutable base/head check snapshots."""

    HEAD_INTRODUCED = "head_introduced"
    BASE_PRE_EXISTING = "base_pre_existing"
    INDETERMINATE = "indeterminate"


class ReviewStallClassification(str, Enum):
    """Closed classifications derived from raw review and CI observations."""

    DEV_NOT_PUSHING = "dev_not_pushing"
    REVIEWER_NOT_RESPONDING = "reviewer_not_responding"
    REVIEWER_NEVER_STARTED = "reviewer_never_started"
    CI_FAILED = "ci_failed"
    CI_BASE_UNSTABLE = "ci_base_unstable"
    CI_INDETERMINATE = "ci_indeterminate"


def classify_review_stall(payload: Mapping[str, object]) -> ReviewStallClassification | None:
    """Classify a timeout/stuck observation without trusting producer labels."""
    kind = payload.get("kind")
    ci_status = payload.get("ci_status")
    if kind == "ci_blocked" and ci_status == "failure":
        attribution = payload.get("attribution", payload.get("ci_attribution"))
        if attribution is None:
            # Legacy watcher payloads predate base/head attribution. Preserve their
            # established CI-failed projection until a typed attribution arrives.
            return ReviewStallClassification.CI_FAILED
        try:
            classified = CiFailureAttribution(attribution)
        except (TypeError, ValueError):
            classified = CiFailureAttribution.INDETERMINATE
        if not isinstance(payload.get("base_sha"), str) or not payload.get("base_sha"):
            classified = CiFailureAttribution.INDETERMINATE
        if not isinstance(payload.get("head_sha"), str) or not payload.get("head_sha"):
            classified = CiFailureAttribution.INDETERMINATE
        return {
            CiFailureAttribution.BASE_PRE_EXISTING: ReviewStallClassification.CI_BASE_UNSTABLE,
            CiFailureAttribution.INDETERMINATE: ReviewStallClassification.CI_INDETERMINATE,
            CiFailureAttribution.HEAD_INTRODUCED: ReviewStallClassification.CI_FAILED,
        }[classified]
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


__all__ = ["CiFailureAttribution", "ReviewStallClassification", "classify_review_stall"]
