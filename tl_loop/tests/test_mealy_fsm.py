from __future__ import annotations

from tl_loop.events.envelope import EventKind
from tl_loop.fsm.child import ChildKind, ChildRecord
from tl_loop.fsm.scope import TLAllMerged, TLDone, TLFinalizing, TLPlanning, TLPRFiled, TLRunning
from tl_loop.fsm.scope_events import (
    FinalizationComplete,
    FinalizationRequested,
    ScopeRole,
    StageReleased,
    WorkerCompleted,
)
from tl_loop.loop.fsm import HeartbeatTick, WatcherObservationEvent, step
from tl_loop.loop.observation import WatcherObservation
from tl_loop.state.schema import (
    BudgetLedger,
    EventCursor,
    FSMState,
    RunState,
    SliceState,
    SliceStatus,
    TLPhase,
)


def _state(status: SliceStatus = SliceStatus.SPAWNED) -> RunState:
    slice_state = SliceState(
        id="slice-a",
        status=status,
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="codex",
        model="gpt-5",
        branch="task/slice-a",
        worktree=".worktrees/slice-a",
        pr_number=42,
        reviewed_head=None,
        attempts=1,
        verdict=None,
        dispatch_intent_id="intent-a",
        dispatch_agent_id="agent-a",
    )
    return RunState(
        version=1,
        revision=0,
        run_id="fsm-run",
        fsm=FSMState(TLPhase.TLWaiting, ("slice-a",)),
        slices={"slice-a": slice_state},
        budgets=BudgetLedger(0, 0),
        gates=(),
        events=EventCursor(0),
    )


def test_heartbeat_step_is_pure_and_deterministic() -> None:
    state = _state()
    first = step(state, HeartbeatTick(10.0))
    second = step(state, HeartbeatTick(10.0))

    assert first == second
    assert first.state == state
    assert first.ignored_reason is not None


def test_watcher_observation_step_records_typed_reconciliation() -> None:
    state = _state()
    event = WatcherObservationEvent(
        "slice-a",
        WatcherObservation.from_response(
            {
                "found": True,
                "publication_ownership_verified": False,
                "publication_ownership_error": "succession is missing",
            }
        ),
        authoritative_owner_id="agent-a",
    )

    transition = step(state, event)

    assert transition.state.slices["slice-a"].reconciliation is not None
    assert transition.actions[0].operation == "park_publication_ownership_unresolved"


def test_watcher_event_kinds_are_part_of_the_closed_alphabet() -> None:
    assert EventKind.WATCHER_PR_OBSERVATION.value == "watcher.pr_observation"
    assert EventKind.WATCHER_OWNERSHIP_UNRESOLVED.value == "watcher.ownership_unresolved"
    assert EventKind.WATCHER_POLL_CYCLE.value == "watcher.poll_cycle"


def _canonical_state(phase: object) -> RunState:
    return RunState(
        version=1,
        revision=0,
        run_id="canonical-run",
        fsm=FSMState(TLPhase.TLPlanning, ()),
        slices={},
        budgets=BudgetLedger(0, 0),
        gates=(),
        events=EventCursor(0),
        recursive_fsm=phase,
    )


def test_mealy_scope_step_projects_root_finalization_and_advances_once() -> None:
    planning = TLPlanning(scope_path=("root",), plan_digest="plan")
    state = _canonical_state(planning)

    all_merged = step(state, StageReleased(0, (), ("root",))).state
    assert isinstance(all_merged.recursive_fsm, TLAllMerged)
    assert all_merged.fsm.phase is TLPhase.TLAllMerged
    assert all_merged.state_version == 1

    finalizing = step(all_merged, FinalizationRequested(ScopeRole.ROOT)).state
    assert isinstance(finalizing.recursive_fsm, TLFinalizing)
    assert finalizing.state_version == 2
    done = step(
        finalizing,
        FinalizationComplete(
            ScopeRole.ROOT,
            {"root_branch": "main", "local_checkout": "checkout"},
        ),
    ).state
    assert isinstance(done.recursive_fsm, TLDone)
    assert done.fsm.phase is TLPhase.TLDone
    assert done.state_version == 3


def test_mealy_scope_step_uses_non_root_aggregate_terminal() -> None:
    all_merged = TLAllMerged(scope_path=("root", "child"), plan_digest="child-plan")
    state = _canonical_state(all_merged)
    finalizing = step(state, FinalizationRequested(ScopeRole.NON_ROOT)).state
    filed = step(
        finalizing,
        FinalizationComplete(
            ScopeRole.NON_ROOT,
            {
                "aggregate_pr": "43",
                "head_sha": "head-43",
                "base_sha": "main",
                "parent_branch": "main",
                "handoff": "handoff-43",
            },
        ),
    ).state

    assert isinstance(filed.recursive_fsm, TLPRFiled)
    assert filed.fsm.phase is TLPhase.TLPRFiled
    assert filed.recursive_fsm.parent_branch == "main"


def test_mealy_scope_step_reduces_typed_worker_completion() -> None:
    worker = ChildRecord("worker", ChildKind.WORKER, manifest_node_id="worker", manifest_revision=1)
    running = TLRunning(
        current_order=0,
        pending_by_order={},
        parallel_pending=(worker,),
    )
    state = _canonical_state(running)
    completed = step(state, WorkerCompleted("worker", "result")).state

    assert isinstance(completed.recursive_fsm, TLAllMerged)
    assert completed.state_version == 1
