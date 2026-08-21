"""Configured idle-wave heartbeat coverage."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.loop.heartbeat import HeartbeatConfig, HeartbeatError, _poll_workers, heartbeat_once
from tl_loop.state.schema import (
    ParkCause,
    RunState,
    SliceStatus,
)
from tl_loop.state.store import RunStore, create


@dataclass
class HeartbeatTransport:
    """Effect transport with deterministic liveness and watcher responses."""

    pane_alive: bool = True
    calls: list[tuple[str, JsonObject]] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, arguments))
        if tool_name == "poll_workers":
            requested = arguments.get("agents")
            worker_name = (
                requested[0]
                if isinstance(requested, list) and requested and isinstance(requested[0], str)
                else "slice-a"
            )
            return cast(
                JsonObject,
                {
                    "success": True,
                    "result": {
                        "workers": [
                            {
                                "name": worker_name,
                                "pane_alive": self.pane_alive,
                                "lifecycle_status": "ACTIVE",
                            }
                        ],
                        "dead_workers": [],
                    },
                },
            )
        if tool_name == "watcher_pr_state":
            return cast(
                JsonObject,
                {
                    "success": True,
                    "result": {
                        "head_sha": "head-new",
                        "review_state": "approved",
                        "ci_status": "success",
                    },
                },
            )
        if tool_name == "chainlink_issue_create":
            return cast(JsonObject, {"success": True, "result": {"issue_id": 7042}})
        return cast(JsonObject, {"success": True, "result": {}})


def test_idle_heartbeat_waits_for_configured_interval_without_polling(
    tmp_path: Path,
) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=9.0)
    transport = HeartbeatTransport()
    effects = EffectClient(transport)
    config = HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0)

    before_due = heartbeat_once(state, store, effects, config, now=13.9)

    assert before_due.fired is False
    assert transport.calls == []

    due = heartbeat_once(state, store, effects, config, now=14.0)

    assert due.fired is True
    assert [name for name, _ in transport.calls] == ["poll_workers"]


def test_silently_dead_worker_is_parked(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    transport = HeartbeatTransport(pane_alive=False)
    effects = EffectClient(transport)

    result = heartbeat_once(
        state,
        store,
        effects,
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
    )

    parked = result.state.slices["slice-a"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.STALL_DETECTED
    assert result.parked_slice_ids == ("slice-a",)
    assert [event.kind for event in result.events] == ["worker.dead"]
    assert [name for name, _ in transport.calls] == [
        "poll_workers",
        "chainlink_issue_create",
        "emit_controller_event",
        "emit_controller_event",
    ]


def test_silent_live_worker_stall_is_observational(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    transport = HeartbeatTransport(pane_alive=True)

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=1.0),
        now=10.0,
    )

    observed = result.state.slices["slice-a"]
    assert observed.status is SliceStatus.SPAWNED
    assert observed.park_cause is None
    assert result.parked_slice_ids == ()
    assert [event.kind for event in result.events] == ["wave.stalled"]
    assert result.events[0].payload["action"] == "observe"


def test_goal_deadline_is_observational_for_live_work(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    transport = HeartbeatTransport(pane_alive=True)

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=2000.0,
    )

    assert result.state.slices["slice-a"].status is SliceStatus.SPAWNED
    assert result.parked_slice_ids == ()
    assert result.events[0].kind == "goal.deadline_elapsed"


def test_repeated_heartbeat_reconciliation_is_idempotent(tmp_path: Path) -> None:
    store, state = _state(
        tmp_path,
        status="in_review",
        heartbeat_at=0.0,
        pr_number=42,
        reviewed_head="head-old",
    )
    transport = HeartbeatTransport()
    effects = EffectClient(transport)
    config = HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0)

    first = heartbeat_once(state, store, effects, config, now=10.0)
    second = heartbeat_once(first.state, store, effects, config, now=20.0)

    reconciled = first.state.slices["slice-a"]
    assert reconciled.verdict is None
    assert reconciled.reviewed_head == "head-new"
    assert [event.kind for event in first.events] == ["pr.updated"]
    assert second.events == ()
    assert second.parked_slice_ids == ()
    assert second.state.budgets == first.state.budgets
    assert [name for name, _ in transport.calls] == [
        "poll_workers",
        "watcher_pr_state",
        "poll_workers",
        "watcher_pr_state",
    ]


def test_poll_workers_uses_persisted_runtime_identity(tmp_path: Path) -> None:
    _, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    transport = HeartbeatTransport()

    _poll_workers(EffectClient(transport), (state.slices["slice-a"],))

    poll_arguments = transport.calls[0][1]
    assert poll_arguments["agents"] == ["agent-slice-a"]


def test_poll_workers_rejects_ambiguous_runtime_identity(tmp_path: Path) -> None:
    _, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    first = state.slices["slice-a"]
    second = replace(first, id="slice-b", dispatch_agent_id=first.dispatch_agent_id)

    with pytest.raises(HeartbeatError, match="ambiguous runtime agent identity"):
        _poll_workers(EffectClient(HeartbeatTransport()), (first, second))


def _state(
    tmp_path: Path,
    *,
    status: str,
    heartbeat_at: float,
    pr_number: int | None = None,
    reviewed_head: str | None = None,
) -> tuple[RunStore, RunState]:
    record: dict[str, object] = {
        "id": "slice-a",
        "status": status,
        "paths": ["src/slice_a.py"],
        "depends_on": [],
        "base_ref": "main",
        "test_plan": ["just tl-loop-test"],
        "agent_type": "codex",
        "model": "gpt-test",
        "branch": "task/slice-a",
        "worktree": None,
        "pr_number": pr_number,
        "reviewed_head": reviewed_head,
        "attempts": 1,
        "verdict": None,
    }
    if status == "spawned":
        record.update(
            {
                "dispatch_intent_id": "heartbeat-intent-1",
                "dispatch_agent_id": "agent-slice-a",
                "dispatch_authoritative_event_seq": 1,
            }
        )
    root_spec = {
        "fsm": {"phase": "tl_planning", "waiting": []},
        "slices": {"slice-a": record},
        "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        "goals": {
            "objective": "finish the wave",
            "deadline": 1000.0,
            "completion_predicate": "all slices terminal",
            "last_heartbeat_at": heartbeat_at,
            "last_progress_at": heartbeat_at,
        },
    }
    create("heartbeat-test", root_spec, root_dir=tmp_path)
    store = RunStore("heartbeat-test", root_dir=tmp_path)
    return store, store.load()
