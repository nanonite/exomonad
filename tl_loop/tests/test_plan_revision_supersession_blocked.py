"""A superseding node must run even when blocked dependents sit before it.

The live defect: stage A is skipped under the ``block`` dependency policy, so
stages B and C are held ``BLOCKED`` with ``blocked_by=A``. ``plan revise`` then
adds A2, which declares ``supersedes: A`` at a later ordered stage. Before the
fix the dry run only *reported* B and C as ``rebound_dependents``: apply left
their ``blocked_by`` pointing at the skipped node, ``current_order`` stayed on
the blocked stage, and A2 was never dispatched.

These tests pin the complete operator workflow: the dry run names the blocked
dependents, apply rebinds them to the replacement, the controller advances past
the held stage, dispatches A2, and releases B and C only after A2 merges.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import TransportClient
from tl_loop.fsm.phase import TLPhase
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.scope import TLDone as RecursiveTLDone
from tl_loop.fsm.scope import TLRunning as RecursiveTLRunning
from tl_loop.loop.driver import (
    LoopCancelled,
    TLLoopConfig,
    WorkPlan,
    _bind_initial_slices,
    _canonical_planning_from_manifest,
    _initial_slices,
    _initialize_ordered_runtime,
    _manifest_for_plan,
    _manifest_plan,
    _reopen_ordered_scope,
    run_tl_loop,
)
from tl_loop.state.plan_manifest import PlanManifest, build_plan_manifest
from tl_loop.state.plan_revision import analyze_plan_revision
from tl_loop.state.plan_revision_apply import apply_plan_revision
from tl_loop.state.plan_revision_state_update import revise_scope_checkpoint
from tl_loop.state.schema import (
    BudgetLedger,
    RepositoryIdentity,
    RunState,
    SliceState,
    SliceStatus,
)
from tl_loop.state.store import RunStore
from tl_loop.tests.checkpoint_fixtures import create
from tl_loop.tests.test_driver import SyntheticQueue

A = "stage-a"
B = "stage-b"
C = "stage-c"
D = "stage-d"
A2 = "stage-a-v2"


def _leaf(name: str) -> dict[str, object]:
    return {"name": name, "task": "implement the change", "chainlink_issue_id": None}


def _stage_plan(name: str) -> dict[str, object]:
    return {"leaves": [_leaf(f"{name}-leaf")]}


def _stage(name: str, order: int) -> dict[str, object]:
    return {
        "name": name,
        "order": order,
        "plan": {},
        "integration": {"aggregate_pr_required": False},
    }


def _baseline_document() -> dict[str, object]:
    return {
        "run_id": "root",
        "plan": {
            "sub_tls": [
                _stage(D, 1),
                _stage(A, 2),
                _stage(B, 3),
                _stage(C, 4),
            ]
        },
    }


def _repair_document(a2_order: int = 5) -> dict[str, object]:
    document = _baseline_document()
    raw_plan = cast(dict[str, object], document["plan"])
    sub_tls = cast(list[dict[str, object]], raw_plan["sub_tls"])
    sub_tls.append({**_stage(A2, a2_order), "supersedes": A})
    return document


def _write_document(state_root: Path, document: dict[str, object]) -> bytes:
    payload = json.dumps(document, sort_keys=True).encode()
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "plan.snapshot").write_bytes(payload)
    (state_root / "plan.snapshot.sha256").write_text(
        hashlib.sha256(payload).hexdigest() + "\n", encoding="ascii"
    )
    (state_root / "plan.json").write_bytes(payload)
    return payload


def _write_candidate(state_root: Path, document: dict[str, object]) -> bytes:
    payload = json.dumps(document, sort_keys=True).encode()
    (state_root / "plan.json").write_bytes(payload)
    return payload


def _plan(document: dict[str, object]) -> WorkPlan:
    raw_plan = cast(dict[str, object], document["plan"])
    return WorkPlan.from_mapping(raw_plan)


def _planning_project(tmp_path: Path, document: dict[str, object]) -> tuple[Path, RunStore]:
    state_root = tmp_path / ".exo" / "tl-loop"
    _write_document(state_root, document)
    plan = _plan(document)
    manifest = build_plan_manifest(_manifest_plan(plan), scope_id="root", owned_branch="main")
    create("root", {"plan_manifest": manifest.to_document()}, root_dir=state_root)
    store = RunStore("root", state_root)
    state = store.load()
    store.checkpoint(
        _canonical_planning_from_manifest(manifest, state.slices),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        plan_manifest=manifest,
    )
    return state_root, store


def _checkpoint_slices(
    store: RunStore,
    slices: Mapping[str, SliceState | Mapping[str, object]],
) -> None:
    state = store.load()
    store.checkpoint(
        state.fsm,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=state.plan_manifest,
        state_version=state.state_version,
    )


def _mark_skip_and_block(
    store: RunStore,
    *,
    blocked: Sequence[str] = (B, C),
    blocked_by: str = A,
) -> None:
    state = store.load()
    slices: dict[str, SliceState] = dict(state.slices)
    slices[A] = replace(
        state.slices[A],
        status=SliceStatus.SKIPPED,
        blocked_by=None,
        park_audit={
            "recovery_action": "held_launch_effect_skip",
            "needs_human": False,
            "gate_name": f"tl-held-launch-effect-{A}-abc123abc123-skip",
            "reason": "the stage record is unrecoverable",
        },
    )
    for name in blocked:
        slices[name] = replace(
            state.slices[name],
            status=SliceStatus.BLOCKED,
            blocked_by=blocked_by,
            park_audit={
                "recovery_action": "held_launch_effect_skip_dependent",
                "needs_human": False,
                "gate_name": f"tl-held-launch-effect-{A}-abc123abc123-skip",
                "reason": "the prerequisite produced no output",
            },
        )
    _checkpoint_slices(store, slices)


def _slice_document(store: RunStore, name: str) -> dict[str, object]:
    payload = json.loads((store.run_dir / "run.json").read_text(encoding="utf-8"))
    slices = cast(dict[str, dict[str, object]], payload["slices"])
    return slices[name]


def _candidate_stage(document: dict[str, object], name: str) -> dict[str, object]:
    raw_plan = cast(dict[str, object], document["plan"])
    return next(
        stage
        for stage in cast(list[dict[str, object]], raw_plan["sub_tls"])
        if stage["name"] == name
    )


# ---------------------------------------------------------------------------
# dry run + apply rebind


def test_dry_run_names_the_blocked_dependents_and_changes_no_file(
    tmp_path: Path,
) -> None:
    state_root, _store = _planning_project(tmp_path, _baseline_document())
    _store = RunStore("root", state_root)
    _mark_skip_and_block(_store)
    _write_candidate(state_root, _repair_document())
    before = {path: path.read_bytes() for path in state_root.rglob("*") if path.is_file()}

    analysis = analyze_plan_revision(tmp_path)

    assert analysis.can_apply, [blocker.to_document() for blocker in analysis.blockers]
    assert len(analysis.supersessions) == 1
    supersession = analysis.supersessions[0]
    assert supersession.superseded_name == A
    assert supersession.replacement_name == A2
    assert supersession.rebound_dependents == (B, C)
    assert supersession.blocked_dependents == (B, C)
    reported = cast(list[dict[str, object]], analysis.to_document()["supersessions"])
    assert reported[0]["blocked_dependents"] == [B, C]
    assert {path: path.read_bytes() for path in state_root.rglob("*") if path.is_file()} == before


def test_apply_rebinds_blocked_dependents_to_the_replacement_and_audits_it(
    tmp_path: Path,
) -> None:
    state_root, store = _planning_project(tmp_path, _baseline_document())
    _mark_skip_and_block(store)
    old_slice = _slice_document(store, A)
    baseline = json.loads((state_root / "plan.snapshot").read_text(encoding="utf-8"))
    _write_candidate(state_root, _repair_document())

    result = apply_plan_revision(tmp_path, "Replace the unrecoverable stage")

    assert result.applied
    state = store.load()
    for name in (B, C):
        held = state.slices[name]
        assert held.status is SliceStatus.BLOCKED
        assert held.blocked_by == A2
    # The replacement starts pending and waits for its turn.
    assert state.slices[A2].status is SliceStatus.PENDING
    # The skipped declaration keeps its identity and history.
    current = _slice_document(store, A)
    for field in (
        "id",
        "status",
        "branch",
        "worktree",
        "park_audit",
        "manifest_node_id",
        "blocked_by",
    ):
        assert current.get(field) == old_slice.get(field), field
    assert current["status"] == SliceStatus.SKIPPED.value
    # The accepted snapshot still carries A's declaration byte-for-byte.
    applied = json.loads((state_root / "plan.snapshot").read_text(encoding="utf-8"))
    assert _candidate_stage(applied, A) == _candidate_stage(baseline, A)
    assert result.audit_record is not None
    reported = cast(list[dict[str, object]], result.audit_record["supersessions"])
    assert reported[0]["blocked_dependents"] == [B, C]
    audit_lines = (state_root / "plan-revisions.jsonl").read_text("utf-8").splitlines()
    assert json.loads(audit_lines[-1])["supersessions"][0]["blocked_dependents"] == [B, C]


def test_reapplying_the_same_revision_is_idempotent(tmp_path: Path) -> None:
    state_root, store = _planning_project(tmp_path, _baseline_document())
    _mark_skip_and_block(store)
    _write_candidate(state_root, _repair_document())
    first = apply_plan_revision(tmp_path, "Replace the unrecoverable stage")
    assert first.applied

    second = apply_plan_revision(tmp_path, "Repeat the same revision")

    assert not second.applied
    state = store.load()
    assert state.slices[B].blocked_by == A2
    assert state.slices[C].blocked_by == A2
    assert len((state_root / "plan-revisions.jsonl").read_text("utf-8").splitlines()) == 1


def test_a_dependent_blocked_on_another_stage_is_not_rebound(tmp_path: Path) -> None:
    state_root, store = _planning_project(tmp_path, _baseline_document())
    _mark_skip_and_block(store, blocked=(B,), blocked_by=D)
    _write_candidate(state_root, _repair_document())

    analysis = analyze_plan_revision(tmp_path)
    assert analysis.can_apply, [blocker.to_document() for blocker in analysis.blockers]
    assert analysis.supersessions[0].blocked_dependents == ()
    result = apply_plan_revision(tmp_path, "Replace the unrecoverable stage")

    assert result.applied
    assert store.load().slices[B].blocked_by == D


# ---------------------------------------------------------------------------
# same-order ownership


def _overlap_document(*, a2_order: int, a2_boundary: str, peer: str) -> dict[str, object]:
    document = _baseline_document()
    raw_plan = cast(dict[str, object], document["plan"])
    sub_tls = cast(list[dict[str, object]], raw_plan["sub_tls"])
    for stage in sub_tls:
        if stage["name"] in {A, peer}:
            stage["plan"] = {
                "leaves": [
                    {
                        "name": f"{stage['name']}-leaf",
                        "task": "implement the change",
                        "chainlink_issue_id": None,
                        "boundary": [f"src/{stage['name']}/**"],
                    }
                ]
            }
    sub_tls.append(
        {
            **_stage(A2, a2_order),
            "supersedes": A,
            "plan": {
                "leaves": [
                    {
                        "name": f"{A2}-leaf",
                        "task": "implement the change",
                        "chainlink_issue_id": None,
                        "boundary": [a2_boundary],
                    }
                ]
            },
        }
    )
    return document


def test_a_replacement_may_share_its_superseded_nodes_order_and_paths(
    tmp_path: Path,
) -> None:
    baseline = _overlap_document(a2_order=2, a2_boundary="src/stage-a/**", peer=A)
    baseline_plan = cast(dict[str, object], baseline["plan"])
    baseline_sub_tls = cast(list[dict[str, object]], baseline_plan["sub_tls"])
    baseline_sub_tls[:] = [stage for stage in baseline_sub_tls if stage["name"] != A2]
    state_root, store = _planning_project(tmp_path, baseline)
    _mark_skip_and_block(store)
    _write_candidate(
        state_root,
        _overlap_document(a2_order=2, a2_boundary="src/stage-a/**", peer=A),
    )

    analysis = analyze_plan_revision(tmp_path)

    assert analysis.can_apply, [blocker.to_document() for blocker in analysis.blockers]
    result = apply_plan_revision(tmp_path, "Replace the skipped stage in place")
    assert result.applied
    state = store.load()
    assert state.slices[A2].status is SliceStatus.PENDING
    assert state.slices[B].blocked_by == A2
    assert state.slices[C].blocked_by == A2


def test_overlap_with_an_unrelated_same_order_peer_is_still_refused(
    tmp_path: Path,
) -> None:
    baseline = _overlap_document(a2_order=3, a2_boundary="src/stage-b/**", peer=B)
    baseline_plan = cast(dict[str, object], baseline["plan"])
    baseline_sub_tls = cast(list[dict[str, object]], baseline_plan["sub_tls"])
    baseline_sub_tls[:] = [stage for stage in baseline_sub_tls if stage["name"] != A2]
    _planning_project(tmp_path, baseline)
    state_root = tmp_path / ".exo" / "tl-loop"
    _write_candidate(
        state_root,
        _overlap_document(a2_order=3, a2_boundary="src/stage-b/**", peer=B),
    )

    with pytest.raises(ValueError, match="ownership overlaps"):
        analyze_plan_revision(tmp_path)


# ---------------------------------------------------------------------------
# controller scheduling


def _running_config(state_root: Path, tmp_path: Path) -> TLLoopConfig:
    return TLLoopConfig(
        root_dir=state_root,
        project_root=tmp_path,
        branch="main",
        ledger_run_id="ledger-run",
        session_mode="continue",
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )


def _running_project(
    tmp_path: Path,
    *,
    a2_order: int = 5,
    blocked: Sequence[str] = (B, C),
    apply_revision: bool = True,
) -> tuple[Path, RunStore, WorkPlan, TLLoopConfig]:
    state_root = tmp_path / ".exo" / "tl-loop"
    baseline = _baseline_document()
    _write_document(state_root, baseline)
    plan = _plan(baseline)
    config = _running_config(state_root, tmp_path)
    manifest = _manifest_for_plan(plan, "root", config)
    initial = _bind_initial_slices(_initial_slices(plan, config, state_root, "root"), manifest)
    create(
        "root",
        {
            "plan_manifest": manifest.to_document(),
            "slices": initial,
            "repository_identity": config.repository_identity.to_document()
            if config.repository_identity is not None
            else None,
        },
        root_dir=state_root,
    )
    store = RunStore("root", state_root)
    state = _initialize_ordered_runtime(plan, store.load(), store)
    slices: dict[str, SliceState | Mapping[str, object]] = {
        name: value for name, value in initial.items()
    }
    slices[D] = replace(
        state.slices[D],
        status=SliceStatus.MERGED,
        post_merge=PostMergeState(
            PostMergePhase.NOT_REQUIRED,
            {"child_id": D, "aggregate_pr_required": "false"},
        ),
    )
    slices[A] = replace(
        state.slices[A],
        status=SliceStatus.SKIPPED,
        blocked_by=None,
        park_audit={
            "recovery_action": "held_launch_effect_skip",
            "needs_human": False,
            "gate_name": f"tl-held-launch-effect-{A}-abc123abc123-skip",
            "reason": "the stage record is unrecoverable",
        },
    )
    for name in blocked:
        slices[name] = replace(
            state.slices[name],
            status=SliceStatus.BLOCKED,
            blocked_by=A,
            park_audit={
                "recovery_action": "held_launch_effect_skip_dependent",
                "needs_human": False,
                "gate_name": f"tl-held-launch-effect-{A}-abc123abc123-skip",
                "reason": "the prerequisite produced no output",
            },
        )
    store.checkpoint(
        state.fsm,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=manifest,
        state_version=state.state_version,
    )
    _reopen_ordered_scope(store.load(), plan, store, None)
    candidate = _repair_document(a2_order=a2_order)
    _write_candidate(state_root, candidate)
    if apply_revision:
        apply_plan_revision(tmp_path, "Replace the unrecoverable stage")
    return state_root, store, _plan(candidate), config


def _bounded_run(
    fixture: tuple[Path, RunStore, WorkPlan, TLLoopConfig], tmp_path: Path
) -> RunState:
    state_root, store, plan, config = fixture
    cancel_event = threading.Event()
    timer = threading.Timer(10.0, cancel_event.set)
    timer.start()
    try:
        run_tl_loop(
            "root",
            plan,
            SyntheticQueue([]),
            ReadOnlyEffectClient(
                EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
            ),
            config=replace(
                config,
                active=False,
                session_mode="continue",
                keep_alive_on_waiting=False,
                cancel_event=cancel_event,
                poll_interval=0.01,
            ),
            root_dir=state_root,
            budgets=BudgetLedger(0, 0),
        )
    except LoopCancelled:
        raise AssertionError(
            "run_tl_loop did not settle; the supersession left the run spinning"
        ) from None
    finally:
        timer.cancel()
    return store.load()


def _is_done(state: RunState) -> bool:
    return isinstance(state.recursive_fsm, RecursiveTLDone) or (state.fsm.phase is TLPhase.TLDone)


def test_controller_advances_to_the_replacement_and_runs_dependents_after_it(
    tmp_path: Path,
) -> None:
    fixture = _running_project(tmp_path)

    state = _bounded_run(fixture, tmp_path)

    assert state.slices[A].status is SliceStatus.SKIPPED
    assert state.slices[A2].status is SliceStatus.MERGED, "the replacement never ran"
    assert state.slices[B].status is SliceStatus.MERGED
    assert state.slices[C].status is SliceStatus.MERGED
    assert state.slices[B].blocked_by is None
    assert state.slices[C].blocked_by is None
    assert _is_done(state)


def test_a_same_order_replacement_runs_before_its_released_dependents(
    tmp_path: Path,
) -> None:
    fixture = _running_project(tmp_path, a2_order=2)

    state = _bounded_run(fixture, tmp_path)

    assert state.slices[A2].status is SliceStatus.MERGED
    assert state.slices[B].status is SliceStatus.MERGED
    assert state.slices[C].status is SliceStatus.MERGED
    assert _is_done(state)


def test_require_named_rebinds_only_the_unnamed_blocked_dependent(
    tmp_path: Path,
) -> None:
    fixture = _running_project(tmp_path, blocked=(C,))

    state = _bounded_run(fixture, tmp_path)

    assert state.slices[A2].status is SliceStatus.MERGED
    assert state.slices[B].status is SliceStatus.MERGED
    assert state.slices[C].status is SliceStatus.MERGED
    assert _is_done(state)


def test_a_failed_replacement_leaves_the_dependents_blocked(tmp_path: Path) -> None:
    state_root, store, plan, config = _running_project(tmp_path)
    state = store.load()
    slices = dict(state.slices)
    slices[A2] = replace(slices[A2], status=SliceStatus.FAILED)
    store.checkpoint(
        state.fsm,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=state.plan_manifest,
        state_version=state.state_version,
    )

    state = _bounded_run((state_root, store, plan, config), tmp_path)

    for name in (B, C):
        assert state.slices[name].status is SliceStatus.BLOCKED, name
        assert state.slices[name].blocked_by == A2, name
    assert not _is_done(state)


def test_a_skipped_replacement_leaves_the_dependents_blocked(tmp_path: Path) -> None:
    state_root, store, plan, config = _running_project(tmp_path)
    state = store.load()
    slices = dict(state.slices)
    slices[A2] = replace(
        slices[A2],
        status=SliceStatus.SKIPPED,
        park_audit={
            "recovery_action": "held_launch_effect_skip",
            "needs_human": False,
            "gate_name": f"tl-held-launch-effect-{A2}-abc123abc123-skip",
            "reason": "the replacement was also unrecoverable",
        },
    )
    store.checkpoint(
        state.fsm,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=state.plan_manifest,
        state_version=state.state_version,
    )

    state = _bounded_run((state_root, store, plan, config), tmp_path)

    for name in (B, C):
        assert state.slices[name].status is SliceStatus.BLOCKED, name
        assert state.slices[name].blocked_by == A2, name
    assert not _is_done(state)
    assert isinstance(state.recursive_fsm, RecursiveTLRunning)


def test_a_replacement_behind_current_order_without_same_order_proof_is_refused(
    tmp_path: Path,
) -> None:
    state_root, _store, _plan, _config = _running_project(
        tmp_path, a2_order=1, apply_revision=False
    )
    # The replacement is new and sits in order 1 while the skipped node it names
    # lives at order 2 and the controller already passed order 1. A fresh node
    # may not be inserted into passed work unless it replaces that same order.
    _write_candidate(state_root, _repair_document(a2_order=1))

    analysis = analyze_plan_revision(tmp_path)
    assert not analysis.can_apply
    assert any(blocker.code == "runtime_rebase_past_work" for blocker in analysis.blockers)


# ---------------------------------------------------------------------------
# recursive scope


def _nested_previous_and_candidate() -> tuple[PlanManifest, PlanManifest]:
    baseline = {
        "sub_tls": [
            {
                "name": "outer",
                "order": 1,
                "plan": {
                    "sub_tls": [
                        {"name": A, "order": 1, "plan": _stage_plan(A)},
                        {"name": B, "order": 2, "plan": _stage_plan(B)},
                    ]
                },
            }
        ]
    }
    candidate = json.loads(json.dumps(baseline))
    candidate["sub_tls"][0]["plan"]["sub_tls"].append(
        {"name": A2, "order": 3, "supersedes": A, "plan": _stage_plan(A2)}
    )
    previous_manifest = build_plan_manifest(
        _manifest_plan(WorkPlan.from_mapping(baseline)),
        scope_id="root",
        owned_branch="main",
    )
    candidate_manifest = build_plan_manifest(
        _manifest_plan(WorkPlan.from_mapping(candidate)),
        scope_id="root",
        owned_branch="main",
    )
    child_key = "root/sub_tl/outer"
    return (
        previous_manifest.child_manifests[child_key],
        candidate_manifest.child_manifests[child_key],
    )


def test_a_nested_revision_rebinds_blocked_dependents_inside_the_child_scope(
    tmp_path: Path,
) -> None:
    previous, candidate = _nested_previous_and_candidate()
    state_root = tmp_path / ".exo" / "tl-loop" / "root"
    create("outer", {"plan_manifest": previous.to_document()}, root_dir=state_root)
    store = RunStore("outer", state_root)
    state = store.load()
    slices = dict(state.slices)
    slices[A] = replace(
        state.slices[A],
        status=SliceStatus.SKIPPED,
        blocked_by=None,
        park_audit={
            "recovery_action": "held_launch_effect_skip",
            "needs_human": False,
            "gate_name": f"tl-held-launch-effect-{A}-abc123abc123-skip",
            "reason": "the stage record is unrecoverable",
        },
    )
    slices[B] = replace(
        state.slices[B],
        status=SliceStatus.BLOCKED,
        blocked_by=A,
        park_audit={"recovery_action": "held_launch_effect_skip_dependent"},
    )
    store.checkpoint(
        _canonical_planning_from_manifest(previous, slices),
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        plan_manifest=previous,
    )
    raw = (store.run_dir / "run.json").read_bytes()

    updated = revise_scope_checkpoint(raw, previous, candidate, store.run_dir / "run.json")

    document = json.loads(updated.decode("utf-8"))
    assert document["slices"][B]["status"] == SliceStatus.BLOCKED.value
    assert document["slices"][B]["blocked_by"] == A2
    assert document["slices"][A]["status"] == SliceStatus.SKIPPED.value


def test_run_sub_tls_releases_dependents_only_after_the_replacement_merges(
    tmp_path: Path,
) -> None:
    state_root, store, plan, config = _running_project(tmp_path)
    # Before running, the blocked dependents still point at the pending A2.
    assert store.load().slices[B].blocked_by == A2

    # The first controller pass dispatches A2 and releases B and C.
    state = _bounded_run((state_root, store, plan, config), tmp_path)

    assert state.slices[B].status is SliceStatus.MERGED
    assert state.slices[C].status is SliceStatus.MERGED
