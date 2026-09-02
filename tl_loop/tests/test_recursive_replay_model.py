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
):
    return replay_fixture(
        FIXTURE_ROOT / "recursive-recovery.json",
        root,
        journal=True,
        live_ledger=live_ledger,
        child_event_transform=transform,
    )


def _journal_shape(entries) -> tuple[tuple[object, object, object], ...]:
    return tuple((entry["operation"], entry["target"], entry["status"]) for entry in entries)


@pytest.mark.parametrize("transform", [_duplicate_review, _delay_ci, _contradictory_duplicate])
def test_recursive_replay_disturbances_preserve_durable_position(tmp_path: Path, transform) -> None:
    baseline = _run(tmp_path / "baseline", live_ledger=False)
    disturbed = _run(tmp_path / "disturbed", live_ledger=False, transform=transform)

    assert _normalize_recovery_state(
        disturbed.durable_state, tmp_path / "disturbed"
    ) == _normalize_recovery_state(baseline.durable_state, tmp_path / "baseline")
    assert disturbed.actions == baseline.actions
    assert _journal_shape(disturbed.journal_entries) == _journal_shape(baseline.journal_entries)
    assert disturbed.cursor == baseline.cursor


def test_recursive_live_ledger_and_replay_sources_are_equivalent(tmp_path: Path) -> None:
    live = _run(tmp_path / "live", live_ledger=True, transform=_duplicate_review)
    replay = _run(tmp_path / "replay", live_ledger=False, transform=_duplicate_review)

    assert _normalize_recovery_state(
        normalize_durable_state(live.durable_state), tmp_path / "live"
    ) == _normalize_recovery_state(
        normalize_durable_state(replay.durable_state), tmp_path / "replay"
    )
    assert live.actions == replay.actions
    assert _journal_shape(live.journal_entries) == _journal_shape(replay.journal_entries)
    assert live.cursor == replay.cursor


def test_recursive_truncation_fails_closed_with_partial_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "truncated"

    with pytest.raises(ReplayTruncated):
        _run(root, live_ledger=False, transform=lambda rows: rows[:4])

    checkpoint = json.loads((root / "replay-recovery" / "run.json").read_text())
    assert checkpoint["fsm"]["payload"]["kind"] == "tl_running"
    assert checkpoint["fsm"]["payload"]["pending_by_order"]
    assert all(state["status"] != "merged" for state in checkpoint["slices"].values())
