"""Coverage for the pure TL stop predicates."""

from __future__ import annotations

from tl_loop.fsm import (
    ChildHandle,
    PhaseValue,
    TLAllMerged,
    TLDispatching,
    TLFailed,
    TLPRFiled,
    TLPlanning,
    TLDone,
    TLMerging,
    TLWaiting,
    is_terminal,
    is_waiting,
)


CHILDREN = {"child": ChildHandle("child", "main.child", "codex")}


def test_waiting_predicate_matches_the_three_nudge_phases() -> None:
    assert is_waiting(TLDispatching())
    assert is_waiting(TLWaiting(CHILDREN))
    assert is_waiting(TLMerging(1, CHILDREN))
    assert not is_waiting(TLPlanning())
    assert not is_waiting(TLAllMerged())
    assert not is_waiting(TLPRFiled(1, "url"))
    assert not is_waiting(TLDone())
    assert not is_waiting(TLFailed("failure"))


def test_terminal_predicate_matches_clean_exit_phases() -> None:
    assert is_terminal(TLPlanning())
    assert is_terminal(TLAllMerged())
    assert is_terminal(TLDone())
    assert is_terminal(TLFailed("failure"))
    assert not is_terminal(TLDispatching())
    assert not is_terminal(TLWaiting(CHILDREN))
    assert not is_terminal(TLMerging(1, CHILDREN))
    assert not is_terminal(TLPRFiled(1, "url"))


def test_waiting_implies_not_terminal_for_every_phase_constructor() -> None:
    phases: list[PhaseValue] = [
        TLPlanning(),
        TLDispatching(),
        TLWaiting(CHILDREN),
        TLMerging(1, CHILDREN),
        TLAllMerged(),
        TLPRFiled(1, "url"),
        TLDone(),
        TLFailed("failure"),
    ]
    assert all(not is_terminal(phase) for phase in phases if is_waiting(phase))
