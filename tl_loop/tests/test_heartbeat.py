"""Configured idle-wave heartbeat coverage."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import pytest

from tl_loop.client.effects import EffectClient, ToolResult, ToolUnavailableError
from tl_loop.client.transport import JsonObject
from tl_loop.fsm.recovery import begin_recovery
from tl_loop.loop.heartbeat import (
    HeartbeatConfig,
    HeartbeatError,
    _poll_workers,
    _result_object,
    _classify_missing_handoff,
    heartbeat_once,
)
from tl_loop.state.schema import (
    DeadlineLedger,
    ParkCause,
    RunState,
    SliceStatus,
)
from tl_loop.state.store import RunStore, create


@dataclass
class HeartbeatTransport:
    """Effect transport with deterministic liveness and watcher responses."""

    pane_alive: bool = True
    worker_present: bool = True
    report_missing_agents: bool = False
    pr_state: str = "open"
    merged: bool = False
    head_reachable: bool = True
    publication_ownership_verified: bool | None = None
    publication_ownership_error: str = ""
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
                        "workers": (
                            [
                                {
                                    "name": worker_name,
                                    "pane_alive": self.pane_alive,
                                    "lifecycle_status": "ACTIVE",
                                }
                            ]
                            if self.worker_present
                            else []
                        ),
                        "dead_workers": [],
                        "missing_agents": (
                            [worker_name]
                            if self.report_missing_agents and not self.worker_present
                            else []
                        ),
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
                        "found": True,
                        "pr_state": self.pr_state,
                        "merged": self.merged,
                        "head_reachable": self.head_reachable,
                        "evidence_error": (
                            "pr_head_unreachable: object missing" if not self.head_reachable else ""
                        ),
                        "publication_ownership_verified": self.publication_ownership_verified,
                        "publication_ownership_error": self.publication_ownership_error,
                    },
                },
            )
        if tool_name == "resolve_live_pr_for_slice":
            return cast(
                JsonObject,
                {
                    "success": True,
                    "result": {
                        "slice_id": arguments.get("slice_id", "slice-a"),
                        "resolution": "live",
                        "pr_number": 42,
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

    before_due = heartbeat_once(state, store, effects, config, now=13.9, project_root=tmp_path)

    assert before_due.fired is False
    assert transport.calls == []

    due = heartbeat_once(state, store, effects, config, now=14.0, project_root=tmp_path)

    assert due.fired is True
    assert [name for name, _ in transport.calls] == [
        "poll_workers",
        "resolve_live_pr_for_slice",
        "watcher_pr_state",
    ]


def test_result_object_classifies_typed_tool_unavailability_without_message_matching() -> None:
    result = ToolResult(
        raw={
            "success": False,
            "error": "arbitrary deployment wording",
            "error_kind": "tool_unavailable",
        },
        success=False,
        result=None,
        error="arbitrary deployment wording",
        error_kind="tool_unavailable",
    )

    with pytest.raises(ToolUnavailableError, match="arbitrary deployment wording"):
        _result_object(result, "resolve_live_pr_for_slice", target="slice-a")


def test_result_object_keeps_domain_failure_as_heartbeat_error() -> None:
    result = ToolResult(
        raw={"success": False, "error": "domain rejected"},
        success=False,
        result=None,
        error="domain rejected",
    )

    with pytest.raises(HeartbeatError, match="domain rejected"):
        _result_object(result, "resolve_live_pr_for_slice", target="slice-a")


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
        project_root=tmp_path,
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
        project_root=tmp_path,
    )

    observed = result.state.slices["slice-a"]
    assert observed.status is SliceStatus.SPAWNED
    assert observed.park_cause is None
    assert result.parked_slice_ids == ()
    assert [event.kind for event in result.events] == ["pr.updated"]


def test_goal_deadline_is_observational_for_live_work(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    transport = HeartbeatTransport(pane_alive=True)

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=2000.0,
        project_root=tmp_path,
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

    first = heartbeat_once(state, store, effects, config, now=10.0, project_root=tmp_path)
    second = heartbeat_once(first.state, store, effects, config, now=20.0, project_root=tmp_path)

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


def test_heartbeat_parks_closed_unmerged_pr(tmp_path: Path) -> None:
    store, state = _state(
        tmp_path,
        status="in_review",
        heartbeat_at=0.0,
        pr_number=42,
        reviewed_head="head-new",
    )
    result = heartbeat_once(
        state,
        store,
        EffectClient(HeartbeatTransport(pr_state="closed")),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    parked = result.state.slices["slice-a"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.PR_CLOSED_UNMERGED
    assert result.parked_slice_ids == ("slice-a",)
    assert result.events[0].kind == "pr.closed_unmerged"


def test_heartbeat_parks_unreachable_pr_head_without_raising(tmp_path: Path) -> None:
    store, state = _state(
        tmp_path,
        status="in_review",
        heartbeat_at=0.0,
        pr_number=42,
        reviewed_head="head-new",
    )
    result = heartbeat_once(
        state,
        store,
        EffectClient(HeartbeatTransport(head_reachable=False)),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    parked = result.state.slices["slice-a"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.PR_HEAD_UNREACHABLE
    assert result.events[0].kind == "pr.head_unreachable"


def test_heartbeat_parks_unresolved_publication_ownership(tmp_path: Path) -> None:
    store, state = _state(
        tmp_path,
        status="in_review",
        heartbeat_at=0.0,
        pr_number=42,
        reviewed_head="head-new",
    )
    result = heartbeat_once(
        state,
        store,
        EffectClient(
            HeartbeatTransport(
                publication_ownership_verified=False,
                publication_ownership_error="invocation succession is missing",
            )
        ),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    parked = result.state.slices["slice-a"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED


def test_poll_workers_uses_persisted_runtime_identity(tmp_path: Path) -> None:
    _, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    transport = HeartbeatTransport()

    snapshot = _poll_workers(EffectClient(transport), (state.slices["slice-a"],))

    poll_arguments = transport.calls[0][1]
    assert poll_arguments["agents"] == ["agent-slice-a"]
    assert snapshot.missing_agents == frozenset()


def test_poll_workers_rejects_ambiguous_runtime_identity(tmp_path: Path) -> None:
    _, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    first = state.slices["slice-a"]
    second = replace(first, id="slice-b", dispatch_agent_id=first.dispatch_agent_id)

    with pytest.raises(HeartbeatError, match="ambiguous runtime agent identity"):
        _poll_workers(EffectClient(HeartbeatTransport()), (first, second))


def test_missing_worker_row_is_persisted_but_not_failed(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    transport = HeartbeatTransport(worker_present=False)

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    observed = result.state.slices["slice-a"]
    assert observed.status is SliceStatus.SPAWNED
    assert observed.reconciliation == {
        "confirmed_stage": "worker_row_missing",
        "authoritative_evidence": [],
        "missing_evidence": ["worker.row"],
        "conflicts": [],
        "next_action": "continue_observing",
    }
    assert [event.kind for event in result.events] == ["worker.missing", "pr.updated"]
    assert [name for name, _ in transport.calls] == [
        "poll_workers",
        "emit_controller_event",
        "resolve_live_pr_for_slice",
        "watcher_pr_state",
    ]


def test_missing_worker_identity_is_recorded_in_reconciliation_evidence(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    transport = HeartbeatTransport(worker_present=False, report_missing_agents=True)

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    assert result.events[0].kind == "worker.missing"
    assert result.events[0].payload["poll_workers_missing"] is True


def test_missing_worker_row_after_invocation_finished_enters_recovery_once(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    invocation_dir = tmp_path / ".exo" / "agents" / "agent-slice-a"
    invocation_dir.mkdir(parents=True)
    (invocation_dir / "invocation.json").write_text(
        json.dumps(
            {
                "invocation_id": "inv-1",
                "runtime": "opencode",
                "trigger": "spawn",
                "started_at": 1,
                "ended_at": 2,
                "status": "killed",
                "exit_code": None,
                "generation": 3,
                "runtime_agent_id": "agent-slice-a",
                "slice_id": "slice-a",
                "branch": "main.slice-a",
                "worktree": ".exo/worktrees/agent-slice-a",
                "exit_reason": "tmux_target_exited_without_exit_marker",
                "exit_classification": "missing_exit_marker",
                "stderr_tail": "worker crashed",
            }
        ),
        encoding="utf-8",
    )
    transport = HeartbeatTransport(worker_present=False)

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    recovered = result.state.slices["slice-a"]
    assert recovered.status is SliceStatus.SPAWNED
    assert recovered.park_cause is None
    assert recovered.recovery is not None
    assert recovered.recovery.cause == ParkCause.HUMAN_DECISION_REQUIRED.value
    assert recovered.recovery.phase.value == "diagnosing"
    assert result.parked_slice_ids == ()
    assert result.events[0].kind == "worker.terminal_reconciled"
    assert result.events[0].payload["generation"] == 3
    assert result.events[0].payload["stderr_tail"] == "worker crashed"
    assert result.events[0].payload["guidance_required"] is True
    assert result.events[0].payload["attempt"] == 1
    event_calls = [
        arguments
        for name, arguments in transport.calls
        if name == "emit_controller_event"
        and arguments.get("event_type") == "worker.terminal_reconciled"
    ]
    assert len(event_calls) == 1
    blocked_calls = [
        arguments
        for name, arguments in transport.calls
        if name == "emit_controller_event" and arguments.get("event_type") == "agent.task_blocked"
    ]
    assert blocked_calls[0]["payload"]["outcome"] == "blocked"
    assert blocked_calls[0]["payload"]["cause"] == "human_decision_required"


def test_late_authoritative_handoff_wins_over_missing_worker_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    monkeypatch.setattr(
        "tl_loop.loop.heartbeat._read_authoritative_handoff",
        lambda *_args, **_kwargs: "agent.completed",
    )

    result = heartbeat_once(
        state,
        store,
        EffectClient(HeartbeatTransport(worker_present=False)),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    assert result.state.slices["slice-a"].status is SliceStatus.SPAWNED
    assert not any(event.kind == "worker.terminal_reconciled" for event in result.events)


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"classification": "terminal_exit", "exit_code": 0}, "clean_no_op"),
        (
            {"classification": "terminal_exit", "exit_code": 1, "has_uncommitted_changes": True},
            "dirty_worktree_no_commit",
        ),
        (
            {
                "classification": "terminal_exit",
                "exit_code": 0,
                "has_commit": True,
                "has_pr": False,
            },
            "commit_no_pr",
        ),
        ({"exit_code": None}, "missing_exit_marker"),
        ({"classification": "worker_crashed", "exit_code": 1}, "worker_crashed"),
        ({"classification": "terminal_exit", "exit_code": 1}, "unknown"),
    ],
)
def test_missing_handoff_classification_is_explicit(
    context: dict[str, object], expected: str
) -> None:
    assert _classify_missing_handoff(context) == expected


def test_missing_worker_evidence_uses_project_root_not_tl_state_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    state_root = project_root / ".exo" / "tl-loop"
    state_root.mkdir(parents=True)
    store, state = _state(state_root, status="spawned", heartbeat_at=0.0)
    invocation_dir = project_root / ".exo" / "agents" / "agent-slice-a"
    invocation_dir.mkdir(parents=True)
    (invocation_dir / "invocation.json").write_text(
        json.dumps(
            {
                "invocation_id": "inv-project-root",
                "slice_id": "slice-a",
                "status": "killed",
                "ended_at": 2,
                "exit_code": None,
                "exit_classification": "missing_exit_marker",
            }
        ),
        encoding="utf-8",
    )

    result = heartbeat_once(
        state,
        store,
        EffectClient(HeartbeatTransport(worker_present=False)),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=project_root,
    )

    recovered = result.state.slices["slice-a"]
    assert recovered.status is SliceStatus.SPAWNED
    assert recovered.recovery is not None
    assert recovered.recovery.cause == ParkCause.HUMAN_DECISION_REQUIRED.value
    assert result.events[0].payload["invocation_id"] == "inv-project-root"


def test_task_budget_exceeded_is_journaled_killed_and_parked(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    current = state.slices["slice-a"]
    updated = replace(
        current,
        dispatch_started_at=1.0,
        task_timeout_seconds=5.0,
        task_timeout_source="slice",
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": updated},
        state.budgets,
        state.events.last_consumed_offset,
    )
    transport = HeartbeatTransport()

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    parked = result.state.slices["slice-a"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.TASK_BUDGET_EXCEEDED
    assert result.events[0].kind == "agent.task_budget_exceeded"
    assert result.events[0].payload["budget_source_layer"] == "slice"
    assert [name for name, _ in transport.calls].count("close_worker_pane") == 1
    journal = json.loads((store.run_dir / "action-journal.json").read_text(encoding="utf-8"))
    assert journal[0]["status"] == "confirmed"


def test_authoritative_terminal_wins_over_budget_in_same_heartbeat(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    current = state.slices["slice-a"]
    updated = replace(
        current,
        dispatch_started_at=1.0,
        task_timeout_seconds=5.0,
        task_timeout_source="slice",
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": updated},
        state.budgets,
        state.events.last_consumed_offset,
    )
    invocation_dir = tmp_path / ".exo" / "agents" / "agent-slice-a"
    invocation_dir.mkdir(parents=True)
    (invocation_dir / "invocation.json").write_text(
        json.dumps({"slice_id": "slice-a", "status": "exited", "ended_at": 2}),
        encoding="utf-8",
    )
    transport = HeartbeatTransport()

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    assert result.state.slices["slice-a"].status is SliceStatus.SPAWNED
    assert not any(name == "close_worker_pane" for name, _ in transport.calls)


def test_zero_task_budget_disables_enforcement(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    current = state.slices["slice-a"]
    updated = replace(
        current,
        dispatch_started_at=1.0,
        task_timeout_seconds=None,
        task_timeout_source="slice",
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": updated},
        state.budgets,
        state.events.last_consumed_offset,
    )
    transport = HeartbeatTransport()

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10000.0,
        project_root=tmp_path,
    )

    assert result.state.slices["slice-a"].status is SliceStatus.SPAWNED
    assert not any(name == "close_worker_pane" for name, _ in transport.calls)


def test_recovery_wait_is_not_charged_to_execution_budget(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    current = state.slices["slice-a"]
    recovered = replace(
        current,
        dispatch_started_at=10.0,
        task_timeout_seconds=5.0,
        task_timeout_source="slice",
        recovery=begin_recovery(
            cause="external_dependency",
            owner_run_id=state.run_id,
            slice_attempt=1,
            owner_agent_id="agent-slice-a",
            entered_at=20.0,
        ),
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": recovered},
        state.budgets,
        state.events.last_consumed_offset,
    )

    transport = HeartbeatTransport()
    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=100.0,
        project_root=tmp_path,
    )

    observed = result.state.slices["slice-a"]
    assert observed.status is SliceStatus.SPAWNED
    assert observed.recovery is not None
    assert observed.deadline_ledger == DeadlineLedger(
        execution_deadline_at=15.0,
        recovery_deadline_at=1820.0,
        run_deadline_at=1000.0,
        suspended_at=20.0,
        execution_seconds=10.0,
        recovery_wait_seconds=80.0,
    )
    assert not any(name == "close_worker_pane" for name, _ in transport.calls)


def test_deadline_ledger_is_durable_and_restart_safe(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    current = state.slices["slice-a"]
    updated = replace(
        current,
        dispatch_started_at=10.0,
        deadline_ledger=DeadlineLedger(
            execution_deadline_at=20.0,
            recovery_deadline_at=None,
            run_deadline_at=1000.0,
            suspended_at=None,
            execution_seconds=7.5,
            recovery_wait_seconds=0.0,
        ),
    )
    checkpointed = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": updated},
        state.budgets,
        state.events.last_consumed_offset,
    )

    restored = store.load().slices["slice-a"]
    assert checkpointed.slices["slice-a"].deadline_ledger == updated.deadline_ledger
    assert restored.deadline_ledger == updated.deadline_ledger


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
