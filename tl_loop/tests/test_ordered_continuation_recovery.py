"""Proof-gated continuation coverage for failed ordered sub-TLs."""

from __future__ import annotations

import json
import queue
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple, cast

import pytest

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import TransportClient
from tl_loop.events.envelope import EventEnvelope
from tl_loop.events.reader import LedgerSnapshot, SequenceStatus
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.scope import TLFailed, TLRunning
from tl_loop.loop import driver
from tl_loop.loop.driver import (
    RECONCILED_SUB_TL_DISPATCH_BOUNDARIES,
    EffectIntent,
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
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty
from tl_loop.select.policy import validate_policy
from tl_loop.state.schema import (
    BudgetLedger,
    GateStatus,
    IntegrationRuntimeState,
    OrderedStageState,
    PublicationBinding,
    RepositoryIdentity,
    RunState,
    SliceStatus,
)
from tl_loop.state.store import RunStore
from tl_loop.tests.checkpoint_fixtures import create


class EmptyQueue:
    """Minimal event source for terminal continuation tests."""

    def get(self, timeout: float | None = None) -> EventEnvelope:
        del timeout
        raise RuntimeError("terminal recovery should not consume events")

    def acknowledge(self, event: EventEnvelope) -> int:
        del event
        raise AssertionError("terminal recovery should not acknowledge events")

    def capture_snapshot(self, cursor: int) -> LedgerSnapshot:
        return LedgerSnapshot((), cursor, SequenceStatus.UNKNOWN, (), None)


class _RecoveryTestTransport:
    """Transport that must remain unused by the offline failure fixture."""

    def call_tool(self, role: str, name: str, tool_name: str, arguments: dict[str, object]):
        del role, name, tool_name, arguments
        raise AssertionError("the offline failure fixture must not call external tools")


class _RetryDispatchTransport:
    """Accept the one authorized retry and expose no unrelated runtime owner."""

    def __init__(self) -> None:
        self.spawn_calls: list[dict[str, object]] = []

    def call_tool(self, role: str, name: str, tool_name: str, arguments: dict[str, object]):
        del role, name
        if tool_name == "spawn_leaf":
            self.spawn_calls.append(arguments)
            return {
                "success": True,
                "result": {"agent_id": "leaf-agent", "invocation_id": "leaf-invocation"},
            }
        if tool_name == "list_agents":
            return {"success": True, "result": {"agents": []}}
        if tool_name == "repository_identity":
            return {
                "success": True,
                "result": {"remote_url": "https://forge.example.com/org/repo.git"},
            }
        return {"success": True, "result": {}}


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
    root_dir: Path | None = None,
    child_plan: WorkPlan | None = None,
) -> tuple[RunStore, WorkPlan, TLLoopConfig]:
    state_root = root_dir or tmp_path / "state"
    child_plan = child_plan or WorkPlan(leaves=(LeafTask("leaf", "implement the child change"),))
    plan = WorkPlan(sub_tls=(SubTLTask("child", child_plan, order=1),))
    config = TLLoopConfig(
        root_dir=state_root,
        project_root=tmp_path,
        branch="main",
        session_mode="continue",
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )
    manifest = _manifest_for_plan(plan, "parent", config)
    parent_slices = _bind_initial_slices(
        _initial_slices(plan, config, state_root, "parent"),
        manifest,
    )
    create(
        "parent",
        {
            "plan_manifest": manifest.to_document(),
            "slices": parent_slices,
            "repository_identity": config.repository_identity.to_document(),
        },
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
        integration=IntegrationRuntimeState(sub_tl_states={"child": IntegrationLifecycle.FAILED}),
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
            "repository_identity": config.repository_identity.to_document(),
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

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.recoverable is True
    assert decision.authorized is True
    assert decision.gate_status is GateStatus.APPROVED
    assert decision.task_name == "child"
    assert GATE in decision.reason
    assert UNPROVABLE_EXIT in decision.reason


@pytest.mark.parametrize("status", [GateStatus.PENDING, GateStatus.REJECTED])
def test_unresolved_or_rejected_recovery_stays_gated(tmp_path: Path, status: GateStatus) -> None:
    """Only approval releases the recovery; the other answers never do."""
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    _unprovable_child_exit(parent_store)
    _answer(parent_store, status)

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

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
        ReadOnlyEffectClient(EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))),
        config=replace(config, active=False),
        root_dir=parent_store.root_dir,
        budgets=BudgetLedger(0, 0),
    )

    after = parent_store.load()
    assert after.gates[0].status is GateStatus.REJECTED
    assert result.diagnostics["recovery_gate_status"] == GateStatus.REJECTED.value
    assert isinstance(result.final_state.recursive_fsm, TLFailed)
    assert after.recursive_fsm == result.final_state.recursive_fsm
    assert after.recursive_fsm.reason != before.recursive_fsm.reason


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
    fsm_child = reopened.recursive_fsm.pending_by_order[1][0]
    assert fsm_child.dispatch_intent_id == child.dispatch_intent_id
    assert fsm_child.invocation_id == child.dispatch_invocation_id


