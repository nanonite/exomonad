"""Proof-gated continuation coverage for failed ordered sub-TLs."""

from __future__ import annotations

import json
import queue
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import TransportClient
from tl_loop.events.envelope import EventEnvelope
from tl_loop.fsm.scope import TLFailed, TLRunning
from tl_loop.loop import driver
from tl_loop.loop.driver import (
    RECONCILED_SUB_TL_DISPATCH_BOUNDARIES,
    LeafTask,
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    _bind_initial_slices,
    _candidate_manifest,
    _child_config,
    _ensure_canonical_scope,
    _hold_ordered_recovery_gate,
    _initial_slices,
    _manifest_for_plan,
    _ordered_terminal_recovery_decision,
    _release_canonical_scope,
    _reopen_ordered_scope,
    _sub_tl_worktree,
    _supervise_live_sub_tl,
    run_tl_loop,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.schema import (
    BudgetLedger,
    GateStatus,
    IntegrationRuntimeState,
    OrderedStageState,
    PublicationBinding,
    RunState,
    SliceStatus,
)
from tl_loop.state.store import RunStore, create


class EmptyQueue:
    """Minimal event source for terminal continuation tests."""

    def get(self, timeout: float | None = None) -> EventEnvelope:
        del timeout
        raise RuntimeError("terminal recovery should not consume events")

    def acknowledge(self, event: EventEnvelope) -> int:
        del event
        raise AssertionError("terminal recovery should not acknowledge events")


class _ExitedProcess:
    """Stand-in for a child controller that exited before authoritative resolution."""

    exitcode = 1

    def is_alive(self) -> bool:
        return False


def _failed_ordered_run(
    tmp_path: Path,
    *,
    child_owner_branch: str | None = None,
    child_status: SliceStatus = SliceStatus.PENDING,
    child_publication: bool = False,
) -> tuple[RunStore, WorkPlan, TLLoopConfig]:
    state_root = tmp_path / "state"
    child_plan = WorkPlan(leaves=(LeafTask("leaf", "implement the child change"),))
    plan = WorkPlan(sub_tls=(SubTLTask("child", child_plan, order=1),))
    config = TLLoopConfig(
        root_dir=state_root,
        project_root=tmp_path,
        branch="main",
        session_mode="continue",
    )
    manifest = _manifest_for_plan(plan, "parent", config)
    parent_slices = _bind_initial_slices(
        _initial_slices(plan, config, state_root, "parent"),
        manifest,
    )
    create(
        "parent",
        {"plan_manifest": manifest.to_document(), "slices": parent_slices},
        root_dir=state_root,
    )
    parent_store = RunStore("parent", state_root)
    parent_state = parent_store.load()
    child = parent_state.slices["child"]
    child_branch = child_owner_branch or "main.child"
    child_worktree = str(_sub_tl_worktree(config, state_root, "parent", plan.sub_tls[0]))
    child = replace(
        child,
        status=SliceStatus.FAILED,
        branch=child_branch,
        worktree=child_worktree,
        dispatch_intent_id="sub-tl-dispatch",
        dispatch_agent_id="child",
        dispatch_authoritative_event_seq=11,
        dispatch_last_boundary="sub_tl_started",
    )
    parent_store.checkpoint(
        TLFailed("recursive child failed"),
        {"child": child},
        parent_state.budgets,
        0,
        current_order=1,
        ordered_stages=(OrderedStageState(1, ("child",)),),
        integration=IntegrationRuntimeState(
            sub_tl_states={"child": IntegrationLifecycle.FAILED}
        ),
    )

    child_manifest = manifest.child_manifests[
        next(node.node_id for node in manifest.nodes if node.name == "child")
    ]
    child_config = replace(
        config,
        root_dir=parent_store.run_dir,
        run_id="child",
        branch="main.child",
        worktree=child_worktree,
        parent_branch="main",
        parent_run_id="parent",
        depth=1,
    )
    child_slices = _bind_initial_slices(
        _initial_slices(child_plan, child_config, parent_store.run_dir, "child"),
        child_manifest,
    )
    create(
        "child",
        {
            "plan_manifest": child_manifest.to_document(),
            "slices": child_slices,
            "owner_branch": "main.child",
            "owner_worktree": child_worktree,
            "parent_branch": "main",
            "parent_run_id": "parent",
            "session_mode": "continue",
        },
        root_dir=parent_store.run_dir,
    )
    child_store = RunStore("child", parent_store.run_dir)
    child_state = _ensure_canonical_scope(child_store.load(), child_manifest, child_store)
    child_state = _release_canonical_scope(child_state, child_store)
    if child_status is not SliceStatus.PENDING:
        leaf = replace(
            child_state.slices["leaf"],
            status=child_status,
            dispatch_intent_id="leaf-dispatch",
            dispatch_agent_id="leaf-agent",
            dispatch_authoritative_event_seq=23,
            dispatch_last_boundary="spawn_request_accepted",
        )
        if child_publication:
            leaf = replace(
                leaf,
                status=SliceStatus.IN_REVIEW,
                pr_number=41,
                publication=PublicationBinding(
                    41,
                    "child-head",
                    "main.child.leaf",
                    "main.child",
                    1,
                    "leaf-invocation",
                ),
            )
        child_state = child_store.checkpoint(
            child_state.fsm,
            {"leaf": leaf},
            child_state.budgets,
            child_state.events.last_consumed_offset,
        )
    # Exercise the live crash path: a child controller that exits before
    # authoritative resolution must persist a recursive failure checkpoint and
    # a diagnostic bound to that exact checkpoint.
    _supervise_live_sub_tl(_ExitedProcess(), child_store, config)
    return parent_store, plan, config


GATE = "tl-ordered-child-recovery-child"
#: The recorded production failure: the child controller died parsing a Chainlink
#: escalation result, which no retry can fix on its own.
UNPROVABLE_EXIT = "chainlink issue result has no positive issue ID: {'cicoIssueId': 816}"


def _answer(parent_store: RunStore, status: GateStatus) -> RunState:
    """Record one operator answer for the named ordered-recovery gate."""
    parent_store.set_gate(GATE)
    return parent_store.answer_gate(GATE, status)


def _unprovable_child_exit(parent_store: RunStore) -> None:
    """Replace the retryable child exit with the recorded nonretryable one.

    The retryable default is recovered by the checkpoint proof alone. This
    fixture is the case that proof must refuse, so it is the one where only an
    operator answer can release the recovery.
    """
    child_store = RunStore("child", parent_store.run_dir)
    state = child_store.load()
    child_store.checkpoint(
        TLFailed(UNPROVABLE_EXIT),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        integration=state.integration,
    )
    driver._record_child_exit_reason(child_store, UNPROVABLE_EXIT)


def test_approved_gate_authorizes_recovery_the_evidence_cannot_prove(tmp_path: Path) -> None:
    """An approval is a decision the checkpoint alone cannot supply.

    The recorded run's child exited on a nonretryable failure, so the proof
    refuses. Answering the gate is how the operator says to proceed anyway.
    """
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    _unprovable_child_exit(parent_store)
    _answer(parent_store, GateStatus.APPROVED)

    decision = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )

    assert decision is not None
    assert decision.recoverable is True
    assert decision.authorized is True
    assert decision.gate_status is GateStatus.APPROVED
    assert decision.task_name == "child"
    assert GATE in decision.reason
    assert UNPROVABLE_EXIT in decision.reason


