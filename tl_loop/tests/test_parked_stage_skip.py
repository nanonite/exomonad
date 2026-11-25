"""Operator skip for a stage parked behind an unresolved launch effect.

The generic fixture is a redacted held-launch shape: one ordered stage is
parked behind ``tl-held-launch-effect-...`` because its child controller
journaled ``resume_blocked_leaf`` as ``unknown`` and the host never recorded a
launch. Rejecting that gate parks forever, abandoning it records the decision
but keeps the slice parked, and retrying the same failed launch is not what the
operator wants. A skip records the decision durably, marks the stage SKIPPED,
and evaluates its dependents under an explicit dependency policy.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, NamedTuple, cast

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import TransportClient
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.scope import TLFailed as RecursiveTLFailed
from tl_loop.fsm.scope import TLRunning
from tl_loop.loop import driver
from tl_loop.loop.driver import (
    EffectIntent,
    LeafTask,
    OrderedRecoveryDecision,
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    _advance_post_merge_boundary,
    _bind_initial_slices,
    _ensure_canonical_scope,
    _hold_ordered_recovery_gate,
    _initial_slices,
    _manifest_for_plan,
    _ordered_terminal_recovery_decision,
    _release_canonical_scope,
    _reopen_after_ordered_recovery,
    _reopen_ordered_scope,
    _sub_tl_worktree,
    _supervise_live_sub_tl,
    normalize_work_plan,
    run_tl_loop,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.loop.launch_resolution import HeldLaunchEffect, classify_held_launch_effect
from tl_loop.ordered import IntegrationContract, IntegrationLifecycle
from tl_loop.state.schema import (
    BudgetLedger,
    GateStatus,
    IntegrationRuntimeState,
    OrderedStageState,
    RepositoryIdentity,
    RunState,
    SkipDependencyPolicy,
    SliceState,
    SliceStatus,
)
from tl_loop.state.store import RunStore
from tl_loop.tests.checkpoint_fixtures import create
from tl_loop.tests.test_chainlink_issue_binding import IssueCloseTransport
from tl_loop.tests.test_driver import SyntheticQueue
from tl_loop.tests.test_held_launch_effect_resolution import (
    LEAF,
    LEDGER_RUN_ID,
    _ExitedProcess,
    _owner_fingerprint,
    _repository_identity,
    _write_ledger,
)

STAGE = "held-stage"
NEXT = "next-stage"
SIBLING = "sibling-stage"
OWNER = f"{LEAF}-opencode"
#: The branch the parked stage owns; its leaf owner branches from it.
PARENT_BRANCH = f"main.{STAGE}"
CHILD_BRANCH = PARENT_BRANCH
LEAF_BRANCH = f"{CHILD_BRANCH}.{OWNER}"
GENERIC_GATE = "tl-ordered-child-recovery-" + STAGE
ISSUE_ID = 701
#: Issues bound to the stages in the Chainlink-semantics cases. The held
#: stage's own issue must stay open for a fresh plan; the next stage's issue
#: closes independently once that stage merges.
STAGE_ISSUE = 704
NEXT_ISSUE = 702
SIBLING_ISSUE = 703
SKIP_REASON = "the held stage record is unrecoverable; re-plan it in a fresh context"


class _Fixture(NamedTuple):
    project: Path
    parent_store: RunStore
    child_store: RunStore
    plan: WorkPlan
    config: TLLoopConfig
    owner_dir: Path
    worktree: Path
    journal: EffectJournal
    entry: Mapping[str, object]
    intent: str


def _seed_owner(
    project: Path,
) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
    """Seed the dormant leaf owner this child journal's intent is bound to."""
    worktree = project / ".exo" / "worktrees" / OWNER
    worktree.mkdir(parents=True)
    _git("init", "-q", "-b", LEAF_BRANCH, str(worktree))
    owner_dir = project / ".exo" / "agents" / OWNER
    owner_dir.mkdir(parents=True)
    (owner_dir / "active_issue").write_text("701\n", encoding="utf-8")
    identity = {
        "agent_name": OWNER,
        "slug": LEAF,
        "slice_id": LEAF,
        "agent_type": "opencode",
        "model": "opencode-go/deepseek-v4.1-flash",
        "effort": "high",
        "birth_branch": LEAF_BRANCH,
        "parent_branch": PARENT_BRANCH,
        "working_dir": str(worktree),
        "topology": "worktree_per_agent",
        "ledger_owned": False,
    }
    invocation = {
        "invocation_id": "opencode-prior-invocation",
        "generation": 1,
        "status": "failed",
        "runtime": "opencode",
        "model": "opencode-go/deepseek-v4.1-flash",
        "effort": "high",
        "trigger": "spawn",
        "branch": LEAF_BRANCH,
        "worktree": str(worktree),
        "pr_number": None,
        "head_sha": None,
        "prior_invocation_id": None,
    }
    (owner_dir / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
    (owner_dir / "invocation.json").write_text(json.dumps(invocation), encoding="utf-8")
    return owner_dir, worktree, identity, invocation


def _git(*arguments: str, cwd: Path | None = None) -> str:
    import subprocess

    return subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
    ).stdout