def test_approved_recovery_reuses_its_invocation_when_repeated(tmp_path: Path) -> None:
    """A repeat approved `--continue` must not mint a second identity."""
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    _unprovable_child_exit(parent_store)
    _answer(parent_store, GateStatus.APPROVED)
    state = parent_store.load()
    decision = _ordered_terminal_recovery_decision(state, plan, config, parent_store)
    assert decision is not None
    first = _reopen_ordered_scope(state, plan, parent_store, decision.task_name, authorized=True)
    second = _reopen_ordered_scope(first, plan, parent_store, decision.task_name, authorized=True)

    assert (
        second.slices["child"].dispatch_invocation_id
        == first.slices["child"].dispatch_invocation_id
    )
    assert second.slices["child"].dispatch_generation == first.slices["child"].dispatch_generation
    assert second.recursive_fsm is not None
    assert isinstance(second.recursive_fsm, TLRunning)
    assert (
        second.recursive_fsm.pending_by_order[1][0].invocation_id
        == second.slices["child"].dispatch_invocation_id
    )


def test_server_invocation_is_checkpointed_before_recovered_child_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    _unprovable_child_exit(parent_store)
    _answer(parent_store, GateStatus.APPROVED)
    state = parent_store.load()
    decision = _ordered_terminal_recovery_decision(state, plan, config, parent_store)
    assert decision is not None
    reopened = _reopen_ordered_scope(
        state,
        plan,
        parent_store,
        decision.task_name,
        authorized=True,
    )
    intent_invocation_id = reopened.slices["child"].dispatch_invocation_id
    assert intent_invocation_id is not None
    provisioned_invocation_id = "server-confirmed-invocation"
    child_state_at_start: list[RunState] = []

    def provision(self: TransportClient, parent_name: str, **kwargs: object) -> str:
        del self, parent_name, kwargs
        return provisioned_invocation_id

    def run_child(root_spec: object, child_config: TLLoopConfig, budgets: object) -> None:
        del root_spec, child_config, budgets
        child_state_at_start.append(parent_store.load())

    monkeypatch.setattr(TransportClient, "provision_ordered_sub_tl", provision)
    monkeypatch.setattr("tl_loop.loop.driver.tl_run", run_child)
    task = replace(plan.sub_tls[0], source=EmptyQueue())
    driver._run_live_sub_tl(
        task,
        replace(config, ordered_recovery_child="child"),
        EmptyQueue(),
        EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"), name="parent"),
        parent_store,
        BudgetLedger(0, 0),
    )

    assert len(child_state_at_start) == 1
    started = child_state_at_start[0]
    child = started.slices["child"]
    assert child.dispatch_invocation_id == provisioned_invocation_id
    assert child.recovery is not None
    assert child.recovery.evidence["invocation_id"] == provisioned_invocation_id
    assert started.recursive_fsm is not None
    assert isinstance(started.recursive_fsm, TLRunning)
    fsm_child = started.recursive_fsm.pending_by_order[1][0]
    assert fsm_child.dispatch_intent_id == child.dispatch_intent_id
    assert fsm_child.invocation_id == provisioned_invocation_id
    assert started.recursive_fsm.dispatch_intents["child"] == child.dispatch_intent_id
    assert child.branch == reopened.slices["child"].branch
    assert child.worktree == reopened.slices["child"].worktree
    revision = started.revision
    driver._checkpoint_ordered_sub_tl_invocation(
        parent_store,
        "child",
        provisioned_invocation_id,
    )
    assert parent_store.load().revision == revision

    def publication(invocation_id: str) -> EventEnvelope:
        return driver.project(
            {
                "type": "pr.filed",
                "run_seq": 1,
                "run_id": "parent",
                "agent_id": child.dispatch_agent_id,
                "invocation_id": invocation_id,
                "generation": child.dispatch_generation,
                "lifecycle_state": "running",
                "observed_at": "2026-10-12T00:00:00Z",
                "data": {
                    "slice_id": "child",
                    "pr_number": 45,
                    "head_sha": "head-45",
                },
            }
        )

    stale_reason = driver._publication_generation_rejection(
        child,
        publication(intent_invocation_id),
        "child",
        started.controller_epoch,
    )
    assert stale_reason is not None
    assert (
        driver._publication_generation_rejection(
            child,
            publication(provisioned_invocation_id),
            "child",
            started.controller_epoch,
        )
        is None
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
        integration=IntegrationRuntimeState(sub_tl_states={"child": IntegrationLifecycle.FAILED}),
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
    transient = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

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
    reopened = _reopen_ordered_scope(state, plan, parent_store, decision.task_name, authorized=True)
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
    reopened = _reopen_ordered_scope(state, plan, parent_store, decision.task_name, authorized=True)
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

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

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

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

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
        ReadOnlyEffectClient(EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))),
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

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.gate_status is None
    first = _hold_ordered_recovery_gate(parent_store, decision)
    second = _hold_ordered_recovery_gate(parent_store, decision)
    assert [gate.name for gate in first.gates] == [GATE]
    assert second.gates[0].status is GateStatus.PENDING