@pytest.mark.parametrize("status", [GateStatus.PENDING, GateStatus.REJECTED])
def test_unresolved_or_rejected_recovery_stays_gated(
    tmp_path: Path, status: GateStatus
) -> None:
    """Only approval releases the recovery; the other answers never do."""
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    _unprovable_child_exit(parent_store)
    _answer(parent_store, status)

    decision = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )

    assert decision is not None
    assert decision.recoverable is False
    assert decision.authorized is False
    assert decision.gate_status is status


def test_rejected_gate_is_never_re_armed_to_pending(tmp_path: Path) -> None:
    """A repeat `--continue` must not re-ask a question already declined."""
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    _unprovable_child_exit(parent_store)
    _answer(parent_store, GateStatus.REJECTED)
    before = parent_store.load()

    result = run_tl_loop(
        "parent",
        plan,
        EmptyQueue(),
        ReadOnlyEffectClient(
            EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
        ),
        config=replace(config, active=False),
        root_dir=parent_store.root_dir,
        budgets=BudgetLedger(0, 0),
    )

    after = parent_store.load()
    assert after.gates[0].status is GateStatus.REJECTED
    assert result.diagnostics["recovery_gate_status"] == GateStatus.REJECTED.value
    assert isinstance(result.final_state.recursive_fsm, TLFailed)
    assert after.recursive_fsm == before.recursive_fsm


