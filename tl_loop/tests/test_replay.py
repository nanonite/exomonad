"""Golden replay tests for complete active-loop trajectories."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tl_loop.client.effects import ToolResult
from tl_loop.events.envelope import project
from tl_loop.events.replay import ReplayEventSource, ReplayTruncated
from tl_loop.loop.driver import EffectIntent
from tl_loop.loop.journal import MUTATING_OPERATIONS
from tl_loop.loop.journal import EffectJournal
from tl_loop.tests.replay import (
    FIXTURE_ROOT,
    expected_actions,
    expected_state,
    replay_fixture,
)

REPLAYS = (
    "clean-two-slice.json",
    "no-go-repair.json",
    "retry-exhausted.json",
)


@pytest.mark.parametrize("fixture_name", REPLAYS)
def test_recorded_stream_replays_exact_actions_and_state(fixture_name: str, tmp_path: Path) -> None:
    first = replay_fixture(FIXTURE_ROOT / fixture_name, tmp_path / "first")
    second = replay_fixture(FIXTURE_ROOT / fixture_name, tmp_path / "second")

    assert first.actions == expected_actions(FIXTURE_ROOT / fixture_name)
    assert _canonical(first.state) == _canonical(expected_state(FIXTURE_ROOT / fixture_name))
    assert first.actions == second.actions
    assert _canonical(first.state) == _canonical(second.state)


def test_permuted_ledger_rows_replay_to_the_same_durable_position(tmp_path: Path) -> None:
    baseline = replay_fixture(FIXTURE_ROOT / "no-go-repair.json", tmp_path / "baseline")
    permuted = replay_fixture(
        FIXTURE_ROOT / "no-go-repair.json",
        tmp_path / "permuted",
        event_transform=lambda events: list(reversed(events)),
    )

    assert permuted.actions == baseline.actions
    assert permuted.durable_state == baseline.durable_state
    assert permuted.cursor == baseline.cursor
    assert permuted.reducer_version == baseline.reducer_version
    assert permuted.transitions == baseline.transitions


def test_replay_source_preserves_event_identity_and_resumes_after_cursor() -> None:
    raw = json.loads((FIXTURE_ROOT / "clean-two-slice.json").read_text(encoding="utf-8"))
    events = [project(value) for value in raw["events"]]
    source = ReplayEventSource([events[2], events[1], events[1]], start_cursor=1)

    first = source.get()
    assert first.run_seq == 2
    assert first.event_id == "replay-2"
    assert first.identity[2] == first.event_id
    source.acknowledge(first)
    assert source.cursor == 2
    assert source.get().run_seq == 2


def test_duplicate_ledger_rows_are_acknowledged_without_reducing_twice(
    tmp_path: Path,
) -> None:
    baseline = replay_fixture(FIXTURE_ROOT / "clean-two-slice.json", tmp_path / "baseline")
    duplicate = replay_fixture(
        FIXTURE_ROOT / "clean-two-slice.json",
        tmp_path / "duplicate",
        event_transform=lambda rows: [*rows, rows[2]],
    )

    assert duplicate.actions == baseline.actions
    assert duplicate.durable_state == baseline.durable_state
    assert duplicate.cursor == baseline.cursor
    assert duplicate.acknowledged.count(3) == 2
    assert duplicate.transitions == baseline.transitions


def test_truncated_ledger_prefix_fails_closed_with_cursor_context(tmp_path: Path) -> None:
    with pytest.raises(ReplayTruncated) as error:
        replay_fixture(
            FIXTURE_ROOT / "clean-two-slice.json",
            tmp_path / "truncated",
            event_transform=lambda rows: rows[:-2],
        )

    assert error.value.cursor == 3
    assert error.value.consumed == 3


def test_journal_probe_and_repeated_continuation_are_noops(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    first = replay_fixture(
        FIXTURE_ROOT / "clean-two-slice.json",
        root,
        journal=True,
        production_clock=True,
        session_mode="continue",
    )
    checkpoint = (root / "replay-clean.controller-epoch").parent / "replay-clean" / "run.json"
    before = checkpoint.read_bytes()
    (root / "replay-clean.controller-epoch").write_text("continued-controller-epoch\n")
    second = replay_fixture(
        FIXTURE_ROOT / "clean-two-slice.json",
        root,
        journal=True,
        production_clock=True,
        session_mode="continue",
    )

    assert first.journal_entries
    assert all(action["operation"] not in MUTATING_OPERATIONS for action in second.actions)
    assert second.cursor == first.cursor
    assert second.durable_state == first.durable_state
    assert second.journal_entries == first.journal_entries
    assert checkpoint.read_bytes() == before


def test_recursive_replay_preserves_position_and_ownership_without_duplicate_effects(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recursive"
    first = replay_fixture(FIXTURE_ROOT / "recursive-position.json", root, journal=True)
    second = replay_fixture(FIXTURE_ROOT / "recursive-position.json", root, journal=True)

    assert first.durable_state == second.durable_state
    assert second.actions == ()
    assert second.cursor == first.cursor == 0
    assert second.journal_entries == first.journal_entries

    manifest = first.durable_state["plan_manifest"]
    assert isinstance(manifest, dict)
    nodes = manifest["nodes"]
    assert isinstance(nodes, list)
    node_names = {node["name"] for node in nodes}
    assert node_names == {"root-leaf", "stage-a", "stage-b"}

    slices = first.durable_state["slices"]
    assert isinstance(slices, dict)
    assert {"root-leaf", "stage-a", "stage-b"}.issubset(slices)
    assert slices["stage-a"]["dispatch_agent_id"] == "stage-a"
    assert slices["stage-a"]["dispatch_intent_id"]
    assert first.durable_state["ordered_stages"] == [
        {"order": 1, "sub_tls": ["stage-a"]},
        {"order": 2, "sub_tls": ["stage-b"]},
    ]

    child = json.loads(
        (root / "replay-recursive" / "stage-a" / "run.json").read_text(encoding="utf-8")
    )
    child_manifest = child["plan_manifest"]
    child_names = {node["name"] for node in child_manifest["nodes"]}
    assert child_names == {"child-leaf", "nested-a", "nested-b"}
    assert child["slices"]["child-leaf"]["manifest_node_id"]
    assert child["ordered_stages"] == [
        {"order": 1, "sub_tls": ["nested-a", "nested-b"]},
    ]


def test_recursive_replay_replays_review_checkpoint_and_journal_once(tmp_path: Path) -> None:
    root = tmp_path / "recursive-recovery"
    first = replay_fixture(FIXTURE_ROOT / "recursive-recovery.json", root, journal=True)
    second = replay_fixture(FIXTURE_ROOT / "recursive-recovery.json", root, journal=True)

    assert first.cursor == second.cursor == 0
    assert second.actions == ()
    assert second.durable_state == first.durable_state
    assert second.journal_entries == first.journal_entries

    child = json.loads(
        (root / "replay-recovery" / "stage-a" / "run.json").read_text(encoding="utf-8")
    )
    assert child["slices"]["child-leaf"]["reviewed_head"] == "head-a"
    assert child["slices"]["child-leaf"]["pr_number"] == 42
    assert child["events"]["last_consumed_offset"] == 6
    assert child["slices"]["child-leaf"]["manifest_node_id"]


@pytest.mark.parametrize(
    "operation",
    [
        "resume_pr",
        "post_merge_parent_sync",
        "post_merge_issue_close",
        "post_merge_changelog",
        "post_merge_push",
    ],
)
def test_replay_probes_each_recovery_boundary_without_redispatch(
    tmp_path: Path, operation: str
) -> None:
    journal_path = tmp_path / "action-journal.json"
    intent = EffectIntent(operation, "recursive-child", {"head_sha": "head-a"}, True)
    result = ToolResult.from_raw(
        {"success": True, "result": {"operation": operation, "head_sha": "head-a"}}
    )

    first = EffectJournal("replay-recovery", journal_path)
    first.append(intent)
    first.mark_result(intent, result)

    restarted = EffectJournal("replay-recovery", journal_path)
    probe = restarted.probe(intent)
    assert probe.status == "confirmed"
    assert probe.is_terminal
    assert probe.result is not None
    assert probe.result.success is True
    assert restarted.probe(intent) == probe


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
