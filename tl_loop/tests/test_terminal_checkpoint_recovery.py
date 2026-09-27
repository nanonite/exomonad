"""Synthetic terminal checkpoint recovery acceptance (#1112, #1117).

A recorded production run ended with the root and its ``recreate-stage`` child
both at ``tl_failed``: the leaf ``recreated-leaf`` was parked for
``publication_ownership_unresolved`` behind Chainlink escalation #9001, and the
child controller then died on the ``{'cicoIssueId': 9001}`` escalation result.
The leaf later republished its head as PR #102.

This module synthesizes that shape from scratch under the test's temporary
directory. It never reads or writes any recorded checkpoint, escalation, or PR,
and it drives the real ``--continue`` code path (``run_tl_loop`` with
``session_mode="continue"``) instead of ``exomonad init``.

The recorded answer is encoded as an assertion: ``--continue`` alone does not
recover this checkpoint. It keeps ``tl_failed`` and opens the named
``tl-ordered-child-recovery-recreate-stage`` gate, so the operator must resolve
the parked publication ownership and answer that gate (or use the recreate
path) before the child is relaunched.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.events.envelope import EventEnvelope
from tl_loop.fsm.scope import TLFailed
from tl_loop.loop.driver import (
    LeafTask,
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    _bind_initial_slices,
    _ensure_canonical_scope,
    _initial_slices,
    _manifest_for_plan,
    _ordered_terminal_recovery_decision,
    _release_canonical_scope,
    _sub_tl_worktree,
    run_tl_loop,
)
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.schema import (
    BudgetLedger,
    IntegrationRuntimeState,
    OrderedStageState,
    ParkCause,
    PublicationBinding,
    SliceStatus,
)
from tl_loop.state.store import RunStore, create

ROOT_RUN = "root"
CHILD = "recreate-stage"
LEAF = "recreated-leaf"
LEAF_AGENT = "recreated-leaf-opencode"
LEAF_INVOCATION = "inv-recreated-1"
HEAD_BRANCH = "main.recreate-stage.recreated-leaf"
HEAD_102 = "b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5"
PR_102 = 102
ESCALATION_ISSUE = 9001
GATE_PREFIX = "tl-ordered-child-recovery-"
PARK_GATE = f"task-blocked:{CHILD}:{LEAF}:1:{ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED.value}"
# The exact pre-fix failure reason the recorded child checkpoint carried.
CHAINLINK_FAILURE = "chainlink issue result has no positive issue ID: {'cicoIssueId': 9001}"
RETRYABLE_EXIT = "sub-TL controller exited before authoritative resolution with code 1"


class EmptyQueue:
    """Event source that proves a terminal continuation consumes nothing."""

    def get(self, timeout: float | None = None) -> EventEnvelope:
        del timeout
        raise AssertionError("a terminal continuation must not consume ledger events")

    def acknowledge(self, event: EventEnvelope) -> int:
        del event
        raise AssertionError("a terminal continuation must not acknowledge ledger events")


class RecordingTransport:
    """Effect transport that fails the run on any attempted tool call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, JsonObject]] = []

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        self.calls.append((role, tool_name, arguments))
        raise AssertionError(
            f"terminal recovery dispatched an effect: {role}/{tool_name} {arguments!r}"
        )


def _read_only(transport: RecordingTransport) -> ReadOnlyEffectClient:
    return ReadOnlyEffectClient(EffectClient(transport))


def _config(state_root: Path, root: Path) -> TLLoopConfig:
    return TLLoopConfig(
        root_dir=state_root,
        project_root=root,
        branch="main",
        session_mode="continue",
        active=False,
    )


