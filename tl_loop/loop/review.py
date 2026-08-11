"""Review verdict freshness and head-SHA merge gates."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from tl_loop.client.effects import ToolResult
from tl_loop.state.schema import SliceState, Verdict

DEFAULT_REVIEW_POLICY = Path(".exo/review-policy.toml")


class ReviewGateError(ValueError):
    """A review verdict cannot safely authorize a merge."""


class MissingVerdict(ReviewGateError):
    """No approved verdict is available for the slice."""


class MissingReviewedHead(ReviewGateError):
    """The verdict did not name the head it judged."""


class ReviewHeadMismatch(ReviewGateError):
    """The PR head changed after the verdict was recorded."""


class StaleVerdict(ReviewGateError):
    """The verdict is older than the configured freshness window."""


class VerdictNotApproved(ReviewGateError):
    """The recorded verdict is not an approval."""


@dataclass(frozen=True)
class ReviewEvidence:
    """The verified current head and verdict age used for one merge."""

    reviewed_head: str
    age_seconds: float


def verify_review(
    slice: SliceState,
    current_head: str,
    *,
    now: datetime | None = None,
    freshness_window_secs: int | None = None,
    policy_path: str | Path = DEFAULT_REVIEW_POLICY,
) -> ReviewEvidence:
    """Require an approved, SHA-matching, fresh verdict."""
    if slice.verdict is None:
        raise MissingVerdict(f"slice {slice.id!r} has no review verdict")
    if slice.reviewed_head is None:
        raise MissingReviewedHead(
            f"slice {slice.id!r} verdict has no reviewed_head"
        )
    if slice.verdict is Verdict.NO_GO:
        raise VerdictNotApproved(f"slice {slice.id!r} verdict is {slice.verdict.value}")
    if not current_head:
        raise ReviewHeadMismatch("watcher_pr_state returned an empty head_sha")
    if current_head != slice.reviewed_head:
        raise ReviewHeadMismatch(
            f"slice {slice.id!r} reviewed {slice.reviewed_head}, current head is {current_head}"
        )
    if slice.verdict_at is None:
        raise StaleVerdict(f"slice {slice.id!r} verdict has no observed timestamp")
    observed = _parse_timestamp(slice.verdict_at)
    current = now or datetime.now(timezone.utc)
    age_seconds = max(0.0, (current - observed).total_seconds())
    window = (
        freshness_window_secs
        if freshness_window_secs is not None
        else load_freshness_window(policy_path)
    )
    if age_seconds > window:
        raise StaleVerdict(
            f"slice {slice.id!r} verdict age {age_seconds:g}s exceeds {window}s"
        )
    return ReviewEvidence(slice.reviewed_head, age_seconds)


def watcher_head(result: ToolResult) -> str:
    """Extract the live PR head from the typed watcher effect result."""
    if result.success is not True:
        raise ReviewGateError(result.error or "watcher_pr_state failed")
    payload = result.result
    if not isinstance(payload, Mapping):
        raise ReviewGateError("watcher_pr_state returned no response object")
    head = payload.get("head_sha")
    if not isinstance(head, str):
        raise ReviewGateError("watcher_pr_state response has no head_sha")
    return head


def load_freshness_window(path: str | Path = DEFAULT_REVIEW_POLICY) -> int:
    """Load the review freshness window from the canonical TOML policy."""
    try:
        with Path(path).open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReviewGateError(f"could not load review policy {path}: {error}") from error
    value = document.get("review_freshness_window_secs")
    if type(value) is not int or value < 0:
        raise ReviewGateError(
            "review_freshness_window_secs must be a non-negative integer"
        )
    return value


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StaleVerdict(f"invalid verdict timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise StaleVerdict("verdict timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "DEFAULT_REVIEW_POLICY",
    "MissingReviewedHead",
    "MissingVerdict",
    "ReviewEvidence",
    "ReviewGateError",
    "ReviewHeadMismatch",
    "StaleVerdict",
    "VerdictNotApproved",
    "load_freshness_window",
    "verify_review",
    "watcher_head",
]
