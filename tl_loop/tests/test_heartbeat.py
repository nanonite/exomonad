"""Configured idle-wave heartbeat coverage."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import pytest

import tl_loop.loop.driver as driver_module
from tl_loop.client.effects import EffectClient, ToolResult, ToolUnavailableError
from tl_loop.client.transport import JsonObject
from tl_loop.events.types import BlockCause
from tl_loop.fsm.recovery import begin_recovery
from tl_loop.loop.abandon import abandon_slice
from tl_loop.loop.heartbeat import (
    HeartbeatConfig,
    HeartbeatError,
    _classify_missing_handoff,
    _poll_workers,
    _result_object,
    heartbeat_once,
)
from tl_loop.loop.work_clock import ActiveWorkClock
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.schema import (
    ActionKind,
    ActionPhase,
    ActionState,
    DeadlineLedger,
    GateStatus,
    ParkCause,
    RunState,
    SliceStatus,
)
from tl_loop.state.store import RunStore
from tl_loop.tests.checkpoint_fixtures import create
from tl_loop.tests.test_driver import _seed_deep_nested_recovery_run


class _MutableClock:
    """A monotonic clock a test can advance by hand."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@dataclass
class HeartbeatTransport:
    """Effect transport with deterministic liveness and watcher responses."""

    pane_alive: bool = True
    worker_present: bool = True
    worker_invocation_id: str | None = None
    report_missing_agents: bool = False
    pr_state: str = "open"
    merged: bool = False
    omit_merged: bool = False
    head_reachable: bool = True
    publication_ownership_verified: bool | None = None
    publication_ownership_error: str = ""
    omit_ownership_error: bool = False
    ownership_error_raw: object | None = None
    publication: JsonObject | None = None
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
            worker = {
                "name": worker_name,
                "pane_alive": self.pane_alive,
                "lifecycle_status": "ACTIVE",
            }
            if self.worker_invocation_id is not None:
                worker["invocation_id"] = self.worker_invocation_id
            return cast(
                JsonObject,
                {
                    "success": True,
                    "result": {
                        "workers": [worker] if self.worker_present else [],
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
            result: dict[str, object] = {
                "pr_number": 42,
                "head_sha": "head-new",
                "head_branch": "task/slice-a",
                "base_branch": "main",
                "review_state": "approved",
                "ci_status": "success",
                "found": True,
                "pr_state": self.pr_state,
                "head_reachable": self.head_reachable,
                "evidence_error": (
                    "pr_head_unreachable: object missing" if not self.head_reachable else ""
                ),
                "publication_ownership_verified": self.publication_ownership_verified,
            }
            if not self.omit_merged:
                result["merged"] = self.merged
            if not self.omit_ownership_error:
                result["publication_ownership_error"] = (
                    self.publication_ownership_error
                    if self.ownership_error_raw is None
                    else self.ownership_error_raw
                )
            if self.publication is not None:
                result["publication"] = self.publication
            return cast(JsonObject, {"success": True, "result": result})
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
        if tool_name == "chainlink_issue_list":
            return cast(JsonObject, {"success": True, "result": []})
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
    assert parked.park_audit is not None
    assert parked.park_audit["reason"] == "pane_alive_false_during_current_invocation"
    assert parked.park_audit["activity_identity_sha256"]
    assert result.parked_slice_ids == ("slice-a",)
    assert [event.kind for event in result.events] == ["worker.dead"]
    assert [name for name, _ in transport.calls] == [
        "poll_workers",
        "chainlink_issue_list",
        "chainlink_issue_create",
        "emit_controller_event",
        "emit_controller_event",
    ]


@pytest.mark.parametrize("proof_failure", ("missing", "corrupt", "conflicting", "stale_summary"))
def test_dead_nested_controller_is_held_when_declared_recovery_proof_is_unavailable(
    tmp_path: Path,
    proof_failure: str,
) -> None:
    """An asserted nested recovery cannot degrade into a generic parent stall."""
    conflict = "inner_parent" if proof_failure == "conflicting" else None
    _plan, config, store, _outer_store, inner_store = _seed_deep_nested_recovery_run(
        tmp_path, conflict=conflict
    )
    state = store.load()
    parent_slice = replace(
        state.slices["outer"],
        status=SliceStatus.SPAWNED,
        park_cause=None,
        park_audit=None,
    )
    integration = state.integration
    if proof_failure == "missing":
        inner_store.path.unlink()
    elif proof_failure == "corrupt":
        inner_store.path.write_text("{not-json", encoding="utf-8")
    elif proof_failure == "stale_summary":
        summary = integration.sub_tl_recovery["outer"]
        integration = replace(
            integration,
            sub_tl_recovery={"outer": replace(summary, next_probe_at=123.0)},
        )
    state = store.checkpoint(
        state.recursive_fsm or state.fsm,
        {**state.slices, "outer": parent_slice},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=1,
        ordered_stages=state.ordered_stages,
        integration=integration,
    )
    manual_gate = "manual-nested-recovery-review"
    state = store.set_gate(manual_gate, GateStatus.PENDING)
    active_work = ActiveWorkClock(_MutableClock(100.0))
    active_work.mark_active("outer")
    transport = HeartbeatTransport(pane_alive=False)
    effects = EffectClient(transport)

    result = heartbeat_once(
        state,
        store,
        effects,
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=1.0),
        now=10.0,
        project_root=config.project_root,
        controller_agent_id=config.agent_id or config.run_id,
        active_work=active_work,
    )

    held = result.state.slices["outer"]
    assert held.status is SliceStatus.SPAWNED
    assert held.park_cause is None
    assert result.parked_slice_ids == ()
    assert active_work.snapshot() == {}
    assert held.reconciliation is not None
    expected_reason = {
        "missing": "checkpoint_missing",
        "corrupt": "checkpoint_unavailable",
        "conflicting": "child_identity_conflict",
        "stale_summary": "summary_conflict",
    }[proof_failure]
    assert held.reconciliation["nested_recovery_hold"]["reason_code"] == expected_reason
    held_events = [event for event in result.events if event.kind == "nested_recovery.proof_held"]
    assert len(held_events) == 1
    assert held_events[0].payload["reason_code"] == expected_reason
    assert not any(name == "chainlink_issue_create" for name, _ in transport.calls)
    assert (
        next(gate for gate in result.state.gates if gate.name == manual_gate).status
        is GateStatus.PENDING
    )
    assert "worker.dead" not in [event.kind for event in result.events]

    repeated = heartbeat_once(
        result.state,
        store,
        EffectClient(HeartbeatTransport(pane_alive=False)),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=1.0),
        now=20.0,
        project_root=config.project_root,
        controller_agent_id=config.agent_id or config.run_id,
    )
    assert repeated.state.slices["outer"].status is SliceStatus.SPAWNED
    assert repeated.state.slices["outer"].park_cause is None
    assert repeated.parked_slice_ids == ()
    assert not any(event.kind == "worker.dead" for event in repeated.events)
    assert (
        repeated.state.slices["outer"].reconciliation["nested_recovery_hold"]
        == held.reconciliation["nested_recovery_hold"]
    )
    assert not any(event.kind == "nested_recovery.proof_held" for event in repeated.events)


@pytest.mark.parametrize("hold_operation", ("create", "clear"))
@pytest.mark.parametrize("concurrent_operation", ("invocation", "abandon", "proof_change"))
def test_nested_recovery_hold_checkpoint_reassesses_concurrent_owner_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hold_operation: str,
    concurrent_operation: str,
) -> None:
    """Hold writes preserve a winner from invocation binding or operator abandon."""
    _plan, config, store, _outer_store, inner_store = _seed_deep_nested_recovery_run(tmp_path)
    seeded_child = inner_store.load()
    seeded_leaf = seeded_child.slices["leaf-a"]
    inner_bytes = inner_store.path.read_bytes()
    inner_store.path.unlink()
    state = store.load()
    state = _activate_nested_parent(store, state)
    state = store.set_gate("unrelated-rejected-gate", GateStatus.REJECTED)
    effects = EffectClient(HeartbeatTransport(pane_alive=False))

    if hold_operation == "clear":
        held = heartbeat_once(
            state,
            store,
            effects,
            HeartbeatConfig(interval_seconds=1.0, stall_threshold_seconds=1.0),
            now=100.0,
            project_root=config.project_root,
            controller_agent_id=config.agent_id or config.run_id,
        )
        assert "nested_recovery_hold" in held.state.slices["outer"].reconciliation
        inner_store.path.write_bytes(inner_bytes)
        state = store.load()

    before_outer = state.slices["outer"]
    before_later = state.slices["later"]
    before_budgets = state.budgets
    before_leaf = seeded_leaf
    inner_journal_path = inner_store.run_dir / "action-journal.json"
    before_inner_journal = inner_journal_path.read_bytes() if inner_journal_path.exists() else None
    root_journal_path = store.run_dir / "action-journal.json"
    before_root_journal = root_journal_path.read_bytes() if root_journal_path.exists() else None

    transport = HeartbeatTransport(pane_alive=False)
    effects = EffectClient(transport)
    concurrent_store = RunStore(store.run_id, store.root_dir)
    raced = False
    root_journal_after_concurrent = None
    original_checkpoint = RunStore.checkpoint

    def interleaved_checkpoint(
        checkpoint_store: RunStore,
        fsm,
        slices,
        budgets,
        offset,
        **kwargs,
    ):
        nonlocal raced, root_journal_after_concurrent
        target = slices.get("outer")
        persisted = (
            store.load().slices["outer"] if checkpoint_store.run_id == store.run_id else None
        )
        target_has_hold = (
            target is not None
            and target.reconciliation is not None
            and "nested_recovery_hold" in target.reconciliation
        )
        persisted_has_hold = (
            persisted is not None
            and persisted.reconciliation is not None
            and "nested_recovery_hold" in persisted.reconciliation
        )
        hold_write = checkpoint_store.run_id == store.run_id and (
            (hold_operation == "create" and target_has_hold and not persisted_has_hold)
            or (hold_operation == "clear" and persisted_has_hold and not target_has_hold)
        )
        if hold_write and not raced:
            raced = True
            if concurrent_operation == "invocation":
                driver_module._checkpoint_ordered_sub_tl_invocation(
                    concurrent_store, "outer", "confirmed-concurrent-invocation"
                )
            elif concurrent_operation == "abandon":
                abandoned = abandon_slice(
                    config.project_root.parent,
                    store.run_id,
                    "outer",
                    effects=EffectClient(HeartbeatTransport()),
                )
                assert abandoned["status"] == "abandoned"
                root_journal_after_concurrent = root_journal_path.read_bytes()
            elif hold_operation == "create":
                inner_store.path.write_bytes(inner_bytes)
            else:
                inner_store.path.unlink()
        return original_checkpoint(checkpoint_store, fsm, slices, budgets, offset, **kwargs)

    monkeypatch.setattr(RunStore, "checkpoint", interleaved_checkpoint)
    result = heartbeat_once(
        state,
        store,
        effects,
        HeartbeatConfig(interval_seconds=1.0, stall_threshold_seconds=1.0),
        now=110.0,
        project_root=config.project_root,
        controller_agent_id=config.agent_id or config.run_id,
    )

    assert raced
    latest = store.load()
    outer = latest.slices["outer"]
    if concurrent_operation == "invocation":
        assert outer.dispatch_invocation_id == "confirmed-concurrent-invocation"
        record = next(
            record
            for records in latest.recursive_fsm.pending_by_order.values()
            for record in records
            if record.child_id == "outer"
        )
        assert record.invocation_id == "confirmed-concurrent-invocation"
    elif concurrent_operation == "abandon":
        assert outer.status is SliceStatus.PARKED
        assert outer.park_cause is ParkCause.ATTEMPT_ABANDONED
        assert outer.park_audit["reason"] == "operator_requested"
    assert result.state.revision == latest.revision
    assert latest.budgets == before_budgets
    assert latest.slices["later"] == before_later
    assert (
        next(gate for gate in latest.gates if gate.name == "unrelated-rejected-gate").status
        is GateStatus.REJECTED
    )
    assert latest.slices["outer"].attempts == before_outer.attempts
    assert latest.slices["outer"].recovery == before_outer.recovery
    child_expected_present = (
        hold_operation == "clear" and concurrent_operation != "proof_change"
    ) or (hold_operation == "create" and concurrent_operation == "proof_change")
    assert inner_store.path.exists() is child_expected_present
    if child_expected_present:
        assert inner_store.path.read_bytes() == inner_bytes
        assert inner_store.load().slices["leaf-a"] == before_leaf
    assert (
        inner_journal_path.read_bytes() if inner_journal_path.exists() else None
    ) == before_inner_journal
    if concurrent_operation == "abandon":
        assert root_journal_after_concurrent is not None
        assert root_journal_path.read_bytes() == root_journal_after_concurrent
    else:
        assert (
            root_journal_path.read_bytes() if root_journal_path.exists() else None
        ) == before_root_journal
    assert not any(name in {"spawn_leaf", "spawn_worker"} for name, _ in transport.calls)
    if hold_operation == "create" and concurrent_operation == "invocation":
        assert outer.reconciliation["nested_recovery_hold"]["reason_code"] == (
            "child_dispatch_registration_conflict"
        )
    if hold_operation == "clear" and concurrent_operation == "abandon":
        assert outer.reconciliation["nested_recovery_hold"]["reason_code"] == "checkpoint_missing"
    if concurrent_operation == "proof_change":
        if hold_operation == "create":
            assert (
                outer.reconciliation is None or "nested_recovery_hold" not in outer.reconciliation
            )
            assert any(event.kind == "nested_recovery.wait" for event in result.events)
        else:
            assert outer.reconciliation["nested_recovery_hold"]["reason_code"] == (
                "checkpoint_missing"
            )