def _derive_intent(project: Path, worktree: Path) -> str:
    from tl_loop.loop.blocked_leaf_resume import (
        blocked_resume_intent_id,
        resolve_owner_for_resume_retry,
    )

    owner, _successor = resolve_owner_for_resume_retry(
        project,
        slice_id=LEAF,
        issue_id=ISSUE_ID,
        expected_parent_branch=PARENT_BRANCH,
        expected_agent_name=OWNER,
        prior_invocation_id="opencode-prior-invocation",
        prior_generation=1,
        expected_branch=LEAF_BRANCH,
        expected_worktree=str(worktree),
        expected_worktree_recorded=str(worktree),
        expected_worktree_fingerprint=_owner_fingerprint(worktree),
        dispatch_intent_id="placeholder",
    )
    return blocked_resume_intent_id(owner)


def _journal_arguments(worktree: Path, intent: str) -> dict[str, object]:
    return {
        "chainlink_issue_id": ISSUE_ID,
        "expected_invocation_id": "opencode-prior-invocation",
        "expected_branch": LEAF_BRANCH,
        "expected_worktree_fingerprint": _owner_fingerprint(worktree),
        "task": "Recover the held stage for the fixture issue",
        "human_approved": True,
        "owner_agent_name": OWNER,
        "prior_generation": 1,
        "expected_worktree": str(worktree),
        "expected_worktree_recorded": str(worktree),
        "dispatch_intent_id": intent,
        "dispatch_attempt": 2,
        "dispatch_started_at": 1000.0,
        "dispatch_generation": 3,
        "dispatch_ledger_floor": 0,
        "controller_epoch": "epoch-1",
        "route_reconciliation_source": "proven_preexecution_retry",
        "runtime": "opencode",
        "model": "opencode-go/deepseek-v4.1-flash",
        "error": "Timed out after 10.000s",
    }


def _launch_row(intent: str, *, run_seq: int = 12) -> dict[str, object]:
    return {
        "type": "agent.spawned",
        "run_id": LEDGER_RUN_ID,
        "run_seq": run_seq,
        "agent_id": "controller",
        "data": {
            "intent_id": intent,
            "child_agent": OWNER,
            "branch": LEAF_BRANCH,
            "spawn_type": "resume_blocked",
            "invocation_id": "opencode-successor-invocation",
        },
    }


def _write_plan_snapshot(state_root: Path, plan: WorkPlan) -> None:
    """Record the accepted plan snapshot and digest the skip decision links to."""
    import hashlib

    from tl_loop.loop.driver import _manifest_plan

    document = {"run_id": "parent", "plan": _manifest_plan(plan)}
    payload = json.dumps(document, sort_keys=True).encode()
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "plan.snapshot").write_bytes(payload)
    (state_root / "plan.snapshot.sha256").write_text(
        hashlib.sha256(payload).hexdigest() + "\n",
        encoding="ascii",
    )


def _stage(
    name: str,
    order: int,
    *,
    policy: SkipDependencyPolicy,
    empty: bool = False,
    issue_id: int | None = None,
    integration: IntegrationContract | None = None,
) -> SubTLTask:
    child_plan = WorkPlan() if empty else WorkPlan(leaves=(LeafTask(LEAF, "implement it"),))
    return SubTLTask(
        name,
        child_plan,
        order=order,
        skip_dependents=policy,
        chainlink_issue_id=issue_id,
        integration=integration or IntegrationContract(),
    )


