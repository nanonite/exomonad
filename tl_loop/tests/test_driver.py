"""Contract tests for the bounded active/shadow TL driver."""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import JsonObject, TransportClient
from tl_loop.events.envelope import EventEnvelope, project
from tl_loop.events.queue import LedgerQueue
from tl_loop.events.reader import LedgerReader
from tl_loop.fsm.event import PRFiled, PRUpdated
from tl_loop.fsm.phase import TLPhase, TLPlanning
from tl_loop.loop.driver import (
    DepthLimitExceeded,
    EventDiagnostics,
    LoopCancelled,
    LoopLimitExceeded,
    SubTLTask,
    TLLoopConfig,
    TLLoopError,
    TLRunResult,
    WorkerTask,
    WorkPlan,
    _event_belongs_to_plan,
    _initial_slices,
    _record_review_event,
    _repair_model,
    _route_ci_event,
    _route_review_event,
    _run_sub_tl_batch,
    _supervise_live_sub_tl,
    run_tl_loop,
    tl_run,
)
from tl_loop.loop.shadow import TLEventDecoder, _update_slices
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest, RlmResponse
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty
from tl_loop.select.model import ModelCatalog
from tl_loop.select.policy import validate_policy
from tl_loop.state.schema import (
    BudgetLedger,
    GateState,
    GateStatus,
    IntegrationCandidateState,
    IntegrationRuntimeState,
    OrderedStageState,
    SliceState,
    SliceStatus,
    Verdict,
)
from tl_loop.state.store import RunStore, create
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


def test_event_diagnostics_exposes_non_terminal_timing() -> None:
    started_at = time.time() - 2.0
    diagnostics = EventDiagnostics(
        controller_started_at=started_at,
        task_started_at={"leaf-a": started_at + 0.1},
        last_authoritative_event_seq=7,
        last_observed_progress_at=started_at + 1.0,
    )

    snapshot = diagnostics.snapshot()

    assert snapshot["controller_started_at"] == started_at
    assert cast(float, snapshot["elapsed_seconds"]) >= 2.0
    assert snapshot["task_started_at"] == {"leaf-a": started_at + 0.1}
    assert snapshot["last_authoritative_event_seq"] == 7
    assert snapshot["last_observed_progress_at"] == started_at + 1.0


@dataclass
class RecordingTransport:
    calls: list[tuple[str, JsonObject]] = field(default_factory=list)
    fail_observability: bool = False
    reject_spawns: bool = False
    spawned_agent_id: str | None = None
    listed_agents: list[JsonObject] = field(default_factory=list)
    next_controller_run_seq: int = 1000

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, arguments))
        if self.fail_observability and tool_name == "emit_controller_event":
            return {"success": False, "error": "ledger unavailable"}
        if self.reject_spawns and tool_name in {"spawn_worker", "spawn_leaf"}:
            return {"success": False, "error": "tmux launch rejected"}
        if tool_name in {"spawn_worker", "spawn_leaf"} and self.spawned_agent_id:
            return {"success": True, "result": {"agent_id": self.spawned_agent_id}}
        if tool_name == "list_agents":
            return {"success": True, "result": {"agents": self.listed_agents}}
        if tool_name == "emit_controller_event":
            self.next_controller_run_seq += 1
            return {
                "success": True,
                "result": {"event_id": "controller-event", "run_seq": self.next_controller_run_seq},
            }
        return {"success": True, "result": None}


def test_live_ordered_batch_uses_independent_durable_controllers(tmp_path: Path) -> None:
    root = tmp_path / "parent"
    source = SyntheticQueue([])
    tasks = (
        SubTLTask("alpha", WorkPlan(), source=source, order=1),
        SubTLTask("beta", WorkPlan(), source=SyntheticQueue([]), order=1),
    )
    config = TLLoopConfig(
        active=True,
        root_dir=root,
        run_id="parent",
        ledger_run_id="swarm-uuid",
    )
    effects = EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
    outcomes = _run_sub_tl_batch(
        tasks,
        config,
        source,
        effects,
        RunStore("parent", tmp_path),
        BudgetLedger(tokens=0, wall_seconds=0),
    )

    assert [phase for _, phase, _ in outcomes] == [TLPhase.TLDone, TLPhase.TLDone]
    assert all(child_state is not None for _, _, child_state in outcomes)
    assert RunStore("alpha", root).load().parent_run_id == "parent"
    assert RunStore("beta", root).load().parent_run_id == "parent"
    assert RunStore("alpha", root).load().ledger_run_id == "swarm-uuid"
    assert RunStore("beta", root).load().ledger_run_id == "swarm-uuid"


def test_live_waiting_child_is_not_terminated_by_elapsed_supervision() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True
            self.joins: list[float | None] = []
            self.terminated = False

        def join(self, timeout: float | None = None) -> None:
            self.joins.append(timeout)
            if len(self.joins) >= 2:
                self.alive = False

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False

    process = FakeProcess()
    child_store = SimpleNamespace(
        load=lambda: SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLWaiting)),
        record_exit_reason=lambda reason: pytest.fail(reason),
    )

    state = _supervise_live_sub_tl(
        process,
        child_store,
        TLLoopConfig(
            keep_alive_on_waiting=True,
            poll_interval=0.001,
        ),
    )

    assert state is not None
    assert process.joins == [0.05, 0.05]
    assert not process.terminated


def test_recursive_tl_waiting_child_is_not_marked_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def waiting_child(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del root_spec, config, budgets
        return SimpleNamespace(
            final_state=SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLWaiting), slices={})
        )

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", waiting_child)
    result = run_tl_loop(
        "waiting-child-parent",
        WorkPlan(sub_tls=(SubTLTask("waiting-child", WorkPlan(), order=1),)),
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=TLLoopConfig(
            active=True,
            keep_alive_on_waiting=False,
          max_parallel_slices=1,
          poll_interval=0.001,
      ),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLWaiting
    assert result.final_state.slices["waiting-child"].status is SliceStatus.SPAWNED


def test_live_waiting_child_is_terminated_on_explicit_cancellation() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True
            self.terminated = False

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False

    process = FakeProcess()
    reasons: list[str] = []
    cancel_event = threading.Event()
    cancel_event.set()
    child_store = SimpleNamespace(
        load=lambda: SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLWaiting)),
        record_exit_reason=reasons.append,
    )

    state = _supervise_live_sub_tl(
        process,
        child_store,
        TLLoopConfig(
            keep_alive_on_waiting=True,
            cancel_event=cancel_event,
        ),
    )

    assert state is not None
    assert process.terminated
    assert reasons == ["sub-TL controller cancelled explicitly"]


def test_exited_waiting_child_is_failed_as_process_loss() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True
            self.exitcode = 0

        def join(self, timeout: float | None = None) -> None:
            del timeout
            self.alive = False

        def is_alive(self) -> bool:
            return self.alive

    reasons: list[str] = []
    child_store = SimpleNamespace(
        load=lambda: SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLWaiting)),
        record_exit_reason=reasons.append,
    )

    state = _supervise_live_sub_tl(
        FakeProcess(),
        child_store,
        TLLoopConfig(poll_interval=0.001),
    )

    assert state is None
    assert reasons == ["sub-TL controller exited before authoritative resolution with code 0"]


@dataclass
class IntegrationTransport(RecordingTransport):
    snapshots: list[JsonObject] = field(default_factory=list)
    merge_response: JsonObject | None = None

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        if tool_name == "watcher_pr_state":
            self.calls.append((tool_name, arguments))
            snapshot = self.snapshots.pop(0)
            return {"success": True, "result": snapshot}
        if tool_name == "merge_pr" and self.merge_response is not None:
            self.calls.append((tool_name, arguments))
            return self.merge_response
        return super().call_tool(role, name, tool_name, arguments)


@dataclass
class OrderedIntegrationTransport(IntegrationTransport):
    """Deterministic Forgejo-shaped transport for recursive acceptance tests."""

    snapshot_history: list[JsonObject] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        if tool_name == "file_pr":
            self.calls.append((tool_name, arguments))
            title = str(arguments.get("title", ""))
            sub_tl_id = "alpha" if "alpha" in title else "beta"
            return {
                "success": True,
                "result": {
                    "pr_number": 101 if sub_tl_id == "alpha" else 102,
                    "head_sha": f"head-{sub_tl_id}",
                    "patch_digest": f"patch-{sub_tl_id}",
                    "base_sha": "base-main",
                },
            }
        if tool_name == "watcher_pr_state" and self.snapshots:
            self.snapshot_history.append(dict(self.snapshots[0]))
        return super().call_tool(role, name, tool_name, arguments)


def _effect_names(transport: RecordingTransport) -> list[str]:
    return [name for name, _ in transport.calls if name != "emit_controller_event"]


def _effect_operations(result: TLRunResult) -> list[str]:
    return [
        effect.operation for effect in result.effects if effect.operation != "emit_controller_event"
    ]


def _merge_decisions(transport: RecordingTransport) -> list[JsonObject]:
    return [
        cast(JsonObject, arguments["payload"])
        for name, arguments in transport.calls
        if name == "emit_controller_event" and arguments["event_type"] == "tl.merge_decided"
    ]


@dataclass
class ReviewRepairTransport(RecordingTransport):
    """Effect double that exposes the PR state required by compose_repair."""

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, arguments))
        if tool_name == "watcher_pr_state":
            return {
                "success": True,
                "result": {
                    "open": True,
                    "merged": False,
                    "head_branch": "main.leaf-a",
                    "head_sha": "head-a",
                },
            }
        return {"success": True, "result": None}