def test_full_heartbeat_preserves_operator_abandon_during_nested_hold_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale pane observation cannot overwrite an abandon winning hold clearance."""
    _plan, config, store, _outer_store, inner_store = _seed_deep_nested_recovery_run(tmp_path)
    inner_store.path.unlink()
    state = _activate_nested_parent(store, store.load())
    first = heartbeat_once(
        state,
        store,
        EffectClient(HeartbeatTransport(pane_alive=True)),
        HeartbeatConfig(interval_seconds=1.0, stall_threshold_seconds=1.0),
        now=100.0,
        project_root=config.project_root,
        controller_agent_id=config.agent_id or config.run_id,
    )
    assert "nested_recovery_hold" in first.state.slices["outer"].reconciliation

    latest = store.load()
    integration = replace(
        latest.integration,
        sub_tl_states={**latest.integration.sub_tl_states, "outer": IntegrationLifecycle.RUNNING},
        sub_tl_recovery={},
    )
    state = store.checkpoint(
        latest.recursive_fsm or latest.fsm,
        latest.slices,
        latest.budgets,
        latest.events.last_consumed_offset,
        ordered_stages=latest.ordered_stages,
        integration=integration,
        plan_manifest=latest.plan_manifest,
    )
    root_journal = store.run_dir / "action-journal.json"
    heartbeat_transport = HeartbeatTransport(pane_alive=False)
    effects = EffectClient(heartbeat_transport)
    original_checkpoint = RunStore.checkpoint
    abandoned = False
    journal_after_abandon: bytes | None = None

    class AbandonTransport:
        def call_tool(self, role, name, tool_name, arguments):
            del role, name, tool_name, arguments
            return cast(JsonObject, {"success": True, "result": {}})

    def interleaved_checkpoint(checkpoint_store, fsm, slices, budgets, offset, **kwargs):
        nonlocal abandoned, journal_after_abandon
        target = slices.get("outer")
        persisted = (
            store.load().slices["outer"] if checkpoint_store.run_id == store.run_id else None
        )
        if (
            not abandoned
            and checkpoint_store.run_id == store.run_id
            and target is not None
            and persisted is not None
            and "nested_recovery_hold" in (persisted.reconciliation or {})
            and "nested_recovery_hold" not in (target.reconciliation or {})
        ):
            abandoned = True
            result = abandon_slice(
                store.root_dir.parent.parent,
                store.run_id,
                "outer",
                effects=EffectClient(AbandonTransport()),
            )
            assert result["status"] == "abandoned"
            journal_after_abandon = root_journal.read_bytes()
        return original_checkpoint(checkpoint_store, fsm, slices, budgets, offset, **kwargs)

    monkeypatch.setattr(RunStore, "checkpoint", interleaved_checkpoint)
    result = heartbeat_once(
        state,
        store,
        effects,
        HeartbeatConfig(interval_seconds=1.0, stall_threshold_seconds=1.0),
        now=110.0,
        project_root=config.project_root,
        controller_agent_id=config.agent_id or config.run_id,
    )

    latest = store.load()
    outer = latest.slices["outer"]
    assert abandoned
    assert outer.status is SliceStatus.PARKED
    assert outer.park_cause is ParkCause.ATTEMPT_ABANDONED
    assert outer.park_audit["reason"] == "operator_requested"
    assert result.state.revision == latest.revision
    assert result.parked_slice_ids == ()
    assert not any(event.kind == "worker.dead" for event in result.events)
    assert root_journal.read_bytes() == journal_after_abandon
    assert not any(
        name in {"chainlink_issue_create", "emit_controller_event", "cleanup"}
        for name, _ in heartbeat_transport.calls
    )
    assert not any(name in {"spawn_leaf", "spawn_worker"} for name, _ in heartbeat_transport.calls)

    repeated = heartbeat_once(
        result.state,
        store,
        effects,
        HeartbeatConfig(interval_seconds=1.0, stall_threshold_seconds=1.0),
        now=120.0,
        project_root=config.project_root,
        controller_agent_id=config.agent_id or config.run_id,
    )
    persisted = store.load().slices["outer"]
    assert persisted.status is SliceStatus.PARKED
    assert persisted.park_cause is ParkCause.ATTEMPT_ABANDONED
    assert repeated.parked_slice_ids == ()
    assert not any(event.kind == "worker.dead" for event in repeated.events)
    assert root_journal.read_bytes() == journal_after_abandon


def test_full_heartbeat_defers_predecessor_pane_observation_after_invocation_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A predecessor's dead-pane row cannot stall a newly bound invocation."""
    _plan, config, store, _outer_store, inner_store = _seed_deep_nested_recovery_run(tmp_path)
    inner_store.path.unlink()
    state = _activate_nested_parent(store, store.load())
    first = heartbeat_once(
        state,
        store,
        EffectClient(HeartbeatTransport(pane_alive=True)),
        HeartbeatConfig(interval_seconds=1.0, stall_threshold_seconds=1.0),
        now=100.0,
        project_root=config.project_root,
        controller_agent_id=config.agent_id or config.run_id,
    )
    assert "nested_recovery_hold" in first.state.slices["outer"].reconciliation

    latest = store.load()
    integration = replace(
        latest.integration,
        sub_tl_states={**latest.integration.sub_tl_states, "outer": IntegrationLifecycle.RUNNING},
        sub_tl_recovery={},
    )
    state = store.checkpoint(
        latest.recursive_fsm or latest.fsm,
        latest.slices,
        latest.budgets,
        latest.events.last_consumed_offset,
        ordered_stages=latest.ordered_stages,
        integration=integration,
        plan_manifest=latest.plan_manifest,
    )
    predecessor = state.slices["outer"].dispatch_invocation_id
    assert predecessor is not None
    concurrent_store = RunStore(store.run_id, store.root_dir)
    transport = HeartbeatTransport(pane_alive=False, worker_invocation_id=predecessor)
    original_checkpoint = RunStore.checkpoint
    rebound = False

    def interleaved_checkpoint(checkpoint_store, fsm, slices, budgets, offset, **kwargs):
        nonlocal rebound
        target = slices.get("outer")
        persisted = (
            store.load().slices["outer"] if checkpoint_store.run_id == store.run_id else None
        )
        clearing_hold = (
            checkpoint_store.run_id == store.run_id
            and target is not None
            and persisted is not None
            and "nested_recovery_hold" in (persisted.reconciliation or {})
            and "nested_recovery_hold" not in (target.reconciliation or {})
        )
        if clearing_hold and not rebound:
            rebound = True
            driver_module._checkpoint_ordered_sub_tl_invocation(
                concurrent_store, "outer", "newer-outer-invocation"
            )
        return original_checkpoint(checkpoint_store, fsm, slices, budgets, offset, **kwargs)

    monkeypatch.setattr(RunStore, "checkpoint", interleaved_checkpoint)
    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=1.0, stall_threshold_seconds=1.0),
        now=110.0,
        project_root=config.project_root,
        controller_agent_id=config.agent_id or config.run_id,
    )

    latest = store.load()
    outer = latest.slices["outer"]
    assert rebound
    assert outer.status is SliceStatus.SPAWNED
    assert outer.dispatch_invocation_id == "newer-outer-invocation"
    assert outer.park_cause is None
    assert result.parked_slice_ids == ()
    assert not any(event.kind == "worker.dead" for event in result.events)
    assert not any(
        name in {"chainlink_issue_create", "emit_controller_event", "cleanup"}
        for name, _ in transport.calls
    )
    assert not any(name in {"spawn_leaf", "spawn_worker"} for name, _ in transport.calls)


