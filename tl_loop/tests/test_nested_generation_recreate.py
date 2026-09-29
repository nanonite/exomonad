"""Regression coverage for #1141: a recreated nested run keeps its own generation.

A leaf dispatched under a child sub-TL never progressed past ``spawned``. #1141's
first fix gave a nested run's children the branch their pull request is filed
against, so the publication could bind at all. This file covers the second
defect the child leg then exposed: the recreated child bound the *predecessor's*
publication, so the run never owned PR B.

A nested run is never ``init``-launched, so ``_controller_epoch`` mints its
first epoch instead of reading an operator-owned marker. It seeded that value
from ``run_id`` alone, which made it a function of the run's *name* rather than
of the run. The marker lives inside the parent run directory, and a confirmed
recreate replaces that directory wholesale, so the recreated child re-minted
the identical epoch. Equal epochs mean an equal ``generation_id``, which means
an equal dispatch intent identity, and so the predecessor generation's
``agent.spawned`` row confirmed the recreated dispatch with the dead
invocation. The pre-recreate ``pr.filed`` row then satisfied the binder's
invocation check and bound PR A.

The root run never hit it because ``exomonad init`` owns the root's marker and
mints a fresh time-based epoch on every launch, so a recreated root always has
a new generation. Only a nested run's generation was name-derived.

These tests drive the production functions directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tl_loop.fsm.event import PRFiled
from tl_loop.loop.driver import (
    TLLoopConfig,
    _bind_publication_evidence,
    _confirm_dispatch_event,
    _controller_epoch,
    _new_dispatch_attempt,
)
from tl_loop.state.schema import RunState, SliceStatus
from tl_loop.state.store import RunStore, create

CHILD_RUN_ID = "stage"
LEAF_SLICE = "out"
LEAF_AGENT_ID = "out-codex"

#: Two parent generations, as a confirmed recreate produces: the predecessor's
#: and the one the recreated root mints on its next launch.
PREDECESSOR_GENERATION = "controller-1790705189485-696887"
RECREATED_GENERATION = "controller-1790705196788-697934"

HEAD_SHA = "0bc9a4ccbe07d9b933867d8276d75c4cf582be7a"
DEAD_INVOCATION = "18474dcd-72f6-4073-a0a2-2866996c5e50"
CURRENT_INVOCATION = "f36b2493-e71f-4ba7-85aa-546932679d35"

PR_A = 1
PR_B = 2

#: The base a nested leaf's pull request is filed against: its own run's branch.
CHILD_BASE = "main.stage"
CHILD_HEAD_BRANCH = "main.stage.out-codex"


def _child_epoch(root: Path, parent_generation: str) -> str:
    """The epoch a child run mints under one parent generation."""
    return _controller_epoch(root, CHILD_RUN_ID, parent_generation)


def test_a_nested_runs_generation_differs_when_its_parent_is_recreated(
    tmp_path: Path,
) -> None:
    """The recreated child is a different run, so it gets a different generation.

    The child marker is written inside the parent run directory, so the recreate
    that replaces that directory also discards the predecessor's marker. What
    the marker is *re-minted from* is the whole difference: a name-derived value
    reproduces the predecessor's generation exactly.
    """
    predecessor_root = tmp_path / "before"
    recreated_root = tmp_path / "after"

    predecessor = _child_epoch(predecessor_root, PREDECESSOR_GENERATION)
    recreated = _child_epoch(recreated_root, RECREATED_GENERATION)

    assert predecessor != recreated
    assert len(predecessor) == len(recreated) == 32


def test_a_nested_runs_epoch_is_stable_for_one_parent_generation(
    tmp_path: Path,
) -> None:
    """A child that is not recreated keeps its generation.

    ``--continue`` re-runs a child against the same parent generation. Its
    dispatch intent must not move, or an attempt still in flight would be
    orphaned by a marker read alone.
    """
    root = tmp_path / "state"

    first = _child_epoch(root, PREDECESSOR_GENERATION)

    assert _child_epoch(root, PREDECESSOR_GENERATION) == first


def test_an_unlaunched_root_runs_epoch_is_unchanged_by_this_fix(
    tmp_path: Path,
) -> None:
    """A run with no parent generation keeps the name-derived seed it always had.

    The root run's marker is operator-owned and time-based. Only the nested
    seed changes, so a caller that supplies no parent generation -- every root
    run, and every existing checkpoint -- must still mint what it minted before.
    """
    root = tmp_path / "state"
    marker = root / f"{CHILD_RUN_ID}.controller-epoch"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("controller-1790705196788-697934\n", encoding="utf-8")

    assert _controller_epoch(root, CHILD_RUN_ID) == "controller-1790705196788-697934"
    assert (
        _controller_epoch(root, CHILD_RUN_ID, PREDECESSOR_GENERATION)
        == "controller-1790705196788-697934"
    )


def _child_state(root: Path, parent_generation: str) -> RunState:
    """Create the child run's state the way the sub-TL batch path does."""
    from tl_loop.loop.driver import LeafTask, WorkPlan, _initial_slices

    root.mkdir(parents=True, exist_ok=True)
    plan = WorkPlan(leaves=(LeafTask(LEAF_SLICE, "publish the leaf"),))
    config = TLLoopConfig(
        branch=CHILD_BASE,
        parent_branch="main",
        parent_run_id="root",
        parent_generation_id=parent_generation,
    )
    create(
        CHILD_RUN_ID,
        {
            "controller_epoch": _child_epoch(root, parent_generation),
            "slices": _initial_slices(plan, config, root, CHILD_RUN_ID),
        },
        root_dir=root,
    )
    return RunStore(CHILD_RUN_ID, root).load()