def test_approved_recovery_reopens_the_child_with_a_fresh_invocation(tmp_path: Path) -> None:
    """An approved recovery must not deliver to the pane that already exited.

    The child controller exited, so the recorded invocation is gone. Reopening
    the scope with the same identity would resolve that dead delivery target
    again, so an authorized recovery mints a fresh validated invocation while
    keeping the same branch, worktree, and controller.
    """
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    _unprovable_child_exit(parent_store)
    _answer(parent_store, GateStatus.APPROVED)
    state = parent_store.load()
    before = state.slices["child"]

    decision = _ordered_terminal_recovery_decision(state, plan, config, parent_store)

    assert decision is not None and decision.authorized is True
    reopened = _reopen_ordered_scope(
        state,
        plan,
        parent_store,
        decision.task_name,
        authorized=decision.authorized,
    )
    child = reopened.slices["child"]
    # The exited child is reopened once, never duplicated.
    assert list(reopened.slices) == ["child"]

    assert child.dispatch_invocation_id != before.dispatch_invocation_id
    assert child.dispatch_intent_id != before.dispatch_intent_id
    assert child.dispatch_generation == before.dispatch_generation + 1
    assert child.dispatch_last_boundary == "sub_tl_recovered"
    # The child itself is reopened, never duplicated, and keeps its ownership.
    assert child.branch == before.branch
    assert child.worktree == before.worktree
    assert child.dispatch_agent_id == before.dispatch_agent_id
    assert child.recovery is not None
    assert child.recovery.evidence["invocation_id"] == child.dispatch_invocation_id
    assert child.recovery.evidence["authorization_source"] == "human"
    assert reopened.recursive_fsm is not None
    assert isinstance(reopened.recursive_fsm, TLRunning)


def test_approved_recovery_reuses_its_invocation_when_repeated(tmp_path: Path) -> None:
    """A repeat approved `--continue` must not mint a second identity."""
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    _unprovable_child_exit(parent_store)
    _answer(parent_store, GateStatus.APPROVED)
    state = parent_store.load()
    decision = _ordered_terminal_recovery_decision(state, plan, config, parent_store)
    assert decision is not None
    first = _reopen_ordered_scope(
        state, plan, parent_store, decision.task_name, authorized=True
    )
    second = _reopen_ordered_scope(
        first, plan, parent_store, decision.task_name, authorized=True
    )

    assert (
        second.slices["child"].dispatch_invocation_id
        == first.slices["child"].dispatch_invocation_id
    )
    assert (
        second.slices["child"].dispatch_generation
        == first.slices["child"].dispatch_generation
    )


#: A transient child-controller failure: the pane exited without ever reaching an
#: authoritative resolution, so the checkpoint alone proves the child retryable.
RETRYABLE_EXIT = "controller exited before authoritative resolution"


def _refail_recovered_child(parent_store: RunStore, reason: str) -> None:
    """Simulate the recovered child controller exiting a second time.

    This is the ordinary second failure of the same child: the reopened slice
    fails, the child persists its own recursive failure checkpoint, and the exit
    diagnostic is rebound to that newer checkpoint.
    """
    child_store = RunStore("child", parent_store.run_dir)
    state = child_store.load()
    child_store.checkpoint(
        TLFailed(reason),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        integration=state.integration,
    )
    driver._record_child_exit_reason(child_store, reason)
    parent_state = parent_store.load()
    parent_store.checkpoint(
        TLFailed("recursive child failed"),
        {"child": replace(parent_state.slices["child"], status=SliceStatus.FAILED)},
        parent_state.budgets,
        parent_state.events.last_consumed_offset,
        current_order=1,
        ordered_stages=(OrderedStageState(1, ("child",)),),
        integration=IntegrationRuntimeState(
            sub_tl_states={"child": IntegrationLifecycle.FAILED}
        ),
    )


