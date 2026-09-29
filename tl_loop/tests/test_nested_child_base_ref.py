"""Regression coverage for #1141: a nested run's children target its own branch.

A leaf dispatched by a child sub-TL never progressed past ``spawned``: its
publication was refused, so it recorded no handoff and a closed PR could never
be reconciled into a park.

``_initial_slices`` gave every worker and leaf of a nested run the branch of
*that run's parent* as its immutable target branch (``base_ref``). A nested
run's own branch is what the shipped spawn path derives the child's head branch
from, and it is what the production ``file_pr`` effect derives the pull
request's base from: ``resolve_base_branch`` in
``rust/exomonad-core/src/services/file_pr.rs`` falls back to
``BirthBranch::parent()``, which strips the last dotted segment of the head
branch. ``main.stage.out-codex`` therefore publishes into ``main.stage``, not
into ``main``. Naming the grandparent made
``_publication_event_rejection`` refuse every nested publication with
``"base branch disagrees with the persisted owner base"``.

The base run never hit it because ``base_ref`` is ``None`` there and the check
is skipped, which is exactly why the root-leaf leg passed and the child leg
did not. Two kinds of child already disagreed on this one branch: a sub-TL
slice is declared with the run's own branch and its spawn boundary confirms
``base_ref=config.branch`` for the slice it dispatches, while a worker or leaf
was declared with the branch one level up.

These tests drive the production functions directly: the slice records
``_initial_slices`` builds, and the binder that accepts or refuses the
publication. No recorded checkpoint is read or edited.
"""

from __future__ import annotations

from pathlib import Path

from tl_loop.events.envelope import EventEnvelope, project
from tl_loop.fsm.event import PRFiled
from tl_loop.loop.driver import (
    LeafTask,
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    WorkerTask,
    _bind_publication_evidence,
    _child_config,
    _initial_slices,
    _publication_event_rejection,
)
from tl_loop.state.schema import SliceState, SliceStatus
from tl_loop.state.store import RunStore

PARENT_BRANCH = "main"
CHILD_BRANCH = "main.stage"
LEAF_SLICE = "out"
LEAF_AGENT_ID = "out-codex"
LEAF_INVOCATION = "inv-leaf-1"
CHILD_EPOCH = "child-controller-epoch"

#: What the shipped spawn path names the leaf's branch, and therefore what the
#: production ``file_pr`` effect derives the pull request's base from.
LEAF_HEAD_BRANCH = "main.stage.out-codex"
LEAF_BASE_BRANCH = "main.stage"


def _child_config_for(tmp_path: Path) -> TLLoopConfig:
    """Build the child run's config exactly as the sub-TL batch path does.

    ``_child_config`` reads the parent store's generation to hand the child its
    own, so the parent checkpoint has to exist first. It is created once and
    reused, because a run state is written exactly once.
    """
    from tl_loop.loop.driver import SubTLTask, create

    root_state = tmp_path / "root-state"
    if not RunStore("root", root_state).path.exists():
        create(
            "root",
            {"controller_epoch": "root-controller-epoch"},
            root_dir=root_state,
        )
    root_config = TLLoopConfig(
        active=False,
        max_events=1,
        root_dir=root_state,
        run_id="root",
        branch=PARENT_BRANCH,
        agent_id="root",
    )
    task = SubTLTask(
        "stage",
        WorkPlan(leaves=(LeafTask(LEAF_SLICE, "publish the leaf"),)),
    )
    return _child_config(
        root_config,
        task,
        source=None,  # type: ignore[arg-type]
        effects=None,  # type: ignore[arg-type]
        store=RunStore("root", root_state),
        branch=CHILD_BRANCH,
        worktree=str(tmp_path / "child-worktree"),
    )


def _child_leaf_slices(tmp_path: Path) -> dict[str, dict[str, object]]:
    return _initial_slices(
        WorkPlan(leaves=(LeafTask(LEAF_SLICE, "publish the leaf"),)),
        _child_config_for(tmp_path),
        tmp_path / "child-state",
        "stage",
    )