def _run(
    tmp_path: Path,
    *,
    policy: SkipDependencyPolicy = SkipDependencyPolicy.PROCEED,
    sibling: bool = False,
    linked_issues: bool = False,
    chain: Sequence[str] = (NEXT,),
    parallel_sibling: bool = False,
    optional_chain: bool = False,
) -> _Fixture:
    """Build ordered stages through the production fixture helpers.

    ``chain`` names the ordered stages after the parked one; ``sibling`` appends
    a further ordered stage, while ``parallel_sibling`` places the sibling in
    stage one beside the parked stage so it does not depend on it. Empty stage
    children with ``optional_chain`` declare no aggregate PR, which lets the
    production loop complete them without a harness.
    """
    if sibling and parallel_sibling:
        raise ValueError("sibling and parallel_sibling both declare the sibling once")
    project = tmp_path
    state_root = tmp_path / "state"
    (project / ".exo").mkdir(parents=True, exist_ok=True)
    owner_dir, worktree, _identity, _invocation = _seed_owner(project)
    intent = _derive_intent(project, worktree)
    _write_ledger(project)

    stage = _stage(
        STAGE,
        1,
        policy=policy,
        issue_id=STAGE_ISSUE if linked_issues else None,
    )
    optional = (
        IntegrationContract(aggregate_pr_required=False) if optional_chain else None
    )
    chain_issues = () if not linked_issues else (NEXT_ISSUE,)
    tasks = [stage]
    for index, name in enumerate(chain):
        tasks.append(
            _stage(
                name,
                2 + index,
                policy=policy,
                empty=True,
                issue_id=chain_issues[index] if index < len(chain_issues) else None,
                integration=optional,
            )
        )
    sibling_order = len(chain) + 2
    if sibling:
        tasks.append(
            _stage(
                SIBLING,
                sibling_order,
                policy=policy,
                empty=True,
                issue_id=SIBLING_ISSUE if linked_issues else None,
                integration=optional,
            )
        )
    if parallel_sibling:
        tasks.append(
            _stage(
                SIBLING,
                1,
                policy=policy,
                empty=True,
                issue_id=SIBLING_ISSUE if linked_issues else None,
                integration=optional,
            )
        )
    plan = normalize_work_plan(WorkPlan(sub_tls=tuple(tasks)))
    config = TLLoopConfig(
        root_dir=state_root,
        project_root=project,
        branch="main",
        ledger_run_id=LEDGER_RUN_ID,
        session_mode="continue",
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )
    manifest = _manifest_for_plan(plan, "parent", config)
    _write_plan_snapshot(project / ".exo" / "tl-loop", plan)
    parent_slices = _bind_initial_slices(
        _initial_slices(plan, config, state_root, "parent"),
        manifest,
    )
    create(
        "parent",
        {
            "plan_manifest": manifest.to_document(),
            "slices": parent_slices,
            "repository_identity": _repository_identity(config).to_document(),
        },
        root_dir=state_root,
    )
    parent_store = RunStore("parent", state_root)
    parent_state = parent_store.load()
    child_branch = f"main.{STAGE}"
    child_worktree = str(_sub_tl_worktree(config, state_root, "parent", stage))
    parked = replace(
        parent_state.slices[STAGE],
        status=SliceStatus.FAILED,
        branch=child_branch,
        worktree=child_worktree,
        dispatch_intent_id="sub-tl-dispatch",
        dispatch_agent_id=STAGE,
        dispatch_authoritative_event_seq=11,
        dispatch_last_boundary="sub_tl_started",
    )
    slices: dict[str, SliceState | Mapping[str, object]] = {STAGE: parked}
    first_stage = (STAGE, SIBLING) if parallel_sibling else (STAGE,)
    stages = [OrderedStageState(1, first_stage)]
    stages.extend(
        OrderedStageState(2 + index, (name,)) for index, name in enumerate(chain)
    )
    states = {STAGE: IntegrationLifecycle.FAILED}
    states.update({name: IntegrationLifecycle.RUNNING for name in chain})
    if sibling:
        stages.append(OrderedStageState(sibling_order, (SIBLING,)))
        states[SIBLING] = IntegrationLifecycle.RUNNING
    if parallel_sibling:
        states[SIBLING] = IntegrationLifecycle.RUNNING
    for task in tasks[1:]:
        slices[task.name] = replace(
            parent_state.slices[task.name],
            branch=f"main.{task.name}",
            worktree=str(_sub_tl_worktree(config, state_root, "parent", task)),
        )
    parent_store.checkpoint(
        RecursiveTLFailed("recursive child failed", scope_path=("parent",)),
        slices,
        parent_state.budgets,
        0,
        current_order=1,
        ordered_stages=tuple(stages),
        integration=IntegrationRuntimeState(sub_tl_states=states),
    )

    child_manifest = manifest.child_manifests[
        next(node.node_id for node in manifest.nodes if node.name == STAGE)
    ]
    child_config = replace(
        config,
        root_dir=parent_store.run_dir,
        run_id=STAGE,
        branch=child_branch,
        worktree=child_worktree,
        parent_branch="main",
        parent_run_id="parent",
        depth=1,
    )
    assert isinstance(stage.plan, WorkPlan)
    child_slices = _bind_initial_slices(
        _initial_slices(stage.plan, child_config, parent_store.run_dir, STAGE),
        child_manifest,
    )
    create(
        STAGE,
        {
            "plan_manifest": child_manifest.to_document(),
            "slices": child_slices,
            "owner_branch": child_branch,
            "owner_worktree": child_worktree,
            "parent_branch": "main",
            "parent_run_id": "parent",
            "session_mode": "continue",
            "repository_identity": _repository_identity(config).to_document(),
        },
        root_dir=parent_store.run_dir,
    )
    child_store = RunStore(STAGE, parent_store.run_dir)
    child_state = _ensure_canonical_scope(child_store.load(), child_manifest, child_store)
    child_state = _release_canonical_scope(child_state, child_store)
    _supervise_live_sub_tl(_ExitedProcess(), child_store, config)

    journal = EffectJournal(STAGE, child_store.run_dir / "action-journal.json")
    journal.append(
        EffectIntent(
            "resume_blocked_leaf",
            LEAF,
            _journal_arguments(worktree, intent),
            True,
        )
    )
    entry = journal.entries_for("resume_blocked_leaf", LEAF)[0]
    journal.resolve_by_key(
        str(entry["key"]),
        status="unknown",
        error="Timed out after 10.000s",
    )
    return _Fixture(
        project,
        parent_store,
        child_store,
        plan,
        config,
        owner_dir,
        worktree,
        journal,
        journal.entries_for("resume_blocked_leaf", LEAF)[0],
        intent,
    )


def _decision(fixture: _Fixture) -> OrderedRecoveryDecision | None:
    return _ordered_terminal_recovery_decision(
        fixture.parent_store.load(), fixture.plan, fixture.config, fixture.parent_store
    )


def _park(fixture: _Fixture) -> OrderedRecoveryDecision:
    decision = _decision(fixture)
    assert decision is not None and decision.held_launch_effect is not None
    _hold_ordered_recovery_gate(fixture.parent_store, decision)
    return decision


def _effect(fixture: _Fixture) -> HeldLaunchEffect:
    parked = _park(fixture)
    assert parked.held_launch_effect is not None
    return parked.held_launch_effect


def _answer_skip(
    fixture: _Fixture,
    *,
    policy: SkipDependencyPolicy,
    dependents: Sequence[str] = (),
    reason: str = SKIP_REASON,
) -> None:
    effect = _effect(fixture)
    fixture.parent_store.answer_gate(
        effect.skip_gate,
        GateStatus.APPROVED,
        reason=reason,
        dependency_policy=policy,
        named_dependents=dependents,
    )


