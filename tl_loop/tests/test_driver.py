"""Contract tests for the bounded active/shadow TL driver."""

from __future__ import annotations

import queue
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.events.envelope import EventEnvelope, project
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.driver import (
    DepthLimitExceeded,
    SubTLTask,
    LoopLimitExceeded,
    TLLoopConfig,
    WorkerTask,
    WorkPlan,
    run_tl_loop,
    tl_run,
)
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty
from tl_loop.select.model import ModelCatalog
from tl_loop.select.policy import validate_policy
from tl_loop.state.schema import BudgetLedger

from tl_loop.state.store import load as load_state

@dataclass
class SyntheticQueue:
    events: list[EventEnvelope]
    acknowledged: list[int] = field(default_factory=list)

    def get(self, timeout: float | None = None) -> EventEnvelope:
        del timeout
        if not self.events:
            raise queue.Empty
        return self.events.pop(0)

    def acknowledge(self, event: EventEnvelope) -> int:
        assert event.run_seq is not None
        self.acknowledged.append(event.run_seq)
        return event.run_seq


@dataclass
class RecordingTransport:
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
        return {"success": True, "result": None}


def test_active_loop_dispatches_direct_children_and_merges_leaf(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport()
    source = SyntheticQueue(_lifecycle_events("active-run"))
    result = run_tl_loop(
        "active-run",
        _plan(),
        source,
        EffectClient(transport),
        config=_config(),
        root_dir=tmp_path,
    )

    assert [name for name, _ in transport.calls] == [
        "spawn_worker",
        "spawn_leaf",
        "merge_pr",
    ]
    assert "fork_wave" not in {name for name, _ in transport.calls}
    assert [intent.operation for intent in result.effects] == [
        "spawn_worker",
        "spawn_leaf",
        "merge_pr",
    ]
    assert all(intent.executed for intent in result.effects)
    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert result.final_state.events.last_consumed_offset == 5
    assert result.final_state.slices["worker-a"].status.value == "merged"
    assert result.final_state.slices["leaf-a"].status.value == "merged"
    assert source.acknowledged == [1, 2, 3, 4, 5]


def test_tl_run_integrates_selection_model_and_atomic_charge(tmp_path: Path) -> None:
    transport = RecordingTransport()
    run_id = "selector-run"
    source = SyntheticQueue(_lifecycle_events(run_id))
    policy = validate_policy(_selector_policy())
    config = TLLoopConfig(
        source=source,
        effects=EffectClient(transport),
        root_dir=tmp_path,
        policy=policy,
        capabilities=CapabilityMap(
            {"codex/gpt-luna": Difficulty.STANDARD, "claude/sonnet": Difficulty.HARD}
        ),
        catalog=ModelCatalog.from_fixture(
            Path(__file__).parent / "fixtures" / "model_catalog.json"
        ),
        requested_model="gpt-5.5",
        max_workers=1,
        max_leaves=1,
        max_events=5,
        poll_interval=0.001,
        idle_timeout=0.1,
    )

    result = tl_run({"run_id": run_id, "plan": _plan()}, config, BudgetLedger(0, 0))

    assert [name for name, _ in transport.calls] == [
        "spawn_worker",
        "spawn_leaf",
        "merge_pr",
    ]
    assert transport.calls[0][1]["agent_type"] == "codex/gpt-luna"
    assert result.final_state.budgets.role_reserved == {"worker": 500}
    assert result.final_state.budgets.harness_reserved == {"codex/gpt-luna": 500}
    assert result.final_state.slices["worker-a"].model == "gpt-5.5"
    assert result.final_state.fsm.phase is TLPhase.TLDone


def test_tl_run_width_gate_dispatches_next_ready_slice_after_completion(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport()
    run_id = "width-run"
    source = SyntheticQueue(_serial_worker_events(run_id))
    policy = validate_policy(_selector_policy())
    config = TLLoopConfig(
        source=source,
        effects=EffectClient(transport),
        root_dir=tmp_path,
        policy=policy,
        capabilities=CapabilityMap(
            {"codex/gpt-luna": Difficulty.STANDARD, "claude/sonnet": Difficulty.HARD}
        ),
        max_workers=2,
        max_leaves=0,
        max_parallel_slices=1,
        max_events=5,
        poll_interval=0.001,
        idle_timeout=0.1,
    )
    plan = WorkPlan(
        workers=(
            WorkerTask("worker-a", "first"),
            WorkerTask("worker-b", "second"),
        )
    )

    result = tl_run({"run_id": run_id, "plan": plan}, config, BudgetLedger(0, 0))

    assert [name for name, _ in transport.calls] == ["spawn_worker", "spawn_worker"]
    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert result.final_state.budgets.role_reserved == {"worker": 500}


def test_shadow_loop_uses_the_same_driver_without_mutating_transport(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport()
    source = SyntheticQueue(_lifecycle_events("shadow-run"))
    result = run_tl_loop(
        "shadow-run",
        _plan(),
        source,
        ReadOnlyEffectClient(EffectClient(transport)),
        config=_config(active=False),
        root_dir=tmp_path,
    )

    assert transport.calls == []
    assert [intent.operation for intent in result.effects] == [
        "spawn_worker",
        "spawn_leaf",
        "merge_pr",
    ]
    assert not any(intent.executed for intent in result.effects)
    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert result.final_state.events.last_consumed_offset == 5


def test_canonical_completion_and_parent_notification_are_idempotent(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport()
    source = SyntheticQueue(_canonical_lifecycle("canonical-run"))
    result = run_tl_loop(
        "canonical-run",
        _plan(),
        source,
        EffectClient(transport),
        config=TLLoopConfig(
            max_workers=1,
            max_leaves=1,
            max_events=6,
            poll_interval=0.001,
            idle_timeout=0.1,
        ),
        root_dir=tmp_path,
    )

    assert [name for name, _ in transport.calls] == [
        "spawn_worker",
        "spawn_leaf",
        "merge_pr",
    ]
    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert source.acknowledged == [1, 2, 3, 4, 5, 6]


def test_loop_rejects_a_plan_over_its_worker_ceiling(tmp_path: Path) -> None:
    source = SyntheticQueue([])
    with pytest.raises(LoopLimitExceeded, match="max_workers"):
        run_tl_loop(
            "bounded-run",
            WorkPlan.from_mapping(
                {"workers": [{"name": "worker-a", "task": "bounded"}]}
            ),
            source,
            EffectClient(RecordingTransport()),
            config=TLLoopConfig(max_workers=0),
            root_dir=tmp_path,
        )


def test_loop_rejects_an_event_stream_over_its_event_ceiling(tmp_path: Path) -> None:
    source = SyntheticQueue(_lifecycle_events("event-bounded-run"))
    with pytest.raises(LoopLimitExceeded, match="event limit"):
        run_tl_loop(
            "event-bounded-run",
            _plan(),
            source,
            EffectClient(RecordingTransport()),
            config=TLLoopConfig(
                max_events=1,
                poll_interval=0.001,
                idle_timeout=0.1,
            ),
            root_dir=tmp_path,
        )


def _selector_policy() -> dict[str, object]:
    role = {
        "allow": ["codex/gpt-luna", "claude/sonnet"],
        "cost_rank": {"codex/gpt-luna": 1, "claude/sonnet": 2},
        "token_budget": 120000,
        "per_harness_budget": {"codex/gpt-luna": 80000, "claude/sonnet": 40000},
        "escalate_after_attempts": 1,
    }
    return {"roles": {"tl": dict(role), "worker": dict(role), "reviewer": dict(role)}}


def _plan() -> WorkPlan:
    return WorkPlan.from_mapping(
        {
            "workers": [{"name": "worker-a", "task": "inspect the repository"}],
            "leaves": [
                {
                    "name": "leaf-a",
                    "task": "implement the requested change",
                    "agent_type": "codex",
                    "verify": ["just tl-loop-test"],
                }
            ],
        }
    )


def _config(*, active: bool = True) -> TLLoopConfig:
    return TLLoopConfig(
        active=active,
        max_workers=1,
        max_leaves=1,
        max_events=5,
        poll_interval=0.001,
        idle_timeout=0.1,
    )


def _lifecycle_events(run_id: str) -> list[EventEnvelope]:
    return [
        _event(1, "child_spawned", "worker-a", run_id=run_id),
        _event(2, "child_spawned", "leaf-a", run_id=run_id),
        _event(3, "child_completed", "worker-a", run_id=run_id),
        _event(4, "child_completed", "leaf-a", pr_number=42, run_id=run_id),
        _event(5, "all_children_done", run_id=run_id),
    ]


def _canonical_lifecycle(run_id: str) -> list[EventEnvelope]:
    return [
        _canonical_event(1, "agent.spawned", "worker-a", run_id),
        _canonical_event(2, "agent.spawned", "leaf-a", run_id),
        _canonical_event(3, "agent.completed", "worker-a", run_id),
        _canonical_event(4, "agent.notify_parent", "worker-a", run_id),
        _canonical_event(5, "agent.completed", "leaf-a", run_id, pr_number=42),
        _event(6, "all_children_done", run_id=run_id),
    ]


def _serial_worker_events(run_id: str) -> list[EventEnvelope]:
    return [
        _event(1, "child_spawned", "worker-a", run_id=run_id),
        _event(2, "child_completed", "worker-a", run_id=run_id),
        _event(3, "child_spawned", "worker-b", run_id=run_id),
        _event(4, "child_completed", "worker-b", run_id=run_id),
        _event(5, "all_children_done", run_id=run_id),
    ]


def _canonical_event(
    run_seq: int,
    event_type: str,
    slug: str,
    run_id: str,
    *,
    pr_number: int | None = None,
) -> EventEnvelope:
    data: dict[str, object] = {}
    if event_type == "agent.spawned":
        data.update(
            {
                "child_agent": slug,
                "agent_type": "codex",
                "branch": f"main.{slug}",
            }
        )
    else:
        data.update({"status": "success", "message": "completed"})
    if pr_number is not None:
        data["pr_number"] = pr_number
    raw = {
        "schema_version": 1,
        "event_id": f"canonical-{run_seq}",
        "id": f"canonical-{run_seq}",
        "event_time": "2026-08-11T00:00:00Z",
        "observed_at": "2026-08-11T00:00:00Z",
        "run_seq": run_seq,
        "type": event_type,
        "agent_id": slug,
        "run_id": run_id,
        "session_id": "session-1",
        "lifecycle_state": "observed",
        "data": data,
    }
    return project(cast(dict[str, object], raw))


def _event(
    run_seq: int,
    kind: str,
    slug: str | None = None,
    *,
    pr_number: int | None = None,
    run_id: str,
) -> EventEnvelope:
    shadow_event: dict[str, object] = {"kind": kind}
    if slug is not None:
        shadow_event["slug"] = slug
    if kind == "child_spawned":
        shadow_event["branch"] = f"main.{slug}"
        shadow_event["agent_type"] = "codex"
    data: dict[str, object] = {"shadow_event": shadow_event}
    if pr_number is not None:
        data["pr_number"] = pr_number
    raw = {
        "schema_version": 1,
        "event_id": f"event-{run_seq}",
        "id": f"event-{run_seq}",
        "event_time": "2026-08-11T00:00:00Z",
        "observed_at": "2026-08-11T00:00:00Z",
        "run_seq": run_seq,
        "type": "agent.notify_parent",
        "agent_id": slug,
        "run_id": run_id,
        "session_id": "session-1",
        "lifecycle_state": "observed",
        "data": data,
    }
    return project(cast(dict[str, object], raw))



def test_recursive_sub_tls_isolate_state_and_branch_coordinates(tmp_path: Path) -> None:
    transport = RecordingTransport()
    grand_source = SyntheticQueue([])
    child_source = SyntheticQueue([])
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "child",
                WorkPlan(
                    sub_tls=(
                        SubTLTask("grandchild", WorkPlan(), source=grand_source),
                    )
                ),
                source=child_source,
            ),
        )
    )

    result = run_tl_loop(
        "recursive-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=_config(),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert set(result.final_state.slices) == {"child"}
    assert result.final_state.slices["child"].branch == "main.child"
    assert result.final_state.slices["child"].base_ref == "main"
    child = load_state(tmp_path / "recursive-run" / "child" / "run.json")
    assert set(child.slices) == {"grandchild"}
    assert child.owner_branch == "main.child"
    assert child.parent_branch == "main"
    assert child.slices["grandchild"].branch == "main.child.grandchild"
    assert child.slices["grandchild"].base_ref == "main.child"
    assert transport.calls == []



def test_recursive_depth_ceiling_parks_schedule_deadlock(tmp_path: Path) -> None:
    with pytest.raises(DepthLimitExceeded):
        run_tl_loop(
            "depth-run",
            WorkPlan(sub_tls=(SubTLTask("child", WorkPlan(), source=SyntheticQueue([])),)),
            SyntheticQueue([]),
            EffectClient(RecordingTransport()),
            config=TLLoopConfig(max_depth=0, poll_interval=0.001, idle_timeout=0.1),
            root_dir=tmp_path,
        )

    state = load_state(tmp_path / "depth-run" / "run.json")
    assert state.fsm.phase is TLPhase.TLFailed
    assert state.slices["child"].status.value == "parked"
__all__ = [

    "RecordingTransport",
    "SyntheticQueue",
    "test_active_loop_dispatches_direct_children_and_merges_leaf",
]
