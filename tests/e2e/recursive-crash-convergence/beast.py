"""Run the captured Beast checkpoint through three explicit continuations."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from evidence import (
    AcceptanceError,
    assert_journal_terminal,
    assert_recursive_effect_cardinality,
    assert_remote_ancestry,
    event_payload,
    read_json,
)


def _required_environment() -> tuple[Path, str]:
    workspace_value = os.environ.get("EXOMONAD_BEAST_WORKSPACE")
    command = os.environ.get("EXOMONAD_BEAST_CONTINUE_COMMAND")
    if not workspace_value or not command:
        raise AcceptanceError(
            "#1057 Beast acceptance requires EXOMONAD_BEAST_WORKSPACE and "
            "EXOMONAD_BEAST_CONTINUE_COMMAND"
        )
    if "{workspace}" not in command:
        raise AcceptanceError(
            "EXOMONAD_BEAST_CONTINUE_COMMAND must contain the {workspace} placeholder"
        )
    workspace = Path(workspace_value).expanduser().resolve()
    if not workspace.is_dir():
        raise AcceptanceError(f"Beast workspace does not exist: {workspace}")
    return workspace, command


def _checkpoint(workspace: Path) -> Path:
    path = workspace / ".exo" / "tl-loop" / "root" / "run.json"
    if not path.is_file():
        raise AcceptanceError(f"captured Beast checkpoint is missing: {path}")
    return path


def _merge_entries(workspace: Path) -> list[dict[str, Any]]:
    journal_path = _checkpoint(workspace).with_name("action-journal.json")
    if not journal_path.is_file():
        raise AcceptanceError(f"Beast action journal is missing: {journal_path}")
    assert_journal_terminal(journal_path)
    value = read_json(journal_path)
    if not isinstance(value, list):
        raise AcceptanceError(f"Beast action journal is not a list: {journal_path}")
    target_pr = _target_pr_number()
    return [
        entry
        for entry in value
        if isinstance(entry, dict)
        and entry.get("operation") == "merge_pr"
        and _entry_pr_number(entry) == target_pr
    ]


def _target_pr_number() -> int:
    value = os.environ.get("EXOMONAD_BEAST_PR_NUMBER", "43")
    try:
        number = int(value)
    except ValueError as error:
        raise AcceptanceError("EXOMONAD_BEAST_PR_NUMBER must be an integer") from error
    if number <= 0:
        raise AcceptanceError("EXOMONAD_BEAST_PR_NUMBER must be positive")
    return number


def _entry_pr_number(entry: dict[str, Any]) -> int | None:
    arguments = entry.get("arguments")
    if not isinstance(arguments, dict):
        return None
    value = arguments.get("pr_number")
    return value if type(value) is int and value > 0 else None


def _phase(document: dict[str, Any]) -> str:
    fsm = document.get("fsm")
    if not isinstance(fsm, dict):
        raise AcceptanceError("Beast checkpoint lacks its discriminated FSM")
    phase = fsm.get("phase", fsm.get("kind"))
    if not isinstance(phase, str) or not phase:
        raise AcceptanceError("Beast checkpoint lacks its FSM phase")
    return phase


def _is_terminal(phase: str) -> bool:
    return phase == "tl_done"


def _assert_bookkeeping(workspace: Path, phase: str) -> None:
    counts = assert_recursive_effect_cardinality(_checkpoint(workspace).parent)
    required = {
        "merge_pr",
        "post_merge_parent_sync",
        "chainlink_issue_close",
        "post_merge_changelog",
        "post_merge_push",
    }
    if phase == "tl_done":
        required.add("root_branch_finalize")
    missing = sorted(
        operation for operation in required if counts.get(operation, 0) < 1
    )
    if missing:
        raise AcceptanceError(
            "Beast bookkeeping is incomplete; expected one of each "
            f"{missing!r}, got {counts!r}"
        )


def _ledger_merge_count(workspace: Path) -> int:
    target_pr = _target_pr_number()
    count = 0
    for path in sorted((workspace / ".exo" / "ledger").rglob("*")):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            event_type = value.get("type", value.get("event_type"))
            if event_type in {"tl.merge_reconciled", "merge.reconciled"}:
                payload = event_payload(value)
                if payload.get("pr_number") == target_pr:
                    count += 1
    return count


def _assert_checkpoint(document: Any, label: str) -> tuple[int, int]:
    if not isinstance(document, dict):
        raise AcceptanceError(f"Beast checkpoint {label} is not an object")
    version = document.get("state_version")
    events = document.get("events")
    cursor = events.get("last_consumed_offset") if isinstance(events, dict) else None
    if type(version) is not int or type(cursor) is not int:
        raise AcceptanceError(f"Beast checkpoint {label} lacks version/cursor")
    return version, cursor


def run_three_continuations() -> dict[str, Any]:
    workspace, command = _required_environment()
    checkpoint = _checkpoint(workspace)
    initial_document = read_json(checkpoint)
    initial_phase = _phase(initial_document)
    if _is_terminal(initial_phase):
        raise AcceptanceError(
            "captured Beast checkpoint is already terminal; no continuation convergence was tested"
        )
    previous_version, previous_cursor = _assert_checkpoint(initial_document, "initial")
    previous_phase = initial_phase
    previous_merges = _ledger_merge_count(workspace)
    if previous_merges > 1:
        raise AcceptanceError(
            f"captured Beast checkpoint already has duplicate merges: {previous_merges}"
        )
    baseline_merges = previous_merges
    progress_count = 0
    phase_progressed = False
    runs: list[dict[str, Any]] = []
    for attempt in range(1, 4):
        rendered = command.format(workspace=str(workspace), checkpoint=str(checkpoint))
        completed = subprocess.run(
            shlex.split(rendered),
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AcceptanceError(
                f"Beast continuation {attempt} failed ({completed.returncode}): "
                f"{completed.stderr[-2000:]}"
            )
        document = read_json(checkpoint)
        version, cursor = _assert_checkpoint(document, f"continuation {attempt}")
        phase = _phase(document)
        if version < previous_version or cursor < previous_cursor:
            raise AcceptanceError(
                f"Beast checkpoint regressed on continuation {attempt}: "
                f"version {previous_version}->{version}, cursor {previous_cursor}->{cursor}"
            )
        progressed = (
            version > previous_version
            or cursor > previous_cursor
            or phase != previous_phase
        )
        if not _is_terminal(phase) and not progressed:
            raise AcceptanceError(
                f"Beast continuation {attempt} made no durable progress before terminal convergence"
            )
        if progressed:
            progress_count += 1
        if phase != previous_phase:
            phase_progressed = True
        merge_count = _ledger_merge_count(workspace)
        max_merges = baseline_merges if baseline_merges else 1
        if merge_count > max_merges or merge_count < previous_merges:
            raise AcceptanceError(
                f"Beast continuation {attempt} violated merge cardinality: "
                f"{previous_merges}->{merge_count}"
            )
        runs.append(
            {
                "attempt": attempt,
                "state_version": version,
                "cursor": cursor,
                "phase": phase,
                "merge_reconciliations": merge_count,
            }
        )
        previous_version, previous_cursor, previous_merges = (
            version,
            cursor,
            merge_count,
        )
        previous_phase = phase
    merge_entries = _merge_entries(workspace)
    if len(merge_entries) != 1:
        raise AcceptanceError(
            f"Beast checkpoint does not contain exactly one merge intent: {merge_entries!r}"
        )
    final_document = read_json(checkpoint)
    final_phase = _phase(final_document)
    if not _is_terminal(final_phase):
        raise AcceptanceError(
            f"Beast checkpoint did not converge to a terminal FSM phase: {final_phase}"
        )
    final_merges = _ledger_merge_count(workspace)
    expected_merges = baseline_merges if baseline_merges else 1
    if final_merges != expected_merges:
        raise AcceptanceError(
            "Beast continuation violated merge cardinality: "
            f"expected {expected_merges}, got {final_merges}"
        )
    if progress_count == 0:
        raise AcceptanceError(
            "Beast continuations made no durable progress from the captured checkpoint"
        )
    if baseline_merges and not phase_progressed:
        raise AcceptanceError(
            "Beast merged baseline was not adopted through a new root FSM phase"
        )
    _assert_bookkeeping(workspace, final_phase)
    assert_remote_ancestry(
        final_document,
        workspace=workspace,
        remote="origin",
        remote_branch="main",
    )
    return {
        "passed": True,
        "runs": runs,
        "merge_intents": len(merge_entries),
        "final_phase": final_phase,
    }
