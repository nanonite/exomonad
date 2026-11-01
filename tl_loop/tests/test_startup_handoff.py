from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

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


@pytest.mark.parametrize(
    "field",
    (
        "schema_version",
        "controller_epoch",
        "run_id",
        "tmux_session",
        "window_id",
        "pane_id",
        "pane_pid",
        "pane_start_ticks",
        "tmux_server_pid",
        "tmux_server_start_ticks",
    ),
)
def test_final_owner_requires_every_rust_ownership_field(tmp_path: Path, field: str) -> None:
    run_dir = tmp_path / "root"
    run_dir.mkdir()
    owner = _owner("expected-epoch", "root")
    del owner[field]
    (run_dir / "controller-target.json").write_text(json.dumps(owner), encoding="utf-8")

    result = wait_for_root_controller_handoff(
        run_dir,
        run_id="root",
        controller_epoch="expected-epoch",
        agent_id="root",
        cancel_event=None,
    )

    assert not result.ready, f"missing owner field {field} was accepted"
    assert result.reason == "final controller ownership is incomplete or malformed"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", True),
        ("controller_epoch", "  "),
        ("controller_epoch", 7),
        ("run_id", ""),
        ("run_id", None),
        ("tmux_session", ""),
        ("tmux_session", 17),
        ("window_id", "4"),
        ("window_id", "@x"),
        ("window_id", ["@1"]),
        ("pane_id", "%"),
        ("pane_id", "%2x"),
        ("pane_id", False),
        ("pane_pid", True),
        ("pane_pid", 0),
        ("pane_pid", "101"),
        ("pane_pid", 2**32),
        ("pane_start_ticks", False),
        ("pane_start_ticks", 0),
        ("pane_start_ticks", 2**64),
        ("tmux_server_pid", True),
        ("tmux_server_pid", -1),
        ("tmux_server_pid", 2**32),
        ("tmux_server_start_ticks", "404"),
        ("tmux_server_start_ticks", 0),
        ("tmux_server_start_ticks", 2**64),
    ),
)
def test_final_owner_rejects_invalid_rust_field_shapes(
    tmp_path: Path, field: str, value: object
) -> None:
    run_dir = tmp_path / "root"
    run_dir.mkdir()
    owner = _owner("expected-epoch", "root")
    owner[field] = value
    (run_dir / "controller-target.json").write_text(json.dumps(owner), encoding="utf-8")

    result = wait_for_root_controller_handoff(
        run_dir,
        run_id="root",
        controller_epoch="expected-epoch",
        agent_id="root",
        cancel_event=None,
    )

    assert not result.ready, f"invalid owner field {field}={value!r} was accepted"
    assert result.reason == "final controller ownership is incomplete or malformed"


@pytest.mark.parametrize(
    ("field", "value"),
    (("run_id", "other-root"), ("controller_epoch", "other-epoch")),
)
def test_final_owner_rejects_identity_conflicts(tmp_path: Path, field: str, value: str) -> None:
    run_dir = tmp_path / "root"
    run_dir.mkdir()
    owner = _owner("expected-epoch", "root")
    owner[field] = value
    (run_dir / "controller-target.json").write_text(json.dumps(owner), encoding="utf-8")

    result = wait_for_root_controller_handoff(
        run_dir,
        run_id="root",
        controller_epoch="expected-epoch",
        agent_id="root",
        cancel_event=None,
    )

    assert not result.ready
    assert result.reason == "final controller ownership does not match this run and epoch"


def test_final_owner_coordinates_are_checked_after_startup_intent_is_removed(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "root"
    run_dir.mkdir()
    expected = _owner("expected-epoch", "root")
    intent = {
        "schema_version": 1,
        "controller_epoch": "expected-epoch",
        "tmux_session": expected["tmux_session"],
        "tmux_server_pid": expected["tmux_server_pid"],
        "tmux_server_start_ticks": expected["tmux_server_start_ticks"],
        "continue_mode": True,
        "root_run_id": "root",
        "target": {key: value for key, value in expected.items() if key != "run_id"},
        "phase": "released",
    }
    startup_path = run_dir / "controller-startup.json"
    startup_path.write_text(json.dumps(intent), encoding="utf-8")
    mismatched = {**expected, "pane_id": "%2"}
    (run_dir / "controller-target.json").write_text(json.dumps(mismatched), encoding="utf-8")

    def finish_handoff() -> None:
        time.sleep(0.02)
        startup_path.unlink()

    remover = threading.Thread(target=finish_handoff)
    remover.start()
    result = wait_for_root_controller_handoff(
        run_dir,
        run_id="root",
        controller_epoch="expected-epoch",
        agent_id="root",
        cancel_event=None,
        timeout_seconds=1,
        poll_seconds=0.001,
    )
    remover.join(timeout=1)

    assert not remover.is_alive()
    assert not result.ready
    assert result.reason == "final controller ownership differs from the startup target"


def test_incomplete_final_owner_is_rejected_after_startup_intent_removal(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "root"
    run_dir.mkdir()
    owner = _owner("expected-epoch", "root")
    target = {key: value for key, value in owner.items() if key != "run_id"}
    intent = {
        "schema_version": 1,
        "controller_epoch": "expected-epoch",
        "tmux_session": owner["tmux_session"],
        "tmux_server_pid": owner["tmux_server_pid"],
        "tmux_server_start_ticks": owner["tmux_server_start_ticks"],
        "continue_mode": True,
        "root_run_id": "root",
        "target": target,
        "phase": "released",
    }
    startup_path = run_dir / "controller-startup.json"
    startup_path.write_text(json.dumps(intent), encoding="utf-8")
    del owner["tmux_server_pid"]
    (run_dir / "controller-target.json").write_text(json.dumps(owner), encoding="utf-8")

    def finish_handoff() -> None:
        time.sleep(0.02)
        startup_path.unlink()

    remover = threading.Thread(target=finish_handoff)
    remover.start()
    result = wait_for_root_controller_handoff(
        run_dir,
        run_id="root",
        controller_epoch="expected-epoch",
        agent_id="root",
        cancel_event=None,
        timeout_seconds=1,
        poll_seconds=0.001,
    )
    remover.join(timeout=1)

    assert not remover.is_alive()
    assert not result.ready
    assert result.reason == "final controller ownership is incomplete or malformed"


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