def _publication_envelope(base_branch: str, head_branch: str) -> EventEnvelope:
    """Project the ``pr.filed`` row the production ``file_pr`` effect writes."""
    return project(
        {
            "schema_version": 1,
            "event_id": "event-102",
            "id": "event-102",
            "event_time": "2026-09-29T00:00:00Z",
            "observed_at": "2026-09-29T00:00:00Z",
            "run_seq": 102,
            "type": "pr.filed",
            "agent_id": LEAF_AGENT_ID,
            "run_id": "swarm",
            "session_id": "session-1",
            "invocation_id": LEAF_INVOCATION,
            "lifecycle_state": "observed",
            "data": {
                "agent_id": LEAF_AGENT_ID,
                "slice_id": LEAF_SLICE,
                "invocation_id": LEAF_INVOCATION,
                "pr_number": 2,
                "head_sha": "b2c3d4e5c6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1",
                "head_branch": head_branch,
                "base_branch": base_branch,
            },
        }
    )


def _confirmed_slice(base_ref: str | None, branch: str) -> SliceState:
    """The slice as the spawn-confirmation boundary leaves it."""
    return SliceState(
        id=LEAF_SLICE,
        status=SliceStatus.SPAWNED,
        paths=(f"tl-loop/{LEAF_SLICE}",),
        depends_on=(),
        base_ref=base_ref,
        test_plan=("controller",),
        agent_type="codex",
        model=None,
        branch=branch,
        worktree="/tmp/child-worktree/out",
        pr_number=None,
        reviewed_head=None,
        attempts=1,
        verdict=None,
        dispatch_agent_id=LEAF_AGENT_ID,
        dispatch_invocation_id=LEAF_INVOCATION,
        dispatch_generation=1,
    )


def test_a_nested_runs_child_slices_target_their_own_runs_branch(tmp_path: Path) -> None:
    """The child's leaf records the branch its pull request is filed against.

    The run's own branch is what ``derive_child_branch`` hangs the child's head
    branch off, and what ``BirthBranch::parent()`` derives as the base. The
    grandparent is neither, and recording it is what refused every nested
    publication.
    """
    config = _child_config_for(tmp_path)
    slices = _child_leaf_slices(tmp_path)

    assert config.branch == CHILD_BRANCH
    assert config.parent_branch == PARENT_BRANCH
    assert slices[LEAF_SLICE]["base_ref"] == CHILD_BRANCH
    assert slices[LEAF_SLICE]["branch"] == f"{CHILD_BRANCH}.{LEAF_SLICE}"


def test_a_nested_runs_every_kind_of_child_agrees_on_one_target_branch(
    tmp_path: Path,
) -> None:
    """Workers, leaves, and sub-TLs all record the one branch they file against.

    A sub-TL slice already recorded the run's own branch; workers and leaves did
    not. The kinds of child disagreed on the one branch every publication is
    checked against, so only one of them could ever bind.
    """
    config = _child_config_for(tmp_path)
    slices = _initial_slices(
        WorkPlan(
            workers=(WorkerTask("helper", "help out"),),
            leaves=(LeafTask(LEAF_SLICE, "publish the leaf"),),
            sub_tls=(
                SubTLTask(
                    "grandchild",
                    WorkPlan(leaves=(LeafTask("deep", "go deep"),)),
                ),
            ),
        ),
        config,
        tmp_path / "child-state",
        "stage",
    )

    assert {name: record["base_ref"] for name, record in slices.items()} == {
        "helper": CHILD_BRANCH,
        LEAF_SLICE: CHILD_BRANCH,
        "grandchild": CHILD_BRANCH,
    }


def test_a_nested_publication_into_the_grandparent_branch_is_refused(
    tmp_path: Path,
) -> None:
    """The identity check that refused every real nested publication still holds.

    Before the fix the recorded base was the grandparent, so this event bound and
    the real one was refused -- the inverse. The refusal itself is unchanged and
    is what makes the corrected value meaningful.
    """
    slices = _child_leaf_slices(tmp_path)
    current = _confirmed_slice(
        slices[LEAF_SLICE]["base_ref"],  # type: ignore[arg-type]
        LEAF_HEAD_BRANCH,
    )
    envelope = _publication_envelope(PARENT_BRANCH, LEAF_HEAD_BRANCH)

    reason, historical = _publication_event_rejection(
        {LEAF_SLICE: current},
        PRFiled(2, "b2c3d4e5c6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1", LEAF_SLICE),
        envelope,
        LEAF_SLICE,
        CHILD_EPOCH,
    )

    assert reason == "base branch disagrees with the persisted owner base"
    assert historical is False