def _terminal_failed_checkpoints(
    root: Path,
    *,
    child_failure_reason: str = CHAINLINK_FAILURE,
    exit_reason: str = CHAINLINK_FAILURE,
    leaf_status: SliceStatus = SliceStatus.PARKED,
) -> tuple[RunStore, WorkPlan, TLLoopConfig]:
    """Build the synthetic root/child ``tl_failed`` pair under ``root``."""
    state_root = root / ".exo" / "tl-loop"
    child_plan = WorkPlan(leaves=(LeafTask(LEAF, "implement the recreated leaf"),))
    plan = WorkPlan(sub_tls=(SubTLTask(CHILD, child_plan, order=1),))
    config = _config(state_root, root)
    manifest = _manifest_for_plan(plan, ROOT_RUN, config)
    create(
        ROOT_RUN,
        {
            "plan_manifest": manifest.to_document(),
            "slices": _bind_initial_slices(
                _initial_slices(plan, config, state_root, ROOT_RUN), manifest
            ),
        },
        root_dir=state_root,
    )
    parent_store = RunStore(ROOT_RUN, state_root)
    parent_state = parent_store.load()
    child_worktree = str(_sub_tl_worktree(config, state_root, ROOT_RUN, plan.sub_tls[0]))
    child_slice = replace(
        parent_state.slices[CHILD],
        status=SliceStatus.FAILED,
        branch=f"main.{CHILD}",
        worktree=child_worktree,
        dispatch_intent_id="sub-tl-dispatch",
        dispatch_agent_id=CHILD,
        dispatch_authoritative_event_seq=11,
        dispatch_last_boundary="sub_tl_started",
    )
    # The parent fails the stage because its ordered child is not recoverable.
    parent_store.checkpoint(
        TLFailed("recursive child is not recoverable"),
        {CHILD: child_slice},
        parent_state.budgets,
        0,
        current_order=1,
        ordered_stages=(OrderedStageState(1, (CHILD,)),),
        integration=IntegrationRuntimeState(sub_tl_states={CHILD: IntegrationLifecycle.FAILED}),
    )

    child_manifest = manifest.child_manifests[
        next(node.node_id for node in manifest.nodes if node.name == CHILD)
    ]
    child_config = replace(
        config,
        root_dir=parent_store.run_dir,
        run_id=CHILD,
        branch=f"main.{CHILD}",
        worktree=child_worktree,
        parent_branch="main",
        parent_run_id=ROOT_RUN,
        depth=1,
    )
    create(
        CHILD,
        {
            "plan_manifest": child_manifest.to_document(),
            "slices": _bind_initial_slices(
                _initial_slices(child_plan, child_config, parent_store.run_dir, CHILD),
                child_manifest,
            ),
            "owner_branch": f"main.{CHILD}",
            "owner_worktree": child_worktree,
            "parent_branch": "main",
            "parent_run_id": ROOT_RUN,
            "session_mode": "continue",
        },
        root_dir=parent_store.run_dir,
    )
    child_store = RunStore(CHILD, parent_store.run_dir)
    child_state = _release_canonical_scope(
        _ensure_canonical_scope(child_store.load(), child_manifest, child_store),
        child_store,
    )
    leaf = replace(
        child_state.slices[LEAF],
        status=leaf_status,
        branch=HEAD_BRANCH,
        base_ref="main",
        attempts=1,
        pr_number=PR_102,
        reviewed_head=HEAD_102,
        publication=PublicationBinding(
            PR_102,
            HEAD_102,
            HEAD_BRANCH,
            "main",
            1,
            LEAF_INVOCATION,
        ),
        dispatch_intent_id="leaf-dispatch",
        dispatch_agent_id=LEAF_AGENT,
        dispatch_invocation_id=LEAF_INVOCATION,
        dispatch_authoritative_event_seq=23,
        dispatch_last_boundary="agent.spawned",
    )
    if leaf_status is SliceStatus.PARKED:
        # The escalation that created #9001 is durable park evidence, not a
        # guess: the park cause, issue id, and named gate are all recorded.
        leaf = replace(
            leaf,
            park_cause=ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED,
            park_issue_id=ESCALATION_ISSUE,
            park_audit={"attempts": 1, "gate_name": PARK_GATE, "attempt": 1},
        )
    child_state = child_store.checkpoint(
        child_state.fsm,
        {LEAF: leaf},
        child_state.budgets,
        child_state.events.last_consumed_offset,
        integration=child_state.integration,
    )
    child_store.set_gate(PARK_GATE)
    # The child controller persisted its own terminal failure before exiting.
    child_state = child_store.load()
    child_store.checkpoint(
        TLFailed(child_failure_reason),
        child_state.slices,
        child_state.budgets,
        child_state.events.last_consumed_offset,
        current_order=child_state.current_order,
        integration=child_state.integration,
    )
    child_store.record_exit_reason(exit_reason)
    return parent_store, plan, config


def _continue(
    parent_store: RunStore, plan: WorkPlan, config: TLLoopConfig
) -> tuple[Any, RecordingTransport]:
    """Run the real ``--continue`` path once against the copied checkpoint."""
    transport = RecordingTransport()
    result = run_tl_loop(
        ROOT_RUN,
        plan,
        EmptyQueue(),
        _read_only(transport),
        config=config,
        root_dir=parent_store.root_dir,
        budgets=BudgetLedger(0, 0),
    )
    return result, transport


def _leaf_evidence(parent_store: RunStore) -> dict[str, Any]:
    child = RunStore(CHILD, parent_store.run_dir).load()
    leaf = child.slices[LEAF]
    return {
        "status": leaf.status.value,
        "pr_number": leaf.pr_number,
        "publication": leaf.publication,
        "park_cause": None if leaf.park_cause is None else leaf.park_cause.value,
        "park_issue_id": leaf.park_issue_id,
        "phase": child.recursive_fsm,
        "gates": [gate.name for gate in child.gates],
    }