@dataclass
class ReviewBackend:
    responses: list[object]
    requests: list[RlmRequest] = field(default_factory=list)

    def complete(self, request: RlmRequest) -> object:
        self.requests.append(request)
        return self.responses.pop(0)


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

    assert _effect_names(transport) == [
        "spawn_worker",
        "spawn_leaf",
        "merge_pr",
    ]
    assert _effect_operations(result) == [
        "spawn_worker",
        "spawn_leaf",
        "merge_pr",
    ]
    assert all(intent.executed for intent in result.effects)
    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert result.final_state.events.last_consumed_offset == 5
    assert result.final_state.slices["worker-a"].status.value == "merged"
    assert result.final_state.slices["leaf-a"].pr_number == 42
    assert result.final_state.slices["leaf-a"].reviewed_head == "head-a"
    assert result.final_state.slices["leaf-a"].status.value == "merged"
    assert result.final_state.slices["worker-a"].dispatch_authoritative_event_seq == 1
    assert result.final_state.slices["leaf-a"].dispatch_authoritative_event_seq == 2
    assert result.final_state.slices["worker-a"].dispatch_last_boundary == "agent.spawned"
    assert result.final_state.slices["leaf-a"].dispatch_last_boundary == "agent.spawned"
    assert source.acknowledged == [1, 2, 3, 4, 5]
    lifecycle_payloads = [
        arguments["payload"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
        and arguments["event_type"]
        in {"tl.slice_status_changed", "tl.phase_changed", "tl.merge_decided"}
    ]
    assert lifecycle_payloads == [
        {
            "slice_id": "leaf-a",
            "from_status": "pending",
            "to_status": "dispatch_unconfirmed",
        },
        {
            "slice_id": "worker-a",
            "from_status": "pending",
            "to_status": "dispatch_unconfirmed",
        },
        {
            "slice_id": "worker-a",
            "from_status": "dispatch_unconfirmed",
            "to_status": "spawned",
        },
        {
            "from_phase": "tl_planning",
            "to_phase": "tl_waiting",
            "run_id": "active-run",
        },
        {
            "slice_id": "leaf-a",
            "from_status": "dispatch_unconfirmed",
            "to_status": "spawned",
        },
        {"slice_id": "worker-a", "from_status": "spawned", "to_status": "merged"},
        {
            "slice_id": "leaf-a",
            "pr_number": 42,
            "decision": "merge",
            "head_sha_hash": hashlib.sha256(b"head-a").hexdigest(),
        },
        {"slice_id": "leaf-a", "from_status": "spawned", "to_status": "merged"},
        {"from_phase": "tl_waiting", "to_phase": "tl_all_merged", "run_id": "active-run"},
        {"from_phase": "tl_all_merged", "to_phase": "tl_done", "run_id": "active-run"},
    ]
    dispatch_events = [
        arguments["event_type"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
        and arguments["event_type"].startswith(("tl.dispatch_", "tl.spawn_"))
    ]
    assert dispatch_events == [
        "tl.dispatch_intended",
        "tl.spawn_requested",
        "tl.spawn_request_accepted",
        "tl.dispatch_intended",
        "tl.spawn_requested",
        "tl.spawn_request_accepted",
        "tl.dispatch_confirmed",
        "tl.dispatch_confirmed",
    ]


def test_observability_failure_does_not_change_terminal_state(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    baseline_transport = RecordingTransport()
    baseline = run_tl_loop(
        "observability-baseline-run",
        _plan(),
        SyntheticQueue(_lifecycle_events("observability-baseline-run")),
        EffectClient(baseline_transport),
        config=_config(),
        root_dir=tmp_path / "baseline",
    )
    failed_transport = RecordingTransport(fail_observability=True)
    with caplog.at_level("WARNING"):
        result = run_tl_loop(
            "observability-failure-run",
            _plan(),
            SyntheticQueue(_lifecycle_events("observability-failure-run")),
            EffectClient(failed_transport),
            config=_config(),
            root_dir=tmp_path / "failed",
        )

    assert result.final_state.fsm.phase is baseline.final_state.fsm.phase is TLPhase.TLDone
    assert (
        result.final_state.slices["leaf-a"].status is baseline.final_state.slices["leaf-a"].status
    )
    assert result.final_state.slices["leaf-a"].status is SliceStatus.MERGED
    assert "merge_pr" in _effect_names(failed_transport)
    assert _merge_decisions(failed_transport) == _merge_decisions(baseline_transport)
    assert _merge_decisions(failed_transport)[0]["decision"] == "merge"
    assert "controller event tl.merge_decided failed: ledger unavailable" in caplog.text


def test_dispatch_waits_for_delayed_authoritative_confirmation(tmp_path: Path) -> None:
    run_id = "delayed-dispatch-run"
    transport = RecordingTransport()
    source = SyntheticQueue([])
    cancel_event = threading.Event()
    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            run_tl_loop(
                run_id,
                WorkPlan.from_mapping({"leaves": [{"name": "leaf-a", "task": "delayed"}]}),
                source,
                EffectClient(transport),
                config=TLLoopConfig(
                      max_workers=0,
                      max_leaves=1,
                      poll_interval=0.001,
                      cancel_event=cancel_event,
                ),
                root_dir=tmp_path,
            )
        except BaseException as error:  # noqa: BLE001 - assert explicit cancellation
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while not any(name == "spawn_leaf" for name, _ in transport.calls):
        if time.monotonic() >= deadline:
            pytest.fail("spawn request was not issued")
        time.sleep(0.001)
    time.sleep(0.02)
    source.events.append(
        _canonical_event(
            1,
            "agent.spawned",
            "leaf-a",
            run_id,
            intent_id=_dispatch_intent(run_id, "leaf-a"),
        )
    )
    deadline = time.monotonic() + 1.0
    while source.acknowledged != [1] and time.monotonic() < deadline:
        time.sleep(0.001)
    cancel_event.set()
    thread.join(timeout=2)

    assert source.acknowledged == [1]
    assert isinstance(outcome.get("error"), LoopCancelled)
    state = RunStore(run_id, tmp_path).load()
    assert state.fsm.phase is TLPhase.TLWaiting
    assert state.slices["leaf-a"].status is SliceStatus.SPAWNED
    assert not state.gates


def test_rejected_spawn_is_persisted_as_dispatch_failure(tmp_path: Path) -> None:
    transport = RecordingTransport(reject_spawns=True)
    plan = WorkPlan.from_mapping({"leaves": [{"name": "leaf-a", "task": "implement the change"}]})
    result = run_tl_loop(
        "dispatch-failure-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
          config=TLLoopConfig(poll_interval=0.001),
        root_dir=tmp_path,
    )

    slice_state = result.final_state.slices["leaf-a"]
    assert result.final_state.fsm.phase is TLPhase.TLFailed
    assert slice_state.status is SliceStatus.DISPATCH_FAILED
    assert slice_state.park_cause.value == "dispatch_failed"
    assert slice_state.dispatch_last_boundary == "spawn_request_failed"
    assert "tmux launch rejected" in (slice_state.dispatch_error or "")
    assert any(
        arguments["event_type"] == "tl.spawn_request_failed"
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    )
    assert not any(
        arguments["event_type"] == "tl.gate_opened"
        and arguments["payload"].get("gate_name") == "tl-timeout"
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    )


def test_restart_reconciles_dispatch_without_duplicate_spawn(tmp_path: Path) -> None:
    run_id = "dispatch-restart-run"
    plan = WorkPlan.from_mapping({"leaves": [{"name": "leaf-a", "task": "implement the change"}]})
    cancel_event = threading.Event()
    config = TLLoopConfig(
        poll_interval=0.001,
        cancel_event=cancel_event,
    )
    initial = _initial_slices(plan, config, tmp_path, run_id)
    initial["leaf-a"].update(
        {
            "status": "dispatching",
            "attempts": 1,
            "dispatch_intent_id": "intent-restart-1",
            "dispatch_started_at": time.time(),
            "dispatch_last_boundary": "dispatch_intended",
        }
    )
    transport = RecordingTransport()

    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            run_tl_loop(
                run_id,
                plan,
                SyntheticQueue([]),
                EffectClient(transport),
                config=config,
                root_dir=tmp_path,
                initial_slices=initial,
            )
        except BaseException as error:  # noqa: BLE001 - assert explicit cancellation
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while "tl.dispatch_reconciliation_completed" not in [
        arguments["event_type"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    ]:
        if time.monotonic() >= deadline:
            pytest.fail("dispatch reconciliation did not complete")
        time.sleep(0.001)
    cancel_event.set()
    thread.join(timeout=2)

    slice_state = RunStore(run_id, tmp_path).load().slices["leaf-a"]
    event_types = [
        arguments["event_type"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    ]
    assert "spawn_leaf" not in _effect_names(transport)
    assert slice_state.dispatch_intent_id == "intent-restart-1"
    assert slice_state.status is SliceStatus.DISPATCH_UNCONFIRMED
    assert "tl.dispatch_reconciliation_started" in event_types
    assert "tl.dispatch_reconciliation_completed" in event_types
    assert isinstance(outcome.get("error"), LoopCancelled)
    assert not RunStore(run_id, tmp_path).load().gates


def test_restart_adopts_owner_by_intent_without_duplicate_spawn(tmp_path: Path) -> None:
    run_id = "dispatch-adopt-run"
    plan = WorkPlan.from_mapping({"leaves": [{"name": "leaf-a", "task": "implement"}]})
    config = TLLoopConfig(poll_interval=0.001)
    initial = _initial_slices(plan, config, tmp_path, run_id)
    initial["leaf-a"].update(
        {
            "status": "dispatching",
            "attempts": 1,
            "dispatch_intent_id": "intent-adopt-1",
            "dispatch_started_at": time.time(),
            "dispatch_last_boundary": "dispatch_intended",
        }
    )
    transport = RecordingTransport(
        listed_agents=[
            {"agent_id": "leaf-a-codex", "intent_id": "intent-adopt-1", "is_alive": True}
        ]
    )

    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue(
            [
                _event(1, "child_completed", "leaf-a", run_id=run_id),
                _event(2, "all_children_done", run_id=run_id),
            ]
        ),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
        initial_slices=initial,
    )

    adopted = result.final_state.slices["leaf-a"]
    assert "spawn_leaf" not in _effect_names(transport)
    assert adopted.dispatch_agent_id == "leaf-a-codex"
    assert adopted.dispatch_authoritative_event_seq is not None
    assert adopted.dispatch_last_boundary == "owner_adopted"


def test_stale_spawn_intent_cannot_confirm_new_attempt(tmp_path: Path) -> None:
    run_id = "dispatch-stale-intent-run"
    plan = WorkPlan.from_mapping({"leaves": [{"name": "leaf-a", "task": "implement"}]})
    cancel_event = threading.Event()
    config = TLLoopConfig(
        poll_interval=0.001,
        cancel_event=cancel_event,
    )
    initial = _initial_slices(plan, config, tmp_path, run_id)
    initial["leaf-a"].update(
        {
            "status": "dispatching",
            "attempts": 2,
            "dispatch_intent_id": "intent-new-2",
            "dispatch_started_at": time.time(),
            "dispatch_last_boundary": "dispatch_intended",
        }
    )

    source = SyntheticQueue(
        [
            _canonical_event(
                1,
                "agent.spawned",
                "leaf-a",
                run_id,
                intent_id="intent-stale-1",
            )
        ]
    )
    outcome: dict[str, BaseException] = {}
    transport = RecordingTransport()

    def run() -> None:
        try:
            run_tl_loop(
                run_id,
                plan,
                source,
                EffectClient(transport),
                config=config,
                root_dir=tmp_path,
                initial_slices=initial,
            )
        except BaseException as error:  # noqa: BLE001 - assert explicit cancellation
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while source.acknowledged != [1] and time.monotonic() < deadline:
        time.sleep(0.001)
    cancel_event.set()
    thread.join(timeout=2)

    stale = RunStore(run_id, tmp_path).load().slices["leaf-a"]
    assert stale.status is SliceStatus.DISPATCH_UNCONFIRMED
    assert stale.dispatch_intent_id == "intent-new-2"
    assert stale.dispatch_authoritative_event_seq is None
    assert stale.park_cause.value == "dispatch_unconfirmed"
    assert source.acknowledged == [1]
    assert isinstance(outcome.get("error"), LoopCancelled)


def test_ledger_run_id_mismatch_reaches_driver_diagnostics(tmp_path: Path) -> None:
    run_id = "production-mismatch-run"
    current_swarm = "current-swarm"
    stale_swarm = "stale-swarm"
    segments = tmp_path / "segments"
    segments.mkdir()
    (segments / "segment-000000000001.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_id": "stale-spawn",
                "id": "stale-spawn",
                "event_time": "2026-08-11T00:00:00Z",
                "observed_at": "2026-08-11T00:00:00Z",
                "run_seq": 1,
                "type": "agent.spawned",
                "agent_id": "leaf-a",
                "run_id": stale_swarm,
                "session_id": "session-1",
                "lifecycle_state": "observed",
                "data": {
                    "child_agent": "leaf-a",
                    "agent_type": "codex",
                    "branch": "main.leaf-a",
                    "intent_id": "stale-intent",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    segment = segments / "segment-000000000001.jsonl"
    source = LedgerQueue(
        LedgerReader(
            segments,
            run_id=run_id,
            state_root=tmp_path,
            ledger_run_id=current_swarm,
        ),
        poll_interval=0.001,
    ).start()
    transport = RecordingTransport()
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = run_tl_loop(
                run_id,
                WorkPlan.from_mapping({"leaves": [{"name": "leaf-a", "task": "implement"}]}),
                source,
                EffectClient(transport),
                  config=TLLoopConfig(
                      poll_interval=0.001,
                      ledger_run_id=current_swarm,
                ),
                root_dir=tmp_path,
            )
        except BaseException as error:  # noqa: BLE001 - fail the test with the worker error
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while not source.findings and time.monotonic() < deadline:
        time.sleep(0.001)
    assert source.findings
    state_path = tmp_path / run_id / "run.json"
    current_intent: str | None = None
    while current_intent is None and time.monotonic() < deadline:
        time.sleep(0.001)
        if not state_path.exists():
            continue
        current_intent = RunStore(run_id, tmp_path).load().slices["leaf-a"].dispatch_intent_id
    assert current_intent
    rows = [
        {
            "schema_version": 1,
            "event_id": f"event-{sequence}",
            "id": f"event-{sequence}",
            "event_time": "2026-08-11T00:00:00Z",
            "observed_at": "2026-08-11T00:00:00Z",
            "run_seq": sequence,
            "type": event_type,
            "agent_id": "leaf-a",
            "run_id": current_swarm,
            "session_id": "session-1",
            "lifecycle_state": "observed",
            "data": data,
        }
        for sequence, event_type, data in (
            (
                2,
                "agent.spawned",
                {
                    "child_agent": "leaf-a",
                    "agent_type": "codex",
                    "branch": "main.leaf-a",
                    "intent_id": current_intent,
                },
            ),
            (3, "agent.completed", {"status": "completed"}),
            (4, "agent.notify_parent", {"shadow_event": {"kind": "all_children_done"}}),
        )
    ]
    with segment.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    thread.join(timeout=2)
    source.close(timeout=2)

    assert "error" not in outcome
    result = cast(TLRunResult, outcome["result"])

    assert result.diagnostics["received"] == 4
    assert result.diagnostics["filtered"] == 1
    assert result.diagnostics["reader_findings"] == [
        "ignored ledger event with run_id 'stale-swarm'; expected 'current-swarm'",
    ]


def test_pr_head_change_clears_per_head_gate_state() -> None:
    current = SliceState(
        id="leaf-a",
        status=SliceStatus.IN_REVIEW,
        paths=("src/leaf.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just tl-loop-test",),
        agent_type="codex",
        model="gpt-5",
        branch="main.leaf-a",
        worktree=".worktrees/leaf-a",
        pr_number=42,
        reviewed_head="head-a",
        attempts=2,
        verdict=Verdict.GO,
        review_findings={
            "head-a": (
                {
                    "severity": "blocking",
                    "path": "src/leaf.py",
                    "rationale": "old finding",
                },
            )
        },
        ci_state={"head-a": "success"},
        reviewer_attempt={"head-a": 1},
        repair_attempts=3,
    )

    updated = _update_slices({"leaf-a": current}, PRUpdated(42, "head-b", "leaf-a"))["leaf-a"]

    assert updated.status is SliceStatus.IN_REVIEW
    assert updated.pr_number == 42
    assert updated.reviewed_head == "head-b"
    assert updated.review_findings == {}
    assert updated.ci_state == {}
    assert updated.reviewer_attempt == {}
    assert updated.repair_attempts == 3

    assert updated.verdict is None
    assert updated.verdict_at is None


def test_decoder_maps_wire_pr_filed_and_pr_updated_events() -> None:
    def raw(event_type: str, sequence: int, head_sha: str) -> dict[str, object]:
        return {
            "type": event_type,
            "run_seq": sequence,
            "run_id": "run-1",
            "agent_id": "leaf-a",
            "lifecycle_state": "emitted",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": head_sha,
            },
        }

    filed = TLEventDecoder().decode(project(raw("pr.filed", 1, "head-a")))
    updated = TLEventDecoder().decode(project(raw("pr.updated", 2, "head-b")))

    assert filed == PRFiled(42, "head-a", "leaf-a")
    assert updated == PRUpdated(42, "head-b", "leaf-a")


def test_opt_in_reviewer_spawn_claims_attempt_and_injects_criteria(tmp_path: Path) -> None:
    run_id = "reviewer-spawn-run"
    raw_pr_filed = {
        "type": "pr.filed",
        "run_seq": 1,
        "run_id": run_id,
        "agent_id": "tunable-operator-body-opencode",
        "lifecycle_state": "emitted",
        "observed_at": "2026-08-12T00:00:00Z",
        "data": {
            "pr_number": 42,
            "head_sha": "head-a",
            "branch": "main.tunable-operator-body-opencode",
        },
    }
    source = SyntheticQueue(
        [
            project(cast(dict[str, object], raw_pr_filed)),
            project(
                {
                    **raw_pr_filed,
                    "run_seq": 2,
                }
            ),
            _event(3, "all_children_done", run_id=run_id),
        ]
    )
    transport = RecordingTransport(spawned_agent_id="tunable-operator-body-opencode")
    plan = WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": "tunable-operator-body",
                    "task": "implement the change",
                    "boundary": ["src/leaf.py"],
                    "verify": ["just tl-loop-test"],
                    "done_criteria": ["the changed behavior is covered"],
                }
            ]
        }
    )

    result = run_tl_loop(
        run_id,
        plan,
        source,
        EffectClient(transport),
        config=TLLoopConfig(
            enable_reviewer_spawn=True,
            max_workers=0,
            max_leaves=1,
              max_events=2,
              poll_interval=0.001,
          ),
        root_dir=tmp_path,
    )

    assert _effect_names(transport) == ["spawn_leaf", "spawn_reviewer"]
    assert sum(name == "spawn_reviewer" for name, _ in transport.calls) == 1
    reviewer_args = next(
        arguments for name, arguments in transport.calls if name == "spawn_reviewer"
    )
    assert reviewer_args["pr_number"] == 42
    assert reviewer_args["head_sha"] == "head-a"
    assert reviewer_args["force"] is False
    criteria = cast(list[object], reviewer_args["acceptance_criteria"])
    assert any("DONE CRITERIA: the changed behavior is covered" in str(item) for item in criteria)
    slice_state = result.final_state.slices["tunable-operator-body"]
    assert slice_state.reviewer_attempt == {"head-a": 1}
    assert source.acknowledged == [1, 2, 3]


def test_binding_review_findings_adjudicate_and_resume_same_pr(tmp_path: Path) -> None:
    backend = ReviewBackend(
        [
            RlmResponse(
                {
                    "verdict": "NO-GO",
                    "reviewed_head": "head-a",
                    "reasons": [
                        {
                            "severity": "blocking",
                            "file": "src/leaf.py",
                            "line": 8,
                            "claim": "The failure path is unhandled",
                        }
                    ],
                    "blocking_count": 1,
                }
            ),
            RlmResponse(
                {
                    "root_cause": "The failure path is unhandled in src/leaf.py",
                    "proposed_solution": "Handle the failure in src/leaf.py",
                    "read_first": ["src/leaf.py"],
                    "steps": ["Update src/leaf.py"],
                    "verify": ["just tl-loop-test"],
                    "boundary": ["Only edit src/leaf.py"],
                    "done_criteria": ["The failure path is covered"],
                }
            ),
        ]
    )
    transport = ReviewRepairTransport()
    store = _review_store(tmp_path)
    event = _review_event()
    state = store.load()
    config = TLLoopConfig(
        active=True,
        review_model_choice=_review_choice(backend),
        review_policy_path=Path(".exo/review-policy.toml"),
    )
    effects_log: list[object] = []

    _route_review_event(
        WorkPlan.from_mapping(
            {
                "leaves": [
                    {
                        "name": "leaf-a",
                        "task": "implement the requested change",
                        "boundary": ["src/leaf.py"],
                        "verify": ["just tl-loop-test"],
                        "done_criteria": ["the failure path is covered"],
                    }
                ]
            }
        ),
        store,
        state,
        TLPlanning(),
        event,
        1,
        config,
        EffectClient(transport),
        effects_log,
    )

    restored = store.load().slices["leaf-a"]
    assert [request.name for request in backend.requests] == [
        "adjudicate_review",
        "compose_repair",
    ]
    assert _effect_names(transport) == [
        "watcher_pr_state",
        "resume_pr",
    ]
    assert restored.status is SliceStatus.REPAIRING
    assert restored.reviewed_head == "head-a"
    assert restored.verdict is Verdict.NO_GO
    assert restored.repair_attempts == 1
    assert restored.review_findings["head-a"][0]["path"] == "src/leaf.py"
    assert all(name != "spawn_leaf" for name, _ in transport.calls)


def test_aggregate_review_advances_hierarchical_lifecycle(tmp_path: Path) -> None:
    backend = ReviewBackend(
        [
            RlmResponse(
                {
                    "verdict": "GO",
                    "reviewed_head": "head-a",
                    "reasons": [],
                    "blocking_count": 0,
                }
            )
        ]
    )
    transport = ReviewRepairTransport()
    store = _review_store(tmp_path)
    current = store.load().slices["leaf-a"]
    current = replace(
        current,
        dispatch_agent_id="review-run:leaf-a:integration",
        dispatch_last_boundary="aggregate_pr_open",
    )
    candidate = IntegrationCandidateState(
        lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
        aggregate_pr_number=42,
        aggregate_head_sha="head-a",
        aggregate_patch_digest="patch-a",
        aggregate_original_base_sha="main",
        integration_owner_id="review-run:leaf-a:integration",
        integration_owner_run_id="leaf-a",
        integration_owner_branch="main.leaf-a",
        integration_owner_worktree=".worktrees/leaf-a",
        head_sha="head-a",
        patch_digest="patch-a",
    )
    state = store.checkpoint(
        TLPlanning(),
        {"leaf-a": current},
        BudgetLedger(0, 0),
        offset=0,
        integration=IntegrationRuntimeState(
            lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
            sub_tl_states={"leaf-a": IntegrationLifecycle.AGGREGATE_PR_OPEN},
            candidates={"leaf-a": candidate},
        ),
    )
    _route_review_event(
        WorkPlan(sub_tls=(SubTLTask("leaf-a", WorkPlan(), source=SyntheticQueue([])),)),
        store,
        state,
        TLPlanning(),
        _review_event(),
        1,
        TLLoopConfig(
            active=True,
            review_model_choice=_review_choice(backend),
            review_policy_path=Path(".exo/review-policy.toml"),
        ),
        EffectClient(transport),
        [],
    )

    restored = store.load()
    assert (
        restored.integration.candidates["leaf-a"].lifecycle
        is IntegrationLifecycle.READY_FOR_INTEGRATION
    )
    assert restored.slices["leaf-a"].verdict is Verdict.GO
    assert _effect_names(transport) == []


def test_go_with_nits_persists_follow_up_in_per_head_state(tmp_path: Path) -> None:
    backend = ReviewBackend(
        [
            RlmResponse(
                {
                    "verdict": "GO-WITH-NITS",
                    "reviewed_head": "head-a",
                    "reasons": [
                        {
                            "severity": "nit",
                            "file": "src/leaf.py",
                            "line": 7,
                            "claim": "Clarify this name",
                        }
                    ],
                    "blocking_count": 0,
                }
            )
        ]
    )
    store = _review_store(tmp_path)
    _route_review_event(
        WorkPlan.from_mapping(
            {
                "leaves": [
                    {
                        "name": "leaf-a",
                        "task": "implement the requested change",
                        "boundary": ["src/leaf.py"],
                        "verify": ["just tl-loop-test"],
                    }
                ]
            }
        ),
        store,
        store.load(),
        TLPlanning(),
        _review_event(finding_severity="nit"),
        1,
        TLLoopConfig(
            active=True,
            review_model_choice=_review_choice(backend),
            review_policy_path=Path(".exo/review-policy.toml"),
        ),
        EffectClient(RecordingTransport()),
        [],
    )

    restored = store.load().slices["leaf-a"]
    assert restored.verdict is Verdict.GO_WITH_NITS
    assert {
        "severity": "nit",
        "path": "src/leaf.py:7",
        "rationale": "Clarify this name",
    } in restored.review_findings["head-a"]


def test_ci_failure_records_head_and_resumes_same_pr(tmp_path: Path) -> None:
    backend = ReviewBackend(
        [
            RlmResponse(
                {
                    "root_cause": "CI exposed a failure in src/leaf.py",
                    "proposed_solution": "Fix the failure in src/leaf.py",
                    "read_first": ["src/leaf.py"],
                    "steps": ["Update src/leaf.py"],
                    "verify": ["just tl-loop-test"],
                    "boundary": ["Only edit src/leaf.py"],
                    "done_criteria": ["CI passes"],
                }
            )
        ]
    )
    transport = ReviewRepairTransport()
    store = _review_store(tmp_path, verdict=Verdict.GO)
    state = store.load()
    _route_ci_event(
        store,
        state,
        TLPlanning(),
        _ci_failure_event(),
        1,
        TLLoopConfig(
            active=True,
            review_model_choice=_review_choice(backend),
            review_policy_path=Path(".exo/review-policy.toml"),
        ),
        EffectClient(transport),
        [],
    )

    restored = store.load().slices["leaf-a"]
    assert restored.ci_state == {"head-a": "failure"}
    assert restored.status is SliceStatus.REPAIRING
    assert restored.verdict is Verdict.NO_GO
    assert _effect_names(transport) == [
        "watcher_pr_state",
        "resume_pr",
    ]


def test_ci_failure_before_review_records_ci_state_without_verdict(tmp_path: Path) -> None:
    """The exact crash shape: a CI failure lands before any head has been reviewed.

    ``reviewed_head`` is None, so the schema invariant that a verdict requires
    a bound reviewed head must not be violated. Before the fix this call raised
    a schema error out of ``store.checkpoint`` and the event cursor never
    advanced, so every restart replayed the same event forever.
    """
    transport = RecordingTransport()
    store = _review_store(tmp_path, reviewed_head=None, verdict=None)
    state = store.load()

    result = _route_ci_event(
        store,
        state,
        TLPlanning(),
        _ci_failure_event(),
        1,
        TLLoopConfig(active=True, review_policy_path=Path(".exo/review-policy.toml")),
        EffectClient(transport),
        [],
    )

    assert result.slices["leaf-a"].verdict is None
    restored = store.load().slices["leaf-a"]
    assert restored.ci_state == {"head-a": "failure"}
    assert restored.verdict is None
    assert restored.verdict_at is None
    assert restored.status is SliceStatus.IN_REVIEW
    assert _effect_names(transport) == []


def test_ci_failure_for_stale_head_does_not_override_verdict(tmp_path: Path) -> None:
    """A late CI result for a head that isn't the currently reviewed head must not
    clobber the verdict bound to the reviewed head."""
    transport = RecordingTransport()
    store = _review_store(tmp_path, reviewed_head="head-b", verdict=Verdict.GO)
    state = store.load()

    result = _route_ci_event(
        store,
        state,
        TLPlanning(),
        _ci_failure_event(),
        1,
        TLLoopConfig(active=True, review_policy_path=Path(".exo/review-policy.toml")),
        EffectClient(transport),
        [],
    )

    assert result.slices["leaf-a"].verdict is Verdict.GO
    restored = store.load().slices["leaf-a"]
    assert restored.ci_state == {"head-a": "failure"}
    assert restored.verdict is Verdict.GO
    assert restored.reviewed_head == "head-b"
    assert _effect_names(transport) == []


def test_ci_failure_after_review_binds_head_then_still_repairs(tmp_path: Path) -> None:
    """A CI failure before review is recorded harmlessly; once review binds the
    same head, a subsequent CI failure on it still sets NO_GO and repairs."""
    backend = ReviewBackend(
        [
            RlmResponse(
                {
                    "root_cause": "CI exposed a failure in src/leaf.py",
                    "proposed_solution": "Fix the failure in src/leaf.py",
                    "read_first": ["src/leaf.py"],
                    "steps": ["Update src/leaf.py"],
                    "verify": ["just tl-loop-test"],
                    "boundary": ["Only edit src/leaf.py"],
                    "done_criteria": ["CI passes"],
                }
            )
        ]
    )
    pre_review_event = project(
        {
            "type": "ci.status_changed",
            "run_seq": 1,
            "run_id": "review-run",
            "agent_id": "leaf-a",
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-x",
                "status": "failure",
                "message": "tests failed",
            },
        }
    )
    pre_review_transport = RecordingTransport()
    store = _review_store(tmp_path, reviewed_head=None, verdict=None)
    state = store.load()
    state = _route_ci_event(
        store,
        state,
        TLPlanning(),
        pre_review_event,
        1,
        TLLoopConfig(active=True, review_policy_path=Path(".exo/review-policy.toml")),
        EffectClient(pre_review_transport),
        [],
    )
    assert state.slices["leaf-a"].verdict is None
    assert state.slices["leaf-a"].ci_state == {"head-x": "failure"}
    assert _effect_names(pre_review_transport) == []

    reviewed = replace(state.slices["leaf-a"], reviewed_head="head-a", verdict=Verdict.GO)
    state = store.checkpoint(TLPlanning(), {**state.slices, "leaf-a": reviewed}, state.budgets, 1)

    repair_transport = ReviewRepairTransport()
    state = _route_ci_event(
        store,
        state,
        TLPlanning(),
        _ci_failure_event(),
        2,
        TLLoopConfig(
            active=True,
            review_model_choice=_review_choice(backend),
            review_policy_path=Path(".exo/review-policy.toml"),
        ),
        EffectClient(repair_transport),
        [],
    )

    restored = store.load().slices["leaf-a"]
    assert restored.ci_state == {"head-x": "failure", "head-a": "failure"}
    assert restored.status is SliceStatus.REPAIRING
    assert restored.verdict is Verdict.NO_GO
    assert _effect_names(repair_transport) == [
        "watcher_pr_state",
        "resume_pr",
    ]