def test_continue_reopens_only_proven_failed_ordered_child(tmp_path: Path) -> None:
    parent_store, plan, config = _failed_ordered_run(tmp_path)

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)
    assert decision is not None
    assert decision.recoverable is True
    assert decision.task_name == "child"

    reopened = _reopen_ordered_scope(parent_store.load(), plan, parent_store, decision.task_name)

    assert isinstance(reopened.recursive_fsm, TLRunning)
    assert reopened.slices["child"].status is SliceStatus.SPAWNED
    assert reopened.slices["child"].dispatch_intent_id == "sub-tl-dispatch"
    assert reopened.integration.sub_tl_states["child"] is IntegrationLifecycle.RUNNING


def test_recovery_keeps_merged_leaf_pending_until_post_merge_completes(
    tmp_path: Path,
) -> None:
    parent_store, parent_plan, _ = _failed_ordered_run(
        tmp_path,
        child_status=SliceStatus.MERGED,
    )
    child_store = RunStore("child", parent_store.run_dir)
    child_state = child_store.load()
    leaf = replace(
        child_state.slices["leaf"],
        status=SliceStatus.MERGED,
        pr_number=41,
        post_merge=PostMergeState(
            PostMergePhase.PARENT_BRANCH_SYNCED,
            {
                "child_id": "leaf",
                "repository": "exomonad/beast-rs",
                "parent_branch": "main.child",
                "pr_number": "41",
                "head_sha": "head-a",
                "merge_journal_id": "merge-journal",
                "lane_epoch": "1",
                "parent_commit_sha": "parent-head",
            },
        ),
    )
    child_state = child_store.checkpoint(
        child_state.recursive_fsm,
        {"leaf": leaf},
        child_state.budgets,
        child_state.events.last_consumed_offset,
        current_order=child_state.current_order,
        integration=child_state.integration,
    )

    reopened = _reopen_ordered_scope(
        child_state,
        parent_plan.sub_tls[0].plan,
        child_store,
        None,
    )

    assert isinstance(reopened.recursive_fsm, TLRunning)
    assert tuple(record.child_id for record in reopened.recursive_fsm.parallel_pending) == ("leaf",)
    assert reopened.slices["leaf"].status is SliceStatus.MERGED
    assert reopened.slices["leaf"].pr_number == 41
    assert reopened.slices["leaf"].post_merge == leaf.post_merge
    assert reopened.slices["leaf"].dispatch_intent_id == "leaf-dispatch"


def test_continue_preserves_accepted_leaf_and_later_pr_evidence(tmp_path: Path) -> None:
    parent_store, plan, config = _failed_ordered_run(
        tmp_path,
        child_status=SliceStatus.IN_REVIEW,
        child_publication=True,
    )
    child_state = RunStore("child", parent_store.run_dir).load()

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

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

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

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
    assert [gate.name for gate in second.final_state.gates] == ["tl-ordered-child-recovery-child"]


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
    assert result.final_state.slices["child"].status is SliceStatus.FAILED
    # Entry removes only the old root verdict. Child retry remains deferred to
    # post-replay reconciliation, rather than being approved before catch-up.
    assert calls == ["loop"]


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

    driver._record_child_exit_reason(child_store, "sub-TL controller completed an unsafe merge")

    refreshed = child_store.exit_diagnostics()
    assert refreshed is not None
    assert refreshed["checkpoint_revision"] != stale["checkpoint_revision"]
    assert refreshed["checkpoint_failure_reason"] == "sub-TL controller completed an unsafe merge"
    assert refreshed["reason"] == "sub-TL controller completed an unsafe merge"


