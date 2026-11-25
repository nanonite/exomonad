"""Operator resolution for a held unknown launch effect in a child sub-TL.

The fixture is a redacted copy of the live shape: a child controller journaled
``resume_blocked_leaf`` as ``unknown`` after its client timed out, and the host
ledger holds no launch, refusal or failure row for that intent. The child's
fail-closed hold is correct; the parent must park only that child behind a
durable operator gate, not fail the whole root controller.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import TransportClient
from tl_loop.events.envelope import EventEnvelope
from tl_loop.events.reader import LedgerSnapshot, SequenceStatus
from tl_loop.fsm.scope import (
    TLFailed,
)
from tl_loop.loop import driver
from tl_loop.loop.blocked_leaf_resume import (
    blocked_resume_intent_id,
    resolve_owner_for_resume_retry,
)
from tl_loop.loop.driver import (
    EffectIntent,
    LeafTask,
    OrderedRecoveryDecision,
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    _bind_initial_slices,
    _ensure_canonical_scope,
    _hold_ordered_recovery_gate,
    _initial_slices,
    _manifest_for_plan,
    _ordered_terminal_recovery_decision,
    _release_canonical_scope,
    _reopen_ordered_scope,
    _run_sub_tl_batch,
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
    RepositoryIdentity,
    RunState,
    SliceStatus,
)
from tl_loop.state.store import RunStore
from tl_loop.tests.checkpoint_fixtures import create

CHILD = "child"
SIBLING = "sibling"
LEAF = "leaf"
OWNER = f"{LEAF}-opencode"
PARENT_BRANCH = "main.child"
LEAF_BRANCH = f"{PARENT_BRANCH}.{OWNER}"
LEDGER_RUN_ID = "parent"
DISPATCH_STARTED_AT = 1000.0
GENERIC_GATE = "tl-ordered-child-recovery-child"


class EmptyQueue:
    """Event source that must never be consumed by these fixtures."""

    def get(self, timeout: float | None = None) -> EventEnvelope:
        del timeout
        raise RuntimeError("held-effect tests should not consume events")

    def acknowledge(self, event: EventEnvelope) -> int:
        del event
        raise AssertionError("held-effect tests should not acknowledge events")

    def capture_snapshot(self, cursor: int) -> LedgerSnapshot:
        return LedgerSnapshot((), cursor, SequenceStatus.UNKNOWN, (), None)


class _ExitedProcess:
    """Stand-in for a child controller that exited before authoritative resolution."""

    exitcode = 1

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        del timeout

    def terminate(self) -> None:
        return None


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


def _run_git(*arguments: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
    ).stdout


def _owner_fingerprint(worktree: Path) -> str:
    import hashlib

    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    return "sha256:" + hashlib.sha256(status).hexdigest()


def _seed_owner(project: Path) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
    worktree = project / ".exo" / "worktrees" / OWNER
    worktree.mkdir(parents=True)
    _run_git("init", "-q", "-b", LEAF_BRANCH, str(worktree))
    owner_dir = project / ".exo" / "agents" / OWNER
    owner_dir.mkdir(parents=True)
    (owner_dir / "active_issue").write_text("814\n", encoding="utf-8")
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


def _derive_intent(project: Path, worktree: Path, owner_dir: Path) -> str:
    owner, _successor = resolve_owner_for_resume_retry(
        project,
        slice_id=LEAF,
        issue_id=814,
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
    del owner_dir
    return blocked_resume_intent_id(owner)


def _journal_arguments(worktree: Path, intent: str) -> dict[str, object]:
    return {
        "chainlink_issue_id": 814,
        "expected_invocation_id": "opencode-prior-invocation",
        "expected_branch": LEAF_BRANCH,
        "expected_worktree_fingerprint": _owner_fingerprint(worktree),
        "task": "Recover the substitution-model Kani stage for issue 814",
        "human_approved": True,
        "owner_agent_name": OWNER,
        "prior_generation": 1,
        "expected_worktree": str(worktree),
        "expected_worktree_recorded": str(worktree),
        "dispatch_intent_id": intent,
        "dispatch_attempt": 2,
        "dispatch_started_at": DISPATCH_STARTED_AT,
        "dispatch_generation": 3,
        "dispatch_ledger_floor": 0,
        "controller_epoch": "epoch-1",
        "route_reconciliation_source": "proven_preexecution_retry",
        "runtime": "opencode",
        "model": "opencode-go/deepseek-v4.1-flash",
        "error": "Timed out after 10.000s",
    }


def _write_ledger(project: Path, *rows: dict[str, object]) -> None:
    segments = project / ".exo" / "ledger" / "segments"
    segments.mkdir(parents=True, exist_ok=True)
    segment = segments / "segment-000000000001.jsonl"
    segment.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


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


def _failed_ordered_run(
    tmp_path: Path,
    *,
    extra_ledger_rows: Sequence[dict[str, object]] = (),
    with_sibling: bool = False,
    sibling_status: SliceStatus = SliceStatus.PENDING,
) -> _Fixture:
    """Build the redacted live shape through production fixture helpers."""
    project = tmp_path
    state_root = tmp_path / "state"
    (project / ".exo").mkdir(parents=True, exist_ok=True)
    owner_dir, worktree, _identity, _invocation = _seed_owner(project)
    intent = _derive_intent(project, worktree, owner_dir)
    _write_ledger(project)

    child_plan = WorkPlan(leaves=(LeafTask(LEAF, "implement the child change"),))
    tasks = [SubTLTask(CHILD, child_plan, order=1)]
    if with_sibling:
        tasks.append(SubTLTask(SIBLING, child_plan, order=1))
    plan = WorkPlan(sub_tls=tuple(tasks))
    config = TLLoopConfig(
        root_dir=state_root,
        project_root=project,
        branch="main",
        ledger_run_id=LEDGER_RUN_ID,
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
            "repository_identity": _repository_identity(config).to_document(),
        },
        root_dir=state_root,
    )
    parent_store = RunStore("parent", state_root)
    parent_state = parent_store.load()
    child = parent_state.slices[CHILD]
    child_branch = "main.child"
    child_worktree = str(_sub_tl_worktree(config, state_root, "parent", plan.sub_tls[0]))
    child = replace(
        child,
        status=SliceStatus.FAILED,
        branch=child_branch,
        worktree=child_worktree,
        dispatch_intent_id="sub-tl-dispatch",
        dispatch_agent_id=CHILD,
        dispatch_authoritative_event_seq=11,
        dispatch_last_boundary="sub_tl_started",
    )
    slices = {"child": child}
    if with_sibling:
        sibling = replace(
            parent_state.slices[SIBLING],
            status=sibling_status,
            branch="main.sibling",
            dispatch_intent_id="sibling-dispatch",
            dispatch_agent_id=SIBLING,
            dispatch_authoritative_event_seq=12,
            dispatch_last_boundary="sub_tl_started",
        )
        slices[SIBLING] = sibling
    parent_store.checkpoint(
        TLFailed("recursive child failed"),
        slices,
        parent_state.budgets,
        0,
        current_order=1,
        ordered_stages=(OrderedStageState(1, tuple(task.name for task in tasks)),),
        integration=IntegrationRuntimeState(
            sub_tl_states={
                CHILD: IntegrationLifecycle.FAILED,
                SIBLING: (
                    IntegrationLifecycle.CHILDREN_MERGED
                    if sibling_status is SliceStatus.MERGED
                    else IntegrationLifecycle.RUNNING
                ),
            }
            if with_sibling
            else {CHILD: IntegrationLifecycle.FAILED}
        ),
    )

    child_manifest = manifest.child_manifests[
        next(node.node_id for node in manifest.nodes if node.name == CHILD)
    ]
    child_config = replace(
        config,
        root_dir=parent_store.run_dir,
        run_id=CHILD,
        branch=child_branch,
        worktree=child_worktree,
        parent_branch="main",
        parent_run_id="parent",
        depth=1,
    )
    child_slices = _bind_initial_slices(
        _initial_slices(child_plan, child_config, parent_store.run_dir, CHILD),
        child_manifest,
    )
    create(
        CHILD,
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
    child_store = RunStore(CHILD, parent_store.run_dir)
    child_state = _ensure_canonical_scope(child_store.load(), child_manifest, child_store)
    child_state = _release_canonical_scope(child_state, child_store)
    _supervise_live_sub_tl(_ExitedProcess(), child_store, config)
    if extra_ledger_rows:
        _write_ledger(project, *extra_ledger_rows)

    journal = EffectJournal(CHILD, child_store.run_dir / "action-journal.json")
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
    entries = journal.entries_for("resume_blocked_leaf", LEAF)
    assert len(entries) == 1
    return _Fixture(
        project,
        parent_store,
        child_store,
        plan,
        config,
        owner_dir,
        worktree,
        journal,
        entries[0],
        intent,
    )


def _repository_identity(config: TLLoopConfig) -> RepositoryIdentity:
    identity = config.repository_identity
    if identity is None:
        raise RuntimeError("fixture has no repository identity")
    return identity


def _decision(fixture: _Fixture) -> OrderedRecoveryDecision | None:
    return _ordered_terminal_recovery_decision(
        fixture.parent_store.load(), fixture.plan, fixture.config, fixture.parent_store
    )


def _park(fixture: _Fixture) -> tuple[OrderedRecoveryDecision, RunState]:
    decision = _decision(fixture)
    assert decision is not None and decision.held_launch_effect is not None
    return decision, _hold_ordered_recovery_gate(fixture.parent_store, decision)


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


def _change_owner_invocation(fixture: _Fixture, **updates: object) -> None:
    path = fixture.owner_dir / "invocation.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value.update(updates)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_a_held_unknown_effect_parks_the_child_without_failing_the_root(
    tmp_path: Path,
) -> None:
    fixture = _failed_ordered_run(tmp_path, with_sibling=True)

    result = run_tl_loop(
        "parent",
        fixture.plan,
        EmptyQueue(),
        ReadOnlyEffectClient(
            EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
        ),
        config=replace(fixture.config, active=False),
        root_dir=fixture.parent_store.root_dir,
        budgets=BudgetLedger(0, 0),
    )

    state = fixture.parent_store.load()
    child = state.slices[CHILD]
    assert child.status is SliceStatus.PARKED
    assert child.park_cause is not None
    assert isinstance(child.park_audit, Mapping)
    assert child.park_audit["recovery_action"] == "held_launch_effect"
    parked_gates = {gate.name: gate.status for gate in state.gates}
    decision = _decision(fixture)
    assert decision is not None and decision.held_launch_effect is not None
    effect = decision.held_launch_effect
    assert parked_gates[effect.abandon_gate] is GateStatus.PENDING
    assert parked_gates[effect.retry_gate] is GateStatus.PENDING
    # The sibling is untouched and the root did not become a brand new failure.
    assert state.slices[SIBLING].status is SliceStatus.PENDING
    assert "unresolved launch effect" in str(result.diagnostics["ordered_recovery"])
    assert effect.intent_id in effect.diagnostic
    assert f"journal index {effect.journal_index}" in effect.diagnostic
    assert "last host evidence none" in effect.diagnostic
    assert "Available operator actions" in effect.diagnostic


def test_a_retry_gate_appends_one_abandon_and_one_authorized_retry(
    tmp_path: Path,
) -> None:
    fixture = _failed_ordered_run(tmp_path)
    decision: OrderedRecoveryDecision | None = _park(fixture)[0]
    assert decision is not None
    effect = decision.held_launch_effect
    assert effect is not None
    fixture.parent_store.answer_gate(effect.retry_gate, GateStatus.APPROVED)
    before_entry = dict(fixture.entry)

    decision = _decision(fixture)

    assert decision is not None
    assert decision.recoverable is True
    assert decision.authorized is True
    resolutions = _resolutions(fixture)
    assert [item["outcome"] for item in resolutions] == [
        "operator_abandoned",
        "retry_authorized",
    ]
    assert all(item["resolves_intent_id"] == fixture.intent for item in resolutions)
    assert all(item["resolves_key"] == before_entry["key"] for item in resolutions)
    # The original unknown entry keeps its bytes and status.
    original = fixture.journal.entry_by_key(str(before_entry["key"]))
    assert dict(original or {}) == before_entry
    assert fixture.journal.pending_entries() == []
    reopened = _reopen_ordered_scope(
        fixture.parent_store.load(),
        fixture.plan,
        fixture.parent_store,
        CHILD,
        authorized=decision.authorized,
    )
    assert reopened.slices[CHILD].status is SliceStatus.SPAWNED
    assert reopened.slices[CHILD].park_cause is None


def test_abandon_without_retry_stays_parked_and_launches_nothing(tmp_path: Path) -> None:
    fixture = _failed_ordered_run(tmp_path)
    decision: OrderedRecoveryDecision | None = _park(fixture)[0]
    assert decision is not None
    effect = decision.held_launch_effect
    assert effect is not None
    fixture.parent_store.answer_gate(effect.abandon_gate, GateStatus.APPROVED)

    decision = _decision(fixture)

    assert decision is not None
    assert decision.recoverable is False
    assert decision.authorized is False
    assert [item["outcome"] for item in _resolutions(fixture)] == ["operator_abandoned"]
    state = fixture.parent_store.load()
    assert state.slices[CHILD].status is SliceStatus.PARKED
    assert fixture.journal.pending_entries() == []
    # A second tick does not append a duplicate and still refuses to launch.
    again = _decision(fixture)
    assert again is not None and again.recoverable is False
    assert [item["outcome"] for item in _resolutions(fixture)] == ["operator_abandoned"]


def test_a_rejected_retry_gate_keeps_the_slice_parked(tmp_path: Path) -> None:
    fixture = _failed_ordered_run(tmp_path)
    decision: OrderedRecoveryDecision | None = _park(fixture)[0]
    assert decision is not None
    effect = decision.held_launch_effect
    assert effect is not None
    fixture.parent_store.answer_gate(effect.retry_gate, GateStatus.REJECTED)

    decision = _decision(fixture)

    assert decision is not None
    assert decision.recoverable is False
    assert decision.gate_status is GateStatus.REJECTED
    assert _resolutions(fixture) == []
    assert fixture.parent_store.load().slices[CHILD].status is SliceStatus.PARKED


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("live_owner", lambda f: _change_owner_invocation(f, status="running")),
        ("wrong_predecessor", lambda f: _change_owner_invocation(
            f, invocation_id="some-other-invocation"
        )),
        (
            "wrong_worktree",
            lambda f: f.journal.resolve_by_key(
                str(f.entry["key"]),
                arguments={
                    **dict(f.entry["arguments"]),
                    "expected_worktree_fingerprint": "sha256:" + "b" * 64,
                },
            ),
        ),
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
def test_negative_owner_proofs_stay_non_overridable(
    tmp_path: Path, case: str, mutate: Callable[[_Fixture], None]
) -> None:
    del case
    fixture = _failed_ordered_run(tmp_path)
    decision: OrderedRecoveryDecision | None = _park(fixture)[0]
    assert decision is not None
    effect = decision.held_launch_effect
    assert effect is not None
    fixture.parent_store.answer_gate(effect.retry_gate, GateStatus.APPROVED)
    mutate(fixture)

    decision = _decision(fixture)

    assert decision is not None
    assert decision.recoverable is False
    assert decision.authorized is False
    assert _resolutions(fixture) == []
    assert fixture.parent_store.load().slices[CHILD].status is SliceStatus.PARKED


@pytest.mark.parametrize(
    "rows",
    [
        ("launch",),
        ("launch_duplicate",),
        ("launch_and_refusal",),
    ],
)
def test_host_evidence_that_is_present_or_conflicting_stays_non_overridable(
    tmp_path: Path, rows: tuple[str, ...]
) -> None:
    fixture = _failed_ordered_run(tmp_path)
    decision: OrderedRecoveryDecision | None = _park(fixture)[0]
    assert decision is not None
    effect = decision.held_launch_effect
    assert effect is not None
    fixture.parent_store.answer_gate(effect.retry_gate, GateStatus.APPROVED)
    launch = _launch_row(fixture.intent)
    refusal = {
        "type": "tool.called",
        "run_id": LEDGER_RUN_ID,
        "run_seq": 13,
        "agent_id": "controller",
        "data": {
            "tool_name": "resume_blocked_leaf",
            "success": False,
            "cancelled": False,
            "error": "agent.spawn_precondition.dirty_worktree",
            "dispatch_intent_id": fixture.intent,
        },
    }
    if rows == ("launch",):
        _write_ledger(fixture.project, launch)
    elif rows == ("launch_duplicate",):
        _write_ledger(fixture.project, launch, {**launch, "run_seq": 14})
    else:
        _write_ledger(fixture.project, launch, refusal)

    decision = _decision(fixture)

    assert decision is not None
    assert decision.recoverable is False
    assert decision.authorized is False
    assert _resolutions(fixture) == []


def test_an_unapproved_or_foreign_gate_never_authorizes_the_launch(
    tmp_path: Path,
) -> None:
    fixture = _failed_ordered_run(tmp_path)
    decision: OrderedRecoveryDecision | None = _park(fixture)[0]
    assert decision is not None
    effect = decision.held_launch_effect
    assert effect is not None
    # A generic recovery answer and a gate scoped to a different intent are not
    # the same decision as this effect's retry gate.
    fixture.parent_store.set_gate(GENERIC_GATE, GateStatus.APPROVED)
    foreign = effect.retry_gate.replace(effect.intent_id.rsplit(":", 1)[-1][:12], "0" * 12)
    fixture.parent_store.set_gate(foreign, GateStatus.APPROVED)

    decision = _decision(fixture)

    assert decision is not None
    assert decision.recoverable is False
    assert _resolutions(fixture) == []
    assert fixture.parent_store.load().slices[CHILD].status is SliceStatus.PARKED


def test_non_launch_effects_remain_non_overridable(tmp_path: Path) -> None:
    fixture = _failed_ordered_run(tmp_path)
    fixture.journal.append(
        EffectIntent("merge_pr", "41", {"pr_number": 41}, True)
    )
    decision = _decision(fixture)

    assert decision is not None
    assert decision.recoverable is False
    assert decision.held_launch_effect is None
    assert "unreconciled effect" in decision.reason


def test_repeated_continue_and_repeated_resolution_are_idempotent(tmp_path: Path) -> None:
    fixture = _failed_ordered_run(tmp_path)
    first = run_tl_loop(
        "parent",
        fixture.plan,
        EmptyQueue(),
        ReadOnlyEffectClient(
            EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
        ),
        config=replace(fixture.config, active=False),
        root_dir=fixture.parent_store.root_dir,
        budgets=BudgetLedger(0, 0),
    )
    second = run_tl_loop(
        "parent",
        fixture.plan,
        EmptyQueue(),
        ReadOnlyEffectClient(
            EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
        ),
        config=replace(fixture.config, active=False),
        root_dir=fixture.parent_store.root_dir,
        budgets=BudgetLedger(0, 0),
    )
    del first, second
    gates = [gate.name for gate in fixture.parent_store.load().gates]
    assert len(gates) == len(set(gates))
    decision = _decision(fixture)
    effect = decision.held_launch_effect if decision is not None else None
    assert effect is not None
    fixture.parent_store.answer_gate(effect.retry_gate, GateStatus.APPROVED)
    _decision(fixture)
    _decision(fixture)
    assert [item["outcome"] for item in _resolutions(fixture)] == [
        "operator_abandoned",
        "retry_authorized",
    ]


def test_concurrent_ticks_resolve_exactly_once(tmp_path: Path) -> None:
    fixture = _failed_ordered_run(tmp_path)
    decision: OrderedRecoveryDecision | None = _park(fixture)[0]
    assert decision is not None
    effect = decision.held_launch_effect
    assert effect is not None
    fixture.parent_store.answer_gate(effect.retry_gate, GateStatus.APPROVED)

    def evaluate(_: int) -> OrderedRecoveryDecision | None:
        return _decision(fixture)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(evaluate, range(8)))

    assert any(result is not None and result.recoverable for result in results)
    outcomes = [item["outcome"] for item in _resolutions(fixture)]
    assert outcomes.count("operator_abandoned") == 1
    assert outcomes.count("retry_authorized") == 1


def test_one_held_sibling_does_not_park_the_healthy_sibling(tmp_path: Path) -> None:
    fixture = _failed_ordered_run(tmp_path, with_sibling=True)
    # Give the sibling an already-finished child checkpoint so it is not
    # re-provisioned in this batch; only the held child should be parked.
    shutil.copytree(
        fixture.child_store.run_dir,
        fixture.parent_store.run_dir / SIBLING,
    )
    child_task = fixture.plan.sub_tls[0]
    sibling_task = fixture.plan.sub_tls[1]

    outcomes = _run_sub_tl_batch(
        (child_task, sibling_task),
        replace(fixture.config, active=False, ordered_recovery_child=CHILD),
        EmptyQueue(),
        ReadOnlyEffectClient(
            EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
        ),
        fixture.parent_store,
        BudgetLedger(0, 0),
    )

    assert len(outcomes) == 2
    state = fixture.parent_store.load()
    assert state.slices[CHILD].status is SliceStatus.PARKED
    assert state.slices[SIBLING].status is SliceStatus.PENDING
    assert not driver._held_launch_parked(state.slices[SIBLING])


def test_nested_hold_parks_only_its_own_level(tmp_path: Path) -> None:
    fixture = _failed_ordered_run(tmp_path, with_sibling=True)
    state = fixture.parent_store.load()
    decision = _decision(fixture)
    assert decision is not None and decision.held_launch_effect is not None

    parked = driver._park_held_launch_slice(
        state, fixture.parent_store, decision.held_launch_effect
    )

    assert parked.slices[CHILD].status is SliceStatus.PARKED
    assert parked.slices[SIBLING].status is SliceStatus.PENDING
    assert isinstance(parked.recursive_fsm, TLFailed)
    assert parked.current_order == 1


def test_recorded_live_shape_replays_through_production_python(tmp_path: Path) -> None:
    fixture = _failed_ordered_run(tmp_path)
    stored = _journal_state(fixture)[0]
    assert stored["operation"] == "resume_blocked_leaf"
    assert stored["status"] == "unknown"
    assert stored["error"] == "Timed out after 10.000s"
    arguments = stored["arguments"]
    assert isinstance(arguments, Mapping)
    assert arguments["dispatch_generation"] == 3
    assert arguments["runtime"] == "opencode"
    assert arguments["model"] == "opencode-go/deepseek-v4.1-flash"

    decision = _decision(fixture)

    assert decision is not None
    assert decision.held_launch_effect is not None
    assert decision.held_launch_effect.intent_id == fixture.intent
    assert decision.held_launch_effect.journal_index == 0


def test_an_unreadable_ledger_never_becomes_no_host_evidence(tmp_path: Path) -> None:
    """An unread ledger is unknown evidence, never "no launch happened"."""
    fixture = _failed_ordered_run(tmp_path)
    decision: OrderedRecoveryDecision | None = _park(fixture)[0]
    assert decision is not None
    effect = decision.held_launch_effect
    assert effect is not None
    fixture.parent_store.answer_gate(effect.retry_gate, GateStatus.APPROVED)
    # Corrupt the only segment: reading the durable evidence must fail. If the
    # "ledger was read" guard is removed, the empty/absent rows are treated as
    # proof that no host launch happened and the approved gate resolves it.
    segment = fixture.project / ".exo" / "ledger" / "segments" / "segment-000000000001.jsonl"
    segment.write_text("{this is not a ledger row\n", encoding="utf-8")

    decision = _decision(fixture)

    assert decision is not None
    assert decision.recoverable is False
    assert decision.authorized is False
    assert decision.held_launch_effect is None
    assert decision.reason is not None
    assert "durable host ledger is unreadable" in decision.reason
    assert _resolutions(fixture) == []
    assert fixture.parent_store.load().slices[CHILD].status is SliceStatus.PARKED


def test_an_intended_launch_entry_stays_non_overridable(tmp_path: Path) -> None:
    """Dispatch never proven is a hard failure even with an approved retry gate."""
    fixture = _failed_ordered_run(tmp_path)
    decision: OrderedRecoveryDecision | None = _park(fixture)[0]
    assert decision is not None
    effect = decision.held_launch_effect
    assert effect is not None
    fixture.parent_store.answer_gate(effect.retry_gate, GateStatus.APPROVED)
    # The client never observed the request complete, so the entry is still
    # ``intended``. That is not the ``unknown`` shape an operator may judge; if
    # the status guard is removed the approved gate resolves and relaunches it.
    fixture.journal.resolve_by_key(str(fixture.entry["key"]), status="intended")

    decision = _decision(fixture)

    assert decision is not None
    assert decision.recoverable is False
    assert decision.authorized is False
    assert decision.held_launch_effect is None
    assert decision.reason is not None
    assert "intended launch effect" in decision.reason
    assert _resolutions(fixture) == []
    assert fixture.parent_store.load().slices[CHILD].status is SliceStatus.PARKED


def test_historical_journal_bytes_are_unchanged_apart_from_appends(
    tmp_path: Path,
) -> None:
    fixture = _failed_ordered_run(tmp_path)
    journal_path = fixture.child_store.run_dir / "action-journal.json"
    before = journal_path.read_text(encoding="utf-8")
    before_rows = json.loads(before)
    _decision(fixture)
    assert journal_path.read_text(encoding="utf-8") == before

    decision: OrderedRecoveryDecision | None = _park(fixture)[0]
    assert decision is not None
    effect = decision.held_launch_effect
    assert effect is not None
    fixture.parent_store.answer_gate(effect.abandon_gate, GateStatus.APPROVED)
    _decision(fixture)

    after_text = journal_path.read_text(encoding="utf-8")
    after_rows = json.loads(after_text)
    assert after_rows[: len(before_rows)] == before_rows
    # The serializer is deterministic, so the historical bytes survive verbatim
    # and the only change is the appended record.
    assert after_text.startswith(before.rstrip()[:-1])
    assert len(after_rows) == len(before_rows) + 1
    assert after_rows[-1]["kind"] == "launch_resolution"