def _skipped(fixture: _Fixture) -> tuple[OrderedRecoveryDecision, RunState]:
    decision = _decision(fixture)
    assert decision is not None
    assert decision.skipped is True, decision.reason
    return decision, _reopen_after_ordered_recovery(
        fixture.parent_store.load(), fixture.plan, fixture.parent_store, decision
    )


def _journal_state(fixture: _Fixture) -> list[dict[str, object]]:
    payload = json.loads(
        (fixture.child_store.run_dir / "action-journal.json").read_text(encoding="utf-8")
    )
    assert isinstance(payload, list)
    return payload


def _resolutions(fixture: _Fixture) -> list[dict[str, object]]:
    return [
        entry
        for entry in _journal_state(fixture)
        if entry.get("kind") == "launch_resolution"
    ]


def _outcomes(fixture: _Fixture) -> list[object]:
    return [item.get("outcome") for item in _resolutions(fixture)]


def test_a_skip_marks_the_stage_skipped_and_releases_the_next_stage(
    tmp_path: Path,
) -> None:
    fixture = _run(tmp_path, policy=SkipDependencyPolicy.PROCEED)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)

    decision, state = _skipped(fixture)

    skipped = state.slices[STAGE]
    assert skipped.status is SliceStatus.SKIPPED
    assert skipped.park_cause is None
    assert isinstance(skipped.park_audit, Mapping)
    assert skipped.park_audit["recovery_action"] == "held_launch_effect_skip"
    assert skipped.park_audit["reason"] == SKIP_REASON
    assert isinstance(state.recursive_fsm, TLRunning)
    assert state.current_order == 2
    assert state.slices[NEXT].status is SliceStatus.PENDING
    assert "skipped" in decision.reason
    assert decision.task_name is None
    # The decision is linked to the original journal entry, and that entry keeps
    # its bytes and status.
    resolutions = _resolutions(fixture)
    assert [item["outcome"] for item in resolutions] == ["operator_skipped"]
    record = resolutions[0]
    assert record["resolves_key"] == fixture.entry["key"]
    assert record["resolves_intent_id"] == fixture.intent
    assert record["gate_name"] == decision.gate_name
    assert isinstance(record["decision"], Mapping)
    assert record["decision"]["reason"] == SKIP_REASON
    assert record["decision"]["dependency_policy"] == "proceed"
    assert fixture.journal.entry_by_key(str(fixture.entry["key"])) == dict(fixture.entry)
    assert fixture.journal.pending_entries() == []


def test_a_the_live_controller_consumes_the_skip_once_and_advances(
    tmp_path: Path,
) -> None:
    """The shipped controller path applies the skip and moves to stage two.

    The synthetic stage-two child has no live harness here, so this asserts the
    skip outcome and the advanced order, not the later stage's own result.
    """
    fixture = _run(tmp_path)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)
    attempts = fixture.parent_store.load().slices[STAGE].attempts
    intent = fixture.parent_store.load().slices[STAGE].dispatch_intent_id

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
        ),
        root_dir=fixture.parent_store.root_dir,
        budgets=BudgetLedger(0, 0),
    )

    state = fixture.parent_store.load()
    assert state.slices[STAGE].status is SliceStatus.SKIPPED
    assert state.slices[STAGE].attempts == attempts
    assert state.slices[STAGE].dispatch_intent_id == intent
    assert state.current_order == 2
    assert _outcomes(fixture) == ["operator_skipped"]


def test_a_held_launch_skip_keeps_the_original_journal_entry_and_ledger(
    tmp_path: Path,
) -> None:
    fixture = _run(tmp_path)
    before_journal = (fixture.child_store.run_dir / "action-journal.json").read_text(
        encoding="utf-8"
    )
    before_ledger = (
        fixture.project / ".exo" / "ledger" / "segments" / "segment-000000000001.jsonl"
    ).read_text(encoding="utf-8")
    before_digest = _snapshot_digest(fixture)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)

    _skipped(fixture)

    after_journal = (fixture.child_store.run_dir / "action-journal.json").read_text(
        encoding="utf-8"
    )
    after_rows = json.loads(after_journal)
    before_rows = json.loads(before_journal)
    assert after_rows[: len(before_rows)] == before_rows
    assert len(after_rows) == len(before_rows) + 1
    assert after_journal.startswith(before_journal.rstrip()[:-1])
    assert (
        (fixture.project / ".exo" / "ledger" / "segments" / "segment-000000000001.jsonl")
        .read_text(encoding="utf-8")
        == before_ledger
    )
    assert _snapshot_digest(fixture) == before_digest


def _snapshot_digest(fixture: _Fixture) -> str | None:
    path = fixture.project / ".exo" / "tl-loop" / "plan.snapshot.sha256"
    return path.read_text(encoding="ascii") if path.is_file() else None


def test_b_proceed_policy_leaves_the_dependent_eligible(tmp_path: Path) -> None:
    fixture = _run(tmp_path, policy=SkipDependencyPolicy.PROCEED)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)

    _decision_state = _skipped(fixture)[1]

    assert _decision_state.slices[NEXT].status is SliceStatus.PENDING
    assert _decision_state.current_order == 2