def _review_choice(backend: ReviewBackend) -> RlmModelChoice:
    return RlmModelChoice(
        model_id="test-model",
        backend=backend,
        store=RlmCallStore(),
        context_length=10_000,
    )


def _repair_slice(*, attempts: int, model: str | None = None) -> SliceState:
    return SliceState(
        id="leaf-a",
        status=SliceStatus.IN_REVIEW,
        paths=("src/leaf.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just tl-loop-test",),
        agent_type="codex",
        model=model,
        branch=None,
        worktree=None,
        pr_number=42,
        reviewed_head="head-a",
        attempts=attempts,
        verdict=Verdict.NO_GO,
    )


def _repair_config(
    *, requested_model: str | None = None, escalate_after_attempts: int = 1
) -> TLLoopConfig:
    catalog = ModelCatalog.from_fixture(
        Path(__file__).parent / "fixtures" / "model_catalog_scored.json"
    )
    policy = validate_policy(_selector_policy(escalate_after_attempts))
    return TLLoopConfig(
        catalog=catalog,
        policy=policy,
        role="worker",
        requested_model=requested_model,
    )


def test_repair_model_preserves_current_model_below_threshold() -> None:
    config = _repair_config(escalate_after_attempts=2)

    assert _repair_model(_repair_slice(attempts=0, model="gpt-5-mini"), config) == "gpt-5-mini"
    assert _repair_model(_repair_slice(attempts=1, model="gpt-5-mini"), config) == "gpt-5-mini"


