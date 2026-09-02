"""Unit checks for the #1057 acceptance contract, without starting infrastructure."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from boundaries import (
    CRASH_BOUNDARIES,
    LOGICAL_BOUNDARY_NAMES,
    boundary_for,
    effect_identity,
    redacted_arguments,
    validate_matrix,
)
from evidence import (
    AcceptanceError,
    assert_checkpoint_progression,
    assert_crash_record,
    assert_effect_events,
    assert_journal_terminal,
    assert_resume_not_redispatched,
)


def test_every_logical_boundary_has_before_and_after_process_death() -> None:
    validate_matrix()
    assert len(CRASH_BOUNDARIES) == 2 * len(LOGICAL_BOUNDARY_NAMES)
    for name in LOGICAL_BOUNDARY_NAMES:
        assert {boundary_for(name, point).point for point in ("before", "after")} == {
            "before",
            "after",
        }


def test_boundary_lookup_and_effect_identity_are_canonical() -> None:
    with pytest.raises(KeyError):
        boundary_for("not-a-real-effect", "before")
    first = effect_identity({"intent_id": "x", "body": "secret", "child_id": "a"})
    second = effect_identity({"child_id": "a", "intent_id": "x"})
    assert first == second
    assert "body" not in redacted_arguments({"body": "secret", "child_id": "a"})
    nested = redacted_arguments(
        {
            "event_type": "pr.review",
            "payload": {"review_id": 7, "body": "secret", "findings": ["secret"]},
        }
    )
    assert nested["payload"]["review_id"] == 7
    assert nested["payload"]["body"] == "<redacted>"
    assert nested["payload"]["findings"] == "<redacted>"


def test_crash_record_requires_one_identity(tmp_path: Path) -> None:
    marker = tmp_path / "crash.jsonl"
    marker.write_text(
        json.dumps({"boundary": "push", "point": "after", "identity": "sha"}) + "\n",
        encoding="utf-8",
    )
    assert assert_crash_record(marker, "push", "after") == "sha"
    with pytest.raises(AcceptanceError, match="expected one"):
        assert_crash_record(marker, "push", "before")


def test_journal_and_checkpoint_assertions_reject_duplicate_or_regressed_state(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "action-journal.json"
    journal.write_text(
        json.dumps([{"key": "a", "operation": "merge_pr", "status": "confirmed"}]),
        encoding="utf-8",
    )
    assert assert_journal_terminal(journal)["merge_pr"] == 1
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(
        json.dumps({"state_version": 1, "events": {"last_consumed_offset": 4}}),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({"state_version": 2, "events": {"last_consumed_offset": 5}}),
        encoding="utf-8",
    )
    assert_checkpoint_progression([first, second])
    second.write_text(
        json.dumps({"state_version": 0, "events": {"last_consumed_offset": 5}}),
        encoding="utf-8",
    )
    with pytest.raises(AcceptanceError, match="regressed"):
        assert_checkpoint_progression([first, second])


def test_effect_event_assertion_requires_one_merge_lifecycle(tmp_path: Path) -> None:
    segments = tmp_path / ".exo" / "ledger" / "segments"
    segments.mkdir(parents=True)
    events = [
        {
            "run_id": "swarm",
            "type": "tl.action_queued",
            "data": {"action": "merge", "action_key": "m"},
        },
        {"run_id": "swarm", "type": "tl.merge_decided", "data": {"decision": "merge"}},
        {"run_id": "swarm", "type": "tl.merge_reconciled", "data": {}},
    ]
    (segments / "segment.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    assert assert_effect_events(tmp_path, "swarm") == {
        "merge_intents": 1,
        "merge_decisions": 1,
        "merge_reconciliations": 1,
    }


def test_resume_trace_enforces_before_and_after_effect_cardinality(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "resume.jsonl"
    trace.write_text(json.dumps({"identity": "same"}) + "\n", encoding="utf-8")
    assert (
        assert_resume_not_redispatched(
            trace, "same", boundary="remote_merge", point="before"
        )
        == 1
    )
    with pytest.raises(AcceptanceError, match="cardinality"):
        assert_resume_not_redispatched(
            trace, "same", boundary="remote_merge", point="after"
        )
