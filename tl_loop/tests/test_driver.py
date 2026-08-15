"""Contract tests for the bounded active/shadow TL driver."""

from __future__ import annotations

import hashlib
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.events.envelope import EventEnvelope, project
from tl_loop.fsm.event import PRFiled, PRUpdated
from tl_loop.fsm.phase import TLPhase, TLPlanning
from tl_loop.loop.driver import (
    DepthLimitExceeded,
    LoopLimitExceeded,
    SubTLTask,
    TLLoopError,
    TLLoopConfig,
    TLRunResult,
    WorkerTask,
    WorkPlan,
    _initial_slices,
    _record_review_event,
    _route_ci_event,
    _route_review_event,
    run_tl_loop,
    tl_run,
)
from tl_loop.loop.shadow import TLEventDecoder, _update_slices
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest, RlmResponse
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty
from tl_loop.select.model import ModelCatalog
from tl_loop.select.policy import validate_policy
from tl_loop.state.schema import (
    BudgetLedger,
    GateState,
    GateStatus,
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


@dataclass
class RecordingTransport:
    calls: list[tuple[str, JsonObject]] = field(default_factory=list)
    fail_observability: bool = False
    reject_spawns: bool = False
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
        if tool_name == "list_agents":
            return {"success": True, "result": {"agents": self.listed_agents}}
        if tool_name == "emit_controller_event":
            self.next_controller_run_seq += 1
            return {
                "success": True,
                "result": {"event_id": "controller-event", "run_seq": self.next_controller_run_seq},
            }
        return {"success": True, "result": None}


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


def test_idle_timeout_parks_with_named_gate_and_never_merges(tmp_path: Path) -> None:
    transport = RecordingTransport()
    result = run_tl_loop(
        "timeout-run",
        _plan(),
        SyntheticQueue([]),
        EffectClient(transport),
        config=TLLoopConfig(
            max_workers=1,
            max_leaves=1,
            max_events=5,
            poll_interval=0.001,
            idle_timeout=0.01,
        ),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLFailed
    assert result.final_state.gates == (
        GateState(name="tl-dispatch-timeout", status=GateStatus.PENDING),
    )
    assert _effect_names(transport) == ["spawn_worker", "spawn_leaf"]
    worker_state = result.final_state.slices["worker-a"]
    assert worker_state.status is SliceStatus.DISPATCH_UNCONFIRMED
    assert worker_state.park_cause.value == "dispatch_timeout"
    assert worker_state.dispatch_intent_id
    assert "authoritative agent.spawned" in (worker_state.dispatch_error or "")
    assert [
        arguments["payload"]
        for tool_name, arguments in transport.calls
        if tool_name == "emit_controller_event" and arguments["event_type"] == "tl.gate_opened"
    ] == [{"gate_name": "tl-dispatch-timeout", "run_id": "timeout-run"}]
    assert "merge_pr" not in _effect_names(transport)


def test_rejected_spawn_is_persisted_as_dispatch_failure(tmp_path: Path) -> None:
    transport = RecordingTransport(reject_spawns=True)
    plan = WorkPlan.from_mapping({"leaves": [{"name": "leaf-a", "task": "implement the change"}]})
    result = run_tl_loop(
        "dispatch-failure-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=TLLoopConfig(poll_interval=0.001, idle_timeout=0.1),
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
    config = TLLoopConfig(poll_interval=0.001, idle_timeout=0.1, dispatch_timeout=0.01)
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

    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
        initial_slices=initial,
    )

    slice_state = result.final_state.slices["leaf-a"]
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
    assert result.final_state.gates == (
        GateState(name="tl-dispatch-timeout", status=GateStatus.PENDING),
    )


def test_restart_adopts_owner_by_intent_without_duplicate_spawn(tmp_path: Path) -> None:
    run_id = "dispatch-adopt-run"
    plan = WorkPlan.from_mapping({"leaves": [{"name": "leaf-a", "task": "implement"}]})
    config = TLLoopConfig(poll_interval=0.001, idle_timeout=0.1, dispatch_timeout=0.01)
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
    config = TLLoopConfig(poll_interval=0.001, idle_timeout=0.1, dispatch_timeout=0.01)
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

    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue(
            [
                _canonical_event(
                    1,
                    "agent.spawned",
                    "leaf-a",
                    run_id,
                    intent_id="intent-stale-1",
                )
            ]
        ),
        EffectClient(RecordingTransport()),
        config=config,
        root_dir=tmp_path,
        initial_slices=initial,
    )

    stale = result.final_state.slices["leaf-a"]
    assert stale.status is SliceStatus.DISPATCH_UNCONFIRMED
    assert stale.dispatch_intent_id == "intent-new-2"
    assert stale.dispatch_authoritative_event_seq is None
    assert stale.park_cause.value == "dispatch_timeout"


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
        "agent_id": "leaf-a",
        "lifecycle_state": "emitted",
        "observed_at": "2026-08-12T00:00:00Z",
        "data": {
            "slice_id": "leaf-a",
            "pr_number": 42,
            "head_sha": "head-a",
        },
    }
    source = SyntheticQueue(
        [
            project(cast(dict[str, object], raw_pr_filed)),
            _event(2, "all_children_done", run_id=run_id),
        ]
    )
    transport = RecordingTransport()
    plan = WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": "leaf-a",
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
            idle_timeout=0.1,
        ),
        root_dir=tmp_path,
    )

    assert _effect_names(transport) == ["spawn_leaf", "spawn_reviewer"]
    reviewer_args = next(
        arguments for name, arguments in transport.calls if name == "spawn_reviewer"
    )
    assert reviewer_args["pr_number"] == 42
    assert reviewer_args["head_sha"] == "head-a"
    assert reviewer_args["force"] is False
    criteria = cast(list[object], reviewer_args["acceptance_criteria"])
    assert any("DONE CRITERIA: the changed behavior is covered" in str(item) for item in criteria)
    slice_state = result.final_state.slices["leaf-a"]
    assert slice_state.reviewer_attempt == {"head-a": 1}
    assert source.acknowledged == [1, 2]


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


