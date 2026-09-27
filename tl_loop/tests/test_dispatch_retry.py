"""Durable, code-classified recovery of retryable leaf dispatch failures.

Every test here is deterministic: the dispatch clock is injected through
``TLLoopConfig.wall_clock``, and no test sleeps, polls, or reads wall time to
decide an outcome. A re-drive is a second bounded controller invocation over
the same checkpoint, which is exactly what a restart does.
"""

from __future__ import annotations

import hashlib
import json
import queue
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.events.envelope import EventEnvelope
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.abandon import AbandonmentError, abandon_slice
from tl_loop.loop.dispatch_classification import (
    DispatchFailureClass,
    classify_dispatch_failure,
    dispatch_retry_delay,
)
from tl_loop.loop.driver import (
    DISPATCH_FAILURE_GATE_NAME,
    DISPATCH_OWNERSHIP_CONFLICT_GATE_NAME,
    TLLoopConfig,
    WorkPlan,
    run_tl_loop,
)
from tl_loop.state.schema import (
    BudgetLedger,
    ParkCause,
    SliceState,
    SliceStatus,
)
from tl_loop.state.store import RunStore, create

JsonObject = dict[str, Any]

BRANCH_EXISTS = "worktree.branch_exists"
OWNERSHIP_CONFLICT = "worktree.branch_ownership_conflict"
BASE_DELAY = 10.0
MAX_DELAY = 40.0


@dataclass
class SyntheticQueue:
    """An event source that is empty, so one run is a bounded dispatch pass."""

    events: list[EventEnvelope] = field(default_factory=list)
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
class ScriptedClock:
    """A clock that only moves when a test moves it."""

    now: float = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class ScriptedTransport:
    """Reject the first `rejections` spawn requests, then accept the rest.

    Each rejection is answered by a scripted `agent.spawn_failed` ledger row
    carrying the machine code, so the controller reads exactly the typed
    channel it reads in production instead of a hand-fed return value.
    """

    code: str | None
    rejections: int
    project_root: Path
    run_id: str
    calls: list[tuple[str, JsonObject]] = field(default_factory=list)
    events: list[JsonObject] = field(default_factory=list)
    spawn_calls: list[JsonObject] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, arguments))
        if tool_name in {"spawn_worker", "spawn_leaf"}:
            self.spawn_calls.append(arguments)
            attempt = len(self.spawn_calls)
            if attempt <= self.rejections:
                _write_spawn_failed(
                    self.project_root,
                    self.run_id,
                    _intent_id(self.run_id, attempt),
                    self.code,
                    run_seq=6 + attempt,
                )
                return {"success": False, "error": "spawn request rejected"}
            return {"success": True, "result": {"agent_id": "leaf-a-opencode"}}
        if tool_name == "list_agents":
            return {"success": True, "result": {"agents": []}}
        if tool_name == "emit_controller_event":
            self.events.append(arguments)
            return {
                "success": True,
                "result": {
                    "event_id": "controller-event",
                    "run_seq": 100 + len(self.events),
                },
            }
        return {"success": True, "result": {}}

    def event_types(self) -> list[str]:
        return [str(arguments.get("event_type")) for arguments in self.events]

    def payloads(self, event_type: str) -> list[JsonObject]:
        return [
            arguments["payload"]
            for arguments in self.events
            if arguments.get("event_type") == event_type
        ]

    def spawned_intent_ids(self) -> list[str]:
        return [
            str(arguments["intent_id"])
            for arguments in self.spawn_calls
            if arguments.get("intent_id") is not None
        ]


