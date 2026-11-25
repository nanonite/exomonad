"""``plan revise`` supersedes a dispatched node instead of deleting it.

The live shape cannot be repaired by editing the plan: a stage whose child
record is unrecoverable must not be deleted, rewritten, or relaunched, and its
dependents must not silently lose their prerequisite. The supported change adds
a new future node that stands in for the old one, keeps the old declaration
byte-for-byte, reports the rebound dependents in the dry run, and writes an
audit entry on apply.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from tl_loop.loop.driver import WorkPlan, _canonical_planning_from_manifest, _manifest_plan
from tl_loop.state.plan_manifest import build_plan_manifest
from tl_loop.state.plan_revision import analyze_plan_revision
from tl_loop.state.plan_revision_apply import apply_plan_revision
from tl_loop.state.plan_revision_types import PlanRevisionError
from tl_loop.state.schema import SliceStatus
from tl_loop.state.store import RunStore
from tl_loop.tests.checkpoint_fixtures import create

FIRST = "first-stage"
BROKEN = "held-stage"
DEPENDENT = "next-stage"
REPAIR = "held-stage-repair"


def _leaf(name: str, issue_id: int | None) -> dict[str, object]:
    return {"name": name, "task": "implement the change", "chainlink_issue_id": issue_id}


def _document(
    *,
    repair: bool = False,
    repair_issue: int | None = None,
    broken_issue: int | None = None,
    repair_name: str = REPAIR,
    broken: str = BROKEN,
) -> dict[str, object]:
    sub_tls: list[dict[str, object]] = [
        {"name": FIRST, "order": 1, "plan": {"leaves": [_leaf("first-leaf", None)]}},
        {
            "name": broken,
            "order": 2,
            "chainlink_issue_id": broken_issue,
            "plan": {"leaves": [_leaf("held-leaf", None)]},
        },
        {"name": DEPENDENT, "order": 3, "plan": {"leaves": [_leaf("next-leaf", None)]}},
    ]
    if repair:
        sub_tls.append(
            {
                "name": repair_name,
                "order": 4,
                "supersedes": broken,
                "chainlink_issue_id": repair_issue,
                "plan": {"leaves": [_leaf("held-leaf-repair", None)]},
            }
        )
    return {"run_id": "root", "plan": {"sub_tls": sub_tls}}


def _project(tmp_path: Path, document: dict[str, object]) -> tuple[Path, RunStore]:
    state_root = tmp_path / ".exo" / "tl-loop"
    state_root.mkdir(parents=True)
    snapshot = json.dumps(document, sort_keys=True).encode()
    (state_root / "plan.snapshot").write_bytes(snapshot)
    (state_root / "plan.snapshot.sha256").write_text(
        hashlib.sha256(snapshot).hexdigest() + "\n", encoding="ascii"
    )
    (state_root / "plan.json").write_bytes(snapshot)
    raw_plan = document["plan"]
    assert isinstance(raw_plan, dict)
    plan = WorkPlan.from_mapping(raw_plan)
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
    _mark_broken_stage_skipped(store)
    return state_root, store


def _mark_broken_stage_skipped(store: RunStore) -> None:
    """Record the operator skip this revision is allowed to stand in for."""
    state = store.load()
    slices = dict(state.slices)
    slices[BROKEN] = replace(
        slices[BROKEN],
        status=SliceStatus.SKIPPED,
        branch=f"main.{BROKEN}",
        worktree=f".exo/worktrees/{BROKEN}",
        park_audit={
            "recovery_action": "held_launch_effect_skip",
            "needs_human": False,
            "gate_name": f"tl-held-launch-effect-{BROKEN}-abc123abc123-skip",
            "reason": "the held-stage record is unrecoverable",
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


def _write_candidate(state_root: Path, document: dict[str, object]) -> bytes:
    candidate = json.dumps(document, sort_keys=True).encode()
    (state_root / "plan.json").write_bytes(candidate)
    return candidate


def test_g_supersede_dry_run_reports_the_replacement_and_rebound_dependents(
    tmp_path: Path,
) -> None:
    state_root, _store = _project(tmp_path, _document())
    _write_candidate(state_root, _document(repair=True))
    before = {
        path: path.read_bytes() for path in state_root.rglob("*") if path.is_file()
    }

    analysis = analyze_plan_revision(tmp_path)

    assert analysis.can_apply, [blocker.to_document() for blocker in analysis.blockers]
    assert len(analysis.supersessions) == 1
    supersession = analysis.supersessions[0]
    assert supersession.scope_id == "root"
    assert supersession.superseded_name == BROKEN
    assert supersession.replacement_name == REPAIR
    assert supersession.rebound_dependents == (DEPENDENT,)
    document = analysis.to_document()
    reported = cast(list[dict[str, object]], document["supersessions"])
    assert reported[0]["replacement_name"] == REPAIR
    assert reported[0]["rebound_dependents"] == [DEPENDENT]
    assert (
        supersession.superseded_node_id
        == f"root/sub_tl/{BROKEN}"
    )
    assert (
        supersession.replacement_node_id
        == f"root/sub_tl/{REPAIR}"
    )
    assert {
        path: path.read_bytes() for path in state_root.rglob("*") if path.is_file()
    } == before


def test_g_apply_adds_the_future_node_and_writes_the_audit(tmp_path: Path) -> None:
    state_root, store = _project(tmp_path, _document())
    before_manifest = store.load().plan_manifest
    assert before_manifest is not None
    _write_candidate(state_root, _document(repair=True))

    result = apply_plan_revision(tmp_path, "Replace the unrecoverable held stage")

    assert result.applied
    assert result.audit_record is not None
    supersessions = result.audit_record["supersessions"]
    assert isinstance(supersessions, list)
    assert supersessions[0]["superseded_name"] == BROKEN
    assert supersessions[0]["replacement_name"] == REPAIR
    state = store.load()
    manifest = state.plan_manifest
    assert manifest is not None
    # The replaced node keeps its identity, branch and dependency history.
    old_previous = next(node for node in before_manifest.nodes if node.name == BROKEN)
    old_now = next(node for node in manifest.nodes if node.name == BROKEN)
    assert old_now.identity() == old_previous.identity()
    assert manifest.superseded_nodes == {
        old_now.node_id: f"root/sub_tl/{REPAIR}"
    }
    # The replacement is a fresh declaration with its own branch and worktree.
    replacement = next(node for node in manifest.nodes if node.name == REPAIR)
    assert replacement.owned_branch != old_now.owned_branch
    assert replacement.order == 4
    pending = state.slices[REPAIR]
    assert pending.status is SliceStatus.PENDING
    assert pending.branch != state.slices[BROKEN].branch
    assert pending.worktree != state.slices[BROKEN].worktree
    # The skipped stage is untouched.
    assert state.slices[BROKEN].status is SliceStatus.SKIPPED
    audit_lines = (state_root / "plan-revisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(audit_lines[-1])["supersessions"][0]["replacement_name"] == REPAIR


def test_g_deleting_a_dispatched_node_is_still_refused(tmp_path: Path) -> None:
    state_root, _store = _project(tmp_path, _document())
    document = _document()
    raw_plan = document["plan"]
    assert isinstance(raw_plan, dict)
    raw_plan["sub_tls"] = [
        {**stage, "order": index + 1}
        for index, stage in enumerate(
            stage for stage in raw_plan["sub_tls"] if stage["name"] != BROKEN
        )
    ]
    _write_candidate(state_root, document)

    analysis = analyze_plan_revision(tmp_path)

    assert not analysis.can_apply
    assert any(blocker.code == "node_removal" for blocker in analysis.blockers)


def test_g_rewriting_a_dispatched_node_is_still_refused(tmp_path: Path) -> None:
    state_root, _store = _project(tmp_path, _document())
    document = _document(repair=True)
    raw_plan = document["plan"]
    assert isinstance(raw_plan, dict)
    for stage in raw_plan["sub_tls"]:
        if stage["name"] == BROKEN:
            stage["plan"] = {"leaves": [_leaf("different work", None)]}
    _write_candidate(state_root, document)

    analysis = analyze_plan_revision(tmp_path)

    assert not analysis.can_apply
    assert any(
        blocker.code == "protected_node_mutation" for blocker in analysis.blockers
    )


def test_g_superseding_a_still_pending_node_is_refused(tmp_path: Path) -> None:
    state_root, store = _project(tmp_path, _document())
    state = store.load()
    slices = dict(state.slices)
    slices[BROKEN] = replace(slices[BROKEN], status=SliceStatus.PENDING, park_audit=None)
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
    _write_candidate(state_root, _document(repair=True))

    analysis = analyze_plan_revision(tmp_path)

    assert not analysis.can_apply
    assert any(
        blocker.code == "superseded_node_still_pending" for blocker in analysis.blockers
    )


def test_g_reusing_the_replaced_issue_binding_is_refused(tmp_path: Path) -> None:
    state_root, _store = _project(tmp_path, _document(broken_issue=814))
    _write_candidate(
        state_root, _document(repair=True, broken_issue=814, repair_issue=814)
    )

    with pytest.raises(PlanRevisionError, match="chainlink_issue_id 814 is bound to both"):
        analyze_plan_revision(tmp_path)


def test_g_a_replacement_cannot_reuse_the_replaced_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot supersede itself"):
        WorkPlan.from_mapping(
            {
                "sub_tls": [
                    {
                        "name": BROKEN,
                        "order": 1,
                        "supersedes": BROKEN,
                        "plan": {"leaves": []},
                    }
                ]
            }
        )


def test_g_supersession_is_never_rewritten_by_a_later_revision(
    tmp_path: Path,
) -> None:
    state_root, store = _project(tmp_path, _document())
    _write_candidate(state_root, _document(repair=True))
    apply_plan_revision(tmp_path, "Replace the unrecoverable held stage")
    manifest = store.load().plan_manifest
    assert manifest is not None and manifest.superseded_nodes

    # A candidate that adds the replacement again without the supersession must
    # not be able to drop the recorded pair.
    _write_candidate(state_root, _document(repair=False))
    analysis = analyze_plan_revision(tmp_path)

    assert not analysis.can_apply


def test_g_a_revision_without_a_supersession_still_works(tmp_path: Path) -> None:
    state_root, _store = _project(tmp_path, _document())
    analysis = analyze_plan_revision(tmp_path)
    assert analysis.can_apply
    assert analysis.supersessions == ()
    del state_root


def test_g_apply_refuses_an_unprovable_supersession(tmp_path: Path) -> None:
    """The apply path reuses the same blockers, so it never writes a bad pair."""
    state_root, store = _project(tmp_path, _document())
    state = store.load()
    slices = dict(state.slices)
    slices[BROKEN] = replace(slices[BROKEN], status=SliceStatus.PENDING, park_audit=None)
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
    _write_candidate(state_root, _document(repair=True))
    before = (state_root / "plan.snapshot").read_bytes()

    with pytest.raises(PlanRevisionError, match="superseded_node_still_pending"):
        apply_plan_revision(tmp_path, "Replace the unrecoverable held stage")

    assert (state_root / "plan.snapshot").read_bytes() == before