def test_a_recreated_child_dispatches_under_an_intent_its_predecessor_cannot_confirm(
    tmp_path: Path,
) -> None:
    """The predecessor's spawn row no longer confirms the recreated dispatch.

    This is the identity that made PR A bind: equal generations, equal intent
    ids, so ``_dispatch_confirmation_matches`` accepted a row belonging to a
    run that no longer exists and installed its dead invocation.
    """
    predecessor = _child_state(tmp_path / "before", PREDECESSOR_GENERATION)
    recreated = _child_state(tmp_path / "after", RECREATED_GENERATION)

    assert predecessor.generation_id != recreated.generation_id

    predecessor_attempt = _new_dispatch_attempt(predecessor, LEAF_SLICE, TLLoopConfig())
    recreated_attempt = _new_dispatch_attempt(recreated, LEAF_SLICE, TLLoopConfig())

    assert predecessor_attempt.intent_id != recreated_attempt.intent_id


def test_a_predecessors_spawn_row_cannot_confirm_the_recreated_childs_dispatch(
    tmp_path: Path,
) -> None:
    """Delivering the old row changes nothing, so the live row is the only one.

    Mirrors the real ledger, which is shared and permanent: after the recreate
    the child re-reads it from the start, so the predecessor's ``agent.spawned``
    arrives before the current one.
    """
    from dataclasses import replace

    from tl_loop.tests.test_driver import _canonical_event

    state = _child_state(tmp_path / "after", RECREATED_GENERATION)
    attempt = _new_dispatch_attempt(state, LEAF_SLICE, TLLoopConfig())
    dispatching = replace(
        state.slices[LEAF_SLICE],
        status=SliceStatus.DISPATCHING,
        attempts=1,
        dispatch_intent_id=attempt.intent_id,
        dispatch_generation=attempt.dispatch_generation,
    )
    slices = {LEAF_SLICE: dispatching}

    stale = replace(
        _canonical_event(
            20, "agent.spawned", LEAF_SLICE, CHILD_RUN_ID, intent_id="stale-intent"
        ),
        invocation_id=DEAD_INVOCATION,
    )
    assert _confirm_dispatch_event(slices, slices, stale, LEAF_SLICE, 20) == slices

    current = replace(
        _canonical_event(
            61, "agent.spawned", LEAF_SLICE, CHILD_RUN_ID, intent_id=attempt.intent_id
        ),
        invocation_id=CURRENT_INVOCATION,
    )
    confirmed = _confirm_dispatch_event(slices, slices, current, LEAF_SLICE, 61)

    assert confirmed[LEAF_SLICE].dispatch_invocation_id == CURRENT_INVOCATION


