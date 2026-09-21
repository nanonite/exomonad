"""Proof-gated continuation coverage for failed ordered sub-TLs."""

from __future__ import annotations

import json
import queue
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
    _candidate_manifest,
    _child_config,
    _ensure_canonical_scope,
    _initial_slices,
    _manifest_for_plan,
    _ordered_terminal_recovery_decision,
    _release_canonical_scope,
    _reopen_ordered_scope,
    _sub_tl_worktree,
    _supervise_live_sub_tl,
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
