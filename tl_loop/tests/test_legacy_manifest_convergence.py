"""Captured-Beast convergence regression for #1060 evidence-bound legacy migration.

Reproduces, in sanitized and independently-constructed form, the exact
Chainlink #1060 case: an active legacy manifest placeholder for one root leaf
whose merge is already confirmed in the durable action journal but not yet
reflected in slice status, continued against a canonical external plan. This
is not a copy of the real captured checkpoint at
/home/goya/beast-workspace/workspace (#1060 forbids editing, restoring, or
rewriting that file) -- every value below is rebuilt to match its documented
shape through the same dataclasses and journal API production code uses.

This exercises the exact two functions run_tl_loop's legacy-manifest
continuation block calls (reconcile_legacy_manifest, RunStore.set_plan_manifest)
followed by the same reconciliation/convergence pipeline run_tl_loop drives
(_reconcile_nonterminal_slices, _apply_convergence) to drain post-merge
bookkeeping to completion -- the same pipeline-level entry points
test_startup_reconciliation.py already uses for the non-legacy case. It does
not drive the full run_tl_loop() outer loop; test_direct_leaf_scope_completion.py
covers that end-to-end, including reaching TLDone from these same fixtures.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.fsm.post_merge import PostMergePhase
from tl_loop.loop.convergence import ConvergenceTracker
from tl_loop.loop.driver import (
    EffectIntent,
    LeafTask,
    TLLoopConfig,
    WorkPlan,
    _apply_convergence,
    _reconcile_nonterminal_slices,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.state.legacy_manifest import LegacyManifestDisposition, reconcile_legacy_manifest
from tl_loop.state.plan_manifest import build_legacy_manifest, build_plan_manifest
from tl_loop.state.schema import (
    ActionKind,
    ActionPhase,
    ActionState,
    DurableReviewEvidence,
    HandoffEvidence,
    PublicationBinding,
    RepositoryIdentity,
    SliceState,
    SliceStatus,
    Verdict,
)
from tl_loop.state.store import RunStore, create
from tl_loop.tests.test_driver import IntegrationTransport

RUN_ID = "root"
SLICE_ID = "tunable-operator-body-retry"
PR_NUMBER = 43
HEAD_SHA = "090098863e9c9945506e6e25ade35e6f4a3eca4d"
MERGE_TREE_SHA = "620d6709ec4d442e65977a647bb702f9fbe4d759"
SPAWN_INTENT_ID = "spawn-intent-captured-beast"
INVOCATION_ID = "invocation-captured-beast"
BRANCH = f"main.{SLICE_ID}"
WORKTREE = "/workspace/tunable-operator-body-retry"
CHAINLINK_ISSUE_ID = 599


def _candidate_plan() -> WorkPlan:
    return WorkPlan(
        leaves=(
            LeafTask(
                name=SLICE_ID,
                task="retry the tunable operator body",
                agent_type="codex",
                boundary=("src",),
            ),
        )
    )


def _candidate_manifest():
    return build_plan_manifest(
        {
            "leaves": [
                {
                    "name": SLICE_ID,
                    "task": "retry the tunable operator body",
                    "agent_type": "codex",
                    "boundary": ["src"],
                }
            ]
        },
        scope_id=RUN_ID,
        owned_branch="main",
    )


def _legacy_manifest():
    return build_legacy_manifest({"slices": {SLICE_ID: {}}}, run_id=RUN_ID)


def _active_legacy_slice(merge_intent_id: str) -> SliceState:
    review_evidence = DurableReviewEvidence(
        review_id=7,
        pr_number=PR_NUMBER,
        head_sha=HEAD_SHA,
        reviewer_agent_id="review-pr-43-codex",
        verdict=Verdict.GO,
        submitted_at="2026-09-03T00:00:00Z",
        validated_at="2026-09-03T00:01:00Z",
    )
    return SliceState(
        id=SLICE_ID,
        status=SliceStatus.IN_REVIEW,
        paths=("src",),
        depends_on=(),
        base_ref="main",
        test_plan=(),
        agent_type="codex",
        model="test-model",
        branch=BRANCH,
        worktree=WORKTREE,
        pr_number=PR_NUMBER,
        reviewed_head=HEAD_SHA,
        attempts=1,
        verdict=Verdict.GO,
        verdict_at=review_evidence.submitted_at,
        review_evidence=review_evidence,
        dispatch_intent_id=SPAWN_INTENT_ID,
        dispatch_agent_id=f"{SLICE_ID}-agent",
        dispatch_invocation_id=INVOCATION_ID,
        publication=PublicationBinding(PR_NUMBER, HEAD_SHA, BRANCH, "main", 1, INVOCATION_ID),
        handoff=HandoffEvidence(
            PR_NUMBER, HEAD_SHA, 1, INVOCATION_ID, f"{SLICE_ID}-agent", "2026-09-03T00:00:00Z"
        ),
        action=ActionState(
            ActionKind.MERGE, ActionPhase.IN_FLIGHT, intent_id=merge_intent_id, head_sha=HEAD_SHA
        ),
        manifest_node_id=f"{RUN_ID}/worker/{SLICE_ID}",
        manifest_revision=1,
    )


def _seed_action_journal(store: RunStore) -> str:
    """Write a sanitized action-journal.json and return the confirmed merge key.

    The captured case's merge action intent_id IS the journal's own stable
    action key -- the driver dispatches merge_pr using that key -- so the
    merge intent id is derived here rather than invented.
    """
    journal = EffectJournal(RUN_ID, store.run_dir / "action-journal.json")
    spawn_intent = EffectIntent(
        "spawn_leaf",
        SLICE_ID,
        {
            "name": SLICE_ID,
            "task": "retry the tunable operator body",
            "agent_type": "codex",
            "boundary": ["src"],
            "intent_id": SPAWN_INTENT_ID,
        },
        True,
    )
    journal.append(spawn_intent)
    journal.mark_result(
        spawn_intent,
        ToolResult(
            raw={"success": True, "result": {"branch_name": BRANCH, "worktree_path": WORKTREE}},
            success=True,
            result={"branch_name": BRANCH, "worktree_path": WORKTREE},
            error=None,
        ),
    )
    merge_intent = EffectIntent(
        "merge_pr", SLICE_ID, {"pr_number": PR_NUMBER, "expected_head_sha": HEAD_SHA}, True
    )
    journal.append(merge_intent)
    journal.mark_result(
        merge_intent,
        ToolResult(
            raw={
                "success": True,
                "result": {
                    "merged": True,
                    "head_sha": HEAD_SHA,
                    "merge_tree_sha": MERGE_TREE_SHA,
                },
            },
            success=True,
            result={"merged": True},
            error=None,
        ),
    )
    return journal.key_for(merge_intent)


def _config() -> TLLoopConfig:
    return TLLoopConfig(
        active=True,
        chainlink_issue_id=CHAINLINK_ISSUE_ID,
        repository_identity=RepositoryIdentity("org", "repo", "main"),
        enable_reviewer_spawn=True,
        ledger_run_id=RUN_ID,
    )


def _merged_watcher_transport() -> IntegrationTransport:
    """Authoritative watcher snapshot for the already-merged captured PR."""
    return IntegrationTransport(
        snapshots=[
            {
                "merged": True,
                "head_sha": HEAD_SHA,
                "base_sha": "base-main",
                "patch_digest": "patch-tunable-operator-body-retry",
                "merge_tree_sha": MERGE_TREE_SHA,
                "ci_status": "success",
                "pr_state": "closed",
            }
        ]
    )


def _seed_and_migrate(tmp_path: Path):
    """Build the sanitized captured-Beast checkpoint and install the migration.

    Returns (store, migrated_state, reconciliation) with the legacy manifest
    already reconciled and replaced by the canonical candidate manifest,
    exactly mirroring run_tl_loop's continuation block (driver.py) for an
    active legacy manifest.
    """
    create(
        RUN_ID,
        {"repository_identity": {"owner": "org", "repo": "repo", "base_branch": "main"}},
        root_dir=tmp_path,
    )
    store = RunStore(RUN_ID, tmp_path)
    merge_intent_id = _seed_action_journal(store)
    legacy = _legacy_manifest()
    state = store.load()
    state = store.checkpoint(
        state.fsm,
        {SLICE_ID: _active_legacy_slice(merge_intent_id)},
        state.budgets,
        state.events.last_consumed_offset,
        plan_manifest=legacy,
    )

    candidate = _candidate_manifest()
    journal = EffectJournal(RUN_ID, store.run_dir / "action-journal.json")
    reconciliation = reconcile_legacy_manifest(
        legacy, candidate, state, journal, child_checkpoint_root=store.run_dir
    )
    assert reconciliation.disposition is LegacyManifestDisposition.PROVEN, reconciliation.reason

    proof = reconciliation.proofs[0]
    rebound = replace(
        state.slices[SLICE_ID],
        branch=proof.branch or state.slices[SLICE_ID].branch,
        worktree=proof.worktree or state.slices[SLICE_ID].worktree,
        legacy_manifest_migration=proof.to_document(),
    )
    migrated = store.set_plan_manifest(candidate, slices={SLICE_ID: rebound})
    return store, migrated, reconciliation


def _mutating_call_names(calls: list) -> list[str]:
    return [name for name, _ in calls if name != "emit_controller_event"]


def _drain_to_complete(store: RunStore, config: TLLoopConfig, effects, effects_log) -> None:
    for _ in range(10):
        state = _apply_convergence(
            store.load(), ConvergenceTracker(), store, config, effects, effects_log
        )
        post_merge = state.slices[SLICE_ID].post_merge
        if post_merge is not None and post_merge.phase is PostMergePhase.COMPLETE:
            return
    raise AssertionError("post-merge recovery did not reach COMPLETE within the bounded drain")


def test_captured_beast_legacy_migration_converges_through_post_merge_complete(
    tmp_path: Path,
) -> None:
    store, migrated, reconciliation = _seed_and_migrate(tmp_path)

    assert reconciliation.bindings == {f"{RUN_ID}/worker/{SLICE_ID}": f"{RUN_ID}/leaf/{SLICE_ID}"}
    assert migrated.plan_manifest.role == "root"
    assert migrated.plan_manifest.owned_branch == "main"
    assert migrated.slices[SLICE_ID].legacy_manifest_migration["disposition"] == "proven"

    config = _config()
    transport = _merged_watcher_transport()
    effects = EffectClient(transport)
    effects_log: list = []
    journal = EffectJournal(RUN_ID, store.run_dir / "action-journal.json")

    reconciled = _reconcile_nonterminal_slices(
        _candidate_plan(), migrated, config, effects, store, journal
    )

    # The already-confirmed merge is adopted from the durable journal --
    # never re-dispatched.
    assert [name for name, _ in transport.calls if name == "merge_pr"] == []
    assert reconciled.slices[SLICE_ID].status is SliceStatus.MERGED
    assert reconciled.slices[SLICE_ID].action is None

    _drain_to_complete(store, config, effects, effects_log)

    final = store.load()
    assert final.slices[SLICE_ID].post_merge.phase is PostMergePhase.COMPLETE
    assert [name for name, _ in transport.calls if name == "merge_pr"] == []

    chainlink_close_calls = [
        arguments for name, arguments in transport.calls if name == "chainlink_issue_close"
    ]
    assert len(chainlink_close_calls) == 1
    assert chainlink_close_calls[0]["issue_id"] == CHAINLINK_ISSUE_ID
    assert [name for name, _ in transport.calls if name == "post_merge_changelog"] == [
        "post_merge_changelog"
    ]
    assert [name for name, _ in transport.calls if name == "post_merge_push"] == ["post_merge_push"]

    gate_names = [gate.name for gate in final.gates]
    assert not any(name.startswith("plan-manifest-migration:") for name in gate_names)


def test_captured_beast_repeated_continuation_is_a_terminal_no_op(tmp_path: Path) -> None:
    store, migrated, _ = _seed_and_migrate(tmp_path)
    config = _config()
    transport = _merged_watcher_transport()
    effects = EffectClient(transport)
    effects_log: list = []
    journal = EffectJournal(RUN_ID, store.run_dir / "action-journal.json")

    # Continuation 1: adopts the confirmed merge and drains post-merge to
    # completion.
    _reconcile_nonterminal_slices(_candidate_plan(), migrated, config, effects, store, journal)
    _drain_to_complete(store, config, effects, effects_log)
    after_first = list(transport.calls)
    assert after_first  # sanity: continuation 1 actually did the work

    # Continuations 2 and 3: same inputs, already-COMPLETE state -- both must
    # be terminal no-ops.
    for _ in range(2):
        state = store.load()
        reconciled = _reconcile_nonterminal_slices(
            _candidate_plan(), state, config, effects, store, journal
        )
        assert reconciled.slices[SLICE_ID].status is SliceStatus.MERGED
        assert reconciled.slices[SLICE_ID].post_merge.phase is PostMergePhase.COMPLETE
        converged = _apply_convergence(
            store.load(), ConvergenceTracker(), store, config, effects, effects_log
        )
        assert converged.slices[SLICE_ID].post_merge.phase is PostMergePhase.COMPLETE

    # Continuations 2 and 3 may still emit informational controller telemetry
    # (e.g. a "no_active_slices" wait-reason observation), but must never
    # repeat a mutating dispatch: the mutating-operation trace stays fixed
    # after continuation 1.
    assert _mutating_call_names(transport.calls) == _mutating_call_names(after_first)
    assert [name for name, _ in transport.calls if name == "merge_pr"] == []
    assert len([n for n, _ in transport.calls if n == "chainlink_issue_close"]) == 1
    assert len([n for n, _ in transport.calls if n == "post_merge_changelog"]) == 1
    assert len([n for n, _ in transport.calls if n == "post_merge_push"]) == 1
