"""Validation for the root controller's init-owned startup handoff."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STARTUP_HANDOFF_TIMEOUT_SECONDS = 30.0
STARTUP_HANDOFF_POLL_SECONDS = 0.05
_PHASES = {"target_created": 1, "state_reconciled": 2, "released": 3}
_TARGET_FIELDS = (
    "controller_epoch",
    "tmux_session",
    "window_id",
    "pane_id",
    "pane_pid",
    "pane_start_ticks",
    "tmux_server_pid",
    "tmux_server_start_ticks",
)


@dataclass(frozen=True)
class HandoffWaitResult:
    ready: bool
    reason: str | None = None


def wait_for_root_controller_handoff(
    run_dir: Path,
    *,
    run_id: str,
    controller_epoch: str | None,
    agent_id: str | None,
    cancel_event: threading.Event | None,
    timeout_seconds: float = STARTUP_HANDOFF_TIMEOUT_SECONDS,
    poll_seconds: float = STARTUP_HANDOFF_POLL_SECONDS,
) -> HandoffWaitResult:
    """Wait for init's exact owner commit before root recovery or live effects."""
    if agent_id != "root":
        return HandoffWaitResult(True)
    if not controller_epoch:
        return HandoffWaitResult(False, "root controller epoch is unavailable")
    startup_path = run_dir / "controller-startup.json"
    first_intent, error = _read_optional_object(startup_path, "controller startup intent")
    if error:
        return HandoffWaitResult(False, error)
    if first_intent is None:
        return _read_and_match_owner(run_dir / "controller-target.json", run_id, controller_epoch)
    expected, error = _validate_intent(first_intent, run_id, controller_epoch)
    if error:
        return HandoffWaitResult(False, error)
    return _wait_for_commit(
        run_dir, expected, run_id, controller_epoch, cancel_event, timeout_seconds, poll_seconds
    )


def _wait_for_commit(
    run_dir: Path,
    expected: dict[str, Any],
    run_id: str,
    epoch: str,
    cancel_event: threading.Event | None,
    timeout: float,
    poll: float,
) -> HandoffWaitResult:
    deadline = time.monotonic() + timeout
    phase = _PHASES[expected["phase"]]
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return HandoffWaitResult(False, "root controller startup handoff was cancelled")
        result, next_phase = _inspect_handoff_state(run_dir, expected, run_id, epoch, phase)
        if result is not None:
            return result
        phase = next_phase
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return HandoffWaitResult(
                False, "controller startup handoff did not commit before timeout"
            )
        _wait_poll(cancel_event, min(poll, remaining))


def _inspect_handoff_state(
    run_dir: Path,
    expected: dict[str, Any],
    run_id: str,
    epoch: str,
    previous_phase: int,
) -> tuple[HandoffWaitResult | None, int]:
    current, error = _read_optional_object(
        run_dir / "controller-startup.json", "controller startup intent"
    )
    if error:
        return HandoffWaitResult(False, error), previous_phase
    if current is None:
        return _read_and_match_owner(
            run_dir / "controller-target.json", run_id, epoch, expected["target"]
        ), previous_phase
    validated, error = _validate_intent(current, run_id, epoch)
    if error:
        return HandoffWaitResult(False, error), previous_phase
    if not _same_handoff(expected, validated):
        return HandoffWaitResult(
            False, "controller startup handoff identity changed"
        ), previous_phase
    phase = _PHASES[validated["phase"]]
    if phase < previous_phase:
        return HandoffWaitResult(
            False, "controller startup handoff phase moved backwards"
        ), previous_phase
    return None, phase


def _read_and_match_owner(
    path: Path, run_id: str, epoch: str, target: dict[str, Any] | None = None
) -> HandoffWaitResult:
    owner, error = _read_required_object(path, "final controller ownership")
    if error:
        return HandoffWaitResult(False, error)
    if not _owner_identity_matches(owner, run_id, epoch):
        return HandoffWaitResult(
            False, "final controller ownership does not match this run and epoch"
        )
    if target is not None and any(owner.get(key) != target.get(key) for key in _TARGET_FIELDS):
        return HandoffWaitResult(
            False, "final controller ownership differs from the startup target"
        )
    return HandoffWaitResult(True)