def test_repair_model_escalates_to_strongest_at_threshold() -> None:
    config = _repair_config(escalate_after_attempts=1)

    assert _repair_model(_repair_slice(attempts=0, model="gpt-5-mini"), config) == "gpt-5-mini"
    assert _repair_model(_repair_slice(attempts=1, model="gpt-5-mini"), config) == "gpt-5.5"


def test_repair_model_requested_model_wins_at_any_attempt() -> None:
    config = _repair_config(requested_model="gpt-5-mini", escalate_after_attempts=1)

    assert _repair_model(_repair_slice(attempts=0), config) == "gpt-5-mini"
    assert _repair_model(_repair_slice(attempts=1), config) == "gpt-5-mini"


def test_repair_model_without_catalog_preserves_current_model() -> None:
    config = TLLoopConfig(role="worker")

    assert _repair_model(_repair_slice(attempts=1, model="gpt-5-mini"), config) == "gpt-5-mini"
    assert _repair_model(_repair_slice(attempts=1), config) is None


def test_requested_model_requires_catalog() -> None:
    with pytest.raises(ValueError, match="requested_model requires a model catalog"):
        TLLoopConfig(requested_model="gpt-5-mini")


def _review_store(
    tmp_path: Path, *, verdict: Verdict | None = None, reviewed_head: str | None = "head-a"
) -> RunStore:
    store = RunStore("review-run", tmp_path)
    create("review-run", {}, root_dir=tmp_path)
    store.checkpoint(
        TLPlanning(),
        {
            "leaf-a": SliceState(
                id="leaf-a",
                status=SliceStatus.IN_REVIEW,
                paths=("src/leaf.py",),
                depends_on=(),
                base_ref="main",
                test_plan=("just tl-loop-test",),
                agent_type="codex",
                model="test-model",
                branch="main.leaf-a",
                worktree=".worktrees/leaf-a",
                pr_number=42,
                reviewed_head=reviewed_head,
                attempts=1,
                verdict=verdict,
            )
        },
        BudgetLedger(0, 0),
        offset=0,
    )
    return store


