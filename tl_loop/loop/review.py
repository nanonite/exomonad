"""Review verdict freshness and head-SHA merge gates."""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tl_loop.client.effects import ToolResult
from tl_loop.state.schema import SliceState, Verdict

DEFAULT_REVIEW_POLICY = Path(".exo/review-policy.toml")


class AcceptanceCriteriaError(ValueError):
    """The TL cannot compose a complete reviewer acceptance contract."""


def compose_acceptance_criteria(
    slice_state: SliceState,
    plan: Mapping[str, object] | object,
) -> tuple[str, ...]:
    """Compose reviewer bullets from TL-owned state and the structured plan.

    The PR body is deliberately not an input. Run-state verification remains
    authoritative, while the plan supplies its additional verification,
    boundary, and Definition-of-Done statements.
    """
    sources = (
        ("Run-state test plan", slice_state.test_plan),
        ("Plan verification", _plan_values(plan, "verify")),
        ("Owned paths", slice_state.paths),
        ("Plan boundary", _plan_values(plan, "boundary")),
        ("DONE CRITERIA", _plan_values(plan, "done_criteria")),
    )
    criteria: list[str] = []
    seen: set[str] = set()
    for label, values in sources:
        for value in values:
            entry = f"{label}: {value}"
            if entry not in seen:
                criteria.append(entry)
                seen.add(entry)
    if not criteria:
        raise AcceptanceCriteriaError(
            f"slice {slice_state.id!r} has no reviewer acceptance criteria"
        )
    return tuple(criteria)


def _plan_values(
    plan: Mapping[str, object] | object,
    field_name: str,
) -> tuple[str, ...]:
    value = (
        plan.get(field_name, ())
        if isinstance(plan, Mapping)
        else getattr(plan, field_name, ())
    )
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AcceptanceCriteriaError(f"plan {field_name} must be an array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise AcceptanceCriteriaError(
            f"plan {field_name} must contain non-empty strings"
        )
    return tuple(item.strip() for item in value)


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


class MissingCIStatus(ReviewGateError):
    """The reviewed head has no recorded CI result."""


class CIStatusNotApproved(ReviewGateError):
    """The reviewed head does not have a successful or neutral CI result."""


class OptionalPolicyRejected(ReviewGateError):
    """An explicitly supplied project-specific merge predicate rejected."""


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
    policy_predicate: Callable[[SliceState], bool] | None = None,
) -> ReviewEvidence:
    """Require TL approval, CI success, and exact live-head binding.

    Freshness and project-specific second-review rules are optional predicates;
    the canonical merge rule does not load or apply them implicitly.
    """
    if slice.verdict is None:
        raise MissingVerdict(f"slice {slice.id!r} has no review verdict")
    if slice.reviewed_head is None:
        raise MissingReviewedHead(
            f"slice {slice.id!r} verdict has no reviewed_head"
        )
    if slice.verdict not in {Verdict.GO, Verdict.GO_WITH_NITS}:
        raise VerdictNotApproved(f"slice {slice.id!r} verdict is {slice.verdict.value}")
    if not current_head:
        raise ReviewHeadMismatch("watcher_pr_state returned an empty head_sha")
    if current_head != slice.reviewed_head:
        raise ReviewHeadMismatch(
            f"slice {slice.id!r} reviewed {slice.reviewed_head}, current head is {current_head}"
        )
    ci_status = slice.ci_state.get(slice.reviewed_head)
    if ci_status is None:
        raise MissingCIStatus(
            f"slice {slice.id!r} has no CI status for {slice.reviewed_head}"
        )
    if ci_status not in {"success", "neutral"}:
        raise CIStatusNotApproved(
            f"slice {slice.id!r} CI status for {slice.reviewed_head} is {ci_status}"
        )
    if policy_predicate is not None and policy_predicate(slice) is not True:
        raise OptionalPolicyRejected(
            f"optional review policy rejected slice {slice.id!r}"
        )
    age_seconds = _freshness_age(
        slice,
        now=now,
        freshness_window_secs=freshness_window_secs,
    )
    return ReviewEvidence(slice.reviewed_head, age_seconds)


def _freshness_age(
    slice: SliceState,
    *,
    now: datetime | None,
    freshness_window_secs: int | None,
) -> float:
    if freshness_window_secs is None:
        return 0.0
    if slice.verdict_at is None:
        raise StaleVerdict(f"slice {slice.id!r} verdict has no observed timestamp")
    observed = _parse_timestamp(slice.verdict_at)
    current = now or datetime.now(UTC)
    age_seconds = max(0.0, (current - observed).total_seconds())
    if age_seconds > freshness_window_secs:
        raise StaleVerdict(
            f"slice {slice.id!r} verdict age {age_seconds:g}s exceeds "
            f"{freshness_window_secs}s"
        )
    return age_seconds


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
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise StaleVerdict(f"invalid verdict timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise StaleVerdict("verdict timestamp must include a timezone")
    return parsed.astimezone(UTC)


__all__ = [
    "DEFAULT_REVIEW_POLICY",
    "AcceptanceCriteriaError",
    "CIStatusNotApproved",
    "MissingCIStatus",
    "MissingReviewedHead",
    "MissingVerdict",
    "OptionalPolicyRejected",
    "ReviewEvidence",
    "ReviewGateError",
    "ReviewHeadMismatch",
    "StaleVerdict",
    "VerdictNotApproved",
    "compose_acceptance_criteria",
    "load_freshness_window",
    "verify_review",
    "watcher_head",
]
