"""Ledger-segment projection coverage for the TL event envelope."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from tl_loop.events.envelope import (
    EVENT_TYPE_BY_KIND,
    MAPPED_EVENT_TYPES,
    SERVER_EMIT_HEAD_SHA_GAPS,
    BlockCause,
    EventEnvelope,
    InvalidLedgerEvent,
    ReviewStallClassification,
    UnmappedEventType,
    project,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ledger_projection_events.json"


def test_every_closed_event_type_projects_without_projection_field_loss() -> None:
    events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))

    projected = [project(event) for event in events]
    assert {event.event_type for event in projected} == MAPPED_EVENT_TYPES
    assert {event.kind: event.event_type for event in projected} == dict(EVENT_TYPE_BY_KIND)

    for raw, envelope in zip(events, projected, strict=True):
        assert isinstance(envelope, EventEnvelope)
        assert envelope.run_seq == raw["run_seq"]
        assert envelope.run_id == raw["run_id"]
        assert envelope.agent_id == raw["agent_id"]
        assert envelope.session_id == raw["session_id"]
        assert envelope.invocation_id == raw["invocation_id"]
        assert envelope.generation == raw["generation"]
        assert envelope.harness == raw["harness"]
        assert envelope.role == raw["role"]
        assert envelope.lifecycle_state == raw["lifecycle_state"]
        assert envelope.observed_at == raw["observed_at"]
        assert dict(envelope.data) == raw["data"]


def test_review_and_ci_fields_are_projected_from_data_without_synthesis() -> None:
    events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    projected = {envelope.event_type: envelope for envelope in (project(event) for event in events)}

    assert projected["pr.filed"].head_sha == "aaa111"
    assert projected["pr.updated"].head_sha == "bbb222"
    assert projected["pr.published"].head_sha == "bbb222"
    assert projected["pr.review"].review_kind == "merge_ready"
    assert projected["pr.review"].notification == (
        "[MERGE READY] PR #101 on branch task-a has CI status success and reviewer approval. "
        "Merge with `merge_pr` tool."
    )
    assert projected["copilot.review"].head_sha == "bbb222"
    assert projected["copilot.review"].review_state == "changes_requested"
    assert projected["ci.status_changed"].head_sha == "bbb222"
    assert projected["ci.status_changed"].ci_status == "failure"
    assert projected["pr.merged"].head_sha == "bbb222"
    assert projected["pr.merge_failed"].head_sha == "ccc333"
    assert projected["agent.sibling_merged"].head_sha == "bbb222"
    assert projected["agent.sibling_merged"].data["recipient"] == "task-b"
    assert projected["agent.sibling_merged"].data["recipient_pr_number"] == 102
    assert projected["agent.sibling_merged"].data["payload"] == {
        "merged_branch": "task-a",
        "parent_branch": "root",
        "sibling_pr_number": 102,
    }
    assert projected["issue.closed"].data["issue_id"] == 313
    assert projected["issue.closed"].data["closed_by"] == "orphan_reconciler"
    assert projected["inbox.message"].data["text"] == "Continue the assigned task."
    assert projected["inbox.poke"].data["unread_count"] == 2
    assert all(projected[event_type].head_sha is None for event_type in SERVER_EMIT_HEAD_SHA_GAPS)
    assert all(
        projected[event_type].data["head_sha_finding"]
        == "not_available_without_verified_pr_context"
        for event_type in SERVER_EMIT_HEAD_SHA_GAPS
    )


def test_task_blocked_projects_normalized_outcome_without_raw_evidence() -> None:
    events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    event = next(project(raw) for raw in events if raw["type"] == "agent.task_blocked")

    assert event.task_blocked is not None
    assert event.task_blocked.cause is BlockCause.BASE_CI_UNSTABLE
    assert event.task_blocked.slice_id == "slice-a"
    assert event.task_blocked.declared_difficulty.value == "standard"
    assert event.task_blocked.matched_difficulty_rule == "standard_slice"
    assert event.task_blocked.attempt == 2
    assert event.task_blocked.attempt_bucket == "2"
    assert event.task_blocked.harness == "codex"
    assert event.task_blocked.role == "worker"
    assert not hasattr(event.task_blocked, "evidence")
    dimensions = event.task_blocked.aggregate_dimensions()
    assert dimensions["attempt_bucket"] == "2"
    assert dimensions["outcome"] == "blocked"
    assert "message" not in dimensions
    assert "evidence" not in dimensions


def test_task_blocked_rejects_contradictory_outcome_and_unknown_cause() -> None:
    events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    raw = next(item for item in events if item["type"] == "agent.task_blocked")
    with pytest.raises(InvalidLedgerEvent, match="outcome"):
        project({**raw, "data": {**cast(dict[str, object], raw["data"]), "outcome": "completed"}})
    with pytest.raises(InvalidLedgerEvent, match="closed vocabulary"):
        project(
            {**raw, "data": {**cast(dict[str, object], raw["data"]), "cause": "harness_failed"}}
        )


def test_unmapped_allowlisted_event_type_is_rejected_at_read_time() -> None:
    events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    unmapped = dict(events[0])
    unmapped["type"] = "agent.guidance.delivery"

    with pytest.raises(UnmappedEventType, match="agent.guidance.delivery") as error:
        project(unmapped)

    assert error.value.event_type == "agent.guidance.delivery"


def test_stall_classification_is_derived_from_raw_review_evidence() -> None:
    base = {
        "type": "pr.review",
        "run_seq": 900,
        "run_id": "run-a",
        "agent_id": "slice-a",
        "lifecycle_state": "observed",
        "observed_at": "2026-08-12T15:00:00Z",
    }
    cases = (
        (
            {
                "kind": "stuck",
                "head_sha": "head-a",
                "last_review_state": "changes_requested",
                "reviewer_registered": True,
                "forgejo_review_present": True,
                "addressed_changes": False,
            },
            ReviewStallClassification.DEV_NOT_PUSHING,
        ),
        (
            {
                "kind": "timeout",
                "head_sha": "head-a",
                "last_review_state": "none",
                "reviewer_registered": True,
                "forgejo_review_present": True,
                "addressed_changes": True,
            },
            ReviewStallClassification.REVIEWER_NOT_RESPONDING,
        ),
        (
            {
                "kind": "timeout",
                "head_sha": "head-a",
                "last_review_state": "none",
                "reviewer_registered": True,
                "forgejo_review_present": False,
                "addressed_changes": False,
            },
            ReviewStallClassification.REVIEWER_NEVER_STARTED,
        ),
        (
            {
                "kind": "ci_blocked",
                "head_sha": "head-a",
                "ci_status": "failure",
            },
            ReviewStallClassification.CI_FAILED,
        ),
    )
    for evidence, expected in cases:
        event = project({**base, "data": {"slice_id": "slice-a", **evidence}})
        assert event.stall_classification is expected

    mislabeled = project(
        {
            **base,
            "data": {
                "slice_id": "slice-a",
                "kind": "stuck",
                "head_sha": "head-a",
                "last_review_state": "changes_requested",
                "stall_classification": "reviewer_never_started",
            },
        }
    )
    assert mislabeled.stall_classification is ReviewStallClassification.DEV_NOT_PUSHING