def test_a_nested_publication_binds_and_records_its_handoff(tmp_path: Path) -> None:
    """The leaf's publication binds, and the run records a durable handoff.

    This is the property the acceptance failed on: without a bound publication
    and its handoff there is no park for a closed PR to reconcile into, so the
    slice stayed at ``spawned`` forever.
    """
    slices = _child_leaf_slices(tmp_path)
    current = _confirmed_slice(
        slices[LEAF_SLICE]["base_ref"],  # type: ignore[arg-type]
        LEAF_HEAD_BRANCH,
    )
    envelope = _publication_envelope(LEAF_BASE_BRANCH, LEAF_HEAD_BRANCH)

    reason, _ = _publication_event_rejection(
        {LEAF_SLICE: current},
        PRFiled(2, "b2c3d4e5c6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1", LEAF_SLICE),
        envelope,
        LEAF_SLICE,
        CHILD_EPOCH,
    )
    assert reason is None

    bound = _bind_publication_evidence(
        {LEAF_SLICE: current},
        PRFiled(2, "b2c3d4e5c6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1", LEAF_SLICE),
        envelope,
        LEAF_SLICE,
        controller_epoch=CHILD_EPOCH,
        validation_slices={LEAF_SLICE: current},
    )
    leaf = bound[LEAF_SLICE]

    assert leaf.publication is not None
    assert leaf.publication.pr_number == 2
    assert leaf.publication.base_branch == LEAF_BASE_BRANCH
    assert leaf.publication.head_branch == LEAF_HEAD_BRANCH
    assert leaf.handoff is not None
    assert leaf.handoff.pr_number == 2
    assert leaf.handoff.invocation_id == LEAF_INVOCATION
    assert leaf.handoff.agent_id == LEAF_AGENT_ID
    assert leaf.status is SliceStatus.IN_REVIEW


def test_a_root_runs_leaves_record_no_immutable_target_branch() -> None:
    """The base run's own leaves are unchanged: they declare no target branch.

    The root run's leaf is spawned from ``main`` and files against ``main``,
    which the binder reads from the publication itself. Naming a base here would
    be a new claim, not a correction, so the fix must not make one.
    """
    root_config = TLLoopConfig(
        active=False,
        max_events=1,
        root_dir=Path("state"),
        run_id="root",
        branch=PARENT_BRANCH,
        agent_id="root",
    )
    slices = _initial_slices(
        WorkPlan(leaves=(LeafTask(LEAF_SLICE, "publish the leaf"),)),
        root_config,
        Path("state"),
        "root",
    )

    assert slices[LEAF_SLICE]["base_ref"] is None
    assert slices[LEAF_SLICE]["branch"] is None


def test_the_spawn_confirmation_boundary_is_what_corrects_the_head_branch(
    tmp_path: Path,
) -> None:
    """The head branch is corrected at spawn time; only the base was left wrong.

    ``_initial_slices`` cannot know the agent-type suffix the server appends, so
    the recorded head branch is a pre-spawn placeholder that the
    ``agent.spawned`` boundary replaces. No boundary corrects ``base_ref``,
    which is why a wrong base survived the whole dispatch.
    """
    slices = _child_leaf_slices(tmp_path)
    placeholder = slices[LEAF_SLICE]["branch"]
    confirmed = _confirmed_slice(
        slices[LEAF_SLICE]["base_ref"],  # type: ignore[arg-type]
        LEAF_HEAD_BRANCH,
    )

    assert placeholder != LEAF_HEAD_BRANCH
    assert confirmed.branch == LEAF_HEAD_BRANCH
    # The placeholder correction is not a base correction: the base the
    # publication must name is the run's own branch either way.
    envelope = _publication_envelope(LEAF_BASE_BRANCH, confirmed.branch)
    reason, _ = _publication_event_rejection(
        {LEAF_SLICE: confirmed},
        PRFiled(2, "b2c3d4e5c6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1", LEAF_SLICE),
        envelope,
        LEAF_SLICE,
        CHILD_EPOCH,
    )
    assert reason is None


def test_the_production_base_is_the_head_branchs_own_parent() -> None:
    """The corrected value is the one the shipped ``file_pr`` derives itself.

    ``resolve_base_branch`` in ``rust/exomonad-core/src/services/file_pr.rs``
    falls back to ``BirthBranch::parent()``, which strips the last dotted
    segment. This pins the constant the tests above use to that derivation so a
    future change to either side has to be deliberate.
    """
    head_branch, _, _ = LEAF_HEAD_BRANCH.rpartition(".")
    assert head_branch == LEAF_BASE_BRANCH
    assert LEAF_BASE_BRANCH != PARENT_BRANCH