def test_repeated_continuation_does_not_reopen_twice(tmp_path: Path) -> None:
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)
    assert decision is not None and decision.recoverable is True

    reopened = _reopen_ordered_scope(parent_store.load(), plan, parent_store, decision.task_name)
    # The second continuation observes a running scope, so it can neither
    # reopen again nor mint a second dispatch intent.
    assert _ordered_terminal_recovery_decision(reopened, plan, config, parent_store) is None
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

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

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

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.recoverable is False
    assert "durable child exit diagnostic" in decision.reason


def _legacy_dirty_preflight_failure(
    tmp_path: Path,
    *,
    active_sibling: bool = False,
    raw_legacy_response: bool = False,
    structured_response: bool = False,
    journal_child_name: str = "leaf",
    wrapped_trailer: str = "",
    requested_harness: str | None = None,
) -> tuple[RunStore, WorkPlan, TLLoopConfig, RunStore]:
    """Build the retained dirty-preflight failure with its exact rejected intent."""
    child_plan = WorkPlan(
        leaves=(
            LeafTask(
                "leaf",
                "implement the child change",
                agent_type=requested_harness,
            ),
            *((LeafTask("sibling", "implement sibling work"),) if active_sibling else ()),
        )
    )
    parent_store, plan, config = _failed_ordered_run(tmp_path, child_plan=child_plan)
    child_store = RunStore("child", parent_store.run_dir)
    child_state = child_store.load()
    driver._reopen_ordered_scope(child_state, child_plan, child_store, None)
    child_state = child_store.load()
    leaf = replace(
        child_state.slices["leaf"],
        status=SliceStatus.DISPATCHING,
        attempts=1,
        dispatch_intent_id="legacy-leaf-intent",
        dispatch_started_at=100.0,
        dispatch_generation=1,
        dispatch_ledger_floor=0,
        dispatch_last_boundary="dispatch_intended",
    )
    child_state = child_store.checkpoint(
        child_state.recursive_fsm,
        {**child_state.slices, "leaf": leaf},
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    attempt = driver.DispatchAttempt(
        intent_id="legacy-leaf-intent",
        started_at=100.0,
        harness="opencode",
        agent_type="opencode",
        attempt=1,
        controller_epoch=child_state.controller_epoch,
        dispatch_generation=1,
        ledger_floor=0,
    )
    rejected = (
        "BLOCKED: cannot spawn agent into a dirty TL worktree. 2 file(s) have uncommitted changes:\n"
        "  .exo/harness_policy.toml\n"
        "  CHANGELOG.md\n"
        "Commit the scaffold (per scaffold-fork-converge) or run `discard_worker_output` "
        "if throwaway, then retry. Workers spawn in-place and would inherit this state; "
        "dev-leaves fork from your branch HEAD and would not see your uncommitted work."
        if raw_legacy_response
        else (
            "host refusal wording changed; stable preflight classification remains authoritative"
            if structured_response
            else (
                "TL preflight failed; spawning is blocked until the worktree is clean or the user "
                "acknowledges with EXOMONAD_TL_PREFLIGHT_ACK=1.\n"
                "worktree check failed: uncommitted or untracked files are present\n"
                "  - .exo/harness_policy.toml\n  - CHANGELOG.md\n"
                "TL preflight runtime exclusions (built-in plus configured): .chainlink/, .exo/, "
                ".claude/settings.local.json/, .codex/, .opencode/, opencode.json/"
                f"{wrapped_trailer}"
            )
        )
    )
    journal = EffectJournal("child", child_store.run_dir / "action-journal.json")
    leaf_intent = EffectIntent(
        "spawn_leaf",
        "leaf",
        {
            "name": journal_child_name,
            "task": "implement the child change",
            "intent_id": attempt.intent_id,
        },
        True,
    )
    journal.append(leaf_intent)
    journal_result = (
        ToolResult.from_raw(
            {
                "success": False,
                "result": None,
                "error": rejected,
                "error_kind": None,
                "error_context": None,
            }
        )
        if raw_legacy_response
        else ToolResult(
            {
                "success": False,
                "error": rejected,
                "error_kind": (
                    "agent.spawn_precondition.dirty_worktree"
                    if structured_response
                    else "agent_error"
                ),
            },
            False,
            None,
            rejected,
            ("agent.spawn_precondition.dirty_worktree" if structured_response else "agent_error"),
        )
    )
    journal.mark_result(leaf_intent, journal_result)
    effects = EffectClient(_RecoveryTestTransport())
    driver._record_terminal_dispatch_failure(
        child_store,
        child_state,
        "leaf",
        attempt,
        rejected,
        ("agent.spawn_precondition.dirty_worktree" if structured_response else "agent_error"),
        replace(config, active=False, run_id="child", root_dir=parent_store.run_dir),
        effects,
        journal,
    )
    if active_sibling:
        current = child_store.load()
        sibling = replace(
            current.slices["sibling"],
            status=SliceStatus.SPAWNED,
            attempts=1,
            dispatch_intent_id="sibling-intent",
            dispatch_started_at=101.0,
            dispatch_generation=1,
            dispatch_agent_id="sibling-agent",
            dispatch_invocation_id="sibling-invocation",
            dispatch_authoritative_event_seq=30,
            dispatch_last_boundary="spawn_request_accepted",
        )
        child_store.checkpoint(
            current.recursive_fsm,
            {**current.slices, "sibling": sibling},
            current.budgets,
            current.events.last_consumed_offset,
        )
    assert isinstance(child_store.load().recursive_fsm, TLFailed)
    child_store.exit_reason_path.unlink(missing_ok=True)
    return parent_store, plan, config, child_store


def test_normal_failed_return_records_bound_diagnostic_and_recovers(tmp_path: Path) -> None:
    """A normal TLFailed return records the same bound evidence as an exit."""
    parent_store, plan, config, child_store = _legacy_dirty_preflight_failure(tmp_path)

    # The child returns TLFailed normally. The supervisor records an accurate
    # terminal observation, not a claim that the child crashed.
    assert (
        _supervise_live_sub_tl(
            _ExitedProcess(),
            child_store,
            config,
        )
        is not None
    )
    diagnostic = child_store.exit_diagnostics()
    assert diagnostic is not None
    assert diagnostic["outcome"] == "supervisor_observed_failure"
    assert diagnostic["scope_run_id"] == "child"
    assert diagnostic["scope_parent_run_id"] == "parent"
    assert diagnostic["dispatch_failures"][0]["dispatch_intent_id"] == "legacy-leaf-intent"

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.recoverable is True


def test_live_child_normal_return_writes_its_own_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_store, plan, config, child_store = _legacy_dirty_preflight_failure(tmp_path)
    task = replace(plan.sub_tls[0], source=NoEventQueue())
    failed_state = child_store.load()
    monkeypatch.setattr(
        driver,
        "tl_run",
        lambda *_args, **_kwargs: SimpleNamespace(final_state=failed_state),
    )

    driver._run_live_sub_tl(
        task,
        replace(config, project_root=None),
        NoEventQueue(),
        EffectClient(_RecoveryTestTransport()),
        parent_store,
        failed_state.budgets,
    )

    diagnostic = child_store.exit_diagnostics()
    assert diagnostic is not None
    assert diagnostic["outcome"] == "normal_return"
    assert diagnostic["scope_run_id"] == "child"
    assert diagnostic["checkpoint_revision"] == failed_state.revision
    assert diagnostic["dispatch_failures"][0]["dispatch_intent_id"] == "legacy-leaf-intent"


def test_exit_diagnostic_writer_skips_a_stale_return_revision(tmp_path: Path) -> None:
    _parent_store, _plan, _config, child_store = _legacy_dirty_preflight_failure(tmp_path)
    old = child_store.load()
    child_store.checkpoint(
        old.recursive_fsm,
        old.slices,
        old.budgets,
        old.events.last_consumed_offset,
    )

    written = child_store.record_exit_reason(
        cast(str, old.recursive_fsm.reason),
        outcome="normal_return",
        expected_revision=old.revision,
    )

    assert written is False
    assert child_store.exit_diagnostics() is None


def test_legacy_rejected_leaf_without_exit_marker_recovers_from_checkpoint_and_journal(
    tmp_path: Path,
) -> None:
    """A legacy normal failure remains recoverable without inventing its marker."""
    parent_store, plan, config, child_store = _legacy_dirty_preflight_failure(tmp_path)
    # The pre-fix producer never emitted this sidecar for a normal return. Keep
    # that historical on-disk shape while exercising the corrected consumer.
    child_store.exit_reason_path.unlink(missing_ok=True)

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert child_store.exit_diagnostics() is None
    assert decision is not None
    assert decision.recoverable is True


def test_exact_historical_raw_spawn_refusal_with_null_error_kind_recovers(
    tmp_path: Path,
) -> None:
    """The retained raw service error remains recoverable without a diagnostic sidecar."""
    parent_store, plan, config, child_store = _legacy_dirty_preflight_failure(
        tmp_path,
        raw_legacy_response=True,
    )

    failed_leaf = child_store.load().slices["leaf"]
    rejected_entry = driver._rejected_spawn_journal_entry(
        child_store, "leaf", failed_leaf.dispatch_intent_id
    )

    assert failed_leaf.dispatch_error_code == "agent_error"
    assert failed_leaf.dispatch_invocation_id is None
    assert failed_leaf.dispatch_error == rejected_entry["result"]["error"]
    assert rejected_entry["result"] == {
        "success": False,
        "result": None,
        "error": failed_leaf.dispatch_error,
        "error_kind": None,
        "error_context": None,
    }
    assert failed_leaf.dispatch_error.startswith(
        "BLOCKED: cannot spawn agent into a dirty TL worktree."
    )
    assert child_store.exit_diagnostics() is None

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.recoverable is True
    child_plan = plan.sub_tls[0].plan
    assert isinstance(child_plan, WorkPlan)
    journal_before = EffectJournal("child", child_store.run_dir / "action-journal.json").snapshot()
    reopened = driver._reopen_ordered_scope(
        child_store.load(),
        child_plan,
        child_store,
        None,
        retry_dispatch_slice="leaf",
    )
    assert reopened.recursive_fsm is not None
    assert reopened.recursive_fsm.evidence["preexecution_dispatch_refusal_format"] == (
        "raw_legacy_null_error_kind"
    )
    assert (
        EffectJournal("child", child_store.run_dir / "action-journal.json").snapshot()
        == journal_before
    )


def test_current_structured_spawn_refusal_recovers_without_message_matching(
    tmp_path: Path,
) -> None:
    """Current stable codes, rather than human wording, classify the refusal."""
    parent_store, plan, config, child_store = _legacy_dirty_preflight_failure(
        tmp_path,
        structured_response=True,
    )
    child_store.exit_reason_path.unlink(missing_ok=True)

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.recoverable is True


def test_unrelated_legacy_agent_error_text_cannot_authorize_dispatch_retry(
    tmp_path: Path,
) -> None:
    """Matching arbitrary error prose does not become a preflight proof."""
    parent_store, plan, config, child_store = _legacy_dirty_preflight_failure(tmp_path)
    state = child_store.load()
    failed_leaf = replace(
        state.slices["leaf"],
        dispatch_error="TL preflight failed; unrelated failure while inspecting repository",
    )
    entry = driver._rejected_spawn_journal_entry(
        child_store, "leaf", failed_leaf.dispatch_intent_id
    )
    assert entry is not None
    unrelated_result = dict(entry["result"])
    unrelated_result["error"] = failed_leaf.dispatch_error
    assert (
        driver._journal_result_dirty_preflight_format(
            {**entry, "result": unrelated_result}, failed_leaf
        )
        is None
    )
    child_store.checkpoint(
        state.recursive_fsm,
        {**state.slices, "leaf": failed_leaf},
        state.budgets,
        state.events.last_consumed_offset,
    )

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.recoverable is False


def test_wrapped_legacy_refusal_rejects_claimed_worker_launch_trailer(
    tmp_path: Path,
) -> None:
    malformed = (
        "TL preflight failed; spawning is blocked until the worktree is clean or the user "
        "acknowledges with EXOMONAD_TL_PREFLIGHT_ACK=1.\n"
        "worktree check failed: uncommitted or untracked files are present\n"
        "  - CHANGELOG.md\n"
        "worker was launched and has an active invocation"
    )
    assert not driver._wrapped_legacy_dirty_preflight(malformed)

    parent_store, plan, config, _child_store = _legacy_dirty_preflight_failure(
        tmp_path,
        wrapped_trailer="\nworker was launched and has an active invocation",
    )

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.recoverable is False


def test_rejected_spawn_journal_must_name_the_failed_child(tmp_path: Path) -> None:
    parent_store, plan, config, child_store = _legacy_dirty_preflight_failure(
        tmp_path,
        raw_legacy_response=True,
        journal_child_name="different-child",
    )
    failed_leaf = child_store.load().slices["leaf"]

    assert (
        driver._rejected_spawn_journal_entry(child_store, "leaf", failed_leaf.dispatch_intent_id)
        is None
    )
    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.recoverable is False


def test_raw_legacy_refusal_requires_its_explicit_null_kind_envelope(
    tmp_path: Path,
) -> None:
    _parent_store, _plan, _config, child_store = _legacy_dirty_preflight_failure(
        tmp_path,
        raw_legacy_response=True,
    )
    slice_state = child_store.load().slices["leaf"]
    entry = driver._rejected_spawn_journal_entry(
        child_store, "leaf", slice_state.dispatch_intent_id
    )
    assert entry is not None
    raw_result = dict(entry["result"])
    raw_result.pop("error_kind")

    assert (
        driver._journal_result_dirty_preflight_format({**entry, "result": raw_result}, slice_state)
        is None
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope_run_id", "wrong-child"),
        ("scope_parent_run_id", "wrong-parent"),
        ("controller_epoch", "stale-epoch"),
        ("plan_manifest_digest", "stale-manifest"),
        ("dispatch_intent_id", "wrong-attempt"),
    ],
)
def test_conflicting_exit_diagnostic_cannot_authorize_retry(
    tmp_path: Path, field: str, value: str
) -> None:
    parent_store, plan, config, child_store = _legacy_dirty_preflight_failure(tmp_path)
    child_state = child_store.load()
    child_store.record_exit_reason(
        cast(str, child_state.recursive_fsm.reason),
        outcome="normal_return",
        expected_revision=child_state.revision,
    )
    diagnostic = json.loads(child_store.exit_reason_path.read_text(encoding="utf-8"))
    if field == "dispatch_intent_id":
        diagnostic["dispatch_failures"][0][field] = value
    else:
        diagnostic[field] = value
    child_store.exit_reason_path.write_text(json.dumps(diagnostic), encoding="utf-8")

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None and not decision.recoverable
    assert "diagnostic conflicts" in decision.reason