def _write_spawn_failed(
    project_root: Path,
    run_id: str,
    intent_id: str,
    code: str | None,
    *,
    run_seq: int,
) -> None:
    """Record the one runtime row the controller classifies a rejection by."""
    segments = project_root / ".exo" / "ledger" / "segments"
    segments.mkdir(parents=True, exist_ok=True)
    event = {
        "schema_version": 1,
        "event_id": f"spawn-failed-{run_seq}",
        "id": f"spawn-failed-{run_seq}",
        "event_time": "2026-08-11T00:00:00Z",
        "observed_at": "2026-08-11T00:00:00Z",
        "run_seq": run_seq,
        "type": "agent.spawn_failed",
        "agent_id": "root",
        "parent_agent_id": "root",
        "run_id": run_id,
        "session_id": "session-1",
        "lifecycle_state": "observed",
        "data": {
            "child_agent": "leaf-a",
            "error": "spawn request rejected",
            "code": code,
            "intent_id": intent_id,
            "source": "rust",
        },
    }
    segment = segments / f"segment-{run_seq:012d}.jsonl"
    segment.write_text(json.dumps(event) + "\n", encoding="utf-8")


def _intent_id(run_id: str, attempt: int) -> str:
    """Mirror the controller's deterministic per-attempt dispatch intent."""
    return hashlib.sha256(f"{run_id}:leaf-a:{attempt}".encode()).hexdigest()[:32]


def _plan() -> WorkPlan:
    return WorkPlan.from_mapping({"leaves": [{"name": "leaf-a", "task": "implement"}]})


def _config(
    tmp_path: Path,
    run_id: str,
    clock: ScriptedClock,
    *,
    dispatch_retry_limit: int = 3,
) -> TLLoopConfig:
    return TLLoopConfig(
        poll_interval=0.0,
        keep_alive_on_waiting=False,
        dispatch_retry_limit=dispatch_retry_limit,
        dispatch_retry_base_delay_seconds=BASE_DELAY,
        dispatch_retry_max_delay_seconds=MAX_DELAY,
        wall_clock=clock,
        project_root=tmp_path,
        ledger_run_id=run_id,
    )


def _invoke(
    tmp_path: Path,
    run_id: str,
    clock: ScriptedClock,
    transport: ScriptedTransport,
    *,
    dispatch_retry_limit: int = 3,
) -> Any:
    return run_tl_loop(
        run_id,
        _plan(),
        SyntheticQueue([]),
        EffectClient(transport),
        config=_config(
            tmp_path, run_id, clock, dispatch_retry_limit=dispatch_retry_limit
        ),
        root_dir=tmp_path,
    )


def test_the_classification_table_reads_codes_and_never_prose() -> None:
    assert classify_dispatch_failure(BRANCH_EXISTS) is DispatchFailureClass.RETRYABLE
    assert (
        classify_dispatch_failure("worktree.lifecycle_lock_timeout")
        is DispatchFailureClass.RETRYABLE
    )
    assert (
        classify_dispatch_failure("dispatch.transport_timeout")
        is DispatchFailureClass.RETRYABLE
    )
    assert classify_dispatch_failure(OWNERSHIP_CONFLICT) is DispatchFailureClass.TERMINAL
    assert (
        classify_dispatch_failure("worktree.pr_context_unavailable")
        is DispatchFailureClass.TERMINAL
    )


@pytest.mark.parametrize(
    ("code", "persisted_code"),
    [(None, None), ("", None), ("worktree.some_future_refusal", "worktree.some_future_refusal")],
)
def test_an_unknown_code_is_terminal_and_never_retryable(
    tmp_path: Path, code: str | None, persisted_code: str | None
) -> None:
    clock = ScriptedClock()
    run_id = f"unknown-{code or 'absent'}"
    transport = ScriptedTransport(
        code=code, rejections=1, project_root=tmp_path, run_id=run_id
    )

    result = _invoke(tmp_path, run_id, clock, transport)

    slice_state = result.final_state.slices["leaf-a"]
    assert slice_state.status is SliceStatus.DISPATCH_FAILED
    # An absent or empty code is recorded as absent, never as a retryable hint.
    assert slice_state.dispatch_error_code == persisted_code
    assert "tl.dispatch_retry_scheduled" not in transport.event_types()
    assert len(transport.spawn_calls) == 1
    assert [gate.name for gate in result.final_state.gates] == [DISPATCH_FAILURE_GATE_NAME]