def _review_event(*, finding_severity: str = "blocking") -> EventEnvelope:
    return project(
        {
            "type": "pr.review",
            "run_seq": 1,
            "run_id": "review-run",
            "agent_id": "leaf-a",
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-a",
                "kind": "changes_requested",
                "findings": [
                    {
                        "severity": finding_severity,
                        "path": "src/leaf.py",
                        "rationale": "The failure path is unhandled",
                    }
                ],
                "diff": {
                    "diff": "@@ -1 +1 @@\\n-old\\n+new\\n",
                    "lines_changed": 1,
                    "paths": ["src/leaf.py"],
                    "review_rounds": 1,
                },
            },
        }
    )


def test_review_stall_classification_is_persisted_by_tl_projection(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    event = project(
        {
            "type": "pr.review",
            "run_seq": 2,
            "run_id": "review-run",
            "agent_id": "leaf-a",
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:01:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-a",
                "kind": "timeout",
                "last_review_state": "changes_requested",
                "reviewer_registered": True,
                "forgejo_review_present": True,
                "addressed_changes": False,
                "wait_seconds": 900,
            },
        }
    )

    _record_review_event(store, store.load(), TLPlanning(), event, 2)

    assert store.load().slices["leaf-a"].stall_classification == "dev_not_pushing"


def _ci_failure_event() -> EventEnvelope:
    return project(
        {
            "type": "ci.status_changed",
            "run_seq": 1,
            "run_id": "review-run",
            "agent_id": "leaf-a",
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-a",
                "status": "failure",
                "message": "tests failed",
            },
        }
    )


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
  )

    result = tl_run({"run_id": run_id, "plan": _plan()}, config, BudgetLedger(0, 0))

    assert _effect_names(transport) == [
        "spawn_worker",
        "spawn_leaf",
        "merge_pr",
    ]
    spawn_worker_call = next(
        arguments for name, arguments in transport.calls if name == "spawn_worker"
    )
    assert spawn_worker_call["agent_type"] == "codex"
    assert spawn_worker_call["model"] == "gpt-5.5"
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
      )
    plan = WorkPlan(
        workers=(
            WorkerTask("worker-a", "first"),
            WorkerTask("worker-b", "second"),
        )
    )

    result = tl_run({"run_id": run_id, "plan": plan}, config, BudgetLedger(0, 0))

    assert _effect_names(transport) == ["spawn_worker", "spawn_worker"]
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
    assert _effect_operations(result) == [
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
          ),
        root_dir=tmp_path,
    )

    assert _effect_names(transport) == [
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
            WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "bounded"}]}),
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
                  test_harness=True,
                  poll_interval=0.001,
              ),
            root_dir=tmp_path,
        )


def test_idle_silence_preserves_spawned_state_until_explicit_cancellation(
    tmp_path: Path,
) -> None:
    run_id = "long-running-run"
    cancel_event = threading.Event()
    source = SyntheticQueue(
        [
            _canonical_event(
                1,
                "agent.spawned",
                "leaf-a",
                run_id,
                intent_id=_dispatch_intent(run_id, "leaf-a"),
            )
        ]
    )
    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            run_tl_loop(
                run_id,
                WorkPlan.from_mapping({"leaves": [{"name": "leaf-a", "task": "long work"}]}),
                source,
                EffectClient(RecordingTransport()),
                config=TLLoopConfig(
                      max_workers=0,
                      max_leaves=1,
                      poll_interval=0.001,
                      cancel_event=cancel_event,
                ),
                root_dir=tmp_path,
            )
        except BaseException as error:  # noqa: BLE001 - assert explicit cancellation
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while not source.acknowledged and time.monotonic() < deadline:
        time.sleep(0.001)
    assert source.acknowledged == [1]
    cancel_event.set()
    thread.join(timeout=2)

    assert isinstance(outcome.get("error"), LoopCancelled)
    state = RunStore(run_id, tmp_path).load()
    assert state.fsm.phase is TLPhase.TLWaiting
    assert state.slices["leaf-a"].status is SliceStatus.SPAWNED
    assert not state.gates


def _selector_policy(escalate_after_attempts: int = 1) -> dict[str, object]:
    role = {
        "allow": ["codex/gpt-luna", "claude/sonnet"],
        "cost_rank": {"codex/gpt-luna": 1, "claude/sonnet": 2},
        "token_budget": 120000,
        "per_harness_budget": {"codex/gpt-luna": 80000, "claude/sonnet": 40000},
        "escalate_after_attempts": escalate_after_attempts,
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
     )


def _lifecycle_events(run_id: str) -> list[EventEnvelope]:
    return [
        _canonical_event(1, "agent.spawned", "worker-a", run_id),
        _canonical_event(2, "agent.spawned", "leaf-a", run_id),
        _event(3, "child_completed", "worker-a", run_id=run_id),
        _event(4, "child_completed", "leaf-a", pr_number=42, head_sha="head-a", run_id=run_id),
        _event(5, "all_children_done", run_id=run_id),
    ]


