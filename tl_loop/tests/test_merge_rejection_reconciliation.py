"""Terminal rejected merge results converge into their integration projection.

Regression for #1223 under parent #1222. Before this fix the controller only
reconciled intended/unknown journal entries, so a rejected ``merge_pr`` entry
left the slice's MERGE action stuck ``IN_FLIGHT`` and its aggregate candidate at
``MERGING`` forever -- waiting for a merge event that a pre-dispatch refusal
never produces.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tl_loop.client.effects import ToolResult
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.driver import (
    EffectIntent,
    TLLoopConfig,
    _reconcile_nonterminal_slices,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.schema import (
    ActionKind,
    ActionPhase,
    ActionState,
    FSMState,
    IntegrationCandidateState,
    IntegrationRuntimeState,
    SliceState,
    SliceStatus,
)
from tl_loop.tests.test_startup_reconciliation import FakeClient, _load_state, _PLAN

_REJECTION = "No Forgejo reviewer response yet on PR #99."


def _candidate() -> IntegrationCandidateState:
    return IntegrationCandidateState(
        lifecycle=IntegrationLifecycle.MERGING,
        aggregate_pr_number=99,
        aggregate_head_sha="head-a",
        aggregate_patch_digest="patch-a",
        aggregate_original_base_sha="main",
        integration_owner_id="agent-a",
        integration_owner_run_id="slice-a",
        integration_owner_branch="task/slice-a",
        integration_owner_worktree=".worktrees/slice-a",
        head_sha="head-a",
        patch_digest="patch-a",
        validated_base_sha="base-a",
        merge_tree_sha="tree-a",
        ci_status="success",
    )


def _merging_with_rejected_action(
    store, state, tmp_path: Path
) -> tuple[EffectJournal, SliceState]:
    journal = EffectJournal("run-1", tmp_path / "action-journal.json")
    intent = EffectIntent("merge_pr", "slice-a", {"pr_number": 99}, True)
    journal.append(intent)
    journal.mark_result(
        intent,
        ToolResult(
            raw={"success": False, "error": _REJECTION},
            success=False,
            result={"success": False, "error": _REJECTION},
            error=_REJECTION,
        ),
    )
    key = journal.key_for(intent)
    action = ActionState(
        ActionKind.MERGE,
        ActionPhase.IN_FLIGHT,
        intent_id=key,
        head_sha="head-a",
        attempt=1,
    )
    current = replace(
        state.slices["slice-a"],
        status=SliceStatus.IN_REVIEW,
        pr_number=99,
        dispatch_agent_id="agent-a",
        dispatch_invocation_id="invocation-a",
        reviewed_head="head-a",
        action=action,
    )
    state = store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("slice-a",)),
        {"slice-a": current},
        state.budgets,
        state.events.last_consumed_offset,
        integration=IntegrationRuntimeState(
            lifecycle=IntegrationLifecycle.MERGING,
            sub_tl_states={"slice-a": IntegrationLifecycle.MERGING},
            aggregate_pr_number=99,
            aggregate_head_sha="head-a",
            head_sha="head-a",
            patch_digest="patch-a",
            validated_base_sha="base-a",
            merge_tree_sha="tree-a",
            ci_status="success",
            candidates={"slice-a": _candidate()},
        ),
    )
    return journal, state.slices["slice-a"]


def test_rejected_merge_retires_stale_in_flight_and_derives_eligible_integration(
    tmp_path: Path,
) -> None:
    store, state = _load_state(tmp_path)
    journal, current = _merging_with_rejected_action(store, state, tmp_path)
    key = current.action.intent_id
    client = FakeClient(merged=False, pr_state="open")
    config = TLLoopConfig(active=True, ledger_run_id="run-1")

    recovered = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, journal)

    slice_state = recovered.slices["slice-a"]
    assert slice_state.status is SliceStatus.IN_REVIEW
    assert slice_state.action is None
    candidate = recovered.integration.candidates["slice-a"]
    assert candidate.lifecycle is IntegrationLifecycle.READY_FOR_INTEGRATION
    assert (
        recovered.integration.sub_tl_states["slice-a"] is IntegrationLifecycle.READY_FOR_INTEGRATION
    )
    # History is preserved byte-for-byte: the rejected result is untouched.
    assert journal.entry_by_key(key)["status"] == "rejected"
    # A fresh approved attempt is not the retired one.
    assert candidate.merge_attempts == 1
    # No operator gate is opened for an already-proven rejection.
    assert not any(gate.name.startswith("tl-merge-retirement-") for gate in recovered.gates)


def test_rejected_merge_with_unavailable_watcher_holds_without_retiring(tmp_path: Path) -> None:
    store, state = _load_state(tmp_path)
    journal, current = _merging_with_rejected_action(store, state, tmp_path)
    key = current.action.intent_id
    client = FakeClient(resolved_pr_number=None)
    config = TLLoopConfig(active=True)

    recovered = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, journal)

    slice_state = recovered.slices["slice-a"]
    assert slice_state.action is not None
    assert slice_state.action.intent_id == key
    candidate = recovered.integration.candidates["slice-a"]
    assert candidate.lifecycle is IntegrationLifecycle.MERGING
    assert journal.entry_by_key(key)["status"] == "rejected"


def test_rejected_merge_with_conflicting_merged_head_holds_with_gate(tmp_path: Path) -> None:
    store, state = _load_state(tmp_path)
    journal, current = _merging_with_rejected_action(store, state, tmp_path)
    key = current.action.intent_id

    class ConflictingClient(FakeClient):
        def watcher_pr_state(self, *, pr_number: int) -> ToolResult:
            payload = super().watcher_pr_state(pr_number=pr_number)
            if payload.result is not None:
                payload.result["merged"] = False  # type: ignore[index]
                payload.result["head_sha"] = "other-head"  # type: ignore[index]
            return payload

    client = ConflictingClient(merged=False, pr_state="open", resolved_pr_number=99)
    config = TLLoopConfig(active=True, ledger_run_id="run-1")

    recovered = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, journal)

    assert any(gate.name.startswith("tl-merge-retirement-") for gate in recovered.gates)
    assert journal.entry_by_key(key)["status"] == "rejected"


def test_rejected_merge_retirement_is_idempotent_across_restarts(tmp_path: Path) -> None:
    """Retiring the same rejected attempt twice converges without duplication."""
    store, state = _load_state(tmp_path)
    journal, current = _merging_with_rejected_action(store, state, tmp_path)
    key = current.action.intent_id
    client = FakeClient(merged=False, pr_state="open")
    config = TLLoopConfig(active=True, ledger_run_id="run-1")

    first = _reconcile_nonterminal_slices(_PLAN, state, config, client, store, journal)
    second = _reconcile_nonterminal_slices(_PLAN, first, config, client, store, journal)

    slice_state = second.slices["slice-a"]
    assert slice_state.action is None
    candidate = second.integration.candidates["slice-a"]
    assert candidate.lifecycle is IntegrationLifecycle.READY_FOR_INTEGRATION
    # The counter advanced exactly once, not once per restart.
    assert candidate.merge_attempts == 1
    # The original rejected result is preserved byte-for-byte.
    assert journal.entry_by_key(key)["status"] == "rejected"
