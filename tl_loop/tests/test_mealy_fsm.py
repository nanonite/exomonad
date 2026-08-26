from __future__ import annotations

from tl_loop.events.envelope import EventKind
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
