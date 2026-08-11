"""Hermetic coverage for policy-gated review adjudication."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import pytest

from tl_loop.rlm.adjudicate import (
    AdjudicationResult,
    NitRecordingError,
    ReviewedHeadMismatch,
    ReviewPolicy,
    adjudicate_review,
)
from tl_loop.rlm.budget import ContextOverflow
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest, RlmResponse
from tl_loop.state.schema import Verdict


@dataclass
class FakeBackend:
    responses: list[object]
    requests: list[RlmRequest] = field(default_factory=list)

    def complete(self, request: RlmRequest) -> object:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("backend was called more times than expected")
        return self.responses.pop(0)


@dataclass
class FakeCommenter:
    calls: list[tuple[int, str]] = field(default_factory=list)

    def chainlink_issue_comment(self, *, issue_id: int, message: str) -> None:
        self.calls.append((issue_id, message))


def _choice(backend: FakeBackend, *, context_length: int = 10_000) -> RlmModelChoice:
    return RlmModelChoice(
        model_id="test-model",
        backend=backend,
        store=RlmCallStore(),
        context_length=context_length,
    )


def _policy(
    *,
    min_review_rounds: int = 0,
    external_review_threshold: int = 10_000,
    external_review_paths: tuple[str, ...] = (),
    require_second_reviewer_complexity: bool = False,
    complexity_line_threshold: int = 10_000,
) -> ReviewPolicy:
    return ReviewPolicy(
        min_review_rounds=min_review_rounds,
        external_review_threshold=external_review_threshold,
        external_review_paths=external_review_paths,
        require_second_reviewer_complexity=require_second_reviewer_complexity,
        complexity_line_threshold=complexity_line_threshold,
    )


def _diff(
    *,
    lines_changed: int = 1,
    paths: list[str] | None = None,
    review_rounds: int = 1,
) -> dict[str, object]:
    return {
        "diff": "@@ -1 +1 @@" + chr(92) + "n-old" + chr(92) + "n+new" + chr(92) + "n",
        "lines_changed": lines_changed,
        "paths": paths or ["src/app.py"],
        "review_rounds": review_rounds,
    }


def _output(
    verdict: str,
    *,
    head: str = "head-a",
    reasons: list[dict[str, object]] | None = None,
) -> RlmResponse:
    values = reasons or []
    return RlmResponse(
        {
            "verdict": verdict,
            "reviewed_head": head,
            "reasons": values,
            "blocking_count": sum(
                reason["severity"] == "blocking" for reason in values
            ),
        }
    )


def _adjudicate(
    backend: FakeBackend,
    *,
    diff: dict[str, object] | None = None,
    policy: ReviewPolicy | None = None,
    commenter: object | None = None,
    issue_id: int | None = None,
    context_length: int = 10_000,
) -> AdjudicationResult:
    return adjudicate_review(
        diff or _diff(),
        [],
        ["all changed behavior is covered"],
        "head-a",
        model_choice=_choice(backend, context_length=context_length),
        policy=policy or _policy(),
        chainlink_issue_id=issue_id,
        issue_commenter=commenter,
    )


def test_go_is_mergeable_when_policy_gates_pass() -> None:
    backend = FakeBackend([_output(Verdict.GO.value)])

    result = _adjudicate(backend)

    assert result.verdict is Verdict.GO
    assert result.reviewed_head == "head-a"
    assert result.blocking_count == 0
    assert result.mergeable is True
    assert result.second_review_required is False


def test_go_with_nits_is_mergeable_and_records_follow_up() -> None:
    commenter = FakeCommenter()
    backend = FakeBackend(
        [
            _output(
                Verdict.GO_WITH_NITS.value,
                reasons=[
                    {
                        "severity": "nit",
                        "file": "src/app.py",
                        "line": 7,
                        "claim": "Clarify this name",
                    }
                ],
            )
        ]
    )

    result = _adjudicate(
        backend,
        commenter=commenter,
        issue_id=42,
    )

    assert result.verdict is Verdict.GO_WITH_NITS
    assert result.mergeable is True
    assert commenter.calls[0][0] == 42
    assert "src/app.py:7: Clarify this name" in commenter.calls[0][1]


def test_no_go_is_not_mergeable_and_preserves_blocking_reason() -> None:
    backend = FakeBackend(
        [
            _output(
                Verdict.NO_GO.value,
                reasons=[
                    {
                        "severity": "blocking",
                        "file": "src/app.py",
                        "line": 12,
                        "claim": "The error path is unhandled",
                    }
                ],
            )
        ]
    )

    result = _adjudicate(backend)

    assert result.verdict is Verdict.NO_GO
    assert result.mergeable is False
    assert result.blocking_count == 1


def test_mismatched_echoed_head_is_rejected() -> None:
    backend = FakeBackend([_output(Verdict.GO.value, head="head-b")])

    with pytest.raises(ReviewedHeadMismatch):
        _adjudicate(backend)


@pytest.mark.parametrize(
    ("diff", "policy"),
    [
        (_diff(review_rounds=0), _policy(min_review_rounds=1)),
        (_diff(lines_changed=301), _policy(external_review_threshold=300)),
        (
            _diff(paths=["proto/schema.proto"]),
            _policy(external_review_paths=("proto/**",)),
        ),
        (
            _diff(lines_changed=501),
            _policy(
                require_second_reviewer_complexity=True,
                complexity_line_threshold=500,
            ),
        ),
    ],
)
def test_policy_gates_require_a_second_review(
    diff: dict[str, object],
    policy: ReviewPolicy,
) -> None:
    backend = FakeBackend([_output(Verdict.GO.value)])

    result = _adjudicate(backend, diff=diff, policy=policy)

    assert result.verdict is Verdict.GO
    assert result.second_review_required is True
    assert result.mergeable is False
    assert any(
        "Second review required" in cast(str, reason["claim"])
        for reason in result.reasons
    )


def test_go_with_nits_without_issue_writer_is_rejected() -> None:
    backend = FakeBackend(
        [
            _output(
                Verdict.GO_WITH_NITS.value,
                reasons=[
                    {
                        "severity": "nit",
                        "file": "src/app.py",
                        "line": 1,
                        "claim": "Use a clearer name",
                    }
                ],
            )
        ]
    )

    with pytest.raises(NitRecordingError):
        _adjudicate(backend)


def test_required_diff_overflow_is_raised_before_adjudication() -> None:
    backend = FakeBackend([_output(Verdict.GO.value)])

    with pytest.raises(ContextOverflow):
        _adjudicate(backend, context_length=10)

    assert backend.requests == []