def test_continue_alone_keeps_the_recorded_failure_and_names_the_gate(
    tmp_path: Path,
) -> None:
    """The answer for the synthetic checkpoint: a named gate, not a continuation."""
    parent_store, plan, config = _terminal_failed_checkpoints(tmp_path)
    before = _leaf_evidence(parent_store)
    assert before["park_issue_id"] == ESCALATION_ISSUE
    assert before["publication"] is not None
    assert before["publication"].pr_number == PR_102

    result, transport = _continue(parent_store, plan, config)

    # No effect of any kind: no relaunch, no re-publication, no escalation.
    assert transport.calls == []
    state = result.final_state
    assert isinstance(state.recursive_fsm, TLFailed)
    assert state.recursive_fsm.reason == "recursive child is not recoverable"
    assert state.fsm.phase.value == "tl_failed"
    assert [gate.name for gate in state.gates] == [f"{GATE_PREFIX}{CHILD}"]
    assert result.diagnostics["recovery_gate"] == f"{GATE_PREFIX}{CHILD}"
    # The child checkpoint keeps PR #102, escalation #9001, and its own failure.
    assert _leaf_evidence(parent_store) == before


def test_continue_names_the_precise_unproven_child_exit(tmp_path: Path) -> None:
    """The recorded Chainlink failure is not a retryable controller boundary."""
    parent_store, plan, config = _terminal_failed_checkpoints(tmp_path)

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.recoverable is False
    assert decision.task_name == CHILD
    assert decision.gate_name == f"{GATE_PREFIX}{CHILD}"
    assert decision.reason == (
        f"child exit is not a retryable startup, transport, or process failure: {CHAINLINK_FAILURE}"
    )


def test_continue_names_the_parked_publication_ownership_slice(
    tmp_path: Path,
) -> None:
    """A retryable child exit still cannot reopen a parked publication owner."""
    parent_store, plan, config = _terminal_failed_checkpoints(
        tmp_path,
        child_failure_reason=RETRYABLE_EXIT,
        exit_reason=RETRYABLE_EXIT,
    )

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.recoverable is False
    assert decision.gate_name == f"{GATE_PREFIX}{CHILD}"
    assert decision.reason == (f"child slice {LEAF!r} has an unsafe terminal status")


def test_repeated_continue_is_idempotent_and_never_guesses_ownership(
    tmp_path: Path,
) -> None:
    """Answering nothing and repeating the continuation changes no evidence."""
    parent_store, plan, config = _terminal_failed_checkpoints(tmp_path)
    first, first_transport = _continue(parent_store, plan, config)
    before = _leaf_evidence(parent_store)
    second, second_transport = _continue(parent_store, plan, config)

    assert first_transport.calls == second_transport.calls == []
    assert [gate.name for gate in second.final_state.gates] == [f"{GATE_PREFIX}{CHILD}"]
    assert second.final_state.recursive_fsm == first.final_state.recursive_fsm
    assert _leaf_evidence(parent_store) == before
    # The pending gate is still the only thing that changed on the root.
    root_document = json.loads((parent_store.run_dir / "run.json").read_text(encoding="utf-8"))
    assert [gate["status"] for gate in root_document["gates"]] == ["pending"]


def test_same_evidence_without_the_parked_slice_reopens_the_child(
    tmp_path: Path,
) -> None:
    """Control: the gate is caused by the recorded evidence, not the mechanism."""
    parent_store, plan, config = _terminal_failed_checkpoints(
        tmp_path,
        child_failure_reason=RETRYABLE_EXIT,
        exit_reason=RETRYABLE_EXIT,
        leaf_status=SliceStatus.SPAWNED,
    )

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.recoverable is True
    assert decision.task_name == CHILD
    assert decision.reason == "durable child startup or transport failure is retryable"

    result, transport = _continue(parent_store, plan, config)

    # The proof passed, so no recovery gate is raised; the still-terminal child
    # checkpoint is relaunched against the same accepted dispatch intent rather
    # than minting a second child or repeating a confirmed effect.
    assert transport.calls == []
    assert [gate.name for gate in result.final_state.gates] == []
    assert result.final_state.slices[CHILD].dispatch_intent_id == "sub-tl-dispatch"
    assert result.final_state.slices[CHILD].dispatch_authoritative_event_seq == 11


@pytest.mark.parametrize("leaf_status", [SliceStatus.PARKED, SliceStatus.FAILED])
def test_unsafe_child_slice_status_never_reopens(leaf_status: SliceStatus, tmp_path: Path) -> None:
    """Both terminal child statuses fail closed on the same proof."""
    parent_store, plan, config = _terminal_failed_checkpoints(
        tmp_path,
        child_failure_reason=RETRYABLE_EXIT,
        exit_reason=RETRYABLE_EXIT,
        leaf_status=leaf_status,
    )

    decision = _ordered_terminal_recovery_decision(parent_store.load(), plan, config, parent_store)

    assert decision is not None
    assert decision.recoverable is False
    assert decision.gate_name == f"{GATE_PREFIX}{CHILD}"