def test_a_retryable_branch_race_recovers_without_parking(tmp_path: Path) -> None:
    clock = ScriptedClock()
    run_id = "branch-race-run"
    transport = ScriptedTransport(
        code=BRANCH_EXISTS, rejections=1, project_root=tmp_path, run_id=run_id
    )

    first = _invoke(tmp_path, run_id, clock, transport)
    boundary = first.final_state.slices["leaf-a"]
    assert boundary.status is SliceStatus.DISPATCH_RETRY_SCHEDULED
    assert boundary.dispatch_retry_attempt == 1
    assert not first.final_state.gates
    assert first.final_state.fsm.phase is not TLPhase.TLFailed

    clock.advance(BASE_DELAY)
    second = _invoke(tmp_path, run_id, clock, transport)

    recovered = second.final_state.slices["leaf-a"]
    assert recovered.status is SliceStatus.DISPATCH_UNCONFIRMED
    assert recovered.dispatch_error_code is None
    assert recovered.attempts == 2
    assert not second.final_state.gates
    assert "tl.gate_opened" not in transport.event_types()
    assert transport.event_types().count("tl.dispatch_retry_scheduled") == 1


def test_a_scheduled_retry_never_claims_a_leaf_exists(tmp_path: Path) -> None:
    clock = ScriptedClock()
    run_id = "no-intent-run"
    transport = ScriptedTransport(
        code=BRANCH_EXISTS, rejections=1, project_root=tmp_path, run_id=run_id
    )

    result = _invoke(tmp_path, run_id, clock, transport)

    persisted = RunStore(run_id, tmp_path).load().slices["leaf-a"]
    assert persisted.status is SliceStatus.DISPATCH_RETRY_SCHEDULED
    # No leaf, agent, or invocation identity may survive an uncreated attempt.
    assert persisted.dispatch_intent_id is None
    assert persisted.dispatch_agent_id is None
    assert persisted.dispatch_invocation_id is None
    assert persisted.dispatch_started_at is None
    assert persisted.dispatch_last_boundary == "dispatch_retry_scheduled"
    assert persisted.dispatch_error_code == BRANCH_EXISTS
    assert result.final_state.gates == ()

    payload = transport.payloads("tl.dispatch_retry_scheduled")[0]
    assert payload["machine_code"] == BRANCH_EXISTS
    assert payload["retry_attempt"] == 1
    assert payload["next_attempt_at"] == 1_000.0 + BASE_DELAY
    # The machine code is its own dimension, separate from the operator prose.
    assert payload["error"] == "spawn request rejected"


def test_repeated_reconciliation_issues_exactly_one_authoritative_spawn(
    tmp_path: Path,
) -> None:
    clock = ScriptedClock()
    run_id = "one-spawn-run"
    transport = ScriptedTransport(
        code=BRANCH_EXISTS, rejections=1, project_root=tmp_path, run_id=run_id
    )
    _invoke(tmp_path, run_id, clock, transport)
    clock.advance(BASE_DELAY)

    first = _invoke(tmp_path, run_id, clock, transport)
    spawns_after_first = len(transport.spawn_calls)
    second = _invoke(tmp_path, run_id, clock, transport)
    third = _invoke(tmp_path, run_id, clock, transport)

    assert spawns_after_first == 2
    assert len(transport.spawn_calls) == 2
    for result in (first, second, third):
        assert result.final_state.slices["leaf-a"].status is (
            SliceStatus.DISPATCH_UNCONFIRMED
        )
        assert result.final_state.slices["leaf-a"].attempts == 2
    assert len(set(transport.spawned_intent_ids())) == 2