def test_b_block_policy_holds_every_dependent_with_a_diagnostic(
    tmp_path: Path,
) -> None:
    fixture = _run(tmp_path, policy=SkipDependencyPolicy.BLOCK)
    _answer_skip(fixture, policy=SkipDependencyPolicy.BLOCK)

    state = _skipped(fixture)[1]

    dependent = state.slices[NEXT]
    assert state.slices[STAGE].status is SliceStatus.SKIPPED
    assert dependent.status is SliceStatus.BLOCKED
    assert dependent.blocked_by == STAGE
    assert isinstance(dependent.park_audit, Mapping)
    assert "dependency policy" in str(dependent.park_audit["reason"])
    assert str(dependent.park_audit["gate_name"]).endswith("-skip")


def test_b_require_named_runs_only_the_named_dependent(tmp_path: Path) -> None:
    fixture = _run(
        tmp_path,
        policy=SkipDependencyPolicy.REQUIRE_NAMED,
        sibling=True,
    )
    _answer_skip(
        fixture,
        policy=SkipDependencyPolicy.REQUIRE_NAMED,
        dependents=(NEXT,),
    )

    state = _skipped(fixture)[1]

    assert state.slices[NEXT].status is SliceStatus.PENDING
    unnamed = state.slices[SIBLING]
    assert unnamed.status is SliceStatus.BLOCKED
    assert isinstance(unnamed.park_audit, Mapping)
    assert "unnamed" in str(unnamed.park_audit["reason"])


def test_b_require_named_with_no_names_holds_the_slice(tmp_path: Path) -> None:
    fixture = _run(tmp_path, policy=SkipDependencyPolicy.REQUIRE_NAMED)
    _answer_skip(fixture, policy=SkipDependencyPolicy.REQUIRE_NAMED, dependents=())

    decision = _decision(fixture)

    assert decision is not None
    assert decision.skipped is False
    assert decision.recoverable is False
    assert "names no dependent" in decision.reason
    assert _outcomes(fixture) == []
    state = fixture.parent_store.load()
    assert state.slices[STAGE].status is SliceStatus.PARKED
    assert state.slices[NEXT].status is SliceStatus.PENDING


def test_b_a_skip_answer_may_not_widen_the_declared_policy(tmp_path: Path) -> None:
    fixture = _run(tmp_path, policy=SkipDependencyPolicy.BLOCK)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)

    decision = _decision(fixture)

    assert decision is not None
    assert decision.skipped is False
    assert decision.recoverable is False
    assert "widens the declared dependency policy" in decision.reason
    assert _outcomes(fixture) == []
    assert fixture.parent_store.load().slices[STAGE].status is SliceStatus.PARKED


def test_b_naming_a_non_dependent_is_refused(tmp_path: Path) -> None:
    fixture = _run(tmp_path, policy=SkipDependencyPolicy.REQUIRE_NAMED)
    _answer_skip(
        fixture,
        policy=SkipDependencyPolicy.REQUIRE_NAMED,
        dependents=("not-a-dependent",),
    )

    decision = _decision(fixture)

    assert decision is not None
    assert decision.skipped is False
    assert "do not depend on the skipped stage" in decision.reason
    assert _outcomes(fixture) == []


def test_b_a_skip_without_a_recorded_policy_is_refused(tmp_path: Path) -> None:
    fixture = _run(tmp_path, policy=SkipDependencyPolicy.PROCEED)
    effect = _effect(fixture)
    fixture.parent_store.answer_gate(
        effect.skip_gate,
        GateStatus.APPROVED,
        reason=SKIP_REASON,
    )

    decision = _decision(fixture)

    assert decision is not None
    assert decision.skipped is False
    assert "records no dependency policy" in decision.reason
    assert _outcomes(fixture) == []


def test_c_reject_parks_without_recording_or_advancing(tmp_path: Path) -> None:
    fixture = _run(tmp_path)
    effect = _effect(fixture)
    fixture.parent_store.answer_gate(effect.skip_gate, GateStatus.REJECTED)

    decision = _decision(fixture)

    assert decision is not None
    assert decision.recoverable is False
    assert decision.gate_status is GateStatus.REJECTED
    assert _outcomes(fixture) == []
    state = fixture.parent_store.load()
    assert state.slices[STAGE].status is SliceStatus.PARKED
    assert state.slices[NEXT].status is SliceStatus.PENDING
    assert state.current_order == 1


def test_c_abandon_and_skip_records_both_and_still_advances(tmp_path: Path) -> None:
    fixture = _run(tmp_path)
    effect = _effect(fixture)
    fixture.parent_store.answer_gate(effect.abandon_gate, GateStatus.APPROVED)
    # Abandon alone leaves the slice parked, exactly as before.
    abandoned = _decision(fixture)
    assert abandoned is not None and abandoned.recoverable is False
    assert _outcomes(fixture) == ["operator_abandoned"]
    assert fixture.parent_store.load().slices[STAGE].status is SliceStatus.PARKED

    fixture.parent_store.answer_gate(
        effect.skip_gate,
        GateStatus.APPROVED,
        reason=SKIP_REASON,
        dependency_policy=SkipDependencyPolicy.PROCEED,
    )
    decision, state = _skipped(fixture)

    assert _outcomes(fixture) == ["operator_abandoned", "operator_skipped"]
    assert decision.gate_name == effect.skip_gate
    assert state.slices[STAGE].status is SliceStatus.SKIPPED
    assert state.current_order == 2