def _approved_reopen(parent_store: RunStore, plan: WorkPlan, config: TLLoopConfig) -> None:
    """Drive one approved recovery and persist the reopened child."""
    _unprovable_child_exit(parent_store)
    _answer(parent_store, GateStatus.APPROVED)
    state = parent_store.load()
    decision = _ordered_terminal_recovery_decision(state, plan, config, parent_store)
    assert decision is not None and decision.authorized is True
    _reopen_ordered_scope(state, plan, parent_store, decision.task_name, authorized=True)


def test_recovered_child_that_fails_again_is_still_provable(tmp_path: Path) -> None:
    """One approved recovery must not permanently foreclose the same child.

    Recovery exists so an exited pane can be relaunched. If the relaunched child
    then exits too — the same scenario this issue exists for — the next
    continuation must still be able to prove it. A recovery that leaves the
    reopened slice at a boundary the proof rejects would instead make every later
    failure of that child unrecoverable, and would report it as a dispatch
    reconciliation fault rather than the exit that actually caused it.
    """
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    _approved_reopen(parent_store, plan, config)

    reopened = parent_store.load().slices["child"]
    assert reopened.dispatch_last_boundary in RECONCILED_SUB_TL_DISPATCH_BOUNDARIES

    # A transient second failure needs no operator answer: the checkpoint proves it.
    _refail_recovered_child(parent_store, RETRYABLE_EXIT)
    transient = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )

    assert transient is not None
    assert transient.recoverable is True
    assert transient.authorized is False
    assert transient.task_name == "child"


def test_recovered_child_nonretryable_failure_stays_operator_overridable(
    tmp_path: Path,
) -> None:
    """A second unprovable exit must still be released by an operator answer.

    Only the classification of the exit reason is ever overridable. If a prior
    recovery moved the child off a reconciled dispatch boundary, an answer could
    no longer release it at all, and the run would be stuck on a diagnosis that
    names dispatch reconciliation instead of the child's exit.
    """
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    _approved_reopen(parent_store, plan, config)
    _refail_recovered_child(parent_store, UNPROVABLE_EXIT)

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    # The operator already authorized recovery of this child, so the standing
    # answer still releases a later failure without being asked again.
    assert decision.authorized is True
    assert decision.recoverable is True
    # The proof reached the exit-reason classification, which is overridable.
    # It did not stop at the dispatch check, which no answer could release.
    assert decision.operator_overridable is True
    assert "dispatch" not in decision.reason


def test_second_recovery_mints_a_new_invocation_for_the_second_exit(
    tmp_path: Path,
) -> None:
    """Each exited invocation gets its own replacement, never a reused identity."""
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    _approved_reopen(parent_store, plan, config)
    first = parent_store.load().slices["child"]

    _refail_recovered_child(parent_store, UNPROVABLE_EXIT)
    state = parent_store.load()
    decision = _ordered_terminal_recovery_decision(state, plan, config, parent_store)
    assert decision is not None and decision.authorized is True
    reopened = _reopen_ordered_scope(
        state, plan, parent_store, decision.task_name, authorized=True
    )
    second = reopened.slices["child"]

    assert second.dispatch_invocation_id != first.dispatch_invocation_id
    assert second.dispatch_generation == first.dispatch_generation + 1
    assert second.dispatch_last_boundary in RECONCILED_SUB_TL_DISPATCH_BOUNDARIES