def _multi_leaf_events(run_id: str) -> list[EventEnvelope]:
    return [
        _canonical_event(1, "agent.spawned", "leaf-a", run_id),
        _canonical_event(2, "agent.spawned", "leaf-b", run_id),
        _event(3, "child_completed", "leaf-a", pr_number=10, head_sha="leaf-a-head", run_id=run_id),
        _event(4, "child_completed", "leaf-b", pr_number=11, head_sha="leaf-b-head", run_id=run_id),
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
    intent_id: str | None = None,
) -> EventEnvelope:
    data: dict[str, object] = {}
    if event_type == "agent.spawned":
        data.update(
            {
                "child_agent": slug,
                "agent_type": "codex",
                "branch": f"main.{slug}",
                "intent_id": intent_id or _dispatch_intent(run_id, slug),
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
    head_sha: str | None = None,
    run_id: str,
) -> EventEnvelope:
    shadow_event: dict[str, object] = {"kind": kind}
    if slug is not None:
        shadow_event["slug"] = slug
    if kind == "child_spawned":
        shadow_event["branch"] = f"main.{slug}"
        shadow_event["agent_type"] = "codex"
        shadow_event["intent_id"] = _dispatch_intent(run_id, slug or "")
    data: dict[str, object] = {"shadow_event": shadow_event}
    if pr_number is not None:
        data["pr_number"] = pr_number
    if head_sha is not None:
        data["head_sha"] = head_sha
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


def _dispatch_intent(run_id: str, slug: str) -> str:
    return hashlib.sha256(f"{run_id}:{slug}:1".encode()).hexdigest()[:32]


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("pr.review", {"findings": [], "head_sha": "aggregate-head"}),
        ("ci.status_changed", {"ci_status": "success", "head_sha": "aggregate-head"}),
    ],
)
def test_aggregate_review_and_ci_events_route_by_persisted_pr(
    event_type: str, payload: dict[str, object]
) -> None:
    payload = {**payload, "pr_number": 77}
    event = project(
        {
            "schema_version": 1,
            "event_id": "aggregate-event",
            "id": "aggregate-event",
            "event_time": "2026-08-11T00:00:00Z",
            "observed_at": "2026-08-11T00:00:00Z",
            "run_seq": 1,
            "type": event_type,
            "agent_id": "aggregate-run:alpha:integration",
            "run_id": "aggregate-run",
            "session_id": "session-1",
            "lifecycle_state": "observed",
            "data": payload,
        }
    )
    state = SimpleNamespace(slices={"alpha": SimpleNamespace(pr_number=77)})

    assert _event_belongs_to_plan(event, set(), state)


def test_recursive_sub_tls_isolate_state_and_branch_coordinates(tmp_path: Path) -> None:
    transport = RecordingTransport()
    grand_source = SyntheticQueue([])
    child_source = SyntheticQueue([])
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "child",
                WorkPlan(sub_tls=(SubTLTask("grandchild", WorkPlan(), source=grand_source),)),
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
    assert all(name == "emit_controller_event" for name, _ in transport.calls)


def test_recursive_depth_ceiling_parks_schedule_deadlock(tmp_path: Path) -> None:
    with pytest.raises(DepthLimitExceeded):
        run_tl_loop(
            "depth-run",
            WorkPlan(sub_tls=(SubTLTask("child", WorkPlan(), source=SyntheticQueue([])),)),
            SyntheticQueue([]),
            EffectClient(RecordingTransport()),
            config=TLLoopConfig(max_depth=0, poll_interval=0.001),
            root_dir=tmp_path,
        )

    state = load_state(tmp_path / "depth-run" / "run.json")
    assert state.fsm.phase is TLPhase.TLFailed
    assert state.slices["child"].status.value == "parked"


def test_same_order_sub_tls_overlap_and_wait_for_prior_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = 0
    maximum = 0
    lock = threading.Lock()
    first_stage = threading.Barrier(2)
    timeline: list[tuple[str, str]] = []

    def fake_tl_run(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del config, budgets
        name = cast(dict[str, object], root_spec)["run_id"]
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            timeline.append(("start", cast(str, name)))
        if name in {"stage-one-a", "stage-one-b"}:
            first_stage.wait(timeout=2)
        time.sleep(0.01)
        with lock:
            active -= 1
            timeline.append(("end", cast(str, name)))
        return SimpleNamespace(
            final_state=SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLDone), slices={})
        )

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", fake_tl_run)
    plan = WorkPlan(
        sub_tls=(
            SubTLTask("stage-one-a", WorkPlan(), order=1),
            SubTLTask("stage-one-b", WorkPlan(), order=1),
            SubTLTask("stage-two", WorkPlan(), order=2),
        )
    )
    result = run_tl_loop(
        "ordered-concurrency-run",
        plan,
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=TLLoopConfig(max_parallel_slices=2, poll_interval=0.001),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert maximum == 2
    assert timeline.index(("start", "stage-two")) > timeline.index(("end", "stage-one-a"))
    assert timeline.index(("start", "stage-two")) > timeline.index(("end", "stage-one-b"))


def test_active_parent_stays_alive_for_later_recursive_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_at = time.monotonic() + 0.02

    class DelayedQueue(SyntheticQueue):
        def get(self, timeout: float | None = None) -> EventEnvelope:
            if not self.events and time.monotonic() < release_at:
                raise queue.Empty
            return super().get(timeout)

    def waiting_child(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del root_spec, config, budgets
        return SimpleNamespace(
            final_state=SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLWaiting), slices={})
        )

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", waiting_child)
    run_id = "waiting-parent"
    result = run_tl_loop(
        run_id,
        WorkPlan(sub_tls=(SubTLTask("waiting-child", WorkPlan(), order=1),)),
        DelayedQueue([_event(1, "all_children_done", run_id=run_id)]),
        EffectClient(RecordingTransport()),
        config=TLLoopConfig(
            active=True,
            keep_alive_on_waiting=True,
            max_parallel_slices=1,
            max_events=1,
            poll_interval=0.001,
        ),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert result.consumed_events == (1,)


def test_recursive_ordered_lifecycle_handles_parallel_leaves_and_ready_order(
    tmp_path: Path,
) -> None:
    child_plan = WorkPlan.from_mapping(
        {
            "leaves": [
                {"name": "leaf-a", "task": "implement alpha part one"},
                {"name": "leaf-b", "task": "implement alpha part two"},
            ]
        }
    )
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "beta",
                child_plan,
                source=SyntheticQueue(_multi_leaf_events("beta")),
                order=1,
            ),
            SubTLTask(
                "alpha",
                child_plan,
                source=SyntheticQueue(_multi_leaf_events("alpha")),
                order=1,
            ),
        )
    )
    transport = OrderedIntegrationTransport(
        snapshots=[
            {
                "head_sha": "head-alpha",
                "base_sha": "base-alpha",
                "patch_digest": "patch-alpha",
                "merge_tree_sha": "tree-alpha",
                "ci_status": "success",
            },
            {
                "head_sha": "head-alpha",
                "base_sha": "base-alpha",
                "patch_digest": "patch-alpha",
                "merge_tree_sha": "tree-alpha",
                "ci_status": "success",
            },
            {
                "head_sha": "head-beta",
                "base_sha": "base-after-alpha",
                "patch_digest": "patch-beta",
                "merge_tree_sha": "tree-beta",
                "ci_status": "success",
            },
            {
                "head_sha": "head-beta",
                "base_sha": "base-after-alpha",
                "patch_digest": "patch-beta",
                "merge_tree_sha": "tree-beta",
                "ci_status": "success",
            },
        ]
    )
    config = TLLoopConfig(
        max_parallel_slices=2,
        max_leaves=2,
        max_workers=0,
        max_events=5,
        poll_interval=0.001,
        keep_alive_on_waiting=False,
    )
    parent_root = tmp_path / "ordered-e2e-run"
    for sub_tl_id, pr_number in (("alpha", 101), ("beta", 102)):
        run_tl_loop(
            sub_tl_id,
            child_plan,
            SyntheticQueue(_multi_leaf_events(sub_tl_id)),
            EffectClient(transport),
            config=config,
            root_dir=parent_root,
        )
        child_store = RunStore(sub_tl_id, parent_root)
        child_state = child_store.load()
        child_slice = child_state.slices["leaf-a"]
        child_store.checkpoint(
            child_state.fsm,
            {
                **child_state.slices,
                "leaf-a": replace(
                    child_slice,
                    pr_number=pr_number,
                    reviewed_head=f"child-{sub_tl_id}-head",
                ),
            },
            child_state.budgets,
            child_state.events.last_consumed_offset,
        )

    first = run_tl_loop(
        "ordered-e2e-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    assert first.final_state.fsm.phase is TLPhase.TLWaiting
    parent_store = RunStore("ordered-e2e-run", tmp_path)
    parent_state = parent_store.load()
    ready_slices = {
        slice_id: replace(
            slice_state,
            verdict=Verdict.GO,
            review_patch_digests={slice_state.reviewed_head or "": f"patch-{slice_id}"},
            ci_state={slice_state.reviewed_head or "": "success"},
        )
        for slice_id, slice_state in parent_state.slices.items()
    }
    parent_store.checkpoint(
        parent_state.fsm,
        ready_slices,
        parent_state.budgets,
        parent_state.events.last_consumed_offset,
        current_order=parent_state.current_order,
        ordered_stages=parent_state.ordered_stages,
        integration=parent_state.integration,
    )

    result = run_tl_loop(
        "ordered-e2e-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLDone
    merge_pr_numbers = [
        arguments["pr_number"] for name, arguments in transport.calls if name == "merge_pr"
    ]
    assert merge_pr_numbers[-2:] == [101, 102]
    assert len(merge_pr_numbers) == 6
    assert [
        arguments["pr_number"] for name, arguments in transport.calls if name == "watcher_pr_state"
    ] == [
        101,
        101,
        102,
        102,
    ]
    assert [snapshot["head_sha"] for snapshot in transport.snapshot_history] == [
        "head-alpha",
        "head-alpha",
        "head-beta",
        "head-beta",
    ]
    assert len([name for name, _ in transport.calls if name == "file_pr"]) == 2
    assert len([name for name, _ in transport.calls if name == "spawn_leaf"]) == 4


def test_failed_ordered_sub_tl_blocks_higher_order_work(tmp_path: Path) -> None:
    failing_child = WorkPlan.from_mapping(
        {"leaves": [{"name": "leaf-fails", "task": "fail without a completion event"}]}
    )
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "failed-stage",
                failing_child,
                source=SyntheticQueue([]),
                effects=EffectClient(RecordingTransport(reject_spawns=True)),
                order=1,
            ),
            SubTLTask("later-stage", WorkPlan(), source=SyntheticQueue([]), order=2),
        )
    )
    transport = RecordingTransport()

    result = run_tl_loop(
        "ordered-failure-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=TLLoopConfig(
            max_parallel_slices=1,
            max_workers=0,
            max_events=5,
            poll_interval=0.001,
        ),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLFailed
    assert result.final_state.slices["failed-stage"].status is SliceStatus.FAILED
    assert result.final_state.slices["later-stage"].status is SliceStatus.PENDING
    assert not any(
        arguments.get("name") == "later-stage"
        for name, arguments in transport.calls
        if name in {"spawn_leaf", "spawn_worker"}
    )