def _activate_nested_parent(store: RunStore, state):
    """Make the retained child scope eligible for the production heartbeat pass."""
    active = replace(
        state.slices["outer"],
        status=SliceStatus.SPAWNED,
        park_cause=None,
        park_audit=None,
    )
    return store.checkpoint(
        state.recursive_fsm or state.fsm,
        {**state.slices, "outer": active},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )


def test_nested_hold_conflict_retries_are_bounded_and_return_latest_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated concurrent revisions do not cause stale writes or unbounded retries."""
    _plan, config, store, _outer_store, inner_store = _seed_deep_nested_recovery_run(tmp_path)
    inner_store.path.unlink()
    state = _activate_nested_parent(store, store.load())
    transport = HeartbeatTransport(pane_alive=False)
    concurrent_store = RunStore(store.run_id, store.root_dir)
    checkpoints = 0
    original_checkpoint = RunStore.checkpoint

    def keep_conflicting(
        checkpoint_store: RunStore,
        fsm,
        slices,
        budgets,
        offset,
        **kwargs,
    ):
        nonlocal checkpoints
        target = slices.get("outer")
        persisted_reconciliation = store.load().slices["outer"].reconciliation or {}
        if (
            checkpoint_store.run_id == store.run_id
            and target is not None
            and target.reconciliation is not None
            and "nested_recovery_hold" in target.reconciliation
            and "nested_recovery_hold" not in persisted_reconciliation
        ):
            checkpoints += 1
            concurrent_store.set_gate(f"concurrent-revision-{checkpoints}", GateStatus.PENDING)
        return original_checkpoint(checkpoint_store, fsm, slices, budgets, offset, **kwargs)

    monkeypatch.setattr(RunStore, "checkpoint", keep_conflicting)
    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=1.0, stall_threshold_seconds=1.0),
        now=100.0,
        project_root=config.project_root,
        controller_agent_id=config.agent_id or config.run_id,
    )

    latest = store.load()
    assert checkpoints == 3
    assert result.state.revision == latest.revision
    assert latest.slices["outer"].status is SliceStatus.SPAWNED
    assert latest.slices["outer"].park_cause is None
    assert latest.slices["outer"].reconciliation is None
    assert len([gate for gate in latest.gates if gate.name.startswith("concurrent-revision-")]) == 3
    assert any(event.kind == "nested_recovery.proof_held" for event in result.events)
    assert not any(name in {"spawn_leaf", "spawn_worker"} for name, _ in transport.calls)


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


def test_review_replay_cannot_restore_stale_head_after_heartbeat_reconciliation(
    tmp_path: Path,
) -> None:
    store, state = _state(
        tmp_path,
        status="in_review",
        heartbeat_at=0.0,
        pr_number=42,
        reviewed_head="head-old",
    )
    result = heartbeat_once(
        state,
        store,
        EffectClient(HeartbeatTransport()),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
        review_replay=lambda replay_state, _slice_state, _watcher: replay_state,
    )

    assert result.state.slices["slice-a"].reviewed_head == "head-new"


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


def test_heartbeat_parks_a_closed_pr_whose_merge_is_not_spelled_out(
    tmp_path: Path,
) -> None:
    """A proto3 boolean answers without a key when it is false.

    Requiring the literal `false` made every closed, unmerged PR
    unclassifiable: the heartbeat polled it for ever and never parked.
    """
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
        EffectClient(HeartbeatTransport(pr_state="closed", omit_merged=True)),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    parked = result.state.slices["slice-a"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.PR_CLOSED_UNMERGED


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


def _refused_publication(invocation_id: str = "inv-other") -> JsonObject:
    return {
        "invocation_id": invocation_id,
        "slice_id": "slice-a",
        "author_agent": "agent-slice-a",
        "succession_invocation_ids": [],
    }


def _heartbeat_in_review(tmp_path: Path, invocation_id: str) -> tuple[RunStore, RunState]:
    store, state = _state(
        tmp_path,
        status="in_review",
        heartbeat_at=0.0,
        pr_number=42,
        reviewed_head="head-old",
    )
    current = state.slices["slice-a"]
    state = store.checkpoint(
        state.fsm,
        {
            "slice-a": replace(
                current,
                dispatch_invocation_id=invocation_id,
                dispatch_agent_id="agent-slice-a",
            )
        },
        state.budgets,
        state.events.last_consumed_offset,
    )
    return store, state


def _run_refused_heartbeat(
    tmp_path: Path, transport: HeartbeatTransport
) -> tuple[RunStore, object]:
    store, state = _heartbeat_in_review(tmp_path, "inv-current")
    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )
    return store, result


def test_heartbeat_refused_publication_cannot_mutate_or_park(tmp_path: Path) -> None:
    store, result = _run_refused_heartbeat(
        tmp_path,
        HeartbeatTransport(
            merged=True,
            pr_state="closed",
            publication_ownership_verified=True,
            publication=_refused_publication(),
        ),
    )

    observed = result.state.slices["slice-a"]
    assert observed.status is SliceStatus.IN_REVIEW
    assert observed.reviewed_head == "head-old"
    assert observed.post_merge is None
    assert observed.park_cause is None
    assert observed.reconciliation is None
    assert result.parked_slice_ids == ()
    assert [event.kind for event in result.events] == ["pr.publication_refused"]
    # The refusal is durable: no watcher-derived state was checkpointed.
    reloaded = store.load().slices["slice-a"]
    assert reloaded.status is SliceStatus.IN_REVIEW
    assert reloaded.reviewed_head == "head-old"
    assert reloaded.post_merge is None


def test_heartbeat_refused_publication_cannot_park_closed_unmerged(tmp_path: Path) -> None:
    _store, result = _run_refused_heartbeat(
        tmp_path,
        HeartbeatTransport(
            merged=False,
            pr_state="closed",
            publication_ownership_verified=True,
            publication=_refused_publication(),
        ),
    )

    observed = result.state.slices["slice-a"]
    assert observed.status is SliceStatus.IN_REVIEW
    assert observed.park_cause is None
    assert result.parked_slice_ids == ()
    assert [event.kind for event in result.events] == ["pr.publication_refused"]


def test_heartbeat_refused_publication_cannot_change_head(tmp_path: Path) -> None:
    _store, result = _run_refused_heartbeat(
        tmp_path,
        HeartbeatTransport(
            merged=False,
            pr_state="open",
            publication_ownership_verified=True,
            publication=_refused_publication(),
        ),
    )

    observed = result.state.slices["slice-a"]
    assert observed.reviewed_head == "head-old"
    assert result.parked_slice_ids == ()
    assert [event.kind for event in result.events] == ["pr.publication_refused"]


def test_heartbeat_unverified_publication_ownership_cannot_mutate(tmp_path: Path) -> None:
    store, result = _run_refused_heartbeat(
        tmp_path,
        HeartbeatTransport(
            merged=False,
            pr_state="open",
            # A publication record whose ownership fields are omitted: the
            # binding was never verified and must not be applied.
            publication=_refused_publication("inv-current"),
        ),
    )

    observed = result.state.slices["slice-a"]
    assert observed.reviewed_head == "head-old"
    assert observed.park_cause is None
    assert result.parked_slice_ids == ()
    assert [event.kind for event in result.events] == ["pr.publication_refused"]
    assert store.load().slices["slice-a"].reviewed_head == "head-old"


def test_heartbeat_unverified_ownership_does_not_persist_resolved_pr(
    tmp_path: Path,
) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0, pr_number=None)
    current = state.slices["slice-a"]
    state = store.checkpoint(
        state.fsm,
        {
            "slice-a": replace(
                current,
                dispatch_invocation_id="inv-current",
                dispatch_agent_id="agent-slice-a",
            )
        },
        state.budgets,
        state.events.last_consumed_offset,
    )
    transport = HeartbeatTransport(publication=_refused_publication("inv-current"))

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    assert result.state.slices["slice-a"].pr_number is None
    assert store.load().slices["slice-a"].pr_number is None
    assert [event.kind for event in result.events] == ["pr.publication_refused"]


@pytest.mark.parametrize(
    "transport_kwargs",
    [
        {"omit_ownership_error": True},
        {"ownership_error_raw": 17},
        {"publication_ownership_error": "contradictory ownership evidence"},
    ],
    ids=["omitted-error", "non-string-error", "contradictory-error"],
)
def test_heartbeat_malformed_ownership_cannot_mutate(
    tmp_path: Path, transport_kwargs: dict[str, object]
) -> None:
    store, result = _run_refused_heartbeat(
        tmp_path,
        HeartbeatTransport(
            merged=False,
            pr_state="open",
            publication_ownership_verified=True,
            publication=_refused_publication("inv-current"),
            **transport_kwargs,
        ),
    )

    observed = result.state.slices["slice-a"]
    assert observed.reviewed_head == "head-old"
    assert observed.reconciliation is None
    assert observed.park_cause is None
    assert result.parked_slice_ids == ()
    assert [event.kind for event in result.events] == ["pr.publication_refused"]
    assert store.load().slices["slice-a"].reviewed_head == "head-old"


def test_heartbeat_malformed_ownership_does_not_persist_resolved_pr(
    tmp_path: Path,
) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0, pr_number=None)
    current = state.slices["slice-a"]
    state = store.checkpoint(
        state.fsm,
        {
            "slice-a": replace(
                current,
                dispatch_invocation_id="inv-current",
                dispatch_agent_id="agent-slice-a",
            )
        },
        state.budgets,
        state.events.last_consumed_offset,
    )
    transport = HeartbeatTransport(
        publication_ownership_verified=True,
        omit_ownership_error=True,
        publication=_refused_publication("inv-current"),
    )

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    assert result.state.slices["slice-a"].pr_number is None
    assert store.load().slices["slice-a"].pr_number is None
    assert [event.kind for event in result.events] == ["pr.publication_refused"]


def test_heartbeat_accepted_publication_reconciles(tmp_path: Path) -> None:
    _store, result = _run_refused_heartbeat(
        tmp_path,
        HeartbeatTransport(
            merged=False,
            pr_state="open",
            publication_ownership_verified=True,
            publication=_refused_publication("inv-current"),
        ),
    )

    observed = result.state.slices["slice-a"]
    assert observed.reviewed_head == "head-new"
    assert result.parked_slice_ids == ()


def test_heartbeat_refused_resolver_publication_does_not_persist_pr_number(
    tmp_path: Path,
) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0, pr_number=None)
    current = state.slices["slice-a"]
    state = store.checkpoint(
        state.fsm,
        {"slice-a": replace(current, dispatch_invocation_id="inv-current")},
        state.budgets,
        state.events.last_consumed_offset,
    )
    transport = HeartbeatTransport(
        publication_ownership_verified=True,
        publication=_refused_publication(),
    )

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    observed = result.state.slices["slice-a"]
    assert observed.pr_number is None
    # The resolved PR number is never checkpointed behind a refused snapshot.
    assert store.load().slices["slice-a"].pr_number is None
    assert [event.kind for event in result.events] == ["pr.publication_refused"]


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
    invocation_dir = tmp_path / ".exo" / "agents" / "agent-slice-a"
    invocation_dir.mkdir(parents=True)
    invocation_path = invocation_dir / "invocation.json"
    invocation_path.write_text(
        json.dumps({"slice_id": "slice-a", "status": "running"}),
        encoding="utf-8",
    )
    transport = HeartbeatTransport()
    now_clock = _MutableClock(1.0)
    active_work = ActiveWorkClock(now_clock)
    active_work.mark_active("slice-a")
    now_clock.value = 10.25

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.25,
        project_root=tmp_path,
        active_work=active_work,
    )

    invocation = json.loads(invocation_path.read_text(encoding="utf-8"))
    assert type(invocation["ended_at"]) is int
    assert invocation["ended_at"] == 10
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


def test_ordered_child_pane_recovery_uses_bounded_same_owner_heartbeat_probe(
    tmp_path: Path,
) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    current = replace(
        state.slices["slice-a"],
        dispatch_invocation_id="ordered-invocation",
        dispatch_generation=2,
    )
    recovery = begin_recovery(
        cause=BlockCause.ORDERED_CHILD_PANE_EXITED.value,
        owner_run_id=state.run_id,
        slice_attempt=1,
        owner_agent_id="agent-slice-a",
        invocation_generation=2,
        evidence={
            "invocation_id": "ordered-invocation",
            "slice_id": "slice-a",
            "authorization_source": "human",
        },
        next_action="resume_same_owner",
        entered_at=1.0,
    )
    persisted = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": replace(current, recovery=recovery)},
        state.budgets,
        state.events.last_consumed_offset,
    )

    result = heartbeat_once(
        persisted,
        store,
        EffectClient(HeartbeatTransport()),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=10.0,
        project_root=tmp_path,
    )

    updated = result.state.slices["slice-a"]
    assert result.fired is True
    assert sum(event.kind == "recovery.probe" for event in result.events) == 1
    assert updated.recovery is not None
    assert updated.recovery.cause == BlockCause.ORDERED_CHILD_PANE_EXITED.value
    assert updated.recovery.phase.value == "waiting_signal"
    assert updated.recovery.next_probe_at == 40.0
    assert updated.dispatch_agent_id == "agent-slice-a"
    assert updated.dispatch_invocation_id == "ordered-invocation"
    assert updated.dispatch_generation == 2


def test_ordered_child_pane_recovery_rejects_a_different_invocation(tmp_path: Path) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    current = replace(
        state.slices["slice-a"],
        dispatch_invocation_id="current-invocation",
        dispatch_generation=2,
        recovery=begin_recovery(
            cause=BlockCause.ORDERED_CHILD_PANE_EXITED.value,
            owner_run_id=state.run_id,
            slice_attempt=1,
            owner_agent_id="agent-slice-a",
            invocation_generation=2,
            evidence={
                "invocation_id": "stale-invocation",
                "slice_id": "slice-a",
                "authorization_source": "human",
            },
            next_action="resume_same_owner",
            entered_at=1.0,
        ),
    )
    persisted = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": current},
        state.budgets,
        state.events.last_consumed_offset,
    )

    with pytest.raises(HeartbeatError, match="same-owner invocation"):
        heartbeat_once(
            persisted,
            store,
            EffectClient(HeartbeatTransport()),
            HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
            now=10.0,
            project_root=tmp_path,
        )


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


def test_the_heartbeat_logs_the_classification_it_did_not_act_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A heartbeat that declines to park says which observation it read.

    Its synthetic events are returned in the run result and never written to
    the ledger, so without this line a run that polls a PR forever leaves a
    caller nothing to read: the classification is the whole reason it neither
    parked nor escalated.
    """
    store, state = _state(
        tmp_path,
        status="in_review",
        heartbeat_at=0.0,
        pr_number=42,
        reviewed_head="head-new",
    )
    with caplog.at_level(logging.INFO, logger="tl_loop.loop.heartbeat"):
        heartbeat_once(
            state,
            store,
            EffectClient(HeartbeatTransport(pr_state="open", publication_ownership_verified=True)),
            HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
            now=10.0,
            project_root=tmp_path,
        )

    lines = [
        record.getMessage()
        for record in caplog.records
        if "heartbeat target=" in record.getMessage()
    ]
    assert lines, "the heartbeat classified nothing"
    assert "pr_state='open'" in lines[0]
    assert "ownership=verified" in lines[0]