def test_approved_recovery_keeps_publication_ownership(tmp_path: Path) -> None:
    """Recovery must not hand a child's published work to another owner."""
    parent_store, plan, config = _failed_ordered_run(
        tmp_path,
        child_status=SliceStatus.IN_REVIEW,
        child_publication=True,
    )
    _unprovable_child_exit(parent_store)
    _answer(parent_store, GateStatus.APPROVED)
    state = parent_store.load()
    child_state = RunStore("child", parent_store.run_dir).load()
    publication = child_state.slices["leaf"].publication
    assert publication is not None and publication.pr_number == 41

    decision = _ordered_terminal_recovery_decision(state, plan, config, parent_store)

    assert decision is not None and decision.authorized is True
    reopened = _reopen_ordered_scope(
        state, plan, parent_store, decision.task_name, authorized=True
    )
    child = reopened.slices["child"]

    assert child.pr_number == state.slices["child"].pr_number
    assert child.publication == state.slices["child"].publication
    recovered = RunStore("child", parent_store.run_dir).load()
    assert recovered.slices["leaf"].publication == publication
    assert recovered.slices["leaf"].dispatch_intent_id == "leaf-dispatch"


class _UnreconciledIntent(NamedTuple):
    """One effect the child recorded as intended and never confirmed."""

    operation: str
    target: str
    arguments: Mapping[str, object]


def test_approved_gate_does_not_release_an_ownership_failure(tmp_path: Path) -> None:
    """Approval cannot make an unproven branch binding provable.

    The parent slice claims a branch that is not the one derived for this child,
    so the controller cannot tell whose work the relaunch would adopt. Approving
    the gate is an operator's judgement about a question, not evidence about
    ownership, so the run stays gated.
    """
    parent_store, plan, config = _failed_ordered_run(tmp_path, child_owner_branch="main.imposter")
    _unprovable_child_exit(parent_store)
    _answer(parent_store, GateStatus.APPROVED)

    decision = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )

    assert decision is not None
    assert decision.recoverable is False
    assert decision.authorized is False
    assert "declared owner" in decision.reason
    assert decision.gate_status is GateStatus.APPROVED


def test_approved_gate_does_not_release_an_unreconciled_effect(tmp_path: Path) -> None:
    """Approval cannot re-dispatch an effect the child may already have run.

    The child recorded an intended effect it never confirmed, so relaunching it
    would risk running that effect twice. The child exited retryably here, so the
    only thing refusing the proof is that unreconciled entry — proving the gate
    is answerable and that answering it still changes nothing.
    """
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    child_store = RunStore("child", parent_store.run_dir)
    EffectJournal("child", child_store.run_dir / "action-journal.json").append(
        _UnreconciledIntent("merge_pr", "41", {"pr_number": 41})
    )
    _answer(parent_store, GateStatus.APPROVED)

    decision = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )

    assert decision is not None
    assert decision.recoverable is False
    assert decision.authorized is False
    assert "unreconciled effect" in decision.reason
    assert decision.gate_status is GateStatus.APPROVED


def test_approved_ownership_failure_dispatches_nothing(tmp_path: Path) -> None:
    """The gated run stays ``tl_failed`` and redispatches no child at all.

    This is the end-to-end shape of the safety property: an approved gate whose
    proof is unprovable for ownership reasons must produce the same
    no-delivery outcome as a rejected one, not a relaunch against a branch the
    controller never proved was this child's.
    """
    parent_store, plan, config = _failed_ordered_run(tmp_path, child_owner_branch="main.imposter")
    _unprovable_child_exit(parent_store)
    _answer(parent_store, GateStatus.APPROVED)
    before = _child_identity(parent_store)

    result = run_tl_loop(
        "parent",
        plan,
        EmptyQueue(),
        ReadOnlyEffectClient(
            EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
        ),
        config=replace(config, active=False),
        root_dir=parent_store.root_dir,
        budgets=BudgetLedger(0, 0),
    )

    assert isinstance(result.final_state.recursive_fsm, TLFailed)
    assert result.final_state.fsm.phase.value == "tl_failed"
    assert result.diagnostics["recovery_gate_status"] == GateStatus.APPROVED.value
    assert _child_identity(parent_store) == before


def _child_identity(parent_store: RunStore) -> tuple[object, ...]:
    """The durable dispatch identity a redispatch would have to change."""
    child = parent_store.load().slices["child"]
    return (
        child.status,
        child.dispatch_invocation_id,
        child.dispatch_intent_id,
        child.dispatch_generation,
        child.dispatch_last_boundary,
    )