def test_unresolved_spawn_effect_blocks_legacy_preexecution_retry(tmp_path: Path) -> None:
    parent_store, plan, config, child_store = _legacy_dirty_preflight_failure(tmp_path)
    unresolved = EffectIntent(
        "spawn_leaf",
        "leaf",
        {"name": "leaf", "task": "implement the child change", "intent_id": "later-unknown"},
        True,
    )
    journal = EffectJournal("child", child_store.run_dir / "action-journal.json")
    journal.append(unresolved)
    journal.mark_unknown(unresolved, RuntimeError("spawn response was interrupted"))

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None and not decision.recoverable
    assert "unreconciled effect" in decision.reason


def test_active_sibling_worker_blocks_legacy_preexecution_retry(tmp_path: Path) -> None:
    parent_store, plan, config, _child_store = _legacy_dirty_preflight_failure(
        tmp_path,
        active_sibling=True,
    )

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None and not decision.recoverable


def test_proven_rejected_leaf_reopens_only_its_failed_dispatch_and_gate(
    tmp_path: Path,
) -> None:
    parent_store, plan, config, child_store = _legacy_dirty_preflight_failure(tmp_path)
    parent_store.set_gate(GATE)
    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)
    assert decision is not None and decision.recoverable
    history_before = EffectJournal("child", child_store.run_dir / "action-journal.json").snapshot()
    child_plan = WorkPlan(leaves=(LeafTask("leaf", "implement the child change"),))

    reopened = driver._reopen_ordered_scope(
        child_store.load(),
        child_plan,
        child_store,
        None,
        retry_dispatch_slice="leaf",
    )

    assert reopened.recursive_fsm is not None
    assert reopened.slices["leaf"].status is SliceStatus.PENDING
    assert reopened.slices["leaf"].dispatch_last_boundary == "preexecution_retry_authorized"
    assert not any(gate.name == "tl-dispatch-failed-leaf" for gate in reopened.gates)
    assert (
        history_before
        == EffectJournal("child", child_store.run_dir / "action-journal.json").snapshot()
    )

    root_reopened = driver._reopen_ordered_scope(parent_store.load(), plan, parent_store, "child")
    assert not any(gate.name == GATE for gate in root_reopened.gates)