def test_the_pr_classification_helpers_are_public() -> None:
    """Startup reconciliation reads the same two helpers across modules (#1144).

    `_reconfirm_park` in `tl_loop.loop.driver` classifies the observation that
    proves a park and writes the audit the heartbeat's parks write, so both
    helpers belong to this module's public surface. Reaching into a private
    name from another module is what this guards.
    """
    from tl_loop.loop import driver, heartbeat

    assert "pr_payload" in heartbeat.__all__
    assert "pr_terminal_cause" in heartbeat.__all__
    assert driver.pr_payload is heartbeat.pr_payload
    assert driver.pr_terminal_cause is heartbeat.pr_terminal_cause


def _spawned_with_historical_dispatch(tmp_path: Path, *, started_at: float, budget: float = 3600.0):
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    current = state.slices["slice-a"]
    updated = replace(
        current,
        dispatch_started_at=started_at,
        task_timeout_seconds=budget,
        task_timeout_source="project",
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": updated},
        state.budgets,
        state.events.last_consumed_offset,
    )
    return store, state


def test_between_invocation_downtime_consumes_zero_work_budget(tmp_path: Path) -> None:
    """A 23-day-old producer timestamp cannot expire this invocation.

    The historical wall timestamp stays visible as audit (its old deadline is
    preserved on the deadline ledger), but the fresh invocation's work clock
    starts at zero, so no park or pane close happens.
    """
    twenty_three_days = 23 * 24 * 60 * 60
    store, state = _spawned_with_historical_dispatch(tmp_path, started_at=1_790_111_703.52)
    transport = HeartbeatTransport()

    now_clock = _MutableClock(5000.0)
    active_work = ActiveWorkClock(now_clock)
    active_work.mark_active("slice-a")
    now_clock.value = 5000.5

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=1_790_111_703.52 + twenty_three_days,
        project_root=tmp_path,
        active_work=active_work,
    )

    observed = result.state.slices["slice-a"]
    assert observed.status is SliceStatus.SPAWNED
    assert observed.park_cause is None
    assert not any(name == "close_worker_pane" for name, _ in transport.calls)
    # Audit history is preserved, not rewritten to make a deadline pass.
    assert observed.deadline_ledger is not None
    assert observed.deadline_ledger.execution_deadline_at == 1_790_111_703.52 + 3600.0