def test_a_terminal_ownership_conflict_parks_immediately_with_a_named_gate(
    tmp_path: Path,
) -> None:
    clock = ScriptedClock()
    run_id = "ownership-run"
    transport = ScriptedTransport(
        code=OWNERSHIP_CONFLICT, rejections=1, project_root=tmp_path, run_id=run_id
    )

    result = _invoke(tmp_path, run_id, clock, transport)

    slice_state = result.final_state.slices["leaf-a"]
    assert slice_state.status is SliceStatus.DISPATCH_FAILED
    assert slice_state.park_cause is ParkCause.DISPATCH_FAILED
    assert slice_state.dispatch_error_code == OWNERSHIP_CONFLICT
    assert [gate.name for gate in result.final_state.gates] == [
        DISPATCH_OWNERSHIP_CONFLICT_GATE_NAME
    ]
    assert "tl.dispatch_retry_scheduled" not in transport.event_types()
    assert transport.event_types().count("tl.gate_opened") == 1
    assert len(transport.spawn_calls) == 1
    # A terminal conflict never waits out a backoff window.
    assert slice_state.dispatch_next_attempt_at is None


def test_retry_exhaustion_opens_exactly_one_gate_after_the_configured_attempts(
    tmp_path: Path,
) -> None:
    clock = ScriptedClock()
    run_id = "exhaustion-run"
    transport = ScriptedTransport(
        code=BRANCH_EXISTS, rejections=9, project_root=tmp_path, run_id=run_id
    )

    for _ in range(4):
        result = _invoke(tmp_path, run_id, clock, transport, dispatch_retry_limit=2)
        clock.advance(MAX_DELAY)

    slice_state = result.final_state.slices["leaf-a"]
    assert slice_state.status is SliceStatus.DISPATCH_FAILED
    assert slice_state.dispatch_retry_attempt == 2
    assert slice_state.dispatch_error_code == BRANCH_EXISTS
    assert [gate.name for gate in result.final_state.gates] == [DISPATCH_FAILURE_GATE_NAME]
    assert transport.event_types().count("tl.gate_opened") == 1
    assert transport.event_types().count("tl.dispatch_retry_scheduled") == 2
    # One initial dispatch plus one re-drive per scheduled retry, and no more.
    assert len(transport.spawn_calls) == 3
    assert len(set(transport.spawned_intent_ids())) == 3


def test_restart_during_backoff_resumes_without_duplicate_intent_or_spawn(
    tmp_path: Path,
) -> None:
    clock = ScriptedClock()
    run_id = "restart-run"
    transport = ScriptedTransport(
        code=BRANCH_EXISTS, rejections=1, project_root=tmp_path, run_id=run_id
    )
    first = _invoke(tmp_path, run_id, clock, transport)

    boundary = first.final_state.slices["leaf-a"]
    assert boundary.status is SliceStatus.DISPATCH_RETRY_SCHEDULED
    scheduled_for = boundary.dispatch_next_attempt_at
    assert scheduled_for == 1_000.0 + BASE_DELAY
    spawns_before = len(transport.spawn_calls)
    events_before = len(transport.events)

    # A controller that restarts inside the window must not act at all.
    inside = _invoke(tmp_path, run_id, clock, transport)
    resumed = inside.final_state.slices["leaf-a"]
    assert resumed.status is SliceStatus.DISPATCH_RETRY_SCHEDULED
    assert resumed.dispatch_retry_attempt == 1
    assert resumed.dispatch_next_attempt_at == scheduled_for
    assert resumed.dispatch_intent_id is None
    assert len(transport.spawn_calls) == spawns_before
    assert "tl.dispatch_intended" not in transport.event_types()[events_before:]

    # The same boundary, once its instant arrives, re-drives exactly once.
    clock.advance(BASE_DELAY)
    after = _invoke(tmp_path, run_id, clock, transport)
    assert after.final_state.slices["leaf-a"].attempts == 2
    assert after.final_state.slices["leaf-a"].status is SliceStatus.DISPATCH_UNCONFIRMED
    assert len(transport.spawn_calls) == spawns_before + 1