def test_unanswered_recovery_opens_exactly_one_pending_gate(tmp_path: Path) -> None:
    """The gate is opened once and stays pending until an operator answers."""
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    _unprovable_child_exit(parent_store)

    decision = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )

    assert decision is not None
    assert decision.gate_status is None
    first = _hold_ordered_recovery_gate(parent_store, decision)
    second = _hold_ordered_recovery_gate(parent_store, decision)
    assert [gate.name for gate in first.gates] == [GATE]
    assert second.gates[0].status is GateStatus.PENDING


def test_continue_reopens_only_proven_failed_ordered_child(tmp_path: Path) -> None:
    parent_store, plan, config = _failed_ordered_run(tmp_path)

    decision = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )
    assert decision is not None
    assert decision.recoverable is True
    assert decision.task_name == "child"

    reopened = _reopen_ordered_scope(
        parent_store.load(), plan, parent_store, decision.task_name
    )

    assert isinstance(reopened.recursive_fsm, TLRunning)
    assert reopened.slices["child"].status is SliceStatus.SPAWNED
    assert reopened.slices["child"].dispatch_intent_id == "sub-tl-dispatch"
    assert reopened.integration.sub_tl_states["child"] is IntegrationLifecycle.RUNNING


def test_continue_preserves_accepted_leaf_and_later_pr_evidence(tmp_path: Path) -> None:
    parent_store, plan, config = _failed_ordered_run(
        tmp_path,
        child_status=SliceStatus.IN_REVIEW,
        child_publication=True,
    )
    child_state = RunStore("child", parent_store.run_dir).load()

    decision = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )

    assert decision is not None and decision.recoverable is True
    publication = child_state.slices["leaf"].publication
    assert publication is not None and publication.pr_number == 41
    assert child_state.slices["leaf"].dispatch_intent_id == "leaf-dispatch"


def test_conflicting_child_identity_opens_named_gate_and_keeps_failure(
    tmp_path: Path,
) -> None:
    parent_store, plan, config = _failed_ordered_run(
        tmp_path,
        child_owner_branch="main.other-child",
    )

    decision = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )

    assert decision is not None
    assert decision.recoverable is False
    assert decision.gate_name == "tl-ordered-child-recovery-child"
    gated = parent_store.set_gate(decision.gate_name)
    assert isinstance(gated.recursive_fsm, TLFailed)
    assert gated.fsm.phase.value == "tl_failed"
    assert gated.gates[0].name == decision.gate_name


def test_continue_terminal_gate_is_idempotent_and_does_not_consume_events(
    tmp_path: Path,
) -> None:
    parent_store, plan, config = _failed_ordered_run(
        tmp_path,
        child_owner_branch="main.other-child",
    )
    effects = ReadOnlyEffectClient(
        EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
    )
    config = replace(config, active=False)
    first = run_tl_loop(
        "parent",
        plan,
        EmptyQueue(),
        effects,
        config=config,
        root_dir=parent_store.root_dir,
        budgets=BudgetLedger(0, 0),
    )
    second = run_tl_loop(
        "parent",
        plan,
        EmptyQueue(),
        effects,
        config=config,
        root_dir=parent_store.root_dir,
        budgets=BudgetLedger(0, 0),
    )

    assert first.final_state.fsm.phase.value == "tl_failed"
    assert second.final_state.fsm.phase.value == "tl_failed"
    assert [gate.name for gate in second.final_state.gates] == [
        "tl-ordered-child-recovery-child"
    ]


def test_continue_recovers_from_manifest_without_reloading_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    parent_store, _plan, config = _failed_ordered_run(tmp_path)
    config = replace(config, active=False)
    effects = ReadOnlyEffectClient(
        EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
    )
    calls: list[str] = []

    def record_sub_tls(_plan, state, *_args):
        calls.append("sub_tls")
        return state

    def record_loop(*args):
        calls.append("loop")
        return driver.TLRunResult(args[6], (), (), ())

    monkeypatch.setattr(driver, "_run_sub_tls", record_sub_tls)
    monkeypatch.setattr(driver, "_run_loop", record_loop)
    result = run_tl_loop(
        "parent",
        None,
        EmptyQueue(),
        effects,
        config=config,
        root_dir=parent_store.root_dir,
        budgets=BudgetLedger(0, 0),
    )

    assert isinstance(result.final_state.recursive_fsm, TLRunning)
    assert result.final_state.slices["child"].status is SliceStatus.SPAWNED
    # A recovered terminal checkpoint must fall through and actually relanch
    # the child in the same invocation, not return and wait for another call.
    assert calls == ["sub_tls", "loop"]


