"""Replay disturbance and live-source equivalence coverage for recursive runs."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tl_loop.events.replay import ReplayTruncated
from tl_loop.tests.replay import FIXTURE_ROOT, normalize_durable_state, replay_fixture
from tl_loop.tests.test_replay import _normalize_recovery_state


def _duplicate_review(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Deliver an identical review after later child evidence."""
    return [*rows, rows[2]]


def _delay_ci(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Delay CI behind the child notifications while preserving all evidence."""
    return [rows[0], rows[1], rows[2], rows[4], rows[3], rows[5]]


def _contradictory_duplicate(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Repeat one event identity with conflicting payload evidence."""
    duplicate = copy.deepcopy(rows[2])
    data = duplicate["data"]
    assert isinstance(data, dict)
    data["verdict"] = "NO-GO"
    return [*rows, duplicate]


def _run(
    root: Path,
    *,
    live_ledger: bool,
    transform=None,
    capture_initial_events: bool | None = None,
    keep_alive_on_waiting: bool = True,
    allow_truncated: bool = False,
):
    return replay_fixture(
        FIXTURE_ROOT / "recursive-recovery.json",
        root,
        journal=True,
        live_ledger=live_ledger,
        capture_initial_events=capture_initial_events,
        keep_alive_on_waiting=keep_alive_on_waiting,
        allow_truncated=allow_truncated,
        child_event_transform=transform,
    )


def _journal_shape(entries) -> tuple[tuple[object, object, object], ...]:
    return tuple((entry["operation"], entry["target"], entry["status"]) for entry in entries)


@pytest.mark.parametrize("transform", [_duplicate_review, _delay_ci, _contradictory_duplicate])
def test_recursive_replay_disturbances_preserve_durable_position(tmp_path: Path, transform) -> None:
    baseline = _run(tmp_path / "baseline", live_ledger=False, allow_truncated=True)
    disturbed = _run(
        tmp_path / "disturbed", live_ledger=False, transform=transform, allow_truncated=True
    )

    assert _normalize_recovery_state(
        disturbed.durable_state, tmp_path / "disturbed"
    ) == _normalize_recovery_state(baseline.durable_state, tmp_path / "baseline")
    assert disturbed.actions == baseline.actions
    assert _journal_shape(disturbed.journal_entries) == _journal_shape(baseline.journal_entries)
    assert disturbed.cursor == baseline.cursor


def test_recursive_startup_holds_duplicate_sequence_before_advancing(tmp_path: Path) -> None:
    live = _run(
        tmp_path / "live",
        live_ledger=True,
        transform=_duplicate_review,
        keep_alive_on_waiting=False,
    )
    root = live.durable_state
    assert root["current_order"] == 1
    assert root["slices"]["stage-a"]["status"] == "spawned"
    assert root["slices"]["stage-b"]["status"] == "pending"

    child = json.loads(
        (tmp_path / "live" / "replay-recovery" / "stage-a" / "run.json").read_text(encoding="utf-8")
    )
    assert child["events"]["last_consumed_offset"] == 0
    assert child["gates"] == [{"name": "tl-recovery-consistency", "status": "pending"}]
    assert child["slices"]["child-leaf"]["status"] == "pending"
    assert not any(action["operation"] in {"spawn_leaf", "merge_pr"} for action in live.actions)

    replay_root = tmp_path / "replay"
    baseline = _run(replay_root / "baseline", live_ledger=False, allow_truncated=True)
    disturbed = _run(
        replay_root / "disturbed",
        live_ledger=False,
        transform=_duplicate_review,
        allow_truncated=True,
    )
    assert _normalize_recovery_state(
        normalize_durable_state(disturbed.durable_state), replay_root / "disturbed"
    ) == _normalize_recovery_state(
        normalize_durable_state(baseline.durable_state), replay_root / "baseline"
    )
    assert disturbed.actions == baseline.actions
    assert _journal_shape(disturbed.journal_entries) == _journal_shape(baseline.journal_entries)
    assert disturbed.cursor == baseline.cursor


def test_recursive_truncation_fails_closed_with_partial_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "truncated"

    with pytest.raises(ReplayTruncated):
        _run(root, live_ledger=False, transform=lambda rows: rows[:4])

    checkpoint = json.loads((root / "replay-recovery" / "run.json").read_text())
    assert checkpoint["fsm"]["payload"]["kind"] == "tl_running"
    assert checkpoint["fsm"]["payload"]["pending_by_order"]
    assert all(state["status"] != "merged" for state in checkpoint["slices"].values())
