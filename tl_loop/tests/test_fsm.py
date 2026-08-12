"""Transition coverage for the pure TL lifecycle FSM."""

from __future__ import annotations

import inspect

import tl_loop.fsm.event as event_module
import tl_loop.fsm.phase as phase_module
import tl_loop.fsm.transition as transition_module
from tl_loop.fsm import (
    AllChildrenDone,
    ChildCompleted,
    ChildFailed,
    ChildHandle,
    ChildSpawned,
    IllegalTransition,
    OwnPRFiled,
    PRFiled,
    PRMerged,
    PRUpdated,
    TLAllMerged,
    TLDispatching,
    TLDone,
    TLEvent,
    TLFailed,
    TLMerging,
    TLPhase,
    TLPlanning,
    TLPRFiled,
    TLWaiting,
    transition,
)

HANDLE_A = ChildHandle("a", "main.a", "codex")
HANDLE_B = ChildHandle("b", "main.b", "claude")


def test_phase_enum_matches_haskell_constructors() -> None:
    assert {phase.name for phase in TLPhase} == {
        "TLPlanning",
        "TLDispatching",
        "TLWaiting",
        "TLMerging",
        "TLAllMerged",
        "TLPRFiled",
        "TLDone",
        "TLFailed",
    }


def test_child_spawned_starts_and_accumulates_waiting_set() -> None:
    started = transition(TLPlanning(), ChildSpawned(HANDLE_A))
    assert started == TLWaiting({"a": HANDLE_A})

    accumulated = transition(started, ChildSpawned(HANDLE_B))
    assert accumulated == TLWaiting({"a": HANDLE_A, "b": HANDLE_B})


def test_child_spawned_from_dispatching_and_other_phases_resets_set() -> None:
    assert transition(TLDispatching(), ChildSpawned(HANDLE_A)) == TLWaiting({"a": HANDLE_A})
    assert transition(TLAllMerged(), ChildSpawned(HANDLE_A)) == TLWaiting({"a": HANDLE_A})


def test_child_completed_shrinks_waiting_set_and_finishes_last_child() -> None:
    phase = TLWaiting({"a": HANDLE_A, "b": HANDLE_B})
    remaining = transition(phase, ChildCompleted("a"))
    assert remaining == TLWaiting({"b": HANDLE_B})
    assert phase == TLWaiting({"a": HANDLE_A, "b": HANDLE_B})
    assert transition(remaining, ChildCompleted("b")) == TLAllMerged()


def test_child_completed_missing_slug_still_finishes_empty_waiting_set() -> None:
    assert transition(TLWaiting({}), ChildCompleted("missing")) == TLAllMerged()


def test_child_failed_uses_the_haskell_failure_message() -> None:
    assert transition(TLWaiting({"a": HANDLE_A}), ChildFailed("a", "timed out")) == TLFailed(
        "a: timed out"
    )


def test_pr_merged_handles_waiting_and_merging_phases() -> None:
    waiting = TLWaiting({"a": HANDLE_A, "b": HANDLE_B})
    assert transition(waiting, PRMerged(10, "a")) == TLWaiting({"b": HANDLE_B})
    assert transition(TLWaiting({"a": HANDLE_A}), PRMerged(10, "a")) == TLAllMerged()
    merging = TLMerging(10, {"a": HANDLE_A, "b": HANDLE_B})
    assert transition(merging, PRMerged(10, "a")) == TLWaiting({"b": HANDLE_B})


def test_pr_lifecycle_transitions_preserve_the_current_tl_phase() -> None:
    phase = TLWaiting({"a": HANDLE_A})
    assert transition(phase, PRFiled(10, "head-a", "a")) == phase
    assert transition(phase, PRUpdated(10, "head-b", "a")) == phase


def test_all_children_done_and_own_pr_filed_are_explicit_wildcard_arms() -> None:
    assert transition(TLPlanning(), AllChildrenDone()) == TLDone()
    assert transition(TLFailed("old"), AllChildrenDone()) == TLDone()
    assert transition(TLAllMerged(), OwnPRFiled(12, "https://forgejo/pr/12", "main.tl")) == (
        TLPRFiled(12, "https://forgejo/pr/12")
    )


def test_every_silent_haskell_noop_is_an_illegal_transition() -> None:
    completed_illegal = [
        TLPlanning(),
        TLDispatching(),
        TLMerging(10, {}),
        TLAllMerged(),
        TLPRFiled(12, "url"),
        TLDone(),
        TLFailed("failure"),
    ]
    merged_illegal = [
        TLPlanning(),
        TLDispatching(),
        TLAllMerged(),
        TLPRFiled(12, "url"),
        TLDone(),
        TLFailed("failure"),
    ]
    for phase in completed_illegal:
        _assert_illegal(phase, ChildCompleted("a"))
    for phase in merged_illegal:
        _assert_illegal(phase, PRMerged(10, "a"))


def test_unknown_event_is_illegal() -> None:
    class UnknownEvent(TLEvent):
        pass

    _assert_illegal(TLPlanning(), UnknownEvent())


def test_fsm_modules_do_not_import_client_effects() -> None:
    for module in (event_module, phase_module, transition_module):
        assert "tl_loop.client" not in inspect.getsource(module)


def _assert_illegal(phase: object, event: TLEvent) -> None:
    try:
        transition(phase, event)  # type: ignore[arg-type]
    except IllegalTransition as error:
        assert error.phase is phase
        assert error.event is event
    else:
        raise AssertionError(f"Expected illegal transition for {phase!r} and {event!r}")