def _review_choice(backend: ReviewBackend) -> RlmModelChoice:
    return RlmModelChoice(
        model_id="test-model",
        backend=backend,
        store=RlmCallStore(),
        context_length=10_000,
    )


def _review_store(tmp_path: Path, *, verdict: Verdict | None = None) -> RunStore:
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
                reviewed_head="head-a",
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
        idle_timeout=0.1,
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
    assert spawn_worker_call["agent_type"] == "codex/gpt-luna"
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
            idle_timeout=0.1,
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
        _canonical_event(1, "agent.spawned", "worker-a", run_id),
        _canonical_event(2, "agent.spawned", "leaf-a", run_id),
        _event(3, "child_completed", "worker-a", run_id=run_id),
        _event(4, "child_completed", "leaf-a", pr_number=42, head_sha="head-a", run_id=run_id),
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
            config=TLLoopConfig(max_depth=0, poll_interval=0.001, idle_timeout=0.1),
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
        config=TLLoopConfig(max_parallel_slices=2, poll_interval=0.001, idle_timeout=0.1),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert maximum == 2
    assert timeline.index(("start", "stage-two")) > timeline.index(("end", "stage-one-a"))
    assert timeline.index(("start", "stage-two")) > timeline.index(("end", "stage-one-b"))


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
    config = TLLoopConfig(max_parallel_slices=2, poll_interval=0.001, idle_timeout=0.1)
    run_tl_loop("restart-run", plan, SyntheticQueue([]), EffectClient(RecordingTransport()), config=config, root_dir=tmp_path)
    run_tl_loop("restart-run", plan, SyntheticQueue([]), EffectClient(RecordingTransport()), config=config, root_dir=tmp_path)

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
