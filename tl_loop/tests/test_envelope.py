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
    EventEnvelope,
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
    assert all(projected[event_type].head_sha is None for event_type in SERVER_EMIT_HEAD_SHA_GAPS)


def test_unmapped_allowlisted_event_type_is_rejected_at_read_time() -> None:
    events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    unmapped = dict(events[0])
    unmapped["type"] = "agent.guidance.delivery"

    with pytest.raises(UnmappedEventType, match="agent.guidance.delivery") as error:
        project(unmapped)

    assert error.value.event_type == "agent.guidance.delivery"
