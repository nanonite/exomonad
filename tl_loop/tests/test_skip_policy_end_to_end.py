"""End-to-end skip-policy and post-skip re-planning coverage (#1599).

``test_parked_stage_skip.py`` pins the skip decision and the immediate dependent
projection. This module drives the same fixture through the production
``run_tl_loop`` path so the operator-visible end state is asserted: what
``proceed``, ``block`` and ``require-named`` leave behind, whether the run ends
in ``TLDone`` or stays held, and whether a fresh run may bind a Chainlink issue
an older run already referenced.

The plan shape is generic: stage A is parked behind a held unknown launch
effect, stages B and C are ordered after it, and sibling D shares stage one with
A so it does not depend on it. Empty stage children declare no aggregate PR, so
the production loop can complete B, C and D without a harness.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

from tl_loop.__main__ import _plan_from_document
from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import JsonObject, TransportClient
from tl_loop.fsm.phase import TLPhase
from tl_loop.fsm.post_merge import PostMergePhase
from tl_loop.fsm.scope import TLDone as RecursiveTLDone
from tl_loop.fsm.scope import TLRunning as RecursiveTLRunning
from tl_loop.loop.driver import (
    LoopCancelled,
    TLLoopConfig,
    WorkPlan,
    _advance_post_merge_boundary,
    _bind_initial_slices,
    _dispatch_children,
    _initial_slices,
    _manifest_for_plan,
    _skip_dependents,
    _skip_withholds_dependent,
    run_tl_loop,
)
from tl_loop.plan_validation import validate_plan_document
from tl_loop.state.plan_manifest import build_plan_manifest
from tl_loop.state.schema import (
    BudgetLedger,
    RepositoryIdentity,
    RunState,
    SkipDependencyPolicy,
    SliceStatus,
)
from tl_loop.state.store import RunStore
from tl_loop.tests.checkpoint_fixtures import create
from tl_loop.tests.test_chainlink_issue_binding import IssueCloseTransport
from tl_loop.tests.test_driver import SyntheticQueue
from tl_loop.tests.test_parked_stage_skip import (
    NEXT,
    SIBLING,
    STAGE,
    STAGE_ISSUE,
    _answer_skip,
    _Fixture,
    _run,
    _skipped,
    _with_post_merge,
)

#: A third ordered stage after NEXT. Generic on purpose: the fixture is not a
#: project-specific plan.
LATER = "later-stage"
#: A freshly created sub-issue for the repair plan.
FRESH_ISSUE = 705
#: The new run that plans the repair under the still-open skipped issue.
REPAIR_RUN = "repair-run"


def _bounded_run(fixture: _Fixture, tmp_path: Path) -> RunState:
    """Drive one production ``run_tl_loop`` call with a wall-clock safety net.

    A held dependent must leave the run waiting rather than hang the suite, so a
    regression of the release path fails with ``LoopCancelled`` instead of
    blocking pytest forever.
    """
    cancel_event = threading.Event()
    timer = threading.Timer(10.0, cancel_event.set)
    timer.start()
    try:
        run_tl_loop(
            "parent",
            fixture.plan,
            SyntheticQueue([]),
            ReadOnlyEffectClient(
                EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
            ),
            config=replace(
                fixture.config,
                active=False,
                session_mode="continue",
                keep_alive_on_waiting=False,
                cancel_event=cancel_event,
                poll_interval=0.01,
            ),
            root_dir=fixture.parent_store.root_dir,
            budgets=BudgetLedger(0, 0),
        )
    except LoopCancelled:
        raise AssertionError(
            "run_tl_loop did not settle; the skip policy left the run spinning"
        ) from None
    finally:
        timer.cancel()
    return fixture.parent_store.load()


def _is_tl_done(state: RunState) -> bool:
    return isinstance(state.recursive_fsm, RecursiveTLDone) or (
        state.fsm.phase is TLPhase.TLDone
    )


def _three_stage_fixture(
    tmp_path: Path, *, policy: SkipDependencyPolicy
) -> _Fixture:
    return _run(
        tmp_path,
        policy=policy,
        chain=(NEXT, LATER),
        parallel_sibling=True,
        optional_chain=True,
    )


# ---------------------------------------------------------------------------
# proceed


def test_proceed_runs_every_dependent_and_ends_the_run(tmp_path: Path) -> None:
    fixture = _three_stage_fixture(tmp_path, policy=SkipDependencyPolicy.PROCEED)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)
    _skipped(fixture)

    state = _bounded_run(fixture, tmp_path)

    assert state.slices[STAGE].status is SliceStatus.SKIPPED
    for name in (NEXT, LATER, SIBLING):
        assert state.slices[name].status is SliceStatus.MERGED, name
    assert _is_tl_done(state), "proceed must end the run in TLDone, not a hold"


def test_proceed_policy_withholds_no_dependent(tmp_path: Path) -> None:
    """Mutation guard: holding any dependent under ``proceed`` fails here."""
    fixture = _three_stage_fixture(tmp_path, policy=SkipDependencyPolicy.PROCEED)
    dependents = _skip_dependents(fixture.plan, STAGE)
    assert dependents == (NEXT, LATER)

    slices = fixture.parent_store.load().slices
    for name in dependents:
        assert (
            _skip_withholds_dependent(
                slices[name], SkipDependencyPolicy.PROCEED, ()
            )
            is None
        ), name
    # A sibling in the skipped stage is not a dependent at all.
    assert SIBLING not in dependents
    assert slices[NEXT].status is SliceStatus.PENDING


# ---------------------------------------------------------------------------
# block and require-named


def test_block_holds_every_dependent_and_names_it(tmp_path: Path) -> None:
    fixture = _three_stage_fixture(tmp_path, policy=SkipDependencyPolicy.BLOCK)
    _answer_skip(fixture, policy=SkipDependencyPolicy.BLOCK)
    _skipped(fixture)

    state = _bounded_run(fixture, tmp_path)

    assert state.slices[STAGE].status is SliceStatus.SKIPPED
    assert state.slices[SIBLING].status is SliceStatus.MERGED
    for name in (NEXT, LATER):
        held = state.slices[name]
        assert held.status is SliceStatus.BLOCKED, name
        assert held.blocked_by == STAGE
        assert held.dispatch_intent_id is None, "a held dependent was dispatched"
        assert held.pr_number is None
        assert isinstance(held.park_audit, Mapping)
        assert "dependency policy" in str(held.park_audit["reason"])
        assert str(held.park_audit["gate_name"]).endswith("-skip")
    # The run is held behind the blocked dependents, not terminal.
    assert isinstance(state.recursive_fsm, RecursiveTLRunning)
    assert not _is_tl_done(state)
    assert state.current_order == 2


def test_require_named_runs_only_the_named_dependent(tmp_path: Path) -> None:
    fixture = _three_stage_fixture(tmp_path, policy=SkipDependencyPolicy.REQUIRE_NAMED)
    _answer_skip(
        fixture,
        policy=SkipDependencyPolicy.REQUIRE_NAMED,
        dependents=(NEXT,),
    )
    _skipped(fixture)

    state = _bounded_run(fixture, tmp_path)

    assert state.slices[STAGE].status is SliceStatus.SKIPPED
    assert state.slices[NEXT].status is SliceStatus.MERGED
    assert state.slices[SIBLING].status is SliceStatus.MERGED
    unnamed = state.slices[LATER]
    assert unnamed.status is SliceStatus.BLOCKED
    assert unnamed.blocked_by == STAGE
    assert unnamed.dispatch_intent_id is None, "an unnamed dependent ran silently"
    assert isinstance(unnamed.park_audit, Mapping)
    assert "unnamed" in str(unnamed.park_audit["reason"])
    # NEXT completed, so the scope advanced to the held stage three.
    assert isinstance(state.recursive_fsm, RecursiveTLRunning)
    assert not _is_tl_done(state)
    assert state.current_order == 3


# ---------------------------------------------------------------------------
# a fresh plan may bind an issue an older run referenced


def _repair_document() -> dict[str, object]:
    return {
        "run_id": REPAIR_RUN,
        "plan": {
            "leaves": [
                {
                    "name": "repair",
                    "task": "repair the skipped work under the reopened issue",
                    "chainlink_issue_id": STAGE_ISSUE,
                },
                {
                    "name": "split",
                    "task": "land the freshly split sub-issue",
                    "chainlink_issue_id": FRESH_ISSUE,
                },
            ]
        },
    }


class _SpawnTransport:
    """Accept the bounded dispatch effects without a live host."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, arguments))
        if tool_name == "spawn_leaf":
            slice_name = str(arguments.get("name"))
            return {
                "success": True,
                "result": {
                    "agent_id": f"agent-{slice_name}",
                    "invocation_id": f"invocation-{slice_name}",
                },
            }
        return {"success": True, "result": {}}


