"""Proof-gated continuation coverage for failed ordered sub-TLs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import TransportClient
from tl_loop.events.envelope import EventEnvelope
from tl_loop.fsm.scope import TLFailed, TLRunning
from tl_loop.loop import driver
from tl_loop.loop.driver import (
    LeafTask,
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    _bind_initial_slices,
    _initial_slices,
    _manifest_for_plan,
    _ordered_terminal_recovery_decision,
    _reopen_ordered_scope,
    _sub_tl_worktree,
    run_tl_loop,
)
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.schema import (
    BudgetLedger,
    IntegrationRuntimeState,
    OrderedStageState,
    PublicationBinding,
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
    child_state = child_store.load()
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
    child_store.checkpoint(
        TLFailed("sub-TL controller exited before authoritative resolution"),
        child_state.slices,
        child_state.budgets,
        0,
        integration=child_state.integration,
    )
    child_store.record_exit_reason("sub-TL controller exited before authoritative resolution")
    return parent_store, plan, config


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

    monkeypatch.setattr(driver, "_run_sub_tls", lambda _plan, state, *_args: state)
    monkeypatch.setattr(
        driver,
        "_run_loop",
        lambda *args: driver.TLRunResult(args[6], (), (), ()),
    )
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
