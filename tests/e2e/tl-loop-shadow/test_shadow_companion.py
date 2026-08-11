"""Focused projection checks for the live shadow companion."""

from __future__ import annotations

from shadow_companion import _is_concrete_spawn
from tl_loop.events.envelope import project


def test_spawn_request_telemetry_is_not_projected_as_child_spawn() -> None:
    request = project(
        {
            "schema_version": 1,
            "event_id": "spawn-request",
            "id": "spawn-request",
            "observed_at": "2026-08-11T00:00:00Z",
            "run_seq": 6,
            "type": "agent.spawned",
            "agent_id": "root",
            "run_id": "run-1",
            "session_id": "session-1",
            "lifecycle_state": "emitted",
            "data": {
                "agent_type": "auto",
                "slug": "shadow-slice-a",
                "task_summary": "create a file",
            },
        }
    )

    assert not _is_concrete_spawn(request)


def test_concrete_spawn_lifecycle_row_is_projected() -> None:
    spawn = project(
        {
            "schema_version": 1,
            "event_id": "spawn",
            "id": "spawn",
            "observed_at": "2026-08-11T00:00:00Z",
            "run_seq": 4,
            "type": "agent.spawned",
            "agent_id": "root",
            "run_id": "run-1",
            "session_id": "session-1",
            "lifecycle_state": "emitted",
            "data": {
                "agent_type": "Claude",
                "branch": "main.shadow-slice-a-claude",
                "child_agent": "shadow-slice-a-claude",
                "spawn_type": "subtree",
            },
        }
    )

    assert _is_concrete_spawn(spawn)
