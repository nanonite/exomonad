"""Golden replay tests for complete active-loop trajectories."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
def test_recorded_stream_replays_exact_actions_and_state(
    fixture_name: str, tmp_path: Path
) -> None:
    first = replay_fixture(FIXTURE_ROOT / fixture_name, tmp_path / "first")
    second = replay_fixture(FIXTURE_ROOT / fixture_name, tmp_path / "second")

    assert first.actions == expected_actions(FIXTURE_ROOT / fixture_name)
    assert _canonical(first.state) == _canonical(expected_state(FIXTURE_ROOT / fixture_name))
    assert first.actions == second.actions
    assert _canonical(first.state) == _canonical(second.state)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
