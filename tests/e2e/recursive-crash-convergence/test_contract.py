"""Unit checks for the #1057 acceptance contract, without starting infrastructure."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import beast
from boundaries import (
    CRASH_BOUNDARIES,
    LOGICAL_BOUNDARY_NAMES,
    boundary_for,
    effect_identity,
    redacted_arguments,
    validate_matrix,
)
from crash_transport import CrashBoundaryTransport
from evidence import (
    AcceptanceError,
    assert_checkpoint_progression,
    assert_crash_record,
    assert_effect_cardinality,
    assert_effect_events,
    assert_journal_terminal,
    assert_recursive_effect_cardinality,
    assert_remote_ancestry,
    assert_required_effects,
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


def test_spawn_boundary_matches_real_child_process_effects_only(tmp_path: Path) -> None:
    transport = CrashBoundaryTransport(
        tmp_path,
        tmp_path / "crash.jsonl",
        boundary_for("spawn", "before"),
    )
    assert transport._matches("spawn_leaf", {})
    assert transport._matches("spawn_worker", {})
    assert not transport._matches(
        "emit_controller_event", {"event_type": "tl.dispatch_confirmed"}
    )


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


def test_effect_cardinality_rejects_a_second_attempt_in_one_generation(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "action-journal.json"
    journal.write_text(
        json.dumps(
            [
                {
                    "key": "first",
                    "operation": "post_merge_push",
                    "target": "child-a",
                    "arguments": {"child_id": "child-a", "generation": 2},
                    "status": "confirmed",
                },
                {
                    "key": "second",
                    "operation": "post_merge_push",
                    "target": "child-a",
                    "arguments": {"child_id": "child-a", "generation": 2},
                    "status": "confirmed",
                },
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(AcceptanceError, match="more than once"):
        assert_effect_cardinality(journal)


def test_recursive_effect_cardinality_covers_nested_scope_journals(
    tmp_path: Path,
) -> None:
    root_journal = tmp_path / "root" / "action-journal.json"
    child_journal = tmp_path / "root" / "child" / "action-journal.json"
    for path, target in ((root_journal, "root"), (child_journal, "child")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {
                        "key": target,
                        "operation": "root_branch_finalize",
                        "target": target,
                        "arguments": {"generation": 1},
                        "status": "confirmed",
                    }
                ]
            ),
            encoding="utf-8",
        )
    assert assert_recursive_effect_cardinality(tmp_path / "root") == {
        "root_branch_finalize": 2
    }


def test_required_effects_accept_any_real_spawn_tool_and_reject_missing_families() -> (
    None
):
    counts = {
        "spawn_leaf": 1,
        "file_pr": 1,
        "resume_pr": 1,
        "merge_pr": 1,
        "chainlink_issue_close": 1,
        "post_merge_parent_sync": 1,
        "post_merge_remote_reconcile": 1,
        "post_merge_changelog": 1,
        "post_merge_push": 1,
        "root_branch_finalize": 1,
    }
    assert_required_effects(counts)
    del counts["spawn_leaf"]
    with pytest.raises(AcceptanceError, match="effect groups"):
        assert_required_effects(counts)


def test_beast_rejects_a_pre_reconciled_checkpoint_as_unproven_convergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "beast"
    checkpoint = workspace / ".exo" / "tl-loop" / "root" / "run.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            {
                "state_version": 1,
                "events": {"last_consumed_offset": 3},
                "fsm": {"phase": "tl_running"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EXOMONAD_BEAST_WORKSPACE", str(workspace))
    monkeypatch.setenv(
        "EXOMONAD_BEAST_CONTINUE_COMMAND",
        "true --workspace {workspace}",
    )
    monkeypatch.setattr(beast, "_ledger_merge_count", lambda _: 1)
    with pytest.raises(AcceptanceError, match="already contains a merge"):
        beast.run_three_continuations()


def test_remote_ancestry_requires_durable_head_and_proof() -> None:
    with pytest.raises(AcceptanceError, match="ancestry evidence"):
        assert_remote_ancestry({})
    assert_remote_ancestry(
        {
            "post_merge": {
                "evidence": {
                    "remote_head_sha": "abcdef1",
                    "ancestry_proof": "ancestor:abcdef1->abcdef2",
                }
            }
        }
    )


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