def test_completed_aggregate_integration_is_never_active_work(tmp_path: Path) -> None:
    """A finished producer awaiting parent integration cannot be budget-killed."""
    store, state = _state(
        tmp_path,
        status="in_review",
        heartbeat_at=0.0,
        pr_number=46,
        reviewed_head="head-a",
    )
    current = state.slices["slice-a"]
    updated = replace(
        current,
        dispatch_started_at=1_790_111_703.52,
        task_timeout_seconds=3600.0,
        task_timeout_source="project",
        action=ActionState(
            ActionKind.MERGE,
            ActionPhase.IN_FLIGHT,
            intent_id="merge-key",
            head_sha="head-a",
            attempt=1,
        ),
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": updated},
        state.budgets,
        state.events.last_consumed_offset,
    )
    transport = HeartbeatTransport()

    class _RaisingClock:
        def __call__(self) -> float:
            raise AssertionError("work clock must not be read for inactive work")

    active_work = ActiveWorkClock(_RaisingClock())

    result = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=1_790_111_703.52 + 30 * 24 * 60 * 60,
        project_root=tmp_path,
        active_work=active_work,
    )

    observed = result.state.slices["slice-a"]
    assert observed.status is SliceStatus.IN_REVIEW
    assert observed.park_cause is None
    assert not any(name == "close_worker_pane" for name, _ in transport.calls)