def test_recovered_dirty_refusal_dispatches_once_and_replay_does_not_duplicate(
    tmp_path: Path,
) -> None:
    parent_store, plan, config, child_store = _legacy_dirty_preflight_failure(
        tmp_path,
        raw_legacy_response=True,
    )
    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)
    assert decision is not None and decision.recoverable
    transport = _RetryDispatchTransport()
    effects = EffectClient(transport)
    task = plan.sub_tls[0]
    child_worktree = str(_sub_tl_worktree(config, parent_store.root_dir, "parent", task))
    child_config = replace(
        _child_config(
            config,
            task,
            NoEventQueue(),
            effects,
            parent_store,
            "main.child",
            child_worktree,
            ordered_recovery=True,
        ),
        project_root=None,
        keep_alive_on_waiting=False,
    )
    child_plan = task.plan
    assert isinstance(child_plan, WorkPlan)
    reopened = driver._reopen_ordered_scope(
        child_store.load(),
        child_plan,
        child_store,
        None,
        retry_dispatch_slice="leaf",
    )
    assert reopened.recursive_fsm is not None
    assert reopened.recursive_fsm.evidence["preexecution_dispatch_refusal_format"] == (
        "raw_legacy_null_error_kind"
    )
    effects_log = EffectJournal("child", child_store.run_dir / "action-journal.json")

    first = driver._dispatch_children(
        child_plan,
        reopened,
        child_config,
        effects,
        effects_log,
        child_store,
    )

    assert len(transport.spawn_calls) == 1
    assert first.slices["leaf"].attempts == 2
    assert first.slices["leaf"].status in {
        SliceStatus.DISPATCH_UNCONFIRMED,
        SliceStatus.SPAWNED,
    }
    assert (
        sum(
            entry.get("operation") == "spawn_leaf" and entry.get("status") == "rejected"
            for entry in effects_log.snapshot()
        )
        == 1
    )

    second = driver._dispatch_children(
        child_plan,
        child_store.load(),
        child_config,
        effects,
        effects_log,
        child_store,
    )

    assert second.slices["leaf"].attempts == 2
    assert len(transport.spawn_calls) == 1