def test_a_recreated_child_binds_the_current_publication_not_the_predecessors(
    tmp_path: Path,
) -> None:
    """The run owns PR B, which is what the acceptance asserts.

    The end-to-end statement of the defect: with the predecessor's row unable
    to confirm, the current invocation owns the slice, so the pre-recreate
    publication is refused as a prior invocation and the current one binds.
    """
    from dataclasses import replace

    from tl_loop.events.envelope import project

    state = _child_state(tmp_path / "after", RECREATED_GENERATION)
    attempt = _new_dispatch_attempt(state, LEAF_SLICE, TLLoopConfig())
    base_slice = state.slices[LEAF_SLICE]
    # The head branch the spawn boundary installs carries the agent-type
    # suffix the server appends, which the initial record cannot predict.
    confirmed = replace(
        base_slice,
        status=SliceStatus.SPAWNED,
        attempts=1,
        branch=CHILD_HEAD_BRANCH,
        dispatch_agent_id=LEAF_AGENT_ID,
        dispatch_invocation_id=CURRENT_INVOCATION,
        dispatch_intent_id=attempt.intent_id,
        dispatch_generation=attempt.dispatch_generation,
    )
    slices = {LEAF_SLICE: confirmed}

    def publication(pr_number: int, invocation: str):
        return project(
            {
                "schema_version": 1,
                "event_id": f"event-{pr_number}",
                "id": f"event-{pr_number}",
                "event_time": "2026-09-29T00:00:00Z",
                "observed_at": "2026-09-29T00:00:00Z",
                "run_seq": 28 if pr_number == PR_A else 70,
                "type": "pr.filed",
                "agent_id": LEAF_AGENT_ID,
                "run_id": "swarm",
                "session_id": "session-1",
                "invocation_id": invocation,
                "lifecycle_state": "observed",
                "data": {
                    "agent_id": LEAF_AGENT_ID,
                    "slice_id": LEAF_SLICE,
                    "invocation_id": invocation,
                    "pr_number": pr_number,
                    "head_sha": HEAD_SHA,
                    "head_branch": CHILD_HEAD_BRANCH,
                    "base_branch": CHILD_BASE,
                },
            }
        )

    after_stale = _bind_publication_evidence(
        slices, PRFiled(PR_A, HEAD_SHA, LEAF_SLICE), publication(PR_A, DEAD_INVOCATION),
        LEAF_SLICE, controller_epoch=None, validation_slices=slices,
    )
    assert after_stale[LEAF_SLICE].publication is None

    bound = _bind_publication_evidence(
        after_stale, PRFiled(PR_B, HEAD_SHA, LEAF_SLICE),
        publication(PR_B, CURRENT_INVOCATION), LEAF_SLICE, controller_epoch=None,
        validation_slices=after_stale,
    )
    leaf = bound[LEAF_SLICE]
    assert leaf.publication is not None
    assert leaf.publication.pr_number == PR_B
    assert leaf.handoff is not None
    assert leaf.handoff.pr_number == PR_B
    assert leaf.handoff.invocation_id == CURRENT_INVOCATION


def test_a_config_rejects_a_non_text_parent_generation() -> None:
    """The new field is validated like every other optional identity field."""
    with pytest.raises(ValueError):
        TLLoopConfig(parent_generation_id=object())  # type: ignore[arg-type]