def test_c_retry_is_unchanged_and_does_not_skip(tmp_path: Path) -> None:
    fixture = _run(tmp_path)
    effect = _effect(fixture)
    fixture.parent_store.answer_gate(effect.retry_gate, GateStatus.APPROVED)

    decision = _decision(fixture)

    assert decision is not None
    assert decision.recoverable is True
    assert decision.authorized is True
    assert decision.skipped is False
    assert _outcomes(fixture) == ["operator_abandoned", "retry_authorized"]
    state = fixture.parent_store.load()
    assert state.slices[STAGE].status is SliceStatus.PARKED
    reopened = _reopen_ordered_scope(
        state, fixture.plan, fixture.parent_store, STAGE, authorized=True
    )
    assert reopened.slices[STAGE].status is SliceStatus.SPAWNED
    assert reopened.current_order == 1


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("live_owner", lambda f: _set_invocation(f, status="running")),
        ("launch_row", lambda f: _write_ledger(f.project, _launch_row(f.intent))),
        (
            "duplicate_launch_rows",
            lambda f: _write_ledger(
                f.project, _launch_row(f.intent), {**_launch_row(f.intent), "run_seq": 14}
            ),
        ),
        ("successor", lambda f: _add_successor(f)),
        (
            "forged_intent",
            lambda f: f.journal.resolve_by_key(
                str(f.entry["key"]),
                arguments={
                    **dict(f.entry["arguments"]),
                    "dispatch_intent_id": "blocked-resume:" + "f" * 64,
                },
            ),
        ),
    ],
)
def test_d_unproven_skip_answers_stay_held(
    tmp_path: Path, case: str, mutate: Callable[[_Fixture], None]
) -> None:
    del case
    fixture = _run(tmp_path)
    answer_effect = _effect(fixture)
    fixture.parent_store.answer_gate(
        answer_effect.skip_gate,
        GateStatus.APPROVED,
        reason=SKIP_REASON,
        dependency_policy=SkipDependencyPolicy.PROCEED,
    )
    mutate(fixture)

    decision = _decision(fixture)

    assert decision is not None
    assert decision.skipped is False
    assert decision.recoverable is False
    assert _outcomes(fixture) == []
    state = fixture.parent_store.load()
    assert state.slices[STAGE].status is SliceStatus.PARKED
    assert state.current_order == 1


def test_d_a_foreign_or_unapproved_skip_gate_never_skips(tmp_path: Path) -> None:
    fixture = _run(tmp_path)
    effect = _effect(fixture)
    fixture.parent_store.set_gate(GENERIC_GATE, GateStatus.APPROVED)
    foreign = effect.skip_gate.replace(
        effect.intent_id.rsplit(":", 1)[-1][:12], "0" * 12
    )
    fixture.parent_store.set_gate(foreign, GateStatus.APPROVED)

    decision = _decision(fixture)

    assert decision is not None
    assert decision.skipped is False
    assert _outcomes(fixture) == []
    assert fixture.parent_store.load().slices[STAGE].status is SliceStatus.PARKED


def test_d_conflicting_answers_consume_nothing(tmp_path: Path) -> None:
    fixture = _run(tmp_path)
    effect = _effect(fixture)
    fixture.parent_store.answer_gate(effect.retry_gate, GateStatus.APPROVED)
    fixture.parent_store.answer_gate(
        effect.skip_gate,
        GateStatus.APPROVED,
        reason=SKIP_REASON,
        dependency_policy=SkipDependencyPolicy.PROCEED,
    )

    decision = _decision(fixture)

    assert decision is not None
    assert decision.recoverable is False
    assert decision.skipped is False
    assert "conflicting operator answers" in decision.reason
    assert _outcomes(fixture) == []


def test_e_restart_after_skip_decides_once_and_never_relaunches(
    tmp_path: Path,
) -> None:
    fixture = _run(tmp_path)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)
    first = _skipped(fixture)[1]
    attempts = first.slices[STAGE].attempts
    intent = first.slices[STAGE].dispatch_intent_id
    before = (fixture.child_store.run_dir / "action-journal.json").read_text(
        encoding="utf-8"
    )

    repeat = _decision(fixture)

    assert repeat is None, "a running scope re-derives no ordered recovery decision"
    after = (fixture.child_store.run_dir / "action-journal.json").read_text(
        encoding="utf-8"
    )
    assert after == before
    state = fixture.parent_store.load()
    assert state.slices[STAGE].status is SliceStatus.SKIPPED
    assert state.slices[STAGE].attempts == attempts
    assert state.slices[STAGE].dispatch_intent_id == intent
    assert _outcomes(fixture) == ["operator_skipped"]
    # Dependents were evaluated once: the policy outcome is still one hold.
    assert state.slices[NEXT].status in {SliceStatus.PENDING, SliceStatus.BLOCKED}