def _admit_repair_run(
    tmp_path: Path, *, state_root: Path
) -> tuple[RunStore, RunState, WorkPlan, TLLoopConfig]:
    """Validate, bind and create the second run exactly as admission does."""
    validated = validate_plan_document(_repair_document())
    plan = _plan_from_document(validated)
    config = TLLoopConfig(
        root_dir=state_root,
        project_root=tmp_path,
        branch="main",
        session_mode="start",
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
        max_workers=2,
        max_leaves=2,
    )
    manifest = _manifest_for_plan(plan, REPAIR_RUN, config)
    slices = _bind_initial_slices(
        _initial_slices(plan, config, state_root, REPAIR_RUN),
        manifest,
    )
    create(
        REPAIR_RUN,
        {"plan_manifest": manifest.to_document(), "slices": slices},
        root_dir=state_root,
    )
    store = RunStore(REPAIR_RUN, state_root)
    return store, store.load(), plan, config


def _run_bytes(run_dir: Path) -> dict[str, bytes]:
    """Every durable file in one run directory, keyed by its relative path.

    Lock files are live coordination, not run state, so they are excluded.
    """
    return {
        path.relative_to(run_dir).as_posix(): path.read_bytes()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.suffix != ".lock" and "__pycache__" not in path.parts
    }