def test_active_worker_times_out_at_its_invocation_budget_not_before(tmp_path: Path) -> None:
    """Live active work still times out -- measured only from this invocation."""
    store, state = _spawned_with_historical_dispatch(tmp_path, started_at=1.0, budget=5.0)
    transport = HeartbeatTransport()
    now_clock = _MutableClock(100.0)
    active_work = ActiveWorkClock(now_clock)
    active_work.mark_active("slice-a")

    # 4.5s of live work: under the 5s budget, no park.
    now_clock.value = 104.5
    early = heartbeat_once(
        state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=1.0, stall_threshold_seconds=100.0),
        now=104.5,
        project_root=tmp_path,
        active_work=active_work,
    )
    assert early.state.slices["slice-a"].status is SliceStatus.SPAWNED

    # Repeated polling must not move the start forward: 6.0s of live work parks.
    now_clock.value = 106.0
    late = heartbeat_once(
        early.state,
        store,
        EffectClient(transport),
        HeartbeatConfig(interval_seconds=1.0, stall_threshold_seconds=100.0),
        now=106.0,
        project_root=tmp_path,
        active_work=active_work,
    )
    parked = late.state.slices["slice-a"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.TASK_BUDGET_EXCEEDED


def test_missing_worker_row_does_not_consume_active_work_budget(tmp_path: Path) -> None:
    """A missing observation ends the timed interval until work is seen again."""
    store, state = _spawned_with_historical_dispatch(tmp_path, started_at=1.0, budget=5.0)
    transport = HeartbeatTransport()
    now_clock = _MutableClock(100.0)
    active_work = ActiveWorkClock(now_clock)
    effects = EffectClient(transport)
    config = HeartbeatConfig(interval_seconds=1.0, stall_threshold_seconds=100.0)

    active = heartbeat_once(
        state,
        store,
        effects,
        config,
        now=100.0,
        project_root=tmp_path,
        active_work=active_work,
    )
    assert active_work.snapshot() == {"slice-a": 100.0}

    now_clock.value = 101.0
    transport.worker_present = False
    missing = heartbeat_once(
        active.state,
        store,
        effects,
        config,
        now=101.0,
        project_root=tmp_path,
        active_work=active_work,
    )
    assert missing.state.slices["slice-a"].status is SliceStatus.SPAWNED
    assert active_work.snapshot() == {}

    now_clock.value = 1000.0
    transport.worker_present = True
    returned = heartbeat_once(
        missing.state,
        store,
        effects,
        config,
        now=1000.0,
        project_root=tmp_path,
        active_work=active_work,
    )
    assert returned.state.slices["slice-a"].status is SliceStatus.SPAWNED
    assert active_work.snapshot() == {"slice-a": 1000.0}
    assert not any(name == "close_worker_pane" for name, _ in transport.calls)

    now_clock.value = 1005.1
    expired = heartbeat_once(
        returned.state,
        store,
        effects,
        config,
        now=1005.1,
        project_root=tmp_path,
        active_work=active_work,
    )
    assert expired.state.slices["slice-a"].status is SliceStatus.PARKED
    assert expired.state.slices["slice-a"].park_cause is ParkCause.TASK_BUDGET_EXCEEDED


def test_replacement_worker_invocation_starts_a_fresh_active_interval(
    tmp_path: Path,
) -> None:
    store, state = _spawned_with_historical_dispatch(tmp_path, started_at=1.0, budget=5.0)
    transport = HeartbeatTransport(worker_invocation_id="worker-invocation-a")
    now_clock = _MutableClock(100.0)
    active_work = ActiveWorkClock(now_clock)
    effects = EffectClient(transport)
    config = HeartbeatConfig(interval_seconds=1.0, stall_threshold_seconds=100.0)

    first = heartbeat_once(
        state,
        store,
        effects,
        config,
        now=100.0,
        project_root=tmp_path,
        active_work=active_work,
    )
    now_clock.value = 101.0
    transport.worker_invocation_id = "worker-invocation-b"
    replacement = heartbeat_once(
        first.state,
        store,
        effects,
        config,
        now=101.0,
        project_root=tmp_path,
        active_work=active_work,
    )

    assert replacement.state.slices["slice-a"].status is SliceStatus.SPAWNED
    assert active_work.snapshot() == {"slice-a": 101.0}

    now_clock.value = 105.5
    still_active = heartbeat_once(
        replacement.state,
        store,
        effects,
        config,
        now=105.5,
        project_root=tmp_path,
        active_work=active_work,
    )
    assert still_active.state.slices["slice-a"].status is SliceStatus.SPAWNED
    assert not any(name == "close_worker_pane" for name, _ in transport.calls)


def test_heartbeat_clears_timer_when_slice_becomes_parked_before_poll(
    tmp_path: Path,
) -> None:
    store, state = _state(tmp_path, status="spawned", heartbeat_at=0.0)
    active_work = ActiveWorkClock(_MutableClock(100.0))
    active_work.mark_active("slice-a")
    current = state.slices["slice-a"]
    parked = replace(
        current,
        status=SliceStatus.PARKED,
        park_cause=ParkCause.TASK_BUDGET_EXCEEDED,
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": parked},
        state.budgets,
        state.events.last_consumed_offset,
    )

    result = heartbeat_once(
        state,
        store,
        EffectClient(HeartbeatTransport()),
        HeartbeatConfig(interval_seconds=5.0, stall_threshold_seconds=100.0),
        now=0.0,
        project_root=tmp_path,
        active_work=active_work,
    )

    assert result.fired is False
    assert active_work.snapshot() == {}