def test_retry_backoff_is_bounded_and_doubles_per_scheduled_retry() -> None:
    assert dispatch_retry_delay(1, 5.0, 60.0) == 5.0
    assert dispatch_retry_delay(2, 5.0, 60.0) == 10.0
    assert dispatch_retry_delay(3, 5.0, 60.0) == 20.0
    assert dispatch_retry_delay(9, 5.0, 60.0) == 60.0
    with pytest.raises(ValueError, match="at least 1"):
        dispatch_retry_delay(0, 5.0, 60.0)


def test_recording_one_rejected_attempt_twice_does_not_spend_the_budget_twice(
    tmp_path: Path,
) -> None:
    """The retry boundary is keyed by the attempt it was scheduled for."""
    from tl_loop.loop.driver import DispatchAttempt, _record_dispatch_failure

    create("root", {}, root_dir=tmp_path / ".exo" / "tl-loop")
    store = RunStore("root", tmp_path / ".exo" / "tl-loop")
    store.checkpoint(
        TLPhase.TLDispatching,
        {"leaf-a": _dispatching_slice()},
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    clock = ScriptedClock()
    transport = ScriptedTransport(
        code=BRANCH_EXISTS, rejections=0, project_root=tmp_path, run_id="root"
    )
    config = _config(tmp_path, "root", clock)
    attempt = DispatchAttempt("intent-1", 1_000.0, "", attempt=1)

    first = _record_dispatch_failure(
        store,
        store.load(),
        "leaf-a",
        attempt,
        "refused",
        config,
        EffectClient(transport),
        [],
        code=BRANCH_EXISTS,
    )
    second = _record_dispatch_failure(
        store,
        first,
        "leaf-a",
        attempt,
        "refused",
        config,
        EffectClient(transport),
        [],
        code=BRANCH_EXISTS,
    )

    boundary = second.slices["leaf-a"]
    assert boundary.status is SliceStatus.DISPATCH_RETRY_SCHEDULED
    assert boundary.dispatch_retry_attempt == 1
    assert boundary.dispatch_next_attempt_at == 1_000.0 + BASE_DELAY
    assert transport.event_types().count("tl.dispatch_retry_scheduled") == 1


def _dispatching_slice() -> SliceState:
    return SliceState(
        id="leaf-a",
        status=SliceStatus.DISPATCHING,
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="opencode",
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        verdict=None,
        attempts=1,
        dispatch_intent_id="intent-1",
        dispatch_started_at=1_000.0,
    )


def test_an_uncreated_retry_boundary_cannot_be_abandoned(tmp_path: Path) -> None:
    """A boundary with nothing created has no attempt for an operator to abandon."""
    create("root", {}, root_dir=tmp_path / ".exo" / "tl-loop")
    store = RunStore("root", tmp_path / ".exo" / "tl-loop")
    store.checkpoint(
        TLPhase.TLDispatching,
        {
            "leaf-a": SliceState(
                id="leaf-a",
                status=SliceStatus.DISPATCH_RETRY_SCHEDULED,
                paths=("src/a.py",),
                depends_on=(),
                base_ref="main",
                test_plan=("just test",),
                agent_type="opencode",
                model=None,
                branch=None,
                worktree=None,
                pr_number=None,
                reviewed_head=None,
                verdict=None,
                attempts=1,
                dispatch_retry_attempt=1,
                dispatch_next_attempt_at=1_010.0,
                dispatch_error_code=BRANCH_EXISTS,
            )
        },
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )

    with pytest.raises(AbandonmentError, match="created no agent"):
        abandon_slice(tmp_path, "root", "leaf-a")

    # The refusal preserves the recovery signal instead of consuming it.
    assert store.load().slices["leaf-a"].status is SliceStatus.DISPATCH_RETRY_SCHEDULED
    assert store.load().slices["leaf-a"].dispatch_retry_attempt == 1