def _owner_identity_matches(owner: dict[str, Any], run_id: str, epoch: str) -> bool:
    return (
        type(owner.get("schema_version")) is int
        and owner["schema_version"] == 1
        and owner.get("run_id") == run_id
        and owner.get("controller_epoch") == epoch
        and _valid_target(owner, owner)
    )


def _validate_intent(
    intent: dict[str, Any], run_id: str, epoch: str
) -> tuple[dict[str, Any] | None, str | None]:
    phase = intent.get("phase")
    target = intent.get("target")
    if not _valid_intent_header(intent, epoch, phase, target):
        return None, "controller startup intent is incomplete or conflicts with this root run"
    expected_run = intent.get("root_run_id")
    if expected_run is not None and expected_run != run_id:
        return None, "controller startup intent names another root run"
    if intent["continue_mode"] and expected_run != run_id:
        return None, "continued controller startup lacks its accepted root identity"
    if not _valid_target(target, intent):
        return None, "controller startup target is incomplete or conflicts with its intent"
    return _validated_intent(intent, phase, expected_run, target), None


def _valid_intent_header(intent: dict[str, Any], epoch: str, phase: object, target: object) -> bool:
    return (
        type(intent.get("schema_version")) is int
        and intent["schema_version"] == 1
        and intent.get("controller_epoch") == epoch
        and type(intent.get("continue_mode")) is bool
        and isinstance(phase, str)
        and phase in _PHASES
        and isinstance(target, dict)
        and _positive_int(intent.get("tmux_server_pid"))
        and _positive_int(intent.get("tmux_server_start_ticks"))
        and isinstance(intent.get("tmux_session"), str)
        and bool(intent["tmux_session"].strip())
    )


def _validated_intent(
    intent: dict[str, Any], phase: str, run_id: str | None, target: dict[str, Any]
) -> dict[str, Any]:
    return {
        "controller_epoch": intent["controller_epoch"],
        "continue_mode": intent["continue_mode"],
        "phase": phase,
        "root_run_id": run_id,
        "target": target,
        "tmux_server_pid": intent["tmux_server_pid"],
        "tmux_server_start_ticks": intent["tmux_server_start_ticks"],
        "tmux_session": intent["tmux_session"],
    }


def _valid_target(target: dict[str, Any], intent: dict[str, Any]) -> bool:
    return (
        type(target.get("schema_version")) is int
        and target["schema_version"] == 1
        and all(target.get(key) == intent.get(key) for key in _TARGET_FIELDS[:2])
        and all(target.get(key) == intent.get(key) for key in _TARGET_FIELDS[6:])
        and isinstance(target.get("window_id"), str)
        and bool(target["window_id"].strip())
        and isinstance(target.get("pane_id"), str)
        and bool(target["pane_id"].strip())
        and _positive_int(target.get("pane_pid"))
        and _positive_int(target.get("pane_start_ticks"))
    )


def _same_handoff(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(
        left[key] == right[key]
        for key in (
            "controller_epoch",
            "continue_mode",
            "root_run_id",
            "target",
            "tmux_server_pid",
            "tmux_server_start_ticks",
            "tmux_session",
        )
    )


def _read_required_object(path: Path, description: str) -> tuple[dict[str, Any] | None, str | None]:
    value, error = _read_optional_object(path, description)
    if value is None and error is None:
        return None, f"{description} is missing"
    return value, error


def _read_optional_object(path: Path, description: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeError) as error:
        return None, f"{description} is unavailable: {type(error).__name__}"
    try:
        value = json.loads(contents)
    except json.JSONDecodeError as error:
        return None, f"{description} is malformed: {type(error).__name__}"
    if not isinstance(value, dict):
        return None, f"{description} is malformed: expected an object"
    return value, None


def _wait_poll(cancel_event: threading.Event | None, seconds: float) -> None:
    if cancel_event is None:
        time.sleep(seconds)
    else:
        cancel_event.wait(seconds)


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0