def test_premature_child_exit_persists_recursive_failure(tmp_path: Path) -> None:
    parent_store, _plan, _config = _failed_ordered_run(tmp_path)

    child_state = RunStore("child", parent_store.run_dir).load()

    assert isinstance(child_state.recursive_fsm, TLFailed)
    assert "exited before authoritative resolution" in child_state.recursive_fsm.reason
    diagnostic = RunStore("child", parent_store.run_dir).exit_diagnostics()
    assert diagnostic is not None
    assert diagnostic["checkpoint_revision"] == child_state.revision


def test_new_failure_replaces_stale_exit_marker(tmp_path: Path) -> None:
    parent_store, _plan, _config = _failed_ordered_run(tmp_path)
    child_store = RunStore("child", parent_store.run_dir)
    stale = child_store.exit_diagnostics()
    assert stale is not None
    child_state = child_store.load()
    child_store.checkpoint(
        TLFailed("sub-TL controller completed an unsafe merge"),
        child_state.slices,
        child_state.budgets,
        child_state.events.last_consumed_offset,
        integration=child_state.integration,
    )

    driver._record_child_exit_reason(
        child_store, "sub-TL controller completed an unsafe merge"
    )

    refreshed = child_store.exit_diagnostics()
    assert refreshed is not None
    assert refreshed["checkpoint_revision"] != stale["checkpoint_revision"]
    assert refreshed["checkpoint_failure_reason"] == "sub-TL controller completed an unsafe merge"
    assert refreshed["reason"] == "sub-TL controller completed an unsafe merge"


def test_repeated_continuation_does_not_reopen_twice(tmp_path: Path) -> None:
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    decision = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )
    assert decision is not None and decision.recoverable is True

    reopened = _reopen_ordered_scope(
        parent_store.load(), plan, parent_store, decision.task_name
    )
    # The second continuation observes a running scope, so it can neither
    # reopen again nor mint a second dispatch intent.
    assert (
        _ordered_terminal_recovery_decision(reopened, plan, config, parent_store) is None
    )
    assert reopened.slices["child"].dispatch_intent_id == "sub-tl-dispatch"


def test_exact_ownership_conflict_is_not_retryable() -> None:
    conflict = (
        "ExoMonad server returned HTTP 409: ordered sub-TL identity or workspace "
        "conflicts with existing ownership"
    )
    assert driver._retryable_ordered_exit_reason(conflict) is False
    assert (
        driver._retryable_ordered_exit_reason(
            "ExoMonad server returned HTTP 409: ordered sub-TL branch already exists "
            "without matching durable identity"
        )
        is False
    )
    assert (
        driver._retryable_ordered_exit_reason(
            "ExoMonad server returned HTTP 503: upstream unavailable"
        )
        is True
    )


def test_unbound_stale_exit_marker_cannot_reopen_newer_failure(
    tmp_path: Path,
) -> None:
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    child_store = RunStore("child", parent_store.run_dir)
    # A diagnostic marker that is not bound to the current checkpoint (for
    # example an older transient exit) must not authorize recovery.
    child_store.exit_reason_path.write_text(
        json.dumps(
            {
                "reason": "sub-TL controller exited before authoritative resolution",
                "recorded_at": 1.0,
            }
        ),
        encoding="utf-8",
    )

    decision = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )

    assert decision is not None
    assert decision.recoverable is False
    assert "not bound to the current child checkpoint revision" in decision.reason


def test_stale_exit_reason_cannot_reopen_newer_failure(tmp_path: Path) -> None:
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    child_store = RunStore("child", parent_store.run_dir)
    child_state = child_store.load()
    # A newer, nonretryable failure checkpoint written after the old transient
    # diagnostic marker leaves that marker's bound failure reason stale.
    child_store.checkpoint(
        TLFailed("sub-TL controller completed an unsafe merge"),
        child_state.slices,
        child_state.budgets,
        child_state.events.last_consumed_offset,
        integration=child_state.integration,
    )

    decision = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )

    assert decision is not None
    assert decision.recoverable is False
    assert "durable child exit diagnostic" in decision.reason


