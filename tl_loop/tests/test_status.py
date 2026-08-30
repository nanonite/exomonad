import json
from pathlib import Path

import pytest

from tl_loop.__main__ import _parser, main


def test_status_without_run_reports_waiting_plan(tmp_path: Path, capsys) -> None:
    assert main(["status", "--project-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "no run yet; controller is waiting for .exo/tl-loop/plan.json" in output
    assert str(tmp_path / ".exo" / "tl-loop" / "plan.json") in output


def test_startup_failure_is_recorded_and_status_reports_it(tmp_path: Path, capsys) -> None:
    assert main(["run", "--project-root", str(tmp_path)]) == 2
    marker = tmp_path / ".exo" / "tl-loop" / "root" / "controller-exit.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert "plan.json" in payload["reason"]

    assert main(["status", "--project-root", str(tmp_path)]) == 0
    assert "controller exited:" in capsys.readouterr().out


def test_status_reports_durable_controller_failure(tmp_path: Path, capsys) -> None:
    marker = tmp_path / ".exo" / "tl-loop" / "root" / "controller-exit.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps({"reason": "capability file is missing"}),
        encoding="utf-8",
    )

    assert main(["status", "--project-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "controller exited: capability file is missing" in output
    assert "controller fingerprint:" in output


def test_status_tolerates_partial_checkpoint(tmp_path: Path, capsys) -> None:
    checkpoint = tmp_path / ".exo" / "tl-loop" / "root" / "run.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("{", encoding="utf-8")
    assert main(["status", "--project-root", str(tmp_path)]) == 0
    assert "checkpoint is currently unavailable; retrying:" in capsys.readouterr().out


def test_status_renders_live_state_without_watch_controls(tmp_path: Path, capsys) -> None:
    checkpoint = tmp_path / ".exo" / "tl-loop" / "root" / "run.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            {
                "version": 1,
                "revision": 3,
                "run_id": "root",
                "fsm": {"phase": "tl_dispatching", "waiting": ["task-a"]},
                "slices": {
                    "task-a": {
                        "id": "task-a",
                        "status": "spawned",
                        "paths": ["src/a.py"],
                        "depends_on": [],
                        "base_ref": "main",
                        "test_plan": ["just test"],
                        "agent_type": "codex",
                        "model": "gpt-5",
                        "branch": "task-a",
                        "worktree": ".worktrees/task-a",
                        "pr_number": 101,
                        "reviewed_head": None,
                        "attempts": 1,
                        "verdict": None,
                        "dispatch_intent_id": "status-intent-1",
                        "dispatch_agent_id": "agent-task-a",
                        "dispatch_authoritative_event_seq": 7,
                        "park_cause": "review_stuck",
                        "park_issue_id": 404,
                    }
                },
                "budgets": {"ledger": {"tokens": 321, "wall_seconds": 45}},
                "gates": [{"name": "review", "status": "pending"}],
                "events": {"last_consumed_offset": 112},
            }
        ),
        encoding="utf-8",
    )

    assert main(["status", "--project-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    document = json.loads(output)
    assert "\x1b[" not in output
    assert document["phase"] == "tl_dispatching"
    assert document["slices"]["task-a"]["pr_number"] == 101
    assert {"name": "review", "status": "pending"} in document["gates"]
    assert any(gate["name"].startswith("plan-manifest-migration:") for gate in document["gates"])
    assert document["park_causes"] == {"task-a": "review_stuck"}
    assert document["last_consumed_offset"] == 112
    assert document["controller_fingerprint"]["status"] == "source"


def test_status_watch_redraws_until_interrupted(tmp_path: Path, capsys, monkeypatch) -> None:
    class StopWatching(Exception):
        pass

    def stop_watching(_: float) -> None:
        raise StopWatching

    monkeypatch.setattr("tl_loop.__main__.time.sleep", stop_watching)
    with pytest.raises(StopWatching):
        main(["status", "--project-root", str(tmp_path), "--watch", "--interval", "0.01"])

    output = capsys.readouterr().out
    assert "\x1b[2J\x1b[H" in output
    assert "no run yet; controller is waiting for .exo/tl-loop/plan.json" in output


def test_status_watch_options_keep_one_shot_default() -> None:
    args = _parser().parse_args(["status", "--watch", "--interval", "0.5"])
    assert args.watch is True
    assert args.interval == 0.5
    assert _parser().parse_args(["status"]).watch is False
    assert _parser().parse_args(["status"]).interval == 2.0