def test_nested_ordered_stages_round_trip_the_recursive_checkpoint(tmp_path: Path) -> None:
    nested = WorkPlan(
        sub_tls=(
            SubTLTask("inner-one", WorkPlan(), source=SyntheticQueue([]), order=1),
            SubTLTask("inner-two", WorkPlan(), source=SyntheticQueue([]), order=2),
        )
    )
    transport = RecordingTransport()

    result = run_tl_loop(
        "nested-ordered-run",
        WorkPlan(sub_tls=(SubTLTask("outer", nested, source=SyntheticQueue([]), order=1),)),
        SyntheticQueue([]),
        EffectClient(transport),
        config=TLLoopConfig(max_parallel_slices=2, poll_interval=0.001),
        root_dir=tmp_path,
    )

    child = load_state(tmp_path / "nested-ordered-run" / "outer" / "run.json")
    assert result.final_state.slices["outer"].status is SliceStatus.MERGED
    assert child.ordered_stages == (
        OrderedStageState(1, ("inner-one",)),
        OrderedStageState(2, ("inner-two",)),
    )
    assert child.slices["inner-one"].status is SliceStatus.MERGED
    assert child.slices["inner-two"].status is SliceStatus.MERGED


def test_ordered_sub_tl_restart_does_not_rerun_merged_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_tl_run(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del config, budgets
        calls.append(cast(str, cast(dict[str, object], root_spec)["run_id"]))
        return SimpleNamespace(
            final_state=SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLDone), slices={})
        )

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", fake_tl_run)
    plan = WorkPlan(
        sub_tls=(
            SubTLTask("restart-a", WorkPlan(), order=1),
            SubTLTask("restart-b", WorkPlan(), order=1),
        )
    )
    config = TLLoopConfig(max_parallel_slices=2, poll_interval=0.001)
    run_tl_loop(
        "restart-run",
        plan,
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=config,
        root_dir=tmp_path,
    )
    run_tl_loop(
        "restart-run",
        plan,
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=config,
        root_dir=tmp_path,
    )

    assert sorted(calls) == ["restart-a", "restart-b"]