def test_child_config_declares_parent_manifest_for_recovery(tmp_path: Path) -> None:
    """A production-spawned child persists the parent's declared child manifest.

    Regression for ordered recovery: rebuilding the child manifest from its own
    run id changes the scope id, so the child checkpoint digest no longer
    matches the parent's ``child_manifests`` entry and continuation gates every
    real child as non-recoverable.
    """
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    task = plan.sub_tls[0]
    child_worktree = str(
        _sub_tl_worktree(config, parent_store.root_dir, parent_store.run_id, task)
    )
    child_config = _child_config(
        config,
        task,
        EmptyQueue(),
        None,
        parent_store,
        "main.child",
        child_worktree,
        ordered_recovery=True,
    )

    parent_manifest = parent_store.load().plan_manifest
    node = next(node for node in parent_manifest.nodes if node.name == "child")
    declared = parent_manifest.child_manifests[node.node_id]

    assert child_config.declared_manifest is not None
    assert child_config.declared_manifest.digest == declared.digest
    candidate = _candidate_manifest(task.plan, task.name, child_config)
    assert candidate.digest == declared.digest
    # The child checkpoint seeded by the helper carries the declared manifest,
    # which is exactly what recovery verifies.
    child_state = RunStore("child", parent_store.run_dir).load()
    assert child_state.plan_manifest is not None
    assert child_state.plan_manifest.digest == declared.digest


def test_wrong_controller_identity_is_not_recoverable(tmp_path: Path) -> None:
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    state = parent_store.load()
    child = replace(state.slices["child"], dispatch_agent_id="wrong-controller")
    parent_store.checkpoint(
        state.recursive_fsm,
        {"child": child},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )

    decision = _ordered_terminal_recovery_decision(
        parent_store.load(), plan, config, parent_store
    )

    assert decision is not None
    assert decision.recoverable is False
    assert "does not match the declared controller" in decision.reason


class NoEventQueue:
    """Event source that reports an empty ledger poll."""

    def get(self, timeout: float | None = None) -> EventEnvelope:
        del timeout
        raise queue.Empty

    def acknowledge(self, event: EventEnvelope) -> int:
        del event
        raise AssertionError("an empty ledger has no event to acknowledge")


def test_recreate_starts_replacement_child_after_archiving_nonterminal_checkpoint(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / ".exo" / "tl-loop"
    root_worktree = str(tmp_path / "worktrees" / "root")
    plan = WorkPlan(sub_tls=(SubTLTask("stage-a", WorkPlan(), order=1),))
    config = TLLoopConfig(
        root_dir=state_root,
        branch="main",
        worktree=root_worktree,
        session_mode="recreate",
        active=False,
        keep_alive_on_waiting=False,
        max_events=8,
    )
    child_worktree = str(_sub_tl_worktree(config, state_root, "root", plan.sub_tls[0]))

    # A previous root ran the ordered child to a nonterminal checkpoint before
    # the confirmed recreate archived the entire root beneath root.invalid-*.
    archived_child = state_root / "root.invalid-1789772880851" / "stage-a" / "run.json"
    archived_child.parent.mkdir(parents=True)
    archived_child.write_text(
        json.dumps(
            {
                "version": 1,
                "revision": 133,
                "run_id": "stage-a",
                "owner_worktree": child_worktree,
                "fsm": {"phase": "tl_running", "waiting": []},
            }
        ),
        encoding="utf-8",
    )
    archived_bytes = archived_child.read_bytes()

    effects = ReadOnlyEffectClient(
        EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
    )
    run_tl_loop(
        "root",
        plan,
        NoEventQueue(),
        effects,
        config=config,
        root_dir=state_root,
        budgets=BudgetLedger(0, 0),
    )

    assert (state_root / "root" / "run.json").is_file()
    replacement = RunStore("stage-a", state_root / "root").load()
    assert replacement.owner_worktree == child_worktree
    assert archived_child.read_bytes() == archived_bytes