def test_a_new_plan_binds_the_skipped_issue_and_a_fresh_sub_issue(
    tmp_path: Path,
) -> None:
    fixture = _run(tmp_path, linked_issues=True)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)
    run_one = _skipped(fixture)[1]
    assert run_one.slices[STAGE].chainlink_issue_id == STAGE_ISSUE
    run_one_bytes = _run_bytes(fixture.parent_store.run_dir)

    store, state, plan, config = _admit_repair_run(
        tmp_path, state_root=fixture.parent_store.root_dir
    )

    assert state.slices["repair"].chainlink_issue_id == STAGE_ISSUE
    assert state.slices["split"].chainlink_issue_id == FRESH_ISSUE
    assert state.slices["repair"].status is SliceStatus.PENDING
    assert state.slices["split"].status is SliceStatus.PENDING

    spawn = _SpawnTransport()
    state = _dispatch_children(
        plan,
        state,
        replace(config, active=True),
        EffectClient(spawn),
        [],
        store,
    )
    assert [arguments["name"] for name, arguments in spawn.calls if name == "spawn_leaf"] == [
        "repair",
        "split",
    ]
    for name in ("repair", "split"):
        assert state.slices[name].status is SliceStatus.DISPATCH_UNCONFIRMED
        assert state.slices[name].dispatch_intent_id is not None

    # The ordinary post-merge close path closes the issue the older run kept
    # open, under the new run's own slice.
    state = _with_post_merge(
        store,
        state,
        "repair",
        status=SliceStatus.MERGED,
        issue_id=STAGE_ISSUE,
        pr_number=91,
        phase=PostMergePhase.PARENT_BRANCH_SYNCED,
    )
    transport = IssueCloseTransport(STAGE_ISSUE)
    effects = EffectClient(transport)
    pending = _advance_post_merge_boundary(
        state, "repair", 91, config, effects, [], store
    )
    confirmed = _advance_post_merge_boundary(
        pending, "repair", 91, config, effects, [], store
    )

    close_calls = [
        arguments for name, arguments in transport.calls if name == "chainlink_issue_close"
    ]
    assert [arguments["issue_id"] for arguments in close_calls] == [STAGE_ISSUE]
    receipt = confirmed.slices["repair"].post_merge
    assert receipt is not None
    assert receipt.phase is PostMergePhase.ISSUE_CLOSE_CONFIRMED
    assert confirmed.slices["split"].chainlink_issue_id == FRESH_ISSUE

    # The older run's state, journal and checkpoint bytes are untouched.
    assert _run_bytes(fixture.parent_store.run_dir) == run_one_bytes


def test_a_plan_binding_an_already_closed_issue_is_adopted(
    tmp_path: Path,
) -> None:
    """A closed issue does not refuse admission; the close path adopts it."""
    fixture = _run(tmp_path, linked_issues=True)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)
    _skipped(fixture)

    store, state, _plan, config = _admit_repair_run(
        tmp_path, state_root=tmp_path / "state"
    )
    assert state.slices["repair"].chainlink_issue_id == STAGE_ISSUE

    state = _with_post_merge(
        store,
        state,
        "repair",
        status=SliceStatus.MERGED,
        issue_id=STAGE_ISSUE,
        pr_number=92,
        phase=PostMergePhase.PARENT_BRANCH_SYNCED,
    )
    transport = IssueCloseTransport(STAGE_ISSUE, issue_status="closed")
    effects = EffectClient(transport)
    pending = _advance_post_merge_boundary(
        state, "repair", 92, config, effects, [], store
    )
    confirmed = _advance_post_merge_boundary(
        pending, "repair", 92, config, effects, [], store
    )

    assert not any(
        name == "chainlink_issue_close" for name, _ in transport.calls
    )
    receipt = confirmed.slices["repair"].post_merge
    assert receipt is not None
    assert receipt.phase is PostMergePhase.ISSUE_CLOSE_CONFIRMED
    assert receipt.evidence["issue_close_outcome"] == "already_closed"
    assert receipt.evidence["issue_close_observed_status"] == "closed"
    assert receipt.evidence["issue_close_observed_issue_id"] == str(STAGE_ISSUE)


def test_plan_validation_binds_the_same_issue_in_a_new_manifest() -> None:
    """Admission is issue-state agnostic: it only checks the closed-key schema."""
    validated = validate_plan_document(_repair_document())
    manifest = build_plan_manifest(cast(Mapping[str, object], validated["plan"]), scope_id=REPAIR_RUN)
    issues = {
        node.name: node.chainlink_issue_id
        for node in manifest.nodes
        if node.kind == "leaf"
    }
    assert issues == {"repair": STAGE_ISSUE, "split": FRESH_ISSUE}