def test_ordered_sub_tl_restart_adopts_terminal_child_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_dir = tmp_path / "adopt-run"
    run_tl_loop(
        "adopt-child",
        WorkPlan(),
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=_config(),
        root_dir=parent_dir,
    )

    def unexpected_child_run(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del root_spec, config, budgets
        raise AssertionError("a terminal child checkpoint must be adopted")

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", unexpected_child_run)
    result = run_tl_loop(
        "adopt-run",
        WorkPlan(sub_tls=(SubTLTask("adopt-child", WorkPlan(), order=1),)),
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=_config(),
        root_dir=tmp_path,
    )

    assert result.final_state.slices["adopt-child"].status is SliceStatus.MERGED


def test_sub_tl_aggregate_pr_is_persisted_and_reused_on_restart(tmp_path: Path) -> None:
    transport = RecordingTransport()
    child_plan = _plan()
    parent_dir = tmp_path / "aggregate-run"
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(transport),
        config=_config(),
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    child_state = child_store.load()
    child_slices = dict(child_state.slices)
    child_slices["leaf-a"] = replace(child_slices["leaf-a"], pr_number=42, reviewed_head="head-a")
    child_store.checkpoint(
        child_state.fsm,
        child_slices,
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    parent_plan = WorkPlan(
        sub_tls=(
            SubTLTask("aggregate-child", child_plan, source=SyntheticQueue([]), order=1),
            SubTLTask("later-stage", WorkPlan(), source=SyntheticQueue([]), order=2),
        )
    )
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        keep_alive_on_waiting=False,
    )

    first = run_tl_loop(
        "aggregate-run",
        parent_plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    second = run_tl_loop(
        "aggregate-run",
        parent_plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    file_pr_calls = [arguments for name, arguments in transport.calls if name == "file_pr"]
    assert len(file_pr_calls) == 1
    assert file_pr_calls[0]["base_branch"] == "main"
    assert first.final_state.fsm.phase is TLPhase.TLWaiting
    assert second.final_state.fsm.phase is TLPhase.TLWaiting
    parent_slice = second.final_state.slices["aggregate-child"]
    assert parent_slice.status is SliceStatus.IN_REVIEW
    assert parent_slice.pr_number == 43
    assert parent_slice.dispatch_agent_id == "aggregate-run:aggregate-child:integration"
    assert second.final_state.slices["later-stage"].status is SliceStatus.PENDING
    child = load_state(parent_dir / "aggregate-child" / "run.json")
    assert child.integration.aggregate_pr_number == parent_slice.pr_number
    assert child.integration.integration_owner_id == parent_slice.dispatch_agent_id
    assert child.integration.integration_owner_run_id == "aggregate-child"
    assert child.integration.integration_owner_branch == "main.aggregate-child"
    assert child.integration.integration_owner_worktree
    candidate = second.final_state.integration.candidates["aggregate-child"]
    assert candidate.integration_owner_run_id == "aggregate-child"
    assert candidate.integration_owner_branch == "main.aggregate-child"
    assert candidate.integration_owner_worktree == child.integration.integration_owner_worktree


def test_parent_serializes_aggregate_merge_after_base_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_plan = _plan()
    parent_dir = tmp_path / "serialized-run"
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(RecordingTransport()),
        config=_config(),
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    child_state = child_store.load()
    child_slices = dict(child_state.slices)
    child_slices["leaf-a"] = replace(child_slices["leaf-a"], pr_number=42, reviewed_head="head-a")
    child_store.checkpoint(
        child_state.fsm,
        child_slices,
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    transport = IntegrationTransport(
        snapshots=[
            {
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
        ]
    )
    plan = WorkPlan(
        sub_tls=(SubTLTask("aggregate-child", child_plan, source=SyntheticQueue([]), order=1),)
    )
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        keep_alive_on_waiting=False,
    )
    verified: list[tuple[str, str, str, str]] = []
    from tl_loop.loop import driver

    original_verify = driver.verify_integration

    def record_verify(state: object, **live: str) -> object:
        verified.append(
            (
                live["base_sha"],
                live["head_sha"],
                live["merge_tree_sha"],
                live["ci_status"],
            )
        )
        return original_verify(state, **live)

    monkeypatch.setattr(driver, "verify_integration", record_verify)
    run_tl_loop(
        "serialized-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    store = RunStore("serialized-run", tmp_path)
    state = store.load()
    current = state.slices["aggregate-child"]
    updated = replace(
        current,
        verdict=Verdict.GO,
        review_patch_digests={"head-a": "patch-a"},
        ci_state={"head-a": "success"},
    )
    store.checkpoint(
        state.fsm,
        {**state.slices, "aggregate-child": updated},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )

    result = run_tl_loop(
        "serialized-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert result.final_state.slices["aggregate-child"].status is SliceStatus.MERGED
    assert verified == [("base-a", "head-a", "tree-a", "success")]
    candidate = result.final_state.integration.candidates["aggregate-child"]
    assert candidate.validated_base_sha == "base-a"
    assert candidate.head_sha == "head-a"
    assert candidate.patch_digest == "patch-a"
    assert candidate.merge_tree_sha == "tree-a"
    assert candidate.ci_status == "success"
    assert [name for name, _ in transport.calls if name == "merge_pr"] == ["merge_pr"]
    assert next(arguments for name, arguments in transport.calls if name == "merge_pr") == {
        "pr_number": 43,
        "strategy": "merge",
        "expected_base_sha": "base-a",
        "expected_head_sha": "head-a",
        "expected_patch_digest": "patch-a",
        "expected_merge_tree_sha": "tree-a",
    }
    event_types = {
        arguments["event_type"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    }
    assert {
        "tl.stage_started",
        "tl.stage_completed",
        "tl.aggregate_pr_opened",
        "tl.integration_validated",
        "tl.integration_revalidated",
        "tl.merge_decided",
    } <= event_types


def test_restart_during_merging_reconciles_without_duplicate_merge(tmp_path: Path) -> None:
    run_id = "merging-restart-run"
    plan = WorkPlan(sub_tls=(SubTLTask("aggregate-child", WorkPlan(), order=1),))
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        keep_alive_on_waiting=False,
    )
    initial = _initial_slices(plan, config, tmp_path, run_id)
    initial["aggregate-child"].update(
        {
            "status": "in_review",
            "pr_number": 42,
            "reviewed_head": "head-a",
            "verdict": "GO",
            "review_patch_digests": {"head-a": "patch-a"},
            "ci_state": {"head-a": "success"},
        }
    )
    transport = IntegrationTransport(snapshots=[{}])
    first = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
        initial_slices=initial,
    )
    store = RunStore(run_id, tmp_path)
    state = store.load()
    store.checkpoint(
        state.fsm,
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=replace(
            state.integration,
            lifecycle=IntegrationLifecycle.MERGING,
            head_sha="head-a",
            patch_digest="patch-a",
            validated_base_sha="base-a",
            merge_tree_sha="tree-a",
            ci_status="success",
            stage_verification="passed",
        ),
    )
    transport.snapshots.append(
        {
            "merged": True,
            "head_sha": "head-a",
            "base_sha": "base-a",
        }
    )

    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert first.final_state.fsm.phase is TLPhase.TLWaiting
    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert result.final_state.slices["aggregate-child"].status is SliceStatus.MERGED
    assert [name for name, _ in transport.calls if name == "merge_pr"] == []
    assert any(
        arguments["event_type"] == "tl.merge_reconciled"
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    )


def test_parent_requeues_aggregate_when_base_changes_before_merge(tmp_path: Path) -> None:
    child_plan = _plan()
    parent_dir = tmp_path / "serialized-run"
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(RecordingTransport()),
        config=_config(),
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    child_state = child_store.load()
    child_store.checkpoint(
        child_state.fsm,
        {
            **child_state.slices,
            "leaf-a": replace(child_state.slices["leaf-a"], pr_number=42, reviewed_head="head-a"),
        },
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    transport = IntegrationTransport(
        snapshots=[
            {
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": "head-a",
                "base_sha": "base-b",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-b",
                "ci_status": "success",
            },
            {
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": "head-a",
                "base_sha": "base-b",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-b",
                "ci_status": "success",
            },
        ]
    )
    plan = WorkPlan(
        sub_tls=(SubTLTask("aggregate-child", child_plan, source=SyntheticQueue([]), order=1),)
    )
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        max_base_revalidations=1,
        keep_alive_on_waiting=False,
    )
    run_tl_loop(
        "serialized-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    store = RunStore("serialized-run", tmp_path)
    state = store.load()
    current = state.slices["aggregate-child"]
    store.checkpoint(
        state.fsm,
        {
            **state.slices,
            "aggregate-child": replace(
                current,
                verdict=Verdict.GO,
                review_patch_digests={"head-a": "patch-a"},
                ci_state={"head-a": "success"},
            ),
        },
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )

    first_revalidation = run_tl_loop(
        "serialized-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    assert first_revalidation.final_state.integration.base_revalidation_count == 1
    result = run_tl_loop(
        "serialized-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert result.final_state.slices["aggregate-child"].status is SliceStatus.IN_REVIEW
    assert [name for name, _ in transport.calls if name == "merge_pr"] == []
    assert (
        result.final_state.integration.sub_tl_states["aggregate-child"]
        is IntegrationLifecycle.NEEDS_BASE_REVALIDATION
    )
    assert result.final_state.integration.validated_base_sha is None
    assert result.final_state.integration.base_revalidation_count == 1
    assert (
        GateState(name="tl-integration-revalidation", status=GateStatus.PENDING)
        in result.final_state.gates
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (("head_sha", "head-b"), ("patch_digest", "patch-b")),
)
def test_head_or_patch_mismatch_opens_conflict_gate(tmp_path: Path, field: str, value: str) -> None:
    parent_dir = tmp_path / "mismatch-run"
    child_plan = _plan()
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(RecordingTransport()),
        config=_config(),
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    child_state = child_store.load()
    child_store.checkpoint(
        child_state.fsm,
        {
            **child_state.slices,
            "leaf-a": replace(child_state.slices["leaf-a"], pr_number=42, reviewed_head="head-a"),
        },
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    second_snapshot = {
        "head_sha": "head-a",
        "base_sha": "base-a",
        "patch_digest": "patch-a",
        "merge_tree_sha": "tree-a",
        "ci_status": "success",
    }
    second_snapshot[field] = value
    transport = IntegrationTransport(
        snapshots=[
            {
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            second_snapshot,
        ]
    )
    plan = WorkPlan(
        sub_tls=(SubTLTask("aggregate-child", child_plan, source=SyntheticQueue([]), order=1),)
    )
    config = TLLoopConfig(
        max_parallel_slices=1,
        max_integration_repairs=0,
        poll_interval=0.001,
        keep_alive_on_waiting=False,
    )
    run_tl_loop(
        "mismatch-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    store = RunStore("mismatch-run", tmp_path)
    state = store.load()
    store.checkpoint(
        state.fsm,
        {
            **state.slices,
            "aggregate-child": replace(
                state.slices["aggregate-child"],
                verdict=Verdict.GO,
                review_patch_digests={"head-a": "patch-a"},
                ci_state={"head-a": "success"},
            ),
        },
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )

    result = run_tl_loop(
        "mismatch-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert (
        GateState(name="tl-integration-conflict", status=GateStatus.PENDING)
        in result.final_state.gates
    )
    assert result.final_state.integration.lifecycle is IntegrationLifecycle.INTEGRATION_CONFLICT
    assert result.final_state.slices["aggregate-child"].dispatch_agent_id == (
        "mismatch-run:aggregate-child:integration"
    )
    event_types = [
        arguments["event_type"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    ]
    assert "tl.integration_base_invalidated" not in event_types
    assert "tl.gate_opened" in event_types


def test_integration_conflict_repairs_same_aggregate_owner(tmp_path: Path) -> None:
    child_plan = _plan()
    parent_dir = tmp_path / "conflict-run"
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(RecordingTransport()),
        config=_config(),
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    child_state = child_store.load()
    child_store.checkpoint(
        child_state.fsm,
        {
            **child_state.slices,
            "leaf-a": replace(child_state.slices["leaf-a"], pr_number=42, reviewed_head="head-a"),
        },
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    backend = ReviewBackend(
        [
            RlmResponse(
                {
                    "root_cause": "The aggregate PR conflicts with the parent base",
                    "proposed_solution": "Rebase the existing aggregate PR and resolve the conflict",
                    "read_first": ["tl-loop/aggregate-child"],
                    "steps": ["Resolve the aggregate conflict"],
                    "verify": ["just tl-loop-test"],
                    "boundary": ["Only edit tl-loop/aggregate-child"],
                    "done_criteria": ["The aggregate PR applies cleanly"],
                }
            )
        ]
    )
    transport = IntegrationTransport(
        snapshots=[
            {
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "open": True,
                "merged": False,
                "head_branch": "main.aggregate-child",
                "head_sha": "head-a",
            },
        ],
        merge_response={"success": False, "error": "merge conflict with parent base"},
    )
    plan = WorkPlan(
        sub_tls=(SubTLTask("aggregate-child", child_plan, source=SyntheticQueue([]), order=1),)
    )
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        review_model_choice=_review_choice(backend),
        keep_alive_on_waiting=False,
    )
    run_tl_loop(
        "conflict-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    store = RunStore("conflict-run", tmp_path)
    state = store.load()
    store.checkpoint(
        state.fsm,
        {
            **state.slices,
            "aggregate-child": replace(
                state.slices["aggregate-child"],
                verdict=Verdict.GO,
                review_patch_digests={"head-a": "patch-a"},
                ci_state={"head-a": "success"},
            ),
        },
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )

    result = run_tl_loop(
        "conflict-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    current = result.final_state.slices["aggregate-child"]
    assert current.status is SliceStatus.REPAIRING
    assert current.repair_attempts == 1
    assert current.dispatch_agent_id == "conflict-run:aggregate-child:integration"
    assert result.final_state.integration.lifecycle is IntegrationLifecycle.REPAIRING_AGGREGATE
    assert result.final_state.integration.head_sha is None
    assert [name for name, _ in transport.calls if name == "resume_pr"] == ["resume_pr"]
    assert not any(name in {"spawn_leaf", "spawn_worker"} for name, _ in transport.calls)


def test_exhausted_integration_conflict_opens_human_gate(tmp_path: Path) -> None:
    child_plan = _plan()
    parent_dir = tmp_path / "gate-run"
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(RecordingTransport()),
        config=_config(),
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    child_state = child_store.load()
    child_store.checkpoint(
        child_state.fsm,
        {
            **child_state.slices,
            "leaf-a": replace(child_state.slices["leaf-a"], pr_number=42, reviewed_head="head-a"),
        },
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    transport = IntegrationTransport(
        snapshots=[
            {
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "open": True,
                "merged": False,
                "head_branch": "main.aggregate-child",
                "head_sha": "head-a",
            },
        ],
        merge_response={"success": False, "error": "merge conflict with parent base"},
    )
    plan = WorkPlan(
        sub_tls=(SubTLTask("aggregate-child", child_plan, source=SyntheticQueue([]), order=1),)
    )
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        max_integration_repairs=0,
        keep_alive_on_waiting=False,
    )
    run_tl_loop(
        "gate-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    store = RunStore("gate-run", tmp_path)
    state = store.load()
    store.checkpoint(
        state.fsm,
        {
            **state.slices,
            "aggregate-child": replace(
                state.slices["aggregate-child"],
                verdict=Verdict.GO,
                review_patch_digests={"head-a": "patch-a"},
                ci_state={"head-a": "success"},
            ),
        },
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )

    result = run_tl_loop(
        "gate-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert (
        GateState(name="tl-integration-conflict", status=GateStatus.PENDING)
        in result.final_state.gates
    )
    assert result.final_state.integration.lifecycle is IntegrationLifecycle.INTEGRATION_CONFLICT
    assert result.final_state.slices["aggregate-child"].status is SliceStatus.IN_REVIEW
    assert not any(name == "resume_pr" for name, _ in transport.calls)


def test_same_order_event_consumers_require_isolated_sources(tmp_path: Path) -> None:
    eventful = WorkPlan(workers=(WorkerTask("worker", "consume"),))
    plan = WorkPlan(
        sub_tls=(
            SubTLTask("event-a", eventful),
            SubTLTask("event-b", eventful),
        )
    )

    with pytest.raises(TLLoopError, match="isolated sources"):
        run_tl_loop(
            "routing-run",
            plan,
            SyntheticQueue([]),
            EffectClient(RecordingTransport()),
            config=TLLoopConfig(max_parallel_slices=2),
            root_dir=tmp_path,
        )


__all__ = [
    "RecordingTransport",
    "SyntheticQueue",
    "test_active_loop_dispatches_direct_children_and_merges_leaf",
]
