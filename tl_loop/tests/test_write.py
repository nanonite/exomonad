"""Crash-safety and concurrency tests for the single run-state writer."""

from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path
from typing import Any, cast

from tl_loop.state.schema import SCHEMA_VERSION, SchemaError, validate
from tl_loop.state.write import ConcurrentWrite, WriteHooks, apply


class SimulatedCrash(RuntimeError):
    """Raised by a test hook after temp-file fsync."""


def test_concurrent_processes_serialize_without_lost_updates(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    context_name = "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    context = multiprocessing.get_context(context_name)
    results: Any = context.Queue()
    process_type = cast(Any, context).Process
    processes = [process_type(target=_increment_worker, args=(str(run_dir), results)) for _ in range(4)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert not process.is_alive()
        assert process.exitcode == 0
    for _ in processes:
        assert results.get(timeout=2) is None

    document = cast(dict[str, object], json.loads((run_dir / "run.json").read_text(encoding="utf-8")))
    validate(document)
    ledger = cast(dict[str, object], cast(dict[str, object], document["budgets"])["ledger"])
    assert ledger["tokens"] == 4
    assert document["revision"] == 4


def test_crash_after_temp_fsync_leaves_previous_document_intact(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    target = run_dir / "run.json"
    before = target.read_bytes()

    try:
        apply(run_dir, _increment, hooks=WriteHooks(before_rename=_crash))
    except SimulatedCrash:
        pass
    else:
        raise AssertionError("expected simulated crash")

    assert target.read_bytes() == before
    assert list(run_dir.glob(".run.json.*.tmp")) == []
    validate(cast(dict[str, object], json.loads(before)))


def test_invalid_mutator_result_does_not_touch_file(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    target = run_dir / "run.json"
    before = target.read_bytes()

    def invalid(document: dict[str, object]) -> dict[str, object]:
        document["unknown"] = True
        return document

    try:
        apply(run_dir, invalid)
    except SchemaError as error:
        assert "run: unknown keys: unknown" in str(error)
    else:
        raise AssertionError("expected schema validation failure")
    assert target.read_bytes() == before
    assert list(run_dir.glob(".run.json.*.tmp")) == []


def test_final_compare_and_swap_rejects_external_change(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)

    def external_change() -> None:
        document = cast(dict[str, object], json.loads((run_dir / "run.json").read_text(encoding="utf-8")))
        document["revision"] = 99
        (run_dir / "run.json").write_bytes((json.dumps(document, sort_keys=True) + "\n").encode("utf-8"))

    try:
        apply(run_dir, _increment, hooks=WriteHooks(before_rename=external_change))
    except ConcurrentWrite:
        pass
    else:
        raise AssertionError("expected concurrent write detection")
    current = cast(dict[str, object], json.loads((run_dir / "run.json").read_text(encoding="utf-8")))
    assert current["revision"] == 99


def _increment_worker(run_dir: str, results: Any) -> None:
    try:
        apply(Path(run_dir), _increment)
        results.put(None)
    except BaseException as error:
        results.put(repr(error))
        raise


def _increment(document: dict[str, object]) -> dict[str, object]:
    time.sleep(0.02)
    budgets = cast(dict[str, object], document["budgets"])
    ledger = cast(dict[str, object], budgets["ledger"])
    ledger["tokens"] = cast(int, ledger["tokens"]) + 1
    return document


def _crash() -> None:
    raise SimulatedCrash("crash before atomic rename")


def _make_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    document = {
        "version": SCHEMA_VERSION,
        "revision": 0,
        "run_id": "run-1",
        "fsm": {"phase": "tl_planning", "waiting": []},
        "slices": {
            "slice-a": {
                "id": "slice-a",
                "status": "pending",
                "paths": ["src/a.py"],
                "depends_on": [],
                "base_ref": None,
                "test_plan": ["just tl-loop-test"],
                "agent_type": None,
                "model": None,
                "branch": None,
                "worktree": None,
                "pr_number": None,
                "reviewed_head": None,
                "attempts": 0,
                "verdict": None,
            }
        },
        "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        "gates": [{"name": "plan", "status": "pending"}],
        "events": {"last_consumed_offset": 0},
    }
    target = run_dir / "run.json"
    target.write_bytes((json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    validate(document)
    return run_dir