def test_e_continue_after_a_skip_is_idempotent(tmp_path: Path) -> None:
    fixture = _run(tmp_path)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)
    _skipped(fixture)
    journal_before = (fixture.child_store.run_dir / "action-journal.json").read_text(
        encoding="utf-8"
    )
    attempts = fixture.parent_store.load().slices[STAGE].attempts

    effects = ReadOnlyEffectClient(
        EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
    )
    for _ in range(2):
        state = driver._reconcile_continued_root_failure(
            fixture.plan,
            fixture.parent_store.load(),
            replace(fixture.config, active=False, session_mode="continue"),
            effects,
            fixture.parent_store,
            [],
            "recursive child failed",
        )

    assert isinstance(state.recursive_fsm, TLRunning)
    assert state.current_order == 2
    assert state.slices[STAGE].status is SliceStatus.SKIPPED
    assert state.slices[STAGE].attempts == attempts
    assert (
        (fixture.child_store.run_dir / "action-journal.json").read_text(encoding="utf-8")
        == journal_before
    )
    assert _outcomes(fixture) == ["operator_skipped"]


def test_f_concurrent_ticks_skip_exactly_once(tmp_path: Path) -> None:
    fixture = _run(tmp_path)
    effect = _effect(fixture)
    fixture.parent_store.answer_gate(
        effect.skip_gate,
        GateStatus.APPROVED,
        reason=SKIP_REASON,
        dependency_policy=SkipDependencyPolicy.PROCEED,
    )

    def evaluate(_: int) -> OrderedRecoveryDecision | None:
        return _decision(fixture)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(evaluate, range(8)))

    assert any(result is not None and result.skipped for result in results)
    outcomes = _outcomes(fixture)
    assert outcomes.count("operator_skipped") == 1
    assert fixture.parent_store.load().slices[STAGE].status is SliceStatus.SKIPPED


def test_h_skipping_a_nested_stage_leaves_siblings_and_outer_stages_alone(
    tmp_path: Path,
) -> None:
    fixture = _run(tmp_path, sibling=True)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)

    state = _skipped(fixture)[1]

    assert state.slices[STAGE].status is SliceStatus.SKIPPED
    assert state.slices[NEXT].status is SliceStatus.PENDING
    assert state.slices[SIBLING].status is SliceStatus.PENDING
    assert state.current_order == 2


def test_i_recorded_held_shape_replays_through_production_classification(
    tmp_path: Path,
) -> None:
    """The recorded held-stage journal and gate state drive the skip decision."""
    fixture = _run(tmp_path)
    stored = _journal_state(fixture)[0]
    assert stored["operation"] == "resume_blocked_leaf"
    assert stored["status"] == "unknown"
    assert stored["error"] == "Timed out after 10.000s"

    classification = classify_held_launch_effect(
        STAGE,
        fixture.child_store,
        project_root=fixture.project,
        ledger_run_id=LEDGER_RUN_ID,
        expected_parent_branch=CHILD_BRANCH,
    )
    assert classification.reason is None
    assert classification.effect is not None
    assert classification.effect.intent_id == fixture.intent
    assert classification.effect.journal_index == 0
    assert classification.effect.skip_gate.endswith("-skip")

    effect = _effect(fixture)
    assert effect.skip_gate == classification.effect.skip_gate
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)
    decision, state = _skipped(fixture)
    assert decision.gate_name == effect.skip_gate
    assert state.slices[STAGE].status is SliceStatus.SKIPPED


def test_j_skip_never_edits_the_snapshot_digest_or_the_child_checkpoint(
    tmp_path: Path,
) -> None:
    fixture = _run(tmp_path)
    child_bytes = (fixture.child_store.run_dir / "run.json").read_bytes()
    digest_bytes = (fixture.project / ".exo" / "tl-loop" / "plan.snapshot.sha256").read_bytes()
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)

    _skipped(fixture)

    record = _resolutions(fixture)[0]
    assert isinstance(record["decision"], Mapping)
    assert record["decision"]["plan_snapshot_sha256"] == digest_bytes.decode().strip()
    assert (fixture.child_store.run_dir / "run.json").read_bytes() == child_bytes
    assert (
        fixture.project / ".exo" / "tl-loop" / "plan.snapshot.sha256"
    ).read_bytes() == digest_bytes


# ---------------------------------------------------------------------------
# Chainlink issue semantics around a skipped stage

_POST_MERGE_EVIDENCE = {
    "repository": "https://forge.example.com/org/repo.git",
    "parent_branch": "main",
    "head_sha": "head-next",
    "merge_journal_id": "merge-next",
    "lane_epoch": "1",
    "parent_commit_sha": "parent-next",
}


def _with_post_merge(
    store: RunStore,
    state: RunState,
    slice_id: str,
    *,
    status: SliceStatus,
    issue_id: int | None,
    pr_number: int | None,
    phase: PostMergePhase,
) -> RunState:
    """Persist one slice carrying a post-merge boundary for a boundary test."""
    current = state.slices[slice_id]
    evidence = {
        **_POST_MERGE_EVIDENCE,
        "child_id": slice_id,
        **({"pr_number": str(pr_number)} if pr_number is not None else {}),
    }
    updated = replace(
        current,
        status=status,
        chainlink_issue_id=issue_id,
        pr_number=pr_number,
        base_ref="main",
        post_merge=PostMergeState(phase, evidence),
    )
    # A fresh (non-recursive) run carries its lifecycle in the legacy phase.
    fsm = state.recursive_fsm if state.recursive_fsm is not None else state.fsm
    return store.checkpoint(
        cast(Any, fsm),
        {**state.slices, slice_id: updated},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )


def _advance_issue_close_twice(
    fixture: _Fixture,
    state: RunState,
    *,
    slice_id: str,
    pr_number: int,
    issue_id: int,
) -> tuple[RunState, IssueCloseTransport]:
    transport = IssueCloseTransport(issue_id)
    effects = EffectClient(transport)
    pending = _advance_post_merge_boundary(
        state,
        slice_id,
        pr_number,
        fixture.config,
        effects,
        [],
        fixture.parent_store,
    )
    confirmed = _advance_post_merge_boundary(
        pending,
        slice_id,
        pr_number,
        fixture.config,
        effects,
        [],
        fixture.parent_store,
    )
    return confirmed, transport


def test_k_a_skip_does_not_close_the_skipped_stages_linked_issue(
    tmp_path: Path,
) -> None:
    fixture = _run(tmp_path, linked_issues=True)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)

    state = _skipped(fixture)[1]

    assert state.slices[STAGE].status is SliceStatus.SKIPPED
    assert state.slices[STAGE].chainlink_issue_id == STAGE_ISSUE
    assert state.slices[STAGE].post_merge is None
    transport = IssueCloseTransport(STAGE_ISSUE)
    effects = EffectClient(transport)
    _advance_post_merge_boundary(
        state,
        STAGE,
        0,
        fixture.config,
        effects,
        [],
        fixture.parent_store,
    )
    assert transport.calls == []
    assert fixture.parent_store.load().slices[STAGE].status is SliceStatus.SKIPPED


def test_k_a_skipped_stage_never_runs_the_post_merge_issue_close_path(
    tmp_path: Path,
) -> None:
    """A skipped stage carries no post-merge boundary and derives no recovery."""
    from tl_loop.loop.reconcile import Quiescent, _derive_slice_action

    fixture = _run(tmp_path, linked_issues=True)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)
    state = _skipped(fixture)[1]

    current = state.slices[STAGE]
    assert current.status is SliceStatus.SKIPPED
    assert current.post_merge is None
    assert isinstance(_derive_slice_action(current), Quiescent)
    transport = IssueCloseTransport(STAGE_ISSUE)
    effects = EffectClient(transport)
    unchanged = _advance_post_merge_boundary(
        state,
        STAGE,
        0,
        fixture.config,
        effects,
        [],
        fixture.parent_store,
    )
    assert unchanged.slices[STAGE] == current
    assert transport.calls == []
    assert not fixture.parent_store.load().slices[STAGE].post_merge


def test_k_the_next_stages_linked_issue_closes_independently(
    tmp_path: Path,
) -> None:
    fixture = _run(tmp_path, linked_issues=True)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)
    state = _skipped(fixture)[1]
    state = _with_post_merge(
        fixture.parent_store,
        state,
        NEXT,
        status=SliceStatus.MERGED,
        issue_id=NEXT_ISSUE,
        pr_number=77,
        phase=PostMergePhase.PARENT_BRANCH_SYNCED,
    )

    confirmed, transport = _advance_issue_close_twice(
        fixture, state, slice_id=NEXT, pr_number=77, issue_id=NEXT_ISSUE
    )

    receipt = confirmed.slices[NEXT].post_merge
    assert receipt is not None
    assert receipt.phase is PostMergePhase.ISSUE_CLOSE_CONFIRMED
    close_calls = [
        arguments for name, arguments in transport.calls if name == "chainlink_issue_close"
    ]
    assert [arguments["issue_id"] for arguments in close_calls] == [NEXT_ISSUE]
    assert confirmed.slices[STAGE].chainlink_issue_id == STAGE_ISSUE
    assert confirmed.slices[STAGE].status is SliceStatus.SKIPPED


def test_k_the_live_controller_does_not_close_the_skipped_issue(
    tmp_path: Path,
) -> None:
    """The shipped loop path never issues a close for the skipped stage."""
    fixture = _run(tmp_path, linked_issues=True)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)

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
        ),
        root_dir=fixture.parent_store.root_dir,
        budgets=BudgetLedger(0, 0),
    )

    state = fixture.parent_store.load()
    assert state.slices[STAGE].status is SliceStatus.SKIPPED
    assert state.slices[STAGE].chainlink_issue_id == STAGE_ISSUE
    assert _outcomes(fixture) == ["operator_skipped"]
    assert state.current_order == 2


def test_k_an_unlinked_stage_is_unaffected_by_another_stages_skip(
    tmp_path: Path,
) -> None:
    fixture = _run(tmp_path, sibling=True)
    _answer_skip(fixture, policy=SkipDependencyPolicy.PROCEED)

    state = _skipped(fixture)[1]

    assert state.slices[STAGE].chainlink_issue_id is None
    assert state.slices[SIBLING].chainlink_issue_id is None
    assert state.slices[SIBLING].status is SliceStatus.PENDING
    transport = IssueCloseTransport(0)
    effects = EffectClient(transport)
    _advance_post_merge_boundary(
        state,
        STAGE,
        0,
        fixture.config,
        effects,
        [],
        fixture.parent_store,
    )
    assert transport.calls == []


def _set_invocation(fixture: _Fixture, **updates: object) -> None:
    path = fixture.owner_dir / "invocation.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value.update(updates)
    path.write_text(json.dumps(value), encoding="utf-8")


def _add_successor(fixture: _Fixture) -> None:
    path = fixture.owner_dir / "invocation.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value.update({"successor_invocation_id": "opencode-successor-invocation"})
    path.write_text(json.dumps(value), encoding="utf-8")
    # A successor invocation may be proven from the ledger rather than the
    # owner record, so seed the authoritative row too.
    _write_ledger(fixture.project, _launch_row(fixture.intent))