def test_proven_preexecution_retry_uses_current_role_route_and_keeps_plan_request(
    tmp_path: Path,
) -> None:
    historical_request = "opencode/opencode-go/deepseek-v4.1-flash"
    parent_store, plan, config, child_store = _legacy_dirty_preflight_failure(
        tmp_path,
        raw_legacy_response=True,
        requested_harness=historical_request,
    )
    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)
    assert decision is not None and decision.recoverable
    task = plan.sub_tls[0]
    child_plan = task.plan
    assert isinstance(child_plan, WorkPlan)
    reopened = _reopen_ordered_scope(
        child_store.load(),
        child_plan,
        child_store,
        None,
        retry_dispatch_slice="leaf",
    )
    assert reopened.slices["leaf"].dispatch_uses_current_role_configuration
    assert reopened.slices["leaf"].requested_harness == historical_request

    route = "codex/gpt-6-luna"
    role = {
        "allow": [route],
        "cost_rank": {route: 1},
        "token_budget": 10000,
        "per_harness_budget": {route: 10000},
        "escalate_after_attempts": 1,
    }
    policy = validate_policy({"roles": {"tl": role, "worker": role, "reviewer": role}})
    capabilities = CapabilityMap({route: Difficulty.HARD})
    child_worktree = str(_sub_tl_worktree(config, parent_store.root_dir, "parent", task))
    child_config = replace(
        _child_config(
            config,
            task,
            EmptyQueue(),
            None,
            parent_store,
            "main.child",
            child_worktree,
            ordered_recovery=True,
        ),
        active=False,
        project_root=tmp_path,
        keep_alive_on_waiting=False,
        policy=policy,
        capabilities=capabilities,
    )
    assert (
        driver.select_agent_type(
            reopened.slices["leaf"],
            child_config.role,
            reopened.budgets,
            policy,
            capabilities,
            requested_harness=historical_request,
        )
        is None
    )

    effects = EffectClient(_RetryDispatchTransport())
    intent = driver._prepare_spawn(
        "leaf",
        reopened,
        child_config,
        effects,
        child_store,
        [],
    )
    persisted = child_store.load().slices["leaf"]

    assert intent.agent_type == "codex"
    assert intent.harness == route
    assert intent.model == "gpt-6-luna"
    assert intent.requested_harness == historical_request
    assert intent.route_reconciliation_source == "proven_preexecution_retry"
    assert persisted.requested_harness == historical_request
    assert persisted.resolved_harness == route
    assert persisted.model == "gpt-6-luna"
    assert persisted.dispatch_uses_current_role_configuration


def test_child_config_declares_parent_manifest_for_recovery(tmp_path: Path) -> None:
    """A production-spawned child persists the parent's declared child manifest.

    Regression for ordered recovery: rebuilding the child manifest from its own
    run id changes the scope id, so the child checkpoint digest no longer
    matches the parent's ``child_manifests`` entry and continuation gates every
    real child as non-recoverable.
    """
    parent_store, plan, config = _failed_ordered_run(tmp_path)
    task = plan.sub_tls[0]
    child_worktree = str(_sub_tl_worktree(config, parent_store.root_dir, parent_store.run_id, task))
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

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

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

    def capture_snapshot(self, cursor: int) -> LedgerSnapshot:
        return LedgerSnapshot((), cursor, SequenceStatus.UNKNOWN, (), None)


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
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
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
