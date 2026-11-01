from __future__ import annotations

import json
from pathlib import Path

from tl_loop.loop.startup_handoff import wait_for_root_controller_handoff


def test_root_handoff_requires_final_owner_to_match_the_epoch_and_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "root"
    run_dir.mkdir()
    (run_dir / "controller-target.json").write_text(
        json.dumps(_owner("another-epoch", "root")), encoding="utf-8"
    )

    result = wait_for_root_controller_handoff(
        run_dir,
        run_id="root",
        controller_epoch="expected-epoch",
        agent_id="root",
        cancel_event=None,
    )

    assert not result.ready
    assert result.reason == "final controller ownership does not match this run and epoch"


def test_root_handoff_rejects_malformed_released_intent(tmp_path: Path) -> None:
    run_dir = tmp_path / "root"
    run_dir.mkdir()
    intent = {
        "schema_version": 1,
        "controller_epoch": "expected-epoch",
        "tmux_session": "session",
        "tmux_server_pid": 11,
        "tmux_server_start_ticks": 12,
        "continue_mode": True,
        "root_run_id": "root",
        "target": {"pane_id": "%1"},
        "phase": "released",
    }
    (run_dir / "controller-startup.json").write_text(json.dumps(intent), encoding="utf-8")

    result = wait_for_root_controller_handoff(
        run_dir,
        run_id="root",
        controller_epoch="expected-epoch",
        agent_id="root",
        cancel_event=None,
    )

    assert not result.ready
    assert result.reason == "controller startup target is incomplete or conflicts with its intent"


def test_non_root_scope_does_not_consume_root_handoff(tmp_path: Path) -> None:
    run_dir = tmp_path / "root"
    run_dir.mkdir()
    (run_dir / "controller-startup.json").write_text("invalid", encoding="utf-8")

    result = wait_for_root_controller_handoff(
        run_dir,
        run_id="child",
        controller_epoch=None,
        agent_id="child",
        cancel_event=None,
    )

    assert result.ready


def _owner(epoch: str, run_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "controller_epoch": epoch,
        "run_id": run_id,
        "tmux_session": "session",
        "window_id": "@0",
        "pane_id": "%1",
        "pane_pid": 101,
        "pane_start_ticks": 202,
        "tmux_server_pid": 303,
        "tmux_server_start_ticks": 404,
    }
