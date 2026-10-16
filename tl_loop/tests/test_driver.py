"""Contract tests for the bounded active/shadow TL driver."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import queue
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import tl_loop.loop.driver as driver_module
from tl_loop.client.effects import EffectClient, ToolResult, ToolUnavailableError
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import JsonObject, TransportClient
from tl_loop.events.envelope import EventEnvelope, project
from tl_loop.events.queue import LedgerQueue, QueueError
from tl_loop.events.reader import LedgerReader, LedgerSnapshot, SequenceStatus
from tl_loop.fsm.child import ChildKind, ChildRecord
from tl_loop.fsm.event import ChildCompleted, PRFiled, PRUpdated
from tl_loop.fsm.phase import ChildHandle, TLPhase, TLPlanning, TLWaiting
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.recovery import RecoveryPhase, RecoveryState, begin_recovery
from tl_loop.fsm.scope import TLAllMerged as RecursiveTLAllMerged
from tl_loop.fsm.scope import TLPRFiled as RecursiveTLPRFiled
from tl_loop.fsm.scope import TLRunning as RecursiveTLRunning
from tl_loop.loop.convergence import ConvergenceInvariantError, ConvergenceTracker
from tl_loop.loop.driver import (
    AGGREGATE_PUBLICATION_GATE_PREFIX,
    DISPATCH_ALREADY_CONFIRMED,
    DISPATCH_CORRELATED,
    DISPATCH_HISTORICAL_AUDIT,
    DISPATCH_INTEGRITY_CONFLICT,
    INTEGRATION_FAILURE_GATE_NAME,
    REPEATED_ACTION_GATE_PREFIX,
    DepthLimitExceeded,
    DispatchAttempt,
    EffectIntent,
    EventDiagnostics,
    LoopCancelled,
    LoopLimitExceeded,
    SubTLTask,
    TLLoopConfig,
    TLLoopError,
    TLRunResult,
    WorkerTask,
    WorkPlan,
    _aggregate_candidate_from_result,
    _aggregate_pr_body,
    _aggregate_pr_title,
    _aggregate_watcher_identity_matches,
    _apply_convergence,
    _apply_reconciliation_observations,
    _bind_publication_evidence,
    _checkpoint_ordered_sub_tl_invocation,
    _child_recovery_projection,
    _confirm_dispatch_event,
    _dispatch_payload,
    _event_belongs_to_plan,
    _execute_direct_reviewer_intent,
    _initial_slices,
    _integrate_one_candidate,
    _is_aggregate_slice,
    _LeafIssueProvenance,
    _merge_result_is_authoritative,
    _migrate_audit_marked_quarantine,
    _new_dispatch_attempt,
    _ordered_child_complete,
    _park_repeated_action,
    _phase_after_slice_merge,
    _publication_from_watcher,
    _reconcile_pending_merge_entry,
    _record_review_event,
    _recover_tool_unavailable,
    _recursive_phase_after_slice_update,
    _replayable_quarantine_documents,
    _route_ci_event,
    _route_review_event,
    _run_live_sub_tl,
    _run_sub_tl_batch,
    _spawn_invocation_id,
    _supervise_live_sub_tl,
    correlate_dispatch_event,
    run_tl_loop,
    tl_run,
)
from tl_loop.loop.escalate import blocked_gate_name
from tl_loop.loop.journal import EffectJournal
from tl_loop.loop.observation import WatcherObservation
from tl_loop.loop.reconcile import (
    ExternalIntent,
    Quiescent,
    ReconciliationResult,
    action_key,
    derive_next_action,
    publication_ownership_malformed,
    publication_ownership_unresolved,
    publication_refused,
    reconcile_merge_observation,
    repeated_action_gate_name,
    validate_publication_from_watcher,
    watcher_presents_publication,
)
from tl_loop.loop.shadow import TLEventDecoder, _update_slices
from tl_loop.ordered import (
    ChildRecoverySummary,
    IntegrationContract,
    IntegrationLifecycle,
    SubTLLifecycle,
)
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmRequest, RlmResponse
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty
from tl_loop.select.model import ModelCatalog
from tl_loop.select.policy import validate_policy
from tl_loop.state.plan_manifest import PlanManifest, build_plan_manifest
from tl_loop.state.plan_reconciliation import analyze_plan_reconciliation
from tl_loop.state.plan_reconciliation_apply import apply_plan_reconciliation
from tl_loop.state.plan_revision_apply import apply_plan_revision
from tl_loop.state.schema import (
    SCHEMA_VERSION,
    ActionKind,
    ActionPhase,
    ActionState,
    BudgetLedger,
    DurableReviewEvidence,
    FSMState,
    GateState,
    GateStatus,
    HandoffEvidence,
    IntegrationCandidateState,
    IntegrationRuntimeState,
    OrderedStageState,
    ParkCause,
    PublicationBinding,
    RepositoryIdentity,
    RunState,
    SliceState,
    SliceStatus,
    Verdict,
)
from tl_loop.state.serialization import DurableWriteError
from tl_loop.state.store import QuarantineStorageError, RunStore
from tl_loop.state.store import load as load_state
from tl_loop.tests.checkpoint_fixtures import create


@dataclass
class SyntheticQueue:
    events: list[EventEnvelope]
    acknowledged: list[int] = field(default_factory=list)

    def get(self, timeout: float | None = None) -> EventEnvelope:
        del timeout
        if not self.events:
            raise queue.Empty
        return self.events.pop(0)

    def acknowledge(self, event: EventEnvelope) -> int:
        assert event.run_seq is not None
        self.acknowledged.append(event.run_seq)
        return event.run_seq

    def capture_snapshot(self, cursor: int) -> LedgerSnapshot:
        """Synthetic queued events represent post-start live input by default."""
        return LedgerSnapshot((), cursor, SequenceStatus.UNKNOWN, (), None)


def _test_git_output(repository: Path, *arguments: str) -> str | None:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _test_git_repository(repository: Path) -> str:
    repository.mkdir(parents=True, exist_ok=True)
    if _test_git_output(repository, "rev-parse", "--show-toplevel") is None:
        subprocess.run(
            ("git", "-C", str(repository), "init", "--initial-branch=main"),
            capture_output=True,
            check=True,
            text=True,
        )
        subprocess.run(
            ("git", "-C", str(repository), "config", "user.name", "TL Test"),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(repository), "config", "user.email", "tl-test@example.invalid"),
            check=True,
        )
        (repository / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(repository), "add", "base.txt"), check=True)
        subprocess.run(("git", "-C", str(repository), "commit", "-m", "base"), check=True)
    head = _test_git_output(repository, "rev-parse", "HEAD")
    assert head is not None
    return head


def _test_git_commit(repository: Path, filename: str, content: str, message: str) -> str:
    (repository / filename).write_text(content, encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", filename), check=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-m", message), check=True)
    head = _test_git_output(repository, "rev-parse", "HEAD")
    assert head is not None
    return head


def _test_git_tree(repository: Path, commit: str) -> str:
    tree = _test_git_output(repository, "rev-parse", f"{commit}^{{tree}}")
    assert tree is not None
    return tree


def test_event_diagnostics_exposes_non_terminal_timing() -> None:
    started_at = time.time() - 2.0
    diagnostics = EventDiagnostics(
        controller_started_at=started_at,
        task_started_at={"leaf-a": started_at + 0.1},
        last_authoritative_event_seq=7,
        last_observed_progress_at=started_at + 1.0,
    )

    snapshot = diagnostics.snapshot()

    assert snapshot["controller_started_at"] == started_at
    assert cast(float, snapshot["elapsed_seconds"]) >= 2.0
    assert snapshot["task_started_at"] == {"leaf-a": started_at + 0.1}
    assert snapshot["last_authoritative_event_seq"] == 7
    assert snapshot["last_observed_progress_at"] == started_at + 1.0


def test_dispatch_attempt_payload_and_invocation_identity_are_explicit() -> None:
    attempt = DispatchAttempt("intent-2", 1.0, "codex/gpt-luna", attempt=2)

    assert _dispatch_payload("slice-a", attempt, "dispatch_intended")["attempt"] == 2
    assert _spawn_invocation_id(SimpleNamespace(result={"invocation_id": "inv-2"})) == "inv-2"
    assert (
        _spawn_invocation_id(SimpleNamespace(result={"invocation": {"invocation_id": "inv-3"}}))
        == "inv-3"
    )


def test_dispatch_correlation_rejects_historical_epoch_and_stale_generation(
    tmp_path: Path,
) -> None:
    run_id = "dispatch-correlation-run"
    plan = WorkPlan.from_mapping(
        {"leaves": [{"name": "leaf-a", "task": "task", "chainlink_issue_id": None}]}
    )
    create(
        run_id,
        {
            "controller_epoch": "epoch-new",
            "slices": _initial_slices(plan, TLLoopConfig(), tmp_path, run_id),
        },
        root_dir=tmp_path,
    )
    state = RunStore(run_id, tmp_path).load()
    current = replace(
        state.slices["leaf-a"],
        status=SliceStatus.DISPATCHING,
        attempts=1,
        dispatch_intent_id=_dispatch_intent(run_id, "leaf-a"),
        dispatch_generation=2,
    )
    state = replace(state, slices={"leaf-a": current})
    base = _canonical_event(
        1,
        "agent.spawned",
        "leaf-a",
        run_id,
        intent_id=current.dispatch_intent_id,
    )

    historical = replace(
        base,
        data={**base.data, "controller_epoch": "epoch-old", "dispatch_generation": 2},
    )
    assert correlate_dispatch_event(state, historical).classification == DISPATCH_HISTORICAL_AUDIT

    stale = replace(
        base,
        data={**base.data, "controller_epoch": "epoch-new", "dispatch_generation": 1},
    )
    assert correlate_dispatch_event(state, stale).classification == DISPATCH_INTEGRITY_CONFLICT

    current_event = replace(
        base,
        data={**base.data, "controller_epoch": "epoch-new", "dispatch_generation": 2},
    )
    assert correlate_dispatch_event(state, current_event).classification == DISPATCH_CORRELATED


@dataclass(frozen=True)
class DuplicateConfirmationCase:
    """A confirmed slice plus the second spawn row for the same intent."""

    state: RunState
    intent_id: str
    controller_epoch: str


def _confirmed_worker(
    root: Path, *, run_id: str = "dispatch-duplicate-run"
) -> DuplicateConfirmationCase:
    """A slice the controller has already confirmed at generation 1.

    This is the durable shape the codex-messaging run parked in: the slice is
    ``spawned``, it records the confirmation's authoritative sequence, and its
    dispatch generation is the one the second row must name.
    """
    plan = WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "task"}]})
    create(
        run_id,
        {
            "slices": _initial_slices(plan, TLLoopConfig(), root, run_id),
            "repository_identity": {"remote_url": "https://forge.example.com/org/repo.git"},
        },
        root_dir=root,
    )
    intent_id = _dispatch_intent(run_id, "worker-a")
    state = RunStore(run_id, root).load()
    confirmed = replace(
        state.slices["worker-a"],
        status=SliceStatus.SPAWNED,
        attempts=1,
        dispatch_intent_id=intent_id,
        dispatch_generation=1,
        dispatch_agent_id="worker-a-codex",
        dispatch_authoritative_event_seq=8,
    )
    state = replace(
        state,
        controller_epoch="epoch-current",
        slices={"worker-a": confirmed},
    )
    return DuplicateConfirmationCase(state, intent_id, "epoch-current")


def _spawn_row(
    run_id: str,
    *,
    run_seq: int,
    data: dict[str, object],
    agent_id: str = "root",
) -> EventEnvelope:
    """One ``agent.spawned`` ledger row projected the way the server writes it."""
    return project(
        cast(
            dict[str, object],
            {
                "schema_version": 1,
                "event_id": f"spawned-{run_seq}",
                "id": f"spawned-{run_seq}",
                "event_time": "2026-09-30T16:22:12Z",
                "observed_at": "2026-09-30T16:22:12Z",
                "run_seq": run_seq,
                "type": "agent.spawned",
                # The server attributes the spawn to the dispatching agent, so
                # agent_id is the controller's own identity, not the slice.
                "agent_id": agent_id,
                "run_id": run_id,
                "session_id": "session-1",
                "invocation_id": None,
                "generation": None,
                "lifecycle_state": "emitted",
                "data": data,
            },
        )
    )


def test_the_second_spawn_row_for_a_live_intent_is_a_duplicate_not_a_conflict(
    tmp_path: Path,
) -> None:
    """The server writes two spawn rows for one dispatch; the second is ours.

    #1148 parked the worker at ``spawned`` because the controller quarantined
    its own dispatch record -- the row carrying ``slug`` and ``task_summary``
    rather than ``child_agent`` -- as ``integrity_conflict``/``intent_mismatch``.
    The slice had already left the dispatching statuses because the controller
    confirmed the dispatch itself, so the intent no longer looked pending.
    """
    case = _confirmed_worker(tmp_path)
    duplicate = _spawn_row(
        "dispatch-duplicate-run",
        run_seq=10,
        data={
            "agent_type": "worker",
            "intent_id": case.intent_id,
            "slug": "worker-a",
            "task_summary": "do the thing",
        },
    )

    correlation = correlate_dispatch_event(case.state, duplicate)

    assert correlation.classification == DISPATCH_ALREADY_CONFIRMED
    assert correlation.slice_id == "worker-a"

    # The other row of the same dispatch names the runtime agent id instead of
    # the slice id, and is a duplicate for the same reason.
    registration = correlate_dispatch_event(
        case.state,
        _spawn_row(
            "dispatch-duplicate-run",
            run_seq=8,
            data={
                "agent_type": "codex",
                "child_agent": "worker-a-codex",
                "intent_id": case.intent_id,
                "spawn_type": "worker",
            },
        ),
    )
    assert registration.classification == DISPATCH_ALREADY_CONFIRMED
    # The guard still refuses a generation that is not the persisted one.
    stale = replace(
        duplicate,
        data={**duplicate.data, "dispatch_generation": 2},
    )
    assert correlate_dispatch_event(case.state, stale).classification == (
        DISPATCH_INTEGRITY_CONFLICT
    )


def test_a_duplicate_confirmation_is_refused_across_epoch_generation_and_intent(
    tmp_path: Path,
) -> None:
    """Recognising a duplicate never widens what the correlation accepts."""
    case = _confirmed_worker(tmp_path)
    duplicate = _spawn_row(
        "dispatch-duplicate-run",
        run_seq=10,
        data={
            "agent_type": "worker",
            "intent_id": case.intent_id,
            "slug": "worker-a",
        },
    )

    historical = correlate_dispatch_event(
        case.state,
        replace(duplicate, data={**duplicate.data, "controller_epoch": "epoch-old"}),
    )
    assert historical.classification == DISPATCH_HISTORICAL_AUDIT
    assert historical.slice_id == "worker-a"

    # No generation, no epoch, and the intent still names its one owner.
    accepted = correlate_dispatch_event(case.state, duplicate)
    assert accepted.classification == DISPATCH_ALREADY_CONFIRMED

    # A foreign intent proves no ownership at all and stays a conflict.
    unowned = correlate_dispatch_event(
        case.state,
        replace(duplicate, data={**duplicate.data, "intent_id": "someone-elses-intent"}),
    )
    assert unowned.classification == DISPATCH_INTEGRITY_CONFLICT
    assert unowned.reason == "intent_mismatch"

    # A row naming somebody else's child is not a duplicate, whatever its
    # intent says: the intent alone must never authorise a foreign
    # confirmation.
    for key, foreign_agent in (("child_agent", "other-slice-codex"), ("slug", "other-slice")):
        crossed = correlate_dispatch_event(
            case.state,
            replace(duplicate, data={**duplicate.data, key: foreign_agent}),
        )
        assert crossed.classification == DISPATCH_INTEGRITY_CONFLICT
        assert crossed.reason == "dispatch_agent_mismatch"

    # An intent no slice records is not a duplicate of anything.
    unrecorded = correlate_dispatch_event(
        case.state,
        replace(duplicate, data={"agent_type": "worker"}),
    )
    assert unrecorded.classification == DISPATCH_INTEGRITY_CONFLICT

    # A slice still awaiting its confirmation is confirmed, not deduplicated.
    pending = replace(
        case.state,
        slices={
            "worker-a": replace(
                case.state.slices["worker-a"],
                status=SliceStatus.DISPATCH_UNCONFIRMED,
                dispatch_authoritative_event_seq=None,
            )
        },
    )
    assert correlate_dispatch_event(pending, duplicate).classification == (DISPATCH_CORRELATED)


def test_a_nested_child_scope_classifies_its_own_duplicate_the_same_way(
    tmp_path: Path,
) -> None:
    """A sub-TL slice's confirmation is duplicated the same way a leaf's is.

    A nested child records ``sub_tl_started`` rather than ``agent.spawned`` as
    its boundary, and its owner is the child controller's own agent id. The
    duplicate rule reads the owner's recorded intent, agent and authoritative
    sequence rather than the boundary name, so it does not depend on which kind
    of slice confirmed -- while still refusing another child's agent.
    """
    run_id = "nested-duplicate-run"
    child_plan = WorkPlan(workers=(WorkerTask("grandchild", "do the thing"),))
    plan = WorkPlan(sub_tls=(SubTLTask("child-a", child_plan, order=1),))
    create(
        run_id,
        {"slices": _initial_slices(plan, TLLoopConfig(), tmp_path, run_id)},
        root_dir=tmp_path,
    )
    state = RunStore(run_id, tmp_path).load()
    intent_id = hashlib.sha256(
        f"{run_id}:child-a:{state.slices['child-a'].attempts + 1}".encode()
    ).hexdigest()[:32]
    nested = replace(
        state,
        slices={
            "child-a": replace(
                state.slices["child-a"],
                status=SliceStatus.SPAWNED,
                dispatch_intent_id=intent_id,
                dispatch_started_at=1.0,
                dispatch_last_boundary="sub_tl_started",
                dispatch_agent_id="child-a-tl",
                dispatch_authoritative_event_seq=4,
                dispatch_generation=0,
            )
        },
    )

    for data in (
        {"intent_id": intent_id, "slug": "child-a"},
        {"intent_id": intent_id, "child_agent": "child-a-tl"},
    ):
        assert (
            correlate_dispatch_event(
                nested, _spawn_row(run_id, run_seq=20, data=data)
            ).classification
            == DISPATCH_ALREADY_CONFIRMED
        )

    crossed = correlate_dispatch_event(
        nested,
        _spawn_row(
            run_id,
            run_seq=21,
            data={"intent_id": intent_id, "child_agent": "a-different-tl"},
        ),
    )
    assert crossed.classification == DISPATCH_INTEGRITY_CONFLICT
    assert crossed.reason == "dispatch_agent_mismatch"


def _notify_parent_row(
    run_id: str,
    *,
    run_seq: int,
    agent_id: str,
    status: str = "success",
) -> EventEnvelope:
    """One ``agent.notify_parent`` row shaped exactly as the server writes it.

    A real Codex worker's report carries no ``slice_id``, ``slug``,
    ``child_agent`` or ``intent_id``: the only link back to the dispatched slice
    is ``agent_id``, which names the runtime agent the server minted. That is
    what the codex-messaging run of #1148 emitted at run_seq 35/36.
    """
    return project(
        cast(
            dict[str, object],
            {
                "schema_version": 1,
                "event_id": f"notify-{run_seq}",
                "id": f"notify-{run_seq}",
                "event_time": "2026-09-30T16:23:01Z",
                "observed_at": "2026-09-30T16:23:01Z",
                "run_seq": run_seq,
                "type": "agent.notify_parent",
                "agent_id": agent_id,
                "run_id": run_id,
                "session_id": "session-1",
                "invocation_id": "inv-1",
                "generation": 1,
                "lifecycle_state": "emitted",
                "data": {
                    "head_sha": None,
                    "head_sha_finding": ("not_available_without_verified_pr_context"),
                    "message": "the worker finished its task",
                    "parent": "root",
                    "source": "agent",
                    "status": status,
                },
            },
        )
    )


def _worker_transport() -> RecordingTransport:
    """A transport that answers the root-scope finalization and watcher calls.

    A worker that files no PR leaves the root branch already fast-forwarded and
    the publication registry with no record for its slice, so the run can reach
    ``tl_done`` on real evidence rather than parking on watcher evidence a
    unit test would otherwise have to invent.
    """
    transport = RecordingTransport()
    original = transport.call_tool

    def call_tool(
        role: str, name: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if tool_name == "resolve_live_pr_for_slice":
            return {"success": True, "result": {"resolution": "never_published"}}
        if tool_name == "root_branch_finalize":
            return {
                "success": True,
                "result": {
                    "branch": "main",
                    "fast_forward": True,
                    "local_head_sha": "a" * 40,
                    "remote_head_sha": "a" * 40,
                    "ancestry_proof": "fast-forward",
                },
            }
        return original(role, name, tool_name, arguments)

    transport.call_tool = call_tool  # type: ignore[method-assign]
    return transport


def test_a_worker_notify_parent_reaches_tl_done_through_its_runtime_agent_id(
    tmp_path: Path,
) -> None:
    """The corrected premise of #1148: a reported completion must be applied.

    The worker runs, calls ``notify_parent``, and the tool reports success, yet
    the controller must reach ``tl_done``. Its row names only the runtime agent
    id, so ownership has to come from the ``dispatch_agent_id`` the slice
    persisted at dispatch, and the decoded child has to be retargeted to the
    slice the recursive barrier is keyed by. Without both, the row is filtered
    before the decoder and the run stays ``tl_running`` forever.
    """
    run_id = "notify-parent-run"
    plan = WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "task"}]})
    case = _confirmed_worker(tmp_path, run_id=run_id)
    store = RunStore(run_id, tmp_path)
    manifest = build_plan_manifest(
        {"workers": [{"name": "worker-a", "task": "task"}]},
        scope_id=run_id,
    )
    store.set_plan_manifest(manifest, slices=case.state.slices)
    store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("worker-a",)),
        case.state.slices,
        case.state.budgets,
        8,
    )
    source = SyntheticQueue([_notify_parent_row(run_id, run_seq=35, agent_id="worker-a-codex")])

    result = run_tl_loop(
        run_id,
        plan,
        cast(Any, source),
        EffectClient(_worker_transport()),
        config=replace(
            _config(),
            run_id=run_id,
            ledger_run_id=run_id,
            repository_identity=None,
            keep_alive_on_waiting=False,
            max_parallel_slices=None,
            policy=None,
        ),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert result.diagnostics["rejected"] == 0
    # The report is consumed as authoritative progress, not filtered away.
    assert result.diagnostics["last_authoritative_event_seq"] == 35
    assert [(t.event_seq, t.after) for t in result.transitions] == [(35, TLPhase.TLAllMerged)]
    assert store.quarantined_events() == ()


def test_a_notify_parent_from_a_foreign_runtime_agent_is_still_refused(
    tmp_path: Path,
) -> None:
    """Ownership stays proven: another agent's report advances nothing."""
    run_id = "notify-parent-foreign-run"
    plan = WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "task"}]})
    case = _confirmed_worker(tmp_path, run_id=run_id)
    store = RunStore(run_id, tmp_path)
    store.set_plan_manifest(
        build_plan_manifest(
            {"workers": [{"name": "worker-a", "task": "task"}]},
            scope_id=run_id,
        ),
        slices=case.state.slices,
    )
    store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("worker-a",)),
        case.state.slices,
        case.state.budgets,
        8,
    )
    foreign = _notify_parent_row(run_id, run_seq=35, agent_id="some-other-agent-codex")
    assert not _event_belongs_to_plan(foreign, set(case.state.slices), case.state)

    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([foreign]),
        EffectClient(_worker_transport()),
        config=replace(
            _config(),
            run_id=run_id,
            ledger_run_id=run_id,
            repository_identity=None,
            keep_alive_on_waiting=False,
            max_parallel_slices=None,
            policy=None,
        ),
        root_dir=tmp_path,
    )

    # The run never completes and the slice is never released: the report was
    # never treated as authoritative progress.
    assert result.final_state.fsm.phase is not TLPhase.TLDone
    assert result.final_state.slices["worker-a"].status is SliceStatus.SPAWNED
    assert result.diagnostics["last_authoritative_event_seq"] is None
    assert result.transitions == ()


def test_a_duplicate_confirmation_advances_the_cursor_without_quarantine(
    tmp_path: Path,
) -> None:
    """The loop absorbs the duplicate row instead of parking it forever."""
    run_id = "dispatch-duplicate-run"
    plan = WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "task"}]})
    case = _confirmed_worker(tmp_path, run_id=run_id)
    store = RunStore(run_id, tmp_path)
    # Persist the confirmed slice so the loop resumes from the parked shape:
    # one spawned worker, one waiting barrier, cursor at the first confirmation.
    store.set_plan_manifest(
        build_plan_manifest(
            {"workers": [{"name": "worker-a", "task": "task"}]},
            scope_id=run_id,
        ),
        slices=case.state.slices,
    )
    store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("worker-a",)),
        case.state.slices,
        case.state.budgets,
        8,
    )
    duplicate = _spawn_row(
        run_id,
        run_seq=10,
        data={
            "agent_type": "worker",
            "intent_id": case.intent_id,
            "slug": "worker-a",
        },
    )
    source = SyntheticQueue([duplicate])

    result = run_tl_loop(
        run_id,
        plan,
        cast(Any, source),
        EffectClient(RecordingTransport()),
        config=replace(
            _config(),
            run_id=run_id,
            ledger_run_id=run_id,
            keep_alive_on_waiting=False,
            max_parallel_slices=None,
            policy=None,
        ),
        root_dir=tmp_path,
    )

    assert result.cursor >= 10
    assert store.quarantined_events() == ()
    assert result.diagnostics["duplicate_confirmations"] == 1
    assert result.diagnostics["rejected"] == 0
    # The slice is untouched: a duplicate is not a second status change.
    assert result.final_state.slices["worker-a"].status is SliceStatus.SPAWNED


def test_the_dispatch_intent_follows_the_run_generation_not_the_live_epoch(
    tmp_path: Path,
) -> None:
    """One attempt keeps one intent across a restart; a new generation does not.

    `exomonad init` mints a fresh controller epoch on every launch,
    `--continue` included, and the driver adopts it into run state. The intent
    identity is therefore taken from the run *generation* the state was created
    under: minting for the same run, slice and attempt after a restart has to
    land on the intent the first launch produced, so the `tl.dispatch_intended`
    row and the `agent.spawned` row that confirms it agree -- while a recreated
    run state must mint an intent no predecessor row can claim, which is what
    keeps a predecessor's owner off the new dispatch.
    """
    run_id = "generation-scoped-intent"
    plan = WorkPlan.from_mapping(
        {"leaves": [{"name": "leaf-a", "task": "task", "chainlink_issue_id": None}]}
    )

    def generation(epoch: str) -> RunState:
        """One run state created under ``epoch``, as ``--start``/``--recreate`` do."""
        root = tmp_path / epoch
        root.mkdir()
        create(
            run_id,
            {
                "controller_epoch": epoch,
                "slices": _initial_slices(plan, TLLoopConfig(), root, run_id),
            },
            root_dir=root,
        )
        return RunStore(run_id, root).load()

    created = generation("epoch-created-under")
    attempt = _new_dispatch_attempt(created, "leaf-a", TLLoopConfig())

    # --continue: init re-mints the epoch and the driver adopts it, but the
    # generation this state was created under does not move with it, so the
    # identity of that attempt does not move either.
    restarted = replace(created, controller_epoch="epoch-after-continue")
    assert restarted.generation_id == "epoch-created-under"
    assert restarted.controller_epoch != created.controller_epoch
    assert _new_dispatch_attempt(restarted, "leaf-a", TLLoopConfig()).intent_id == attempt.intent_id

    # The attempt persists exactly as `_prepare_spawn` persists it, and the row
    # the first launch produced confirms it -- once: after the confirmation the
    # slice has left the dispatching statuses, so the same row delivered again
    # changes nothing.
    in_flight = replace(
        created.slices["leaf-a"],
        status=SliceStatus.DISPATCHING,
        attempts=1,
        dispatch_intent_id=attempt.intent_id,
        dispatch_generation=attempt.dispatch_generation,
    )
    slices = {"leaf-a": in_flight}
    spawn_row = replace(
        _canonical_event(11, "agent.spawned", "leaf-a", run_id, intent_id=attempt.intent_id),
        invocation_id="inv-original",
    )
    confirmed = _confirm_dispatch_event(
        slices, slices, spawn_row, "leaf-a", 11, "epoch-after-continue"
    )
    assert confirmed["leaf-a"].dispatch_intent_id == attempt.intent_id
    assert confirmed["leaf-a"].dispatch_invocation_id == "inv-original"
    assert confirmed["leaf-a"].dispatch_authoritative_event_seq == 11
    assert confirmed["leaf-a"].status is SliceStatus.SPAWNED
    assert (
        _confirm_dispatch_event(
            confirmed, confirmed, spawn_row, "leaf-a", 11, "epoch-after-continue"
        )
        == confirmed
    )

    # --recreate: a new run state, so a new generation, so a different intent
    # -- and the predecessor's row can no longer confirm anything.
    recreated_state = generation("epoch-recreated")
    assert recreated_state.generation_id == "epoch-recreated"
    recreated_attempt = _new_dispatch_attempt(recreated_state, "leaf-a", TLLoopConfig())
    assert recreated_attempt.intent_id != attempt.intent_id
    pending = replace(in_flight, dispatch_intent_id=recreated_attempt.intent_id)
    assert _confirm_dispatch_event(
        {"leaf-a": pending},
        {"leaf-a": pending},
        spawn_row,
        "leaf-a",
        11,
        "epoch-recreated",
    ) == {"leaf-a": pending}


def test_continue_adopts_the_new_controller_reconciliation_epoch(tmp_path: Path) -> None:
    run_id = "continued-epoch"
    epoch_path = tmp_path / f"{run_id}.controller-epoch"
    epoch_path.write_text("epoch-new\n", encoding="utf-8")
    create(
        run_id,
        {
            "controller_epoch": "epoch-old",
            "repository_identity": {"remote_url": "https://forge.example.com/org/repo.git"},
        },
        root_dir=tmp_path,
    )

    result = tl_run(
        {"run_id": run_id, "plan": WorkPlan()},
        TLLoopConfig(
            active=True,
            session_mode="continue",
            root_dir=tmp_path,
            max_events=1,
            source=SyntheticQueue([]),
            effects=EffectClient(RecordingTransport()),
        ),
        BudgetLedger(0, 0),
    )

    assert result.final_state.controller_epoch == "epoch-new"
    assert RunStore(run_id, tmp_path).load().controller_epoch == "epoch-new"


@pytest.mark.parametrize(
    ("result", "authoritative"),
    [
        (
            ToolResult({"success": True, "result": {"merged": True}}, True, {"merged": True}, None),
            True,
        ),
        (ToolResult({"success": True, "result": {}}, True, {}, None), False),
        (ToolResult({"success": True, "result": None}, True, None, None), False),
        (ToolResult({"success": False, "error": "pending"}, False, None, "pending"), False),
    ],
)
def test_merge_response_requires_authoritative_merged_flag(
    result: ToolResult,
    authoritative: bool,
) -> None:
    assert _merge_result_is_authoritative(result) is authoritative


@dataclass
class RecordingTransport:
    calls: list[tuple[str, JsonObject]] = field(default_factory=list)
    fail_observability: bool = False
    reject_spawns: bool = False
    spawned_agent_id: str | None = None
    spawned_invocation_id: str | None = None
    listed_agents: list[JsonObject] = field(default_factory=list)
    next_controller_run_seq: int = 1000
    aggregate_reconciliation: JsonObject | None = None
    aggregate_reconciliations: dict[str, JsonObject] = field(default_factory=dict)
    aggregate_git_evidence: dict[str, tuple[str, str]] = field(default_factory=dict)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role
        self.calls.append((tool_name, arguments))
        if self.fail_observability and tool_name == "emit_controller_event":
            return {"success": False, "error": "ledger unavailable"}
        if self.reject_spawns and tool_name in {"spawn_worker", "spawn_leaf"}:
            return {"success": False, "error": "tmux launch rejected"}
        if tool_name in {"spawn_worker", "spawn_leaf"} and self.spawned_agent_id:
            result: JsonObject = {"agent_id": self.spawned_agent_id}
            if self.spawned_invocation_id is not None:
                result["invocation_id"] = self.spawned_invocation_id
            return {"success": True, "result": result}
        if tool_name == "list_agents":
            return {"success": True, "result": {"agents": self.listed_agents}}
        if tool_name == "repository_identity":
            return {
                "success": True,
                "result": {"remote_url": "https://forge.example.com/org/repo.git"},
            }
        if tool_name == "resolve_live_pr_for_slice":
            slice_id = str(arguments["slice_id"])
            reconciliation = self.aggregate_reconciliations.get(slice_id)
            if reconciliation is None and self.aggregate_reconciliation is not None:
                publication = self.aggregate_reconciliation.get("publication")
                if isinstance(publication, dict) and publication.get("slice_id") == slice_id:
                    reconciliation = self.aggregate_reconciliation
            if reconciliation is None:
                return {
                    "success": True,
                    "result": {
                        "resolution": "never_published",
                        "slice_id": slice_id,
                    },
                }
            return {
                "success": True,
                "result": {
                    "resolution": "live",
                    "pr_number": reconciliation["pr_number"],
                    "publication": reconciliation["publication"],
                    "slice_id": slice_id,
                },
            }
        if tool_name == "watcher_pr_state":
            reconciliation = next(
                (
                    record
                    for record in self.aggregate_reconciliations.values()
                    if record.get("pr_number") == arguments["pr_number"]
                ),
                self.aggregate_reconciliation,
            )
            if reconciliation is None or reconciliation.get("pr_number") != arguments["pr_number"]:
                return {"success": True, "result": {"found": False}}
            return {"success": True, "result": reconciliation["watcher"]}
        if tool_name == "emit_controller_event":
            self.next_controller_run_seq += 1
            return {
                "success": True,
                "result": {"event_id": "controller-event", "run_seq": self.next_controller_run_seq},
            }
        if tool_name == "file_pr":
            title = str(arguments.get("title", ""))
            if title.startswith("Aggregate ") and " into " in title:
                slice_id = title.removeprefix("Aggregate ").split(" into ", 1)[0]
                base_branch = str(arguments.get("base_branch", "main"))
                publication_identity = arguments.get("publication_identity")
                identity = publication_identity if isinstance(publication_identity, Mapping) else {}
                invocation_id = str(identity.get("invocation_id") or f"{slice_id}-invocation")
                head_branch = str(identity.get("head_branch") or f"{base_branch}.{slice_id}")
                author_agent = str(identity.get("author_agent") or name)
                head_sha, head_tree = self.aggregate_git_evidence.get(slice_id, ("head-a", ""))
                reconciliation = _aggregate_watcher_fixture(
                    invocation_id,
                    slice_id=slice_id,
                    author_agent=author_agent,
                    head_branch=head_branch,
                    base_branch=base_branch,
                )
                reconciliation["watcher"]["head_sha"] = head_sha
                if head_tree:
                    reconciliation["watcher"]["pr_head_tree_sha"] = head_tree
                self.aggregate_reconciliations[slice_id] = reconciliation
                return {
                    "success": True,
                    "result": {
                        "pr_number": 43,
                        "head_sha": head_sha,
                        "patch_digest": "patch-a",
                        "base_sha": "base-a",
                        "head_branch": head_branch,
                        "base_branch": base_branch,
                        "slice_id": slice_id,
                        "invocation_id": invocation_id,
                        "author_agent": author_agent,
                    },
                }
            return {
                "success": True,
                "result": {
                    "pr_number": 42,
                    "head_sha": "aggregate-head",
                    "patch_digest": "aggregate-patch",
                    "base_sha": "base-main",
                },
            }
        if tool_name == "post_merge_parent_sync":
            return {
                "success": True,
                "result": {
                    **arguments,
                    "parent_commit_sha": arguments["expected_base_sha"],
                    "remote_head_sha": arguments["expected_base_sha"],
                    "ancestry_proof": (
                        f"ancestor:{arguments['merged_head_sha']}->{arguments['expected_base_sha']}"
                    ),
                },
            }
        if tool_name == "chainlink_issue_close":
            return {
                "success": True,
                "result": {
                    "issue_id": arguments["issue_id"],
                    "receipt_id": "issue-close-receipt",
                },
            }
        if tool_name == "chainlink_issue_show":
            return {
                "success": True,
                "result": {
                    "id": arguments["issue_id"],
                    "title": "linked work",
                    "status": "open",
                },
            }
        if tool_name == "post_merge_changelog":
            return {
                "success": True,
                "result": {
                    **arguments,
                    "commit_sha": "changelog-commit",
                },
            }
        if tool_name == "post_merge_push":
            return {
                "success": True,
                "result": {
                    **arguments,
                    "push_receipt_id": "push-receipt",
                    "observed_remote_head": arguments["pushed_commit"],
                    "ancestry_proof": (
                        f"ancestor:{arguments['pushed_commit']}->{arguments['pushed_commit']}"
                    ),
                },
            }
        if tool_name == "root_branch_finalize":
            return {
                "success": True,
                "result": {
                    "branch": arguments["branch"],
                    "local_head_sha": "root-head",
                    "remote_head_sha": "root-head",
                    "ancestry_proof": "ancestor:root-before->root-head",
                    "fast_forward": True,
                },
            }
        if tool_name == "merge_pr":
            return {"success": True, "result": {"merged": True}}
        if tool_name == "chainlink_issue_list":
            return {"success": True, "result": []}
        return {"success": True, "result": None}


def _install_ordered_invocation(
    monkeypatch: pytest.MonkeyPatch,
    invocation_id: str,
) -> None:
    """Record a server-confirmed subTL invocation in synthetic controller tests."""
    original = driver_module._prepare_sub_tl_stage

    def prepare(tasks, state, config, source, effects, store, effects_log, order):
        state, prepared = original(tasks, state, config, source, effects, store, effects_log, order)
        _checkpoint_ordered_sub_tl_invocation(store, tasks[0].name, invocation_id)
        return store.load(), prepared

    monkeypatch.setattr(driver_module, "_prepare_sub_tl_stage", prepare)


def _aggregate_watcher_fixture(
    invocation_id: str,
    *,
    slice_id: str = "aggregate-child",
    author_agent: str = "aggregate-child",
    head_branch: str = "main.aggregate-child",
    base_branch: str = "main",
    succession_invocation_ids: tuple[str, ...] = (),
) -> JsonObject:
    publication = {
        "invocation_id": invocation_id,
        "slice_id": slice_id,
        "author_agent": author_agent,
        "succession_invocation_ids": list(succession_invocation_ids),
    }
    return {
        "pr_number": 43,
        "publication": publication,
        "watcher": {
            "found": True,
            "pr_number": 43,
            "head_sha": "head-a",
            "base_sha": "base-a",
            "patch_digest": "patch-a",
            "head_branch": head_branch,
            "base_branch": base_branch,
            "publication_ownership_verified": True,
            "publication_ownership_error": "",
            "head_reachable": True,
            "publication": publication,
        },
    }


@dataclass
class NamedRecordingTransport(RecordingTransport):
    identities: list[tuple[str, str, str]] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        self.identities.append((role, name, tool_name))
        return super().call_tool(role, name, tool_name, arguments)


def test_required_aggregate_completion_needs_publication_and_post_merge(
    tmp_path: Path,
) -> None:
    required = SubTLTask("required", WorkPlan(), order=1)
    optional = SubTLTask(
        "optional",
        WorkPlan(),
        order=1,
        integration=IntegrationContract(aggregate_pr_required=False),
    )
    plan = WorkPlan(sub_tls=(required, optional))
    run_id = "aggregate-contract"
    create(
        run_id,
        {"slices": _initial_slices(plan, TLLoopConfig(), tmp_path, run_id)},
        root_dir=tmp_path,
    )
    slices = RunStore(run_id, tmp_path).load().slices
    incomplete_required = replace(
        slices["required"],
        status=SliceStatus.MERGED,
        pr_number=None,
        post_merge=None,
    )
    complete_optional = replace(
        slices["optional"],
        status=SliceStatus.MERGED,
        pr_number=None,
        post_merge=None,
    )

    assert not _ordered_child_complete(required, incomplete_required)
    assert _ordered_child_complete(optional, complete_optional)


def test_live_ordered_batch_uses_independent_durable_controllers(tmp_path: Path) -> None:
    root = tmp_path / "parent"
    source = SyntheticQueue([])
    tasks = (
        SubTLTask("alpha", WorkPlan(), source=source, order=1),
        SubTLTask("beta", WorkPlan(), source=SyntheticQueue([]), order=1),
    )
    config = TLLoopConfig(
        active=True,
        root_dir=root,
        run_id="parent",
        ledger_run_id="swarm-uuid",
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )
    effects = EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
    outcomes = _run_sub_tl_batch(
        tasks,
        config,
        source,
        effects,
        RunStore("parent", tmp_path),
        BudgetLedger(tokens=0, wall_seconds=0),
    )

    assert [phase for _, phase, _ in outcomes] == [TLPhase.TLDone, TLPhase.TLDone]
    assert all(child_state is not None for _, _, child_state in outcomes)
    assert RunStore("alpha", root).load().parent_run_id == "parent"
    assert RunStore("beta", root).load().parent_run_id == "parent"
    assert RunStore("alpha", root).load().ledger_run_id == "swarm-uuid"
    assert RunStore("beta", root).load().ledger_run_id == "swarm-uuid"


def test_live_ordered_child_starts_beneath_archived_root_claim(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / ".exo" / "tl-loop"
    root_worktree = str(tmp_path / ".worktrees" / "root")
    child_worktree = str(tmp_path / ".worktrees" / "root" / "stage-a")
    create(
        "root",
        {
            "owner_branch": "main",
            "owner_worktree": root_worktree,
            "repository_identity": {"remote_url": "https://forge.example.com/org/repo.git"},
        },
        root_dir=state_root,
    )

    archived_run = state_root / "root.invalid-1789772880851" / "stage-a" / "run.json"
    archived_run.parent.mkdir(parents=True)
    archived_run.write_text(
        json.dumps(
            {
                "version": 1,
                "revision": 133,
                "run_id": "stage-a",
                "owner_worktree": child_worktree,
                "fsm": {"phase": TLPhase.TLRunning.value, "waiting": []},
            }
        ),
        encoding="utf-8",
    )
    archived_bytes = archived_run.read_bytes()

    config = TLLoopConfig(
        active=True,
        root_dir=state_root,
        run_id="root",
        branch="main",
        worktree=root_worktree,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )
    effects = EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"))
    task = SubTLTask("stage-a", WorkPlan(), source=SyntheticQueue([]), order=1)
    outcomes = _run_sub_tl_batch(
        (task,),
        config,
        SyntheticQueue([]),
        effects,
        RunStore("root", state_root),
        BudgetLedger(tokens=0, wall_seconds=0),
    )

    assert [phase for _, phase, _ in outcomes] == [TLPhase.TLDone]
    replacement = RunStore("stage-a", state_root / "root").load()
    assert replacement.owner_worktree == child_worktree
    assert (state_root / "root" / "stage-a" / "run.json").is_file()
    assert archived_run.read_bytes() == archived_bytes


def test_live_ordered_child_is_provisioned_before_controller_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def provision(self: TransportClient, parent_name: str, **kwargs: object) -> str:
        del self, parent_name, kwargs
        calls.append("provision")
        return "server-invocation"

    def run_child(root_spec: object, config: TLLoopConfig, budgets: object) -> None:
        del root_spec, config, budgets
        calls.append("run")

    monkeypatch.setattr(TransportClient, "provision_ordered_sub_tl", provision)
    monkeypatch.setattr("tl_loop.loop.driver.tl_run", run_child)
    _run_live_sub_tl(
        SubTLTask("child", WorkPlan(), order=1),
        TLLoopConfig(
            active=True,
            project_root=tmp_path,
            branch="main",
            worktree=tmp_path,
        ),
        SyntheticQueue([]),
        EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"), name="parent"),
        RunStore("parent", tmp_path / "state"),
        BudgetLedger(tokens=0, wall_seconds=0),
    )

    assert calls == ["provision", "run"]


def test_live_child_owns_scoped_ledger_source_after_fork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    segment = segments / "segment-000000000001.jsonl"
    segment.write_text("", encoding="utf-8")
    context = multiprocessing.get_context("fork")
    observed = context.Queue()

    def fake_tl_run(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del root_spec, budgets
        child_source = cast(LedgerQueue, config.source)
        observed.put(
            (
                "started",
                str(child_source.reader.run_dir),
                child_source.reader.ledger_run_id,
                child_source.reader.scope_agent_id,
            )
        )
        event = child_source.get(timeout=3)
        observed.put(("event", event.event_type, event.agent_id))
        return SimpleNamespace(
            final_state=SimpleNamespace(
                fsm=SimpleNamespace(phase=TLPhase.TLDone),
                recursive_fsm=None,
            )
        )

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", fake_tl_run)
    store = RunStore("parent", tmp_path / "state")
    task = SubTLTask("child", WorkPlan(), agent_id="child-agent")
    config = TLLoopConfig(
        active=True,
        root_dir=store.root_dir,
        run_id="parent",
        ledger_run_id="swarm-uuid",
        poll_interval=0.01,
    )
    process = context.Process(
        target=_run_live_sub_tl,
        args=(
            task,
            config,
            segments,
            EffectClient(TransportClient(socket_path=tmp_path / "unused.sock")),
            store,
            BudgetLedger(tokens=0, wall_seconds=0),
        ),
    )
    process.start()
    try:
        started = observed.get(timeout=3)
        assert started == (
            "started",
            str(store.run_dir / "child"),
            "swarm-uuid",
            "child-agent",
        )
        segment.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "event_id": "spawned-1",
                    "id": "spawned-1",
                    "event_time": "2026-08-11T00:00:00Z",
                    "observed_at": "2026-08-11T00:00:00Z",
                    "run_seq": 1,
                    "type": "agent.spawned",
                    "agent_id": "child-agent",
                    "parent_agent_id": "parent",
                    "run_id": "swarm-uuid",
                    "session_id": "session-1",
                    "lifecycle_state": "observed",
                    "data": {
                        "child_agent": "child-agent",
                        "agent_type": "codex",
                        "branch": "main.child",
                        "intent_id": "child-intent",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        assert observed.get(timeout=3) == ("event", "agent.spawned", "child-agent")
        process.join(timeout=3)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)


def test_live_child_consumes_delayed_publication_after_staying_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    segment = segments / "segment-000000000001.jsonl"
    segment.write_text("", encoding="utf-8")
    context = multiprocessing.get_context("fork")
    observed = context.Queue()

    def fake_tl_run(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del root_spec, budgets
        child_source = cast(LedgerQueue, config.source)
        observed.put(("started", child_source.reader.scope_agent_id))
        spawned = child_source.get(timeout=3)
        observed.put(("spawned", spawned.event_type))
        publication = child_source.get(timeout=3)
        observed.put(("publication", publication.event_type, publication.pr_number))
        completion = child_source.get(timeout=3)
        observed.put(("completion", completion.event_type, completion.agent_id))
        return SimpleNamespace(
            final_state=SimpleNamespace(
                fsm=SimpleNamespace(phase=TLPhase.TLDone),
                recursive_fsm=None,
            )
        )

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", fake_tl_run)
    store = RunStore("parent", tmp_path / "state")
    task = SubTLTask("child", WorkPlan(), agent_id="child-controller")
    config = TLLoopConfig(
        active=True,
        root_dir=store.root_dir,
        run_id="parent",
        ledger_run_id="swarm-uuid",
        poll_interval=0.01,
    )
    process = context.Process(
        target=_run_live_sub_tl,
        args=(
            task,
            config,
            segments,
            EffectClient(TransportClient(socket_path=tmp_path / "unused.sock")),
            store,
            BudgetLedger(tokens=0, wall_seconds=0),
        ),
    )
    process.start()
    try:
        assert observed.get(timeout=3) == ("started", "child-controller")
        segment.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "event_id": "spawned-1",
                    "id": "spawned-1",
                    "event_time": "2026-08-11T00:00:00Z",
                    "observed_at": "2026-08-11T00:00:00Z",
                    "run_seq": 1,
                    "type": "agent.spawned",
                    "agent_id": "child-controller",
                    "parent_agent_id": "parent",
                    "run_id": "swarm-uuid",
                    "session_id": "session-1",
                    "lifecycle_state": "observed",
                    "data": {
                        "child_agent": "dev-leaf",
                        "agent_type": "codex",
                        "branch": "main.child",
                        "intent_id": "child-intent",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        assert observed.get(timeout=3) == ("spawned", "agent.spawned")
        assert process.is_alive()
        with segment.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_id": "published-2",
                        "id": "published-2",
                        "event_time": "2026-08-11T00:00:01Z",
                        "observed_at": "2026-08-11T00:00:01Z",
                        "run_seq": 2,
                        "type": "pr.filed",
                        "agent_id": "dev-leaf",
                        "run_id": "swarm-uuid",
                        "session_id": "session-1",
                        "lifecycle_state": "observed",
                        "data": {
                            "slice_id": "child",
                            "pr_number": 42,
                            "head_sha": "head-child",
                            "head_branch": "main.child",
                            "base_branch": "main",
                        },
                    }
                )
                + "\n"
            )
        assert observed.get(timeout=3) == ("publication", "pr.filed", 42)
        with segment.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_id": "completed-3",
                        "id": "completed-3",
                        "event_time": "2026-08-11T00:00:02Z",
                        "observed_at": "2026-08-11T00:00:02Z",
                        "run_seq": 3,
                        "type": "agent.completed",
                        "agent_id": "dev-leaf",
                        "run_id": "swarm-uuid",
                        "session_id": "session-1",
                        "lifecycle_state": "completed",
                        "data": {"slice_id": "child", "status": "success", "pr_number": 42},
                    }
                )
                + "\n"
            )
        assert observed.get(timeout=3) == ("completion", "agent.completed", "dev-leaf")
        process.join(timeout=3)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)


def test_live_restart_revalidates_child_identity_before_controller_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    parent_store = RunStore("parent", state_root)
    create("child", {}, root_dir=parent_store.run_dir)
    child_store = RunStore("child", parent_store.run_dir)
    checkpoint_before = child_store.load()
    calls: list[str] = []

    def provision(self: TransportClient, parent_name: str, **kwargs: object) -> str:
        del self, parent_name, kwargs
        calls.append("provision")
        return "server-invocation"

    def run_child(root_spec: object, config: TLLoopConfig, budgets: object) -> None:
        del root_spec, config, budgets
        calls.append("run")

    monkeypatch.setattr(TransportClient, "provision_ordered_sub_tl", provision)
    monkeypatch.setattr("tl_loop.loop.driver.tl_run", run_child)
    task = SubTLTask("child", WorkPlan(), source=SyntheticQueue([]), order=1)
    _run_live_sub_tl(
        task,
        TLLoopConfig(active=True, project_root=tmp_path, root_dir=state_root),
        SyntheticQueue([]),
        EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"), name="parent"),
        parent_store,
        BudgetLedger(tokens=0, wall_seconds=0),
    )

    assert calls == ["provision", "run"]
    assert child_store.load() == checkpoint_before


def test_live_restart_fails_before_controller_on_identity_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    parent_store = RunStore("parent", state_root)
    create("child", {}, root_dir=parent_store.run_dir)
    calls: list[str] = []

    def provision(self: TransportClient, parent_name: str, **kwargs: object) -> None:
        del self, parent_name, kwargs
        calls.append("provision")
        raise RuntimeError("ordered sub-TL identity conflict")

    def run_child(root_spec: object, config: TLLoopConfig, budgets: object) -> None:
        del root_spec, config, budgets
        calls.append("run")

    monkeypatch.setattr(TransportClient, "provision_ordered_sub_tl", provision)
    monkeypatch.setattr("tl_loop.loop.driver.tl_run", run_child)
    task = SubTLTask("child", WorkPlan(), source=SyntheticQueue([]), order=1)

    with pytest.raises(RuntimeError, match="identity conflict"):
        _run_live_sub_tl(
            task,
            TLLoopConfig(active=True, project_root=tmp_path, root_dir=state_root),
            SyntheticQueue([]),
            EffectClient(TransportClient(socket_path=tmp_path / "unused.sock"), name="parent"),
            parent_store,
            BudgetLedger(tokens=0, wall_seconds=0),
        )

    assert calls == ["provision"]


def test_live_waiting_child_is_not_terminated_by_elapsed_supervision() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True
            self.joins: list[float | None] = []
            self.terminated = False

        def join(self, timeout: float | None = None) -> None:
            self.joins.append(timeout)
            if len(self.joins) >= 2:
                self.alive = False

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False

    process = FakeProcess()
    child_store = SimpleNamespace(
        load=lambda: SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLWaiting)),
        record_exit_reason=lambda reason: pytest.fail(reason),
    )

    state = _supervise_live_sub_tl(
        process,
        child_store,
        TLLoopConfig(
            keep_alive_on_waiting=True,
            poll_interval=0.001,
        ),
    )

    assert state is not None
    assert process.joins == [0.05, 0.05]
    assert not process.terminated


def test_recursive_tl_waiting_child_is_not_marked_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def waiting_child(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del root_spec, config, budgets
        return SimpleNamespace(
            final_state=SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLWaiting), slices={})
        )

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", waiting_child)
    result = run_tl_loop(
        "waiting-child-parent",
        WorkPlan(sub_tls=(SubTLTask("waiting-child", WorkPlan(), order=1),)),
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=TLLoopConfig(
            active=True,
            keep_alive_on_waiting=False,
            max_parallel_slices=1,
            poll_interval=0.001,
        ),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLRunning
    assert result.final_state.slices["waiting-child"].status is SliceStatus.SPAWNED


def test_recursive_recovery_is_projected_without_advancing_higher_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery = begin_recovery(
        cause="external_dependency",
        owner_run_id="recovering-child",
        slice_attempt=1,
        owner_agent_id="leaf-agent",
        entered_at=1,
    )
    recovering = SimpleNamespace(
        run_id="recovering-child",
        fsm=SimpleNamespace(phase=TLPhase.TLWaiting),
        slices={"leaf": SimpleNamespace(recovery=recovery)},
    )
    completed = SimpleNamespace(
        run_id="sibling-child",
        fsm=SimpleNamespace(phase=TLPhase.TLDone),
        slices={},
    )

    def fake_tl_run(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del config, budgets
        child_name = cast(dict[str, object], root_spec)["run_id"]
        return SimpleNamespace(
            final_state=recovering if child_name == "recovering-child" else completed
        )

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", fake_tl_run)
    plan = WorkPlan(
        sub_tls=(
            SubTLTask("recovering-child", WorkPlan(), order=1),
            SubTLTask("sibling-child", WorkPlan(), order=1),
            SubTLTask("later-child", WorkPlan(), order=2),
        )
    )
    result = run_tl_loop(
        "recovery-parent",
        plan,
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=TLLoopConfig(
            max_parallel_slices=2,
            max_workers=0,
            keep_alive_on_waiting=False,
            poll_interval=0.001,
        ),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLRunning
    assert (
        result.final_state.integration.sub_tl_states["recovering-child"]
        is SubTLLifecycle.RECOVERING
    )
    summary = result.final_state.integration.sub_tl_recovery["recovering-child"]
    assert summary.owner_run_id == "recovering-child"
    assert summary.child_path == ("recovery-parent", "recovering-child", "leaf")
    assert result.final_state.slices["sibling-child"].status is SliceStatus.MERGED
    assert result.final_state.slices["later-child"].status is SliceStatus.PENDING


def test_nested_recovery_projection_preserves_complete_owner_path() -> None:
    summary = ChildRecoverySummary(
        owner_run_id="inner",
        child_path=("outer", "inner", "leaf"),
        slice_id="leaf",
        cause="external_dependency",
        recovery_round=2,
        next_probe_at=12.0,
    )
    outer = SimpleNamespace(
        run_id="outer",
        slices={"inner": SimpleNamespace(recovery=None)},
        integration=SimpleNamespace(
            sub_tl_states={"inner": SubTLLifecycle.HUMAN_GATE},
            sub_tl_recovery={"inner": summary},
        ),
    )

    lifecycle, projected = _child_recovery_projection(outer, "root")
    assert lifecycle is SubTLLifecycle.HUMAN_GATE
    assert projected.child_path == ("root", "outer", "inner", "leaf")


def test_live_waiting_child_is_terminated_on_explicit_cancellation() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True
            self.terminated = False

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False

    process = FakeProcess()
    reasons: list[str] = []
    cancel_event = threading.Event()
    cancel_event.set()
    child_store = SimpleNamespace(
        load=lambda: SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLWaiting)),
        record_exit_reason=reasons.append,
    )

    state = _supervise_live_sub_tl(
        process,
        child_store,
        TLLoopConfig(
            keep_alive_on_waiting=True,
            cancel_event=cancel_event,
        ),
    )

    assert state is not None
    assert process.terminated
    assert reasons == ["sub-TL controller cancelled explicitly"]


def test_exited_waiting_child_is_failed_as_process_loss() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True
            self.exitcode = 0

        def join(self, timeout: float | None = None) -> None:
            del timeout
            self.alive = False

        def is_alive(self) -> bool:
            return self.alive

    reasons: list[str] = []
    child_store = SimpleNamespace(
        load=lambda: SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLWaiting)),
        record_exit_reason=reasons.append,
    )

    state = _supervise_live_sub_tl(
        FakeProcess(),
        child_store,
        TLLoopConfig(poll_interval=0.001),
    )

    assert state is None
    assert reasons == ["sub-TL controller exited before authoritative resolution with code 0"]


@dataclass
class IntegrationTransport(RecordingTransport):
    snapshots: list[JsonObject] = field(default_factory=list)
    merge_response: JsonObject | None = None
    last_snapshot: JsonObject | None = None

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        if tool_name == "watcher_pr_state":
            self.calls.append((tool_name, arguments))
            if self.snapshots:
                self.last_snapshot = self.snapshots.pop(0)
            snapshot = self.last_snapshot or {"found": False}
            record = next(
                (
                    item
                    for item in self.aggregate_reconciliations.values()
                    if item.get("pr_number") == arguments.get("pr_number")
                ),
                self.aggregate_reconciliation,
            )
            watcher = record.get("watcher") if isinstance(record, Mapping) else None
            if isinstance(watcher, Mapping):
                snapshot = {**watcher, **snapshot}
            return {"success": True, "result": snapshot}
        if tool_name == "merge_pr" and self.merge_response is not None:
            self.calls.append((tool_name, arguments))
            return self.merge_response
        return super().call_tool(role, name, tool_name, arguments)


@dataclass
class OrderedIntegrationTransport(IntegrationTransport):
    """Deterministic Forgejo-shaped transport for recursive acceptance tests."""

    snapshot_history: list[JsonObject] = field(default_factory=list)
    observed_root_store: RunStore | None = None

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        if tool_name == "repository_identity":
            self.calls.append((tool_name, arguments))
            return {
                "success": True,
                "result": {"remote_url": "https://forge.example.test/test/repository.git"},
            }
        if tool_name == "file_pr":
            self.calls.append((tool_name, arguments))
            title = str(arguments.get("title", ""))
            sub_tl_id = "alpha" if "alpha" in title else "beta"
            base_branch = str(arguments.get("base_branch", "main"))
            pr_number = 101 if sub_tl_id == "alpha" else 102
            head_sha, head_tree = self.aggregate_git_evidence.get(
                sub_tl_id, (f"head-{sub_tl_id}", "")
            )
            patch_digest = f"patch-{sub_tl_id}"
            base_sha = "base-alpha" if sub_tl_id == "alpha" else "base-after-alpha"
            head_branch = f"{base_branch}.{sub_tl_id}"
            publication = {
                "invocation_id": f"{sub_tl_id}-invocation",
                "slice_id": sub_tl_id,
                "author_agent": sub_tl_id,
                "succession_invocation_ids": [],
            }
            self.aggregate_reconciliations[sub_tl_id] = {
                "pr_number": pr_number,
                "publication": publication,
                "watcher": {
                    "found": True,
                    "pr_number": pr_number,
                    "merged": False,
                    "head_sha": head_sha,
                    "base_sha": base_sha,
                    "patch_digest": patch_digest,
                    "pr_head_tree_sha": head_tree,
                    "merge_tree_sha": f"tree-{sub_tl_id}",
                    "ci_status": "success",
                    "head_branch": head_branch,
                    "base_branch": base_branch,
                    "head_reachable": True,
                    "publication_ownership_verified": True,
                    "publication_ownership_error": "",
                    "publication": publication,
                },
            }
            return {
                "success": True,
                "result": {
                    "pr_number": pr_number,
                    "head_sha": head_sha,
                    "patch_digest": patch_digest,
                    "base_sha": base_sha,
                    "head_branch": head_branch,
                    "base_branch": base_branch,
                    "slice_id": sub_tl_id,
                    "invocation_id": f"{sub_tl_id}-invocation",
                    "author_agent": sub_tl_id,
                },
            }
        if tool_name == "watcher_pr_state":
            self.calls.append((tool_name, arguments))
            record = next(
                (
                    candidate
                    for candidate in self.aggregate_reconciliations.values()
                    if candidate.get("pr_number") == arguments.get("pr_number")
                ),
                None,
            )
            if record is None:
                return {"success": True, "result": {"found": False}}
            snapshot = record.get("watcher")
            if not isinstance(snapshot, dict):
                return {"success": True, "result": {"found": False}}
            self.snapshot_history.append(dict(snapshot))
            return {"success": True, "result": dict(snapshot)}
        if (
            tool_name == "emit_controller_event"
            and self.observed_root_store is not None
            and arguments.get("event_type") == "tl.dispatch_confirmed"
        ):
            payload = arguments.get("payload")
            if isinstance(payload, dict) and payload.get("slice_id") == "beta":
                alpha = self.observed_root_store.load().slices["alpha"]
                assert alpha.status is SliceStatus.MERGED
                assert alpha.post_merge is not None
                assert alpha.post_merge.phase is PostMergePhase.COMPLETE
        return super().call_tool(role, name, tool_name, arguments)


def _effect_names(transport: RecordingTransport) -> list[str]:
    return [
        name
        for name, _ in transport.calls
        if name not in {"emit_controller_event", "repository_identity"}
    ]


def _effect_operations(result: TLRunResult) -> list[str]:
    return [
        effect.operation
        for effect in result.effects
        if effect.operation not in {"emit_controller_event", "repository_identity"}
    ]


def _merge_decisions(transport: RecordingTransport) -> list[JsonObject]:
    return [
        cast(JsonObject, arguments["payload"])
        for name, arguments in transport.calls
        if name == "emit_controller_event" and arguments["event_type"] == "tl.merge_decided"
    ]


@dataclass
class ReviewRepairTransport(RecordingTransport):
    """Effect double that exposes the PR state required by compose_repair."""

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, arguments))
        if tool_name == "watcher_pr_state":
            return {
                "success": True,
                "result": {
                    "open": True,
                    "merged": False,
                    "head_branch": "main.leaf-a",
                    "head_sha": "head-a",
                },
            }
        return {"success": True, "result": None}


@dataclass
class ReviewBackend:
    responses: list[object]
    requests: list[RlmRequest] = field(default_factory=list)

    def complete(self, request: RlmRequest) -> object:
        self.requests.append(request)
        return self.responses.pop(0)


def test_active_loop_dispatches_direct_children_and_merges_leaf(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport()
    source = SyntheticQueue(_lifecycle_events("active-run"))
    result = run_tl_loop(
        "active-run",
        _plan(),
        source,
        EffectClient(transport),
        config=_config(),
        root_dir=tmp_path,
    )

    assert _effect_names(transport) == [
        "spawn_worker",
        "spawn_leaf",
        "root_branch_finalize",
    ]
    assert _effect_operations(result) == [
        "spawn_worker",
        "spawn_leaf",
        "root_branch_finalize",
    ]
    assert all(intent.executed for intent in result.effects)
    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert result.final_state.events.last_consumed_offset == 5
    assert result.final_state.slices["worker-a"].status.value == "spawned"
    assert result.final_state.slices["leaf-a"].pr_number is None
    assert result.final_state.slices["leaf-a"].reviewed_head is None
    assert result.final_state.slices["leaf-a"].status.value == "spawned"
    assert result.final_state.slices["worker-a"].dispatch_authoritative_event_seq == 1
    assert result.final_state.slices["leaf-a"].dispatch_authoritative_event_seq == 2
    assert result.final_state.slices["worker-a"].dispatch_last_boundary == "agent.spawned"
    assert result.final_state.slices["leaf-a"].dispatch_last_boundary == "agent.spawned"
    assert source.acknowledged == [1, 2, 3, 4, 5]


def test_active_loop_records_typed_task_block_without_failing_controller(
    tmp_path: Path,
) -> None:
    run_id = "task-blocked-run"
    cancel_event = threading.Event()
    source = SyntheticQueue(
        [_blocked_event(1, "leaf-a", run_id, invocation_id=None, generation=None)]
    )
    outcome: dict[str, BaseException] = {}

    class BlockedTransport(RecordingTransport):
        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            result = super().call_tool(role, name, tool_name, arguments)
            if tool_name == "chainlink_issue_create":
                return {"success": True, "result": {"issue_id": 704}}
            return result

    def run() -> None:
        try:
            run_tl_loop(
                run_id,
                _plan(),
                source,
                EffectClient(BlockedTransport()),
                config=TLLoopConfig(
                    max_workers=1,
                    max_leaves=1,
                    poll_interval=0.001,
                    cancel_event=cancel_event,
                ),
                root_dir=tmp_path,
            )
        except BaseException as error:  # noqa: BLE001 - assert cancellation boundary
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while source.acknowledged != [1] and time.monotonic() < deadline:
        time.sleep(0.001)
    cancel_event.set()
    thread.join(timeout=2)

    state = RunStore(run_id, tmp_path).load()
    assert isinstance(outcome.get("error"), LoopCancelled)
    assert state.fsm.phase is not TLPhase.TLFailed
    assert state.slices["leaf-a"].status is SliceStatus.DISPATCH_UNCONFIRMED
    assert state.slices["leaf-a"].park_cause is ParkCause.DISPATCH_UNCONFIRMED
    assert state.slices["leaf-a"].recovery is not None
    assert state.slices["leaf-a"].recovery.phase.value == "diagnosing"
    assert state.slices["leaf-a"].recovery.cause == ParkCause.BASE_CI_UNSTABLE.value


def _blocked_event(
    run_seq: int,
    slug: str,
    run_id: str,
    *,
    reporter: str | None = None,
    invocation_id: str | None = "inv-1",
    generation: int | None = 1,
    attempt: int = 1,
    cause: str = "base_ci_unstable",
    scope_attribution: str = "base",
    matched_difficulty_rule: str = "standard_slice",
    role: str = "worker",
) -> EventEnvelope:
    return project(
        {
            "schema_version": 1,
            "event_id": f"blocked-{run_seq}",
            "id": f"blocked-{run_seq}",
            "event_time": "2026-08-11T00:00:00Z",
            "observed_at": "2026-08-11T00:00:00Z",
            "run_seq": run_seq,
            "type": "agent.task_blocked",
            "agent_id": reporter or slug,
            "run_id": run_id,
            "session_id": "session-1",
            "invocation_id": invocation_id,
            "generation": generation,
            "harness": "codex",
            "role": role,
            "lifecycle_state": "authoritative",
            "data": {
                "outcome": "blocked",
                "slice_id": slug,
                "cause": cause,
                "scope_attribution": scope_attribution,
                "needs_human": True,
                "retryable": True,
                "recovery_action": "repair base CI",
                "declared_difficulty": "standard",
                "matched_difficulty_rule": matched_difficulty_rule,
                "attempt": attempt,
            },
        }
    )


def _seed_nested_aggregate_in_review(root_store: RunStore) -> RunState:
    state = root_store.load()
    current = state.slices["architecture"]
    aggregate_head = (
        _test_git_output(Path(current.worktree), "rev-parse", "HEAD") if current.worktree else None
    ) or "e" * 40
    publication = PublicationBinding(
        pr_number=273,
        head_sha=aggregate_head,
        head_branch=current.branch or "main.architecture",
        base_branch="main",
        attempt=1,
        invocation_id="aggregate-invocation",
    )
    waiting = replace(
        current,
        status=SliceStatus.IN_REVIEW,
        pr_number=publication.pr_number,
        publication=publication,
        dispatch_agent_id=f"{root_store.run_id}:architecture:integration",
        dispatch_invocation_id=publication.invocation_id,
        dispatch_generation=1,
        dispatch_last_boundary="aggregate_pr_open",
        reviewer_agent_id="aggregate-reviewer",
        verdict=None,
        ci_state={},
    )
    candidate = IntegrationCandidateState(
        lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
        aggregate_pr_number=publication.pr_number,
        aggregate_head_sha=publication.head_sha,
        aggregate_patch_digest="aggregate-patch",
        aggregate_original_base_sha="aggregate-base",
        integration_owner_id=f"{root_store.run_id}:architecture:integration",
        integration_owner_run_id="architecture",
        integration_owner_branch=current.branch,
        integration_owner_worktree=current.worktree,
        head_sha=publication.head_sha,
        patch_digest="aggregate-patch",
    )
    integration = replace(
        state.integration,
        candidates={**state.integration.candidates, "architecture": candidate},
    )
    return root_store.checkpoint(
        state.recursive_fsm or state.fsm,
        {**state.slices, "architecture": waiting},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=integration,
    )


def _seed_nested_aggregate_with_original_dispatch_owner(root_store: RunStore) -> RunState:
    """Model the current split between subTL dispatch and integration ownership."""
    state = _seed_nested_aggregate_in_review(root_store)
    current = state.slices["architecture"]
    return root_store.checkpoint(
        state.recursive_fsm or state.fsm,
        {**state.slices, "architecture": replace(current, dispatch_agent_id="architecture")},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )


def _aggregate_watcher_snapshot(
    state: RunState,
    *,
    publication_author: str = "architecture",
    publication_invocation: str = "aggregate-invocation",
) -> JsonObject:
    """Return the exact watcher evidence needed to reconcile one aggregate."""
    current = state.slices["architecture"]
    publication = current.publication
    candidate = state.integration.candidates["architecture"]
    assert publication is not None
    assert candidate.aggregate_head_sha is not None
    assert candidate.aggregate_original_base_sha is not None
    assert candidate.aggregate_patch_digest is not None
    return {
        "found": True,
        "pr_number": candidate.aggregate_pr_number,
        "merged": False,
        "head_sha": candidate.aggregate_head_sha,
        "base_sha": candidate.aggregate_original_base_sha,
        "patch_digest": candidate.aggregate_patch_digest,
        "merge_tree_sha": "aggregate-tree",
        "ci_status": "success",
        "head_branch": current.branch,
        "base_branch": current.base_ref,
        "head_reachable": True,
        "publication_ownership_verified": True,
        "publication_ownership_error": "",
        "publication": {
            "invocation_id": publication_invocation,
            "slice_id": "architecture",
            "author_agent": publication_author,
            "succession_invocation_ids": [],
        },
    }


def test_legacy_aggregate_coordinator_binding_reconciles_from_exact_publication(
    tmp_path: Path,
) -> None:
    run_id = "legacy-aggregate-owner"
    plan, root_store, _ = _nested_blocked_owner_stores(tmp_path, run_id)
    repository = RepositoryIdentity("https://forge.example.test/test/repository.git")
    root_store.set_repository_identity(repository)
    seeded = _seed_nested_aggregate_in_review(root_store)
    root_store.set_gate(driver_module.INTEGRITY_RECONCILIATION_GATE_NAME, GateStatus.PENDING)
    root_store.set_gate("unrelated-human-gate", GateStatus.PENDING)
    snapshot = _aggregate_watcher_snapshot(seeded)
    transport = IntegrationTransport(snapshots=[snapshot])
    config = replace(
        _config(),
        root_dir=tmp_path,
        run_id=run_id,
        repository_identity=repository,
        ledger_run_id=run_id,
    )

    reconciled = driver_module._reconcile_nonterminal_slices(
        plan,
        root_store.load(),
        config,
        EffectClient(transport),
        root_store,
        [],
    )

    current = reconciled.slices["architecture"]
    coordinator_id = f"{run_id}:architecture:integration"
    assert current.dispatch_agent_id == "architecture"
    assert current.dispatch_invocation_id == "aggregate-invocation"
    assert reconciled.integration.candidates["architecture"].integration_owner_id == coordinator_id
    assert not any(
        gate.name == driver_module.INTEGRITY_RECONCILIATION_GATE_NAME
        and gate.status is GateStatus.PENDING
        for gate in reconciled.gates
    )
    assert any(
        gate.name == "unrelated-human-gate" and gate.status is GateStatus.PENDING
        for gate in reconciled.gates
    )
    identity_audits = [
        arguments["payload"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
        and arguments.get("event_type") == "tl.aggregate_dispatch_identity_reconciled"
    ]
    assert len(identity_audits) == 1
    assert identity_audits[0]["old_dispatch_agent_id"] == coordinator_id
    assert identity_audits[0]["restored_dispatch_agent_id"] == "architecture"
    assert identity_audits[0]["dispatch_invocation_id"] == "aggregate-invocation"
    assert identity_audits[0]["child_manifest_digest"]

    replay_transport = IntegrationTransport(snapshots=[snapshot])
    replayed = driver_module._reconcile_nonterminal_slices(
        plan,
        root_store.load(),
        config,
        EffectClient(replay_transport),
        root_store,
        [],
    )
    assert replayed.slices["architecture"].dispatch_agent_id == "architecture"
    assert not any(
        arguments.get("event_type") == "tl.aggregate_dispatch_identity_reconciled"
        for name, arguments in replay_transport.calls
        if name == "emit_controller_event"
    )


def test_legacy_aggregate_coordinator_binding_stays_parked_on_owner_disagreement(
    tmp_path: Path,
) -> None:
    run_id = "legacy-aggregate-owner-conflict"
    plan, root_store, _ = _nested_blocked_owner_stores(tmp_path, run_id)
    repository = RepositoryIdentity("https://forge.example.test/test/repository.git")
    root_store.set_repository_identity(repository)
    seeded = _seed_nested_aggregate_in_review(root_store)
    root_store.set_gate(driver_module.INTEGRITY_RECONCILIATION_GATE_NAME, GateStatus.PENDING)
    transport = IntegrationTransport(
        snapshots=[_aggregate_watcher_snapshot(seeded, publication_author="foreign-owner")]
    )
    config = replace(
        _config(),
        root_dir=tmp_path,
        run_id=run_id,
        repository_identity=repository,
        ledger_run_id="ordered-run-ledger",
    )

    reconciled = driver_module._reconcile_nonterminal_slices(
        plan,
        root_store.load(),
        config,
        EffectClient(transport),
        root_store,
        [],
    )

    assert reconciled.slices["architecture"].dispatch_agent_id == (
        f"{run_id}:architecture:integration"
    )
    assert any(
        gate.name == driver_module.INTEGRITY_RECONCILIATION_GATE_NAME
        and gate.status is GateStatus.PENDING
        for gate in reconciled.gates
    )
    assert not any(
        arguments.get("event_type") == "tl.aggregate_dispatch_identity_reconciled"
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    )


def test_historical_child_merge_does_not_complete_parent_aggregate(
    tmp_path: Path,
) -> None:
    run_id = "historical-child-merge"
    cancel_event = threading.Event()
    plan, root_store, _ = _nested_blocked_owner_stores(tmp_path, run_id)
    root_store.set_repository_identity(
        RepositoryIdentity("https://forge.example.test/test/repository.git")
    )
    seeded = _seed_nested_aggregate_in_review(root_store)
    before = seeded.slices["architecture"]
    event = _canonical_event(88, "pr.merged", "architecture", run_id, pr_number=45)

    class ObservingQueue(SyntheticQueue):
        def acknowledge(self, observed: EventEnvelope) -> int:
            current = root_store.load().slices["architecture"]
            assert current.pr_number == 273
            assert current.status is SliceStatus.IN_REVIEW
            assert current.post_merge is None
            assert current.dispatch_agent_id == before.dispatch_agent_id
            cancel_event.set()
            return super().acknowledge(observed)

    source = ObservingQueue([event])
    transport = RecordingTransport()
    config = replace(
        _config(),
        active=True,
        root_dir=tmp_path,
        run_id=run_id,
        ledger_run_id=run_id,
        poll_interval=0.001,
        cancel_event=cancel_event,
    )
    with pytest.raises(LoopCancelled):
        driver_module._run_loop(
            run_id,
            plan,
            source,
            EffectClient(transport),
            config,
            root_store,
            seeded,
            [],
            TLEventDecoder(),
        )

    after = root_store.load()
    aggregate = after.slices["architecture"]
    assert source.acknowledged == [88]
    assert after.current_order == 1
    assert aggregate.pr_number == 273
    assert aggregate.status is SliceStatus.IN_REVIEW
    assert aggregate.post_merge is None
    assert aggregate.action == before.action
    assert after.slices["kani"].status is SliceStatus.PENDING
    audits = root_store.audited_events()
    assert len(audits) == 1
    assert audits[0]["correlation"] == "child_merge_does_not_bind_aggregate"
    assert audits[0]["run_seq"] == 88
    assert not any(
        name in {"merge_pr", "spawn_leaf", "spawn_worker"} for name, _ in transport.calls
    )


def test_completed_nested_blocker_is_audited_without_reopening_or_bypassing_aggregate_gates(
    tmp_path: Path,
) -> None:
    run_id = "completed-nested-blocker"
    cancel_event = threading.Event()
    plan, root_store, child_store = _nested_blocked_owner_stores(
        tmp_path,
        run_id,
    )
    assert child_store is not None
    completed_state = _complete_legacy_shape_nested_leaf(child_store, event_seq=77)
    completed_leaf = completed_state.slices["leaf-a"]
    assert completed_leaf.chainlink_issue_id is None
    assert completed_leaf.attempts == 6
    assert completed_leaf.publication is not None and completed_leaf.publication.attempt == 1
    assert completed_leaf.handoff is not None and completed_leaf.handoff.attempt == 1
    assert completed_leaf.post_merge is not None
    assert completed_leaf.post_merge.evidence["repository"] == "test/repository"
    assert "issue_close_outcome" not in completed_leaf.post_merge.evidence
    action_journal_path = child_store.run_dir / "action-journal.json"
    action_journal_before = action_journal_path.read_bytes()
    root_store.set_repository_identity(
        RepositoryIdentity("https://forge.example.test/test/repository.git")
    )
    _seed_nested_aggregate_in_review(root_store)
    active_root = root_store.load()
    assert active_root.plan_manifest is not None
    recursive_root = RecursiveTLRunning(
        current_order=1,
        pending_by_order={
            1: (ChildRecord("architecture", ChildKind.SUB_TL, "architecture-intent"),),
            2: (ChildRecord("kani", ChildKind.SUB_TL),),
        },
        scope_path=(run_id,),
        plan_digest=active_root.plan_manifest.digest,
        dispatch_intents={"architecture": "architecture-intent"},
    )
    active_root = root_store.checkpoint(
        recursive_root,
        active_root.slices,
        active_root.budgets,
        active_root.events.last_consumed_offset,
        current_order=1,
        ordered_stages=active_root.ordered_stages,
        integration=active_root.integration,
    )
    owner_gate = "task-blocked-owner:77"
    root_store.set_gate(owner_gate, GateStatus.PENDING)
    root_budgets = root_store.load().budgets
    child_budgets = completed_state.budgets
    event = _blocked_event(
        77,
        "leaf-a",
        run_id,
        reporter="architecture",
        invocation_id=None,
        generation=None,
        attempt=6,
        cause="human_decision_required",
        scope_attribution="agent_lifecycle",
        matched_difficulty_rule="missing_handoff:nonzero_exit",
        role="controller",
    )
    recovery_config = replace(
        _config(),
        root_dir=tmp_path,
        run_id=run_id,
        ledger_run_id=run_id,
    )
    legacy_lookup = driver_module._resolve_task_blocked_owner(event, root_store.load(), root_store)
    assert legacy_lookup.owner is None
    assert legacy_lookup.reason_code == "reporter_scope_unproven"
    startup_transport = IntegrationTransport(
        snapshots=[_aggregate_watcher_snapshot(root_store.load())]
    )
    recovered_root = driver_module._reconcile_nonterminal_slices(
        plan,
        root_store.load(),
        recovery_config,
        EffectClient(startup_transport),
        root_store,
        [],
    )
    assert recovered_root.slices["architecture"].dispatch_agent_id == "architecture"
    assert (
        recovered_root.integration.candidates["architecture"].integration_owner_id
        == f"{run_id}:architecture:integration"
    )
    owner_lookup = driver_module._resolve_task_blocked_owner(event, recovered_root, root_store)
    assert owner_lookup.owner is not None, owner_lookup
    completion = driver_module._classify_task_blocked_disposition(
        event,
        owner_lookup.owner,
        owner_lookup.owner.store.load(),
        owner_lookup,
    )
    assert completion.decision == "superseded_by_durable_completion", completion

    class ObservingQueue(SyntheticQueue):
        def acknowledge(self, observed: EventEnvelope) -> int:
            child = child_store.load().slices["leaf-a"]
            assert child.status is SliceStatus.MERGED
            assert child.recovery is None
            assert child.post_merge == completed_leaf.post_merge
            assert child_store.load().budgets == child_budgets
            assert root_store.load().events.last_consumed_offset == observed.run_seq
            assert not any(gate.name == owner_gate for gate in root_store.load().gates)
            result = super().acknowledge(observed)
            cancel_event.set()
            return result

    source = ObservingQueue([event])
    transport = RecordingTransport()
    config = replace(
        recovery_config,
        max_parallel_slices=1,
        poll_interval=0.001,
        cancel_event=cancel_event,
    )
    with pytest.raises(LoopCancelled):
        driver_module._run_loop(
            run_id,
            plan,
            source,
            EffectClient(transport),
            config,
            root_store,
            root_store.load(),
            [],
            TLEventDecoder(),
        )

    root_after = root_store.load()
    child_after = child_store.load()
    dispositions = root_store.task_blocked_dispositions()
    assert source.acknowledged == [77]
    assert len(dispositions) == 1
    assert dispositions[0]["event_id"] == event.event_id
    assert dispositions[0]["decision"] == "superseded_by_durable_completion"
    assert dispositions[0]["subject_slice_id"] == "leaf-a"
    assert dispositions[0]["reporter_scope_path"] == [run_id, "architecture"]
    assert "blocker_attempt=6" in dispositions[0]["proof"]
    assert "completion_attempt=1" in dispositions[0]["proof"]
    assert "subject_consumed_offset=78" in dispositions[0]["proof"]
    assert root_after.current_order == 1
    assert root_after.slices["architecture"].pr_number == 273
    assert root_after.slices["architecture"].status is SliceStatus.IN_REVIEW
    assert root_after.slices["architecture"].verdict is None
    assert root_after.slices["architecture"].ci_state.get("e" * 40) == "success"
    assert root_after.slices["kani"].status is SliceStatus.PENDING
    assert root_after.budgets == root_budgets
    assert child_after.slices["leaf-a"].post_merge == completed_leaf.post_merge
    assert child_after.slices["leaf-a"].handoff == completed_leaf.handoff
    assert child_after.slices["leaf-a"].publication == completed_leaf.publication
    assert child_after.slices["leaf-a"].attempts == 6
    assert action_journal_path.read_bytes() == action_journal_before
    assert not any(
        name in {"spawn_worker", "spawn_leaf", "merge_pr", "chainlink_issue_close"}
        for name, _ in transport.calls
    )


@pytest.mark.parametrize(
    ("mutation", "expected_reason", "expected_missing"),
    [
        ("cursor", "completed_child_evidence_incomplete", "subject_scope_consumed_event"),
        ("repository", "completed_child_evidence_incomplete", "merge_repository_identity"),
        (
            "closure",
            "completed_child_evidence_incomplete",
            "legacy_issue_close_action_receipt_missing",
        ),
        ("future_attempt", "completed_attempt_mismatch", "completed_attempt_mismatch"),
    ],
)
def test_legacy_completion_shape_requires_attempt_repository_and_closure_corroboration(
    tmp_path: Path,
    mutation: str,
    expected_reason: str,
    expected_missing: str,
) -> None:
    run_id = f"legacy-completion-{mutation}"
    _, root_store, child_store = _nested_blocked_owner_stores(tmp_path, run_id)
    assert child_store is not None
    _complete_legacy_shape_nested_leaf(child_store, event_seq=90)
    _seed_nested_aggregate_with_original_dispatch_owner(root_store)
    if mutation == "cursor":
        child_state = child_store.load()
        child_store.checkpoint(
            child_state.recursive_fsm or child_state.fsm,
            child_state.slices,
            child_state.budgets,
            89,
        )
    elif mutation == "repository":
        _rewrite_legacy_completion_evidence(
            child_store,
            repository="other/repository",
        )
    elif mutation == "closure":
        _rewrite_legacy_completion_evidence(
            child_store,
            issue_close_journal_id="unrelated-close-receipt",
        )
    attempt = 7 if mutation == "future_attempt" else 6
    event = _blocked_event(
        90,
        "leaf-a",
        run_id,
        reporter="architecture",
        invocation_id=None,
        generation=None,
        attempt=attempt,
        role="controller",
    )
    lookup = driver_module._resolve_task_blocked_owner(event, root_store.load(), root_store)
    assert lookup.owner is not None

    disposition = driver_module._classify_task_blocked_disposition(
        event,
        lookup.owner,
        child_store.load(),
        lookup,
    )

    assert disposition.decision == "unresolved"
    assert disposition.reason_code == expected_reason
    assert expected_missing in disposition.missing_proof


def test_completed_nested_blocker_with_incomplete_receipts_stays_unresolved(
    tmp_path: Path,
) -> None:
    run_id = "incomplete-completed-blocker"
    plan, root_store, child_store = _nested_blocked_owner_stores(tmp_path, run_id)
    assert child_store is not None
    completed = _complete_nested_leaf(child_store)
    leaf = completed.slices["leaf-a"]
    assert leaf.handoff is not None
    child_store.checkpoint(
        completed.recursive_fsm or completed.fsm,
        {
            **completed.slices,
            "leaf-a": replace(leaf, handoff=replace(leaf.handoff, attempt=5)),
        },
        completed.budgets,
        completed.events.last_consumed_offset,
    )
    event = _blocked_event(
        88,
        "leaf-a",
        run_id,
        reporter="architecture",
        invocation_id=None,
        generation=None,
        attempt=6,
        role="controller",
    )
    source = SyntheticQueue([event])
    result = driver_module._run_loop(
        run_id,
        plan,
        source,
        EffectClient(RecordingTransport()),
        TLLoopConfig(root_dir=tmp_path, run_id=run_id),
        root_store,
        root_store.load(),
        [],
        TLEventDecoder(),
    )

    child_after = child_store.load().slices["leaf-a"]
    assert source.acknowledged == []
    assert root_store.load().events.last_consumed_offset == 0
    assert root_store.task_blocked_dispositions() == ()
    assert child_after.status is SliceStatus.MERGED
    assert child_after.recovery is None
    assert child_after.handoff is not None
    assert child_after.handoff.attempt == 5
    assert any(gate.name == "task-blocked-owner:88" for gate in result.final_state.gates)
    diagnostic = " ".join(result.diagnostics["unresolved_events"])
    assert "completed_child_evidence_incomplete" in diagnostic
    assert "handoff_attempt" in diagnostic


def test_foreign_reporter_conflict_is_diagnosed_with_scope_and_alias_evidence(
    tmp_path: Path,
) -> None:
    run_id = "foreign-blocker-reporter"
    plan, root_store, child_store = _nested_blocked_owner_stores(tmp_path, run_id)
    assert child_store is not None
    _seed_nested_aggregate_with_original_dispatch_owner(root_store)
    event = _blocked_event(
        89,
        "leaf-a",
        run_id,
        reporter="kani",
        invocation_id=None,
        generation=None,
        role="controller",
    )
    source = SyntheticQueue([event])
    result = driver_module._run_loop(
        run_id,
        plan,
        source,
        EffectClient(RecordingTransport()),
        TLLoopConfig(root_dir=tmp_path, run_id=run_id),
        root_store,
        root_store.load(),
        [],
        TLEventDecoder(),
    )

    assert source.acknowledged == []
    assert child_store.load().slices["leaf-a"].recovery is None
    assert root_store.load().events.last_consumed_offset == 0
    diagnostic = " ".join(result.diagnostics["unresolved_events"])
    assert "reporter='kani'" in diagnostic
    assert "subject='leaf-a'" in diagnostic
    assert "candidate_scopes" in diagnostic
    assert "agent_id->kani" in diagnostic
    assert "reporter_scope_unproven" in diagnostic


def test_nested_blocker_reporter_uses_durable_controller_alias(tmp_path: Path) -> None:
    run_id = "nested-blocker-controller-alias"
    _, root_store, _ = _nested_blocked_owner_stores(tmp_path, run_id)
    state = root_store.load()
    root_slices = dict(state.slices)
    root_slices["architecture"] = replace(
        root_slices["architecture"],
        dispatch_agent_id="architecture-controller",
    )
    state = root_store.checkpoint(
        state.recursive_fsm or state.fsm,
        root_slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )
    event = _blocked_event(
        95,
        "leaf-a",
        run_id,
        reporter="architecture-controller",
        invocation_id=None,
        generation=None,
        role="controller",
    )

    lookup = driver_module._resolve_task_blocked_owner(event, state, root_store)

    assert lookup.owner is not None
    assert lookup.owner.slice_id == "leaf-a"
    assert lookup.reporter_scope_path == (run_id, "architecture")


def test_blocker_alias_conflicts_do_not_route_to_a_nested_owner(tmp_path: Path) -> None:
    run_id = "conflicting-blocker-aliases"
    plan, root_store, child_store = _nested_blocked_owner_stores(tmp_path, run_id)
    assert child_store is not None
    event = _blocked_event(
        91,
        "leaf-a",
        run_id,
        reporter="architecture",
        invocation_id=None,
        generation=None,
        role="controller",
    )
    conflicting = replace(event, data={**event.data, "intent_id": "architecture-intent"})
    lookup = driver_module._resolve_task_blocked_owner(
        conflicting,
        root_store.load(),
        root_store,
    )
    assert lookup.owner is None
    assert lookup.reason_code == "owner_identity_conflict"
    assert "intent_id->architecture" in lookup.conflicting_aliases
    assert "consistent_event_aliases" in lookup.missing_proof

    source = SyntheticQueue([conflicting])
    result = driver_module._run_loop(
        run_id,
        plan,
        source,
        EffectClient(RecordingTransport()),
        TLLoopConfig(root_dir=tmp_path, run_id=run_id),
        root_store,
        root_store.load(),
        [],
        TLEventDecoder(),
    )
    assert source.acknowledged == []
    assert child_store.load().slices["leaf-a"].recovery is None
    diagnostic = " ".join(result.diagnostics["unresolved_events"])
    assert "intent_id->architecture" in diagnostic
    assert "consistent_event_aliases" in diagnostic


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("invocation_id", "unbound-invocation", "reporter_invocation_binding_missing"),
        ("generation", 7, "reporter_generation_mismatch"),
    ),
)
def test_unbound_reporter_invocation_evidence_is_rejected(
    tmp_path: Path,
    field: str,
    value: object,
    reason: str,
) -> None:
    run_id = f"unbound-reporter-{field}"
    _, root_store, _ = _nested_blocked_owner_stores(tmp_path, run_id)
    event = _blocked_event(
        94,
        "leaf-a",
        run_id,
        reporter="architecture",
        invocation_id=None,
        generation=None,
        role="controller",
    )
    conflicting = replace(event, **{field: value})
    lookup = driver_module._resolve_task_blocked_owner(
        conflicting,
        root_store.load(),
        root_store,
    )
    assert lookup.owner is not None
    disposition = driver_module._classify_task_blocked_disposition(
        conflicting,
        lookup.owner,
        lookup.owner.store.load(),
        lookup,
    )
    assert disposition.decision == "unresolved"
    assert disposition.reason_code == reason


def test_unavailable_nested_checkpoint_is_parked_with_scope_diagnostic(tmp_path: Path) -> None:
    run_id = "unavailable-blocker-child"
    _, root_store, child_store = _nested_blocked_owner_stores(tmp_path, run_id)
    assert child_store is not None
    child_store.path.write_text("{ malformed", encoding="utf-8")
    event = _blocked_event(
        92,
        "leaf-a",
        run_id,
        reporter="architecture",
        invocation_id=None,
        generation=None,
        role="controller",
    )

    lookup = driver_module._resolve_task_blocked_owner(event, root_store.load(), root_store)

    assert lookup.owner is None
    assert lookup.reason_code == "child_state_unavailable"
    assert lookup.unavailable_paths == ((run_id, "architecture"),)
    parked = driver_module._park_task_blocked_owner_issue(
        event,
        root_store.load(),
        root_store,
        lookup,
        EventDiagnostics(),
    )
    assert parked.events.last_consumed_offset == 0
    assert any(gate.name == "task-blocked-owner:92" for gate in parked.gates)
    audit = root_store.audited_events()[0]
    assert audit["unavailable_owner_paths"] == [[run_id, "architecture"]]


def test_superseded_event_ack_crash_replays_from_durable_disposition(
    tmp_path: Path,
) -> None:
    run_id = "superseded-ack-crash"
    plan, root_store, child_store = _nested_blocked_owner_stores(
        tmp_path,
        run_id,
        leaf_issue_id=42,
    )
    assert child_store is not None
    _complete_nested_leaf(child_store)
    _seed_nested_aggregate_with_original_dispatch_owner(root_store)
    event = _blocked_event(
        93,
        "leaf-a",
        run_id,
        reporter="architecture",
        invocation_id=None,
        generation=None,
        attempt=6,
        role="controller",
    )

    class CrashAfterCheckpointQueue(SyntheticQueue):
        def acknowledge(self, observed: EventEnvelope) -> int:
            assert root_store.load().events.last_consumed_offset == observed.run_seq
            assert len(root_store.task_blocked_dispositions()) == 1
            assert not any(gate.name == "task-blocked-owner:93" for gate in root_store.load().gates)
            raise SystemExit("simulated crash after durable checkpoint")

    with pytest.raises(SystemExit, match="after durable checkpoint"):
        driver_module._run_loop(
            run_id,
            plan,
            CrashAfterCheckpointQueue([event]),
            EffectClient(RecordingTransport()),
            TLLoopConfig(root_dir=tmp_path, run_id=run_id),
            root_store,
            root_store.load(),
            [],
            TLEventDecoder(),
        )

    assert root_store.load().events.last_consumed_offset == 93
    assert len(root_store.task_blocked_dispositions()) == 1
    cancel_event = threading.Event()

    class RestartQueue(SyntheticQueue):
        def acknowledge(self, observed: EventEnvelope) -> int:
            assert observed.event_id == event.event_id
            assert len(root_store.task_blocked_dispositions()) == 1
            cancel_event.set()
            return super().acknowledge(observed)

    source = RestartQueue([event])
    with pytest.raises(LoopCancelled):
        driver_module._run_loop(
            run_id,
            plan,
            source,
            EffectClient(RecordingTransport()),
            TLLoopConfig(
                active=True,
                root_dir=tmp_path,
                run_id=run_id,
                poll_interval=0.001,
                cancel_event=cancel_event,
            ),
            root_store,
            root_store.load(),
            [],
            TLEventDecoder(),
        )
    assert source.acknowledged == [93]
    assert len(root_store.task_blocked_dispositions()) == 1
    assert child_store.load().slices["leaf-a"].status is SliceStatus.MERGED


def test_reconciled_blocker_integrates_without_root_review_before_next_stage(
    tmp_path: Path,
) -> None:
    run_id = "blocker-aggregate-gates"
    plan, root_store, child_store = _nested_blocked_owner_stores(
        tmp_path,
        run_id,
        leaf_issue_id=42,
    )
    assert child_store is not None
    root_store.set_controller_epoch("blocker-parent-epoch")
    repository_path = Path(root_store.load().slices["architecture"].worktree or "")
    _complete_nested_leaf(child_store, repository=repository_path)
    repository = RepositoryIdentity("https://forge.example.test/test/repository.git")
    root_store.set_repository_identity(repository)
    seeded = _seed_nested_aggregate_in_review(root_store)
    root_store.set_gate(driver_module.INTEGRITY_RECONCILIATION_GATE_NAME, GateStatus.PENDING)
    root_store.set_gate("unrelated-human-gate", GateStatus.PENDING)
    root_slice = seeded.slices["architecture"]
    recovery = begin_recovery(
        cause="ordered_child_pane_exited",
        owner_run_id=run_id,
        slice_attempt=1,
        owner_agent_id="architecture",
        invocation_generation=1,
        evidence={
            "invocation_id": "aggregate-invocation",
            "slice_id": "architecture",
            "authorization_source": "human",
            "reopened_from": "ordered_child_recovery",
        },
        next_action="resume_same_owner",
    )
    seeded = root_store.checkpoint(
        seeded.fsm,
        {**seeded.slices, "architecture": replace(root_slice, recovery=recovery)},
        seeded.budgets,
        77,
        current_order=seeded.current_order,
        ordered_stages=seeded.ordered_stages,
        integration=seeded.integration,
    )
    root_store.record_task_blocked_disposition(
        {
            "disposition_id": "agent.task_blocked:77:legacy-child-event",
            "decision": "superseded_by_durable_completion",
            "run_seq": 77,
            "event_id": "legacy-child-event",
            "event_type": "agent.task_blocked",
            "reporter_id": "architecture",
            "reporter_scope_path": [run_id, "architecture"],
            "subject_slice_id": "leaf-a",
            "owner_path": [run_id, "architecture"],
            "attempt": 6,
            "proof": [
                "blocker_attempt=6",
                "completion_attempt=1",
                "subject_consumed_offset=78",
                "merge_journal_id=leaf-merge-journal",
            ],
            "evidence_digest": "d" * 64,
        }
    )
    assert root_slice.publication is not None
    aggregate_head = root_slice.publication.head_sha
    publication_record = {
        "invocation_id": root_slice.publication.invocation_id,
        "slice_id": "architecture",
        "author_agent": "architecture",
        "succession_invocation_ids": [],
    }
    snapshot = {
        "found": True,
        "pr_number": root_slice.pr_number,
        "merged": False,
        "head_sha": aggregate_head,
        "base_sha": "aggregate-base",
        "patch_digest": "aggregate-patch",
        "merge_tree_sha": "aggregate-merge-tree",
        "pr_head_tree_sha": _test_git_tree(repository_path, aggregate_head),
        "ci_status": "success",
        "head_branch": root_slice.branch,
        "base_branch": root_slice.base_ref,
        "head_reachable": True,
        "publication_ownership_verified": True,
        "publication_ownership_error": "",
        "publication": publication_record,
    }

    class GateIntegrationTransport(IntegrationTransport):
        def call_tool(self, role, name, tool_name, arguments):
            if tool_name == "repository_identity":
                self.calls.append((tool_name, arguments))
                return {
                    "success": True,
                    "result": {"remote_url": "https://forge.example.test/test/repository.git"},
                }
            if tool_name == "watcher_pr_state":
                self.calls.append((tool_name, arguments))
                return {"success": True, "result": dict(snapshot)}
            if tool_name == "post_merge_changelog_context":
                self.calls.append((tool_name, arguments))
                return {
                    "success": True,
                    "result": {
                        "changelog_present": False,
                        "unreleased_present": False,
                    },
                }
            return super().call_tool(role, name, tool_name, arguments)

    transport = GateIntegrationTransport()
    ci = project(
        {
            "schema_version": 1,
            "event_id": "aggregate-ci-78",
            "id": "aggregate-ci-78",
            "event_time": "2026-10-14T00:00:01Z",
            "run_seq": 78,
            "type": "ci.status_changed",
            "run_id": run_id,
            "agent_id": "watcher",
            "session_id": "watcher-session",
            "lifecycle_state": "observed",
            "observed_at": "2026-10-14T00:00:01Z",
            "pr_number": root_slice.pr_number,
            "head_sha": aggregate_head,
            "data": {
                "slice_id": "architecture",
                "pr_number": root_slice.pr_number,
                "head_sha": aggregate_head,
                "status": "success",
            },
        }
    )

    class GateObservingQueue(SyntheticQueue):
        def acknowledge(self, event: EventEnvelope) -> int:
            current = root_store.load()
            if event.run_seq == 78:
                assert current.slices["architecture"].status is SliceStatus.MERGED, transport.calls
                assert current.current_order == 2
                assert current.slices["kani"].status is not SliceStatus.PENDING
            return super().acknowledge(event)

    source = GateObservingQueue([ci])
    config = replace(
        _config(),
        root_dir=tmp_path,
        run_id=run_id,
        repository_identity=repository,
        ledger_run_id=run_id,
        keep_alive_on_waiting=True,
        max_parallel_slices=1,
        policy=validate_policy(_selector_policy()),
        tl_model_choice=_tl_choice(ReviewBackend([])),
        agent_id=run_id,
    )
    reconciled_start = driver_module._reconcile_nonterminal_slices(
        plan,
        root_store.load(),
        config,
        EffectClient(transport),
        root_store,
        [],
    )
    assert reconciled_start.slices["architecture"].dispatch_agent_id == "architecture"
    assert reconciled_start.slices["architecture"].recovery == recovery
    assert any(
        gate.name == "unrelated-human-gate" and gate.status is GateStatus.PENDING
        for gate in reconciled_start.gates
    )
    integrated = driver_module._run_loop(
        run_id,
        plan,
        source,
        EffectClient(transport),
        config,
        root_store,
        root_store.load(),
        [],
        TLEventDecoder(),
    )

    assert source.acknowledged == [78], integrated.diagnostics.get("unresolved_events")
    assert integrated.final_state.current_order == 2
    assert integrated.final_state.slices["architecture"].status is SliceStatus.MERGED
    assert integrated.final_state.slices["architecture"].reviewed_head is None
    assert integrated.final_state.slices["architecture"].verdict is None
    assert integrated.final_state.slices["architecture"].reviewer_agent_id == "aggregate-reviewer"
    assert integrated.final_state.slices["architecture"].recovery is None
    assert len(root_store.task_blocked_dispositions()) == 1
    assert integrated.final_state.slices["architecture"].post_merge is not None
    assert integrated.final_state.slices["architecture"].post_merge.phase is PostMergePhase.COMPLETE
    assert [args["pr_number"] for name, args in transport.calls if name == "merge_pr"] == [
        root_slice.pr_number
    ]
    assert not any(name == "spawn_reviewer" for name, _ in transport.calls)
    assert any(
        arguments.get("event_type") == "tl.ordered_recovery_superseded"
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    )
    kani_dispatches = [
        arguments
        for name, arguments in transport.calls
        if name == "emit_controller_event"
        and arguments.get("event_type") == "tl.dispatch_confirmed"
        and isinstance(arguments.get("payload"), Mapping)
        and arguments["payload"].get("slice_id") == "kani"
    ]
    assert len(kani_dispatches) == 1

    driver_module._run_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config,
        root_store,
        root_store.load(),
        [],
        TLEventDecoder(),
    )
    assert (
        len(
            [
                arguments
                for name, arguments in transport.calls
                if name == "emit_controller_event"
                and arguments.get("event_type") == "tl.dispatch_confirmed"
                and isinstance(arguments.get("payload"), Mapping)
                and arguments["payload"].get("slice_id") == "kani"
            ]
        )
        == 1
    )


def test_superseded_blocker_replay_recovers_after_checkpoint_failure_without_duplicate_disposition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "superseded-blocker-restart"
    plan, root_store, child_store = _nested_blocked_owner_stores(
        tmp_path,
        run_id,
        leaf_issue_id=42,
    )
    assert child_store is not None
    _complete_nested_leaf(child_store)
    _seed_nested_aggregate_with_original_dispatch_owner(root_store)
    event = _blocked_event(
        90,
        "leaf-a",
        run_id,
        reporter="architecture",
        invocation_id=None,
        generation=None,
        attempt=6,
        role="controller",
    )
    original_checkpoint = RunStore.checkpoint
    failed_clear = False

    def fail_supersession_checkpoint(self: RunStore, *args: Any, **kwargs: Any) -> RunState:
        nonlocal failed_clear
        if self.path == root_store.path and kwargs.get("clear_gates") and not failed_clear:
            failed_clear = True
            raise OSError("injected crash before supersession checkpoint")
        return original_checkpoint(self, *args, **kwargs)

    monkeypatch.setattr(RunStore, "checkpoint", fail_supersession_checkpoint)
    first_source = SyntheticQueue([event])
    config = TLLoopConfig(root_dir=tmp_path, run_id=run_id)
    first = driver_module._run_loop(
        run_id,
        plan,
        first_source,
        EffectClient(RecordingTransport()),
        config,
        root_store,
        root_store.load(),
        [],
        TLEventDecoder(),
    )
    assert first_source.acknowledged == []
    assert root_store.load().events.last_consumed_offset == 0
    assert len(root_store.task_blocked_dispositions()) == 1
    assert any(gate.name == "task-blocked-owner:90" for gate in first.final_state.gates)

    monkeypatch.setattr(RunStore, "checkpoint", original_checkpoint)
    cancel_event = threading.Event()

    class RestartObservingQueue(SyntheticQueue):
        def acknowledge(self, observed: EventEnvelope) -> int:
            current = root_store.load()
            assert current.events.last_consumed_offset == observed.run_seq
            assert not any(gate.name == "task-blocked-owner:90" for gate in current.gates)
            assert len(root_store.task_blocked_dispositions()) == 1
            cancel_event.set()
            return super().acknowledge(observed)

    restarted_source = RestartObservingQueue([event])
    with pytest.raises(LoopCancelled):
        driver_module._run_loop(
            run_id,
            plan,
            restarted_source,
            EffectClient(RecordingTransport()),
            replace(config, active=True, cancel_event=cancel_event, poll_interval=0.001),
            root_store,
            root_store.load(),
            [],
            TLEventDecoder(),
        )
    assert restarted_source.acknowledged == [90]
    assert root_store.load().events.last_consumed_offset == 90
    assert len(root_store.task_blocked_dispositions()) == 1
    assert child_store.load().slices["leaf-a"].status is SliceStatus.MERGED


def test_nested_controller_reporter_routes_legacy_blocker_to_child_before_ack(
    tmp_path: Path,
) -> None:
    run_id = "nested-controller-reporter"
    cancel_event = threading.Event()
    plan, root_store, child_store = _nested_blocked_owner_stores(tmp_path, run_id)
    assert child_store is not None

    class ObservingQueue(SyntheticQueue):
        def acknowledge(self, event: EventEnvelope) -> int:
            child = child_store.load().slices["leaf-a"]
            assert child.recovery is not None
            assert child.recovery.cause == ParkCause.HUMAN_DECISION_REQUIRED.value
            assert root_store.load().events.last_consumed_offset == event.run_seq
            result = super().acknowledge(event)
            cancel_event.set()
            return result

    event = _blocked_event(
        1,
        "leaf-a",
        run_id,
        reporter="architecture",
        invocation_id=None,
        generation=None,
        attempt=1,
        cause="human_decision_required",
        scope_attribution="agent_lifecycle",
        matched_difficulty_rule="missing_handoff:nonzero_exit",
        role="controller",
    )
    source = ObservingQueue([event])
    config = TLLoopConfig(
        root_dir=tmp_path,
        run_id=run_id,
        max_parallel_slices=1,
        poll_interval=0.001,
        cancel_event=cancel_event,
    )

    with pytest.raises(LoopCancelled):
        driver_module._run_loop(
            run_id,
            plan,
            source,
            EffectClient(RecordingTransport()),
            config,
            root_store,
            root_store.load(),
            [],
            TLEventDecoder(),
        )

    assert source.acknowledged == [1]
    assert child_store.load().slices["leaf-a"].attempts == 1


def _nested_blocked_owner_stores(
    root_dir: Path,
    run_id: str,
    *,
    direct_leaf: bool = False,
    create_child: bool = True,
    leaf_issue_id: int | None = None,
) -> tuple[WorkPlan, RunStore, RunStore | None]:
    """Persist a root stage and its declared child scope for routing tests."""
    child_plan = WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": "leaf-a",
                    "task": "blocked leaf",
                    "chainlink_issue_id": leaf_issue_id,
                }
            ]
        }
    )
    root_plan = WorkPlan(
        leaves=(
            WorkPlan.from_mapping(
                {"leaves": [{"name": "leaf-a", "task": "direct leaf", "chainlink_issue_id": None}]}
            ).leaves
            if direct_leaf
            else ()
        ),
        sub_tls=(
            SubTLTask("architecture", child_plan, order=1),
            SubTLTask(
                "kani",
                WorkPlan(),
                order=2,
                integration=IntegrationContract(aggregate_pr_required=False),
            ),
        ),
    )
    config = TLLoopConfig(root_dir=root_dir, run_id=run_id)
    root_manifest = driver_module._manifest_for_plan(root_plan, run_id, config)
    root_slices = _initial_slices(root_plan, config, root_dir, run_id)
    create(run_id, {"slices": root_slices}, root_dir=root_dir)
    root_store = RunStore(run_id, root_dir)
    root_store.set_plan_manifest(root_manifest, slices=root_slices)
    root_state = root_store.set_ordered_state(
        1,
        (
            driver_module.OrderedStageState(1, ("architecture",)),
            driver_module.OrderedStageState(2, ("kani",)),
        ),
        root_store.load().integration,
    )
    root_slices = dict(root_state.slices)
    root_slices["architecture"] = replace(
        root_slices["architecture"],
        status=SliceStatus.SPAWNED,
        dispatch_intent_id="architecture-intent",
        dispatch_agent_id="architecture",
        dispatch_authoritative_event_seq=0,
        dispatch_last_boundary="sub_tl_started",
    )
    root_store.checkpoint(
        root_state.fsm.phase,
        root_slices,
        root_state.budgets,
        root_state.events.last_consumed_offset,
        current_order=1,
        ordered_stages=root_state.ordered_stages,
        integration=root_state.integration,
    )
    if not create_child:
        return root_plan, root_store, None
    child_node = next(node for node in root_manifest.nodes if node.name == "architecture")
    child_manifest = root_manifest.child_manifests[child_node.node_id]
    child_store = RunStore("architecture", root_store.run_dir)
    child_config = replace(
        config,
        root_dir=root_store.run_dir,
        run_id="architecture",
        branch=driver_module.derive_child_branch(config.branch, "architecture"),
        parent_run_id=run_id,
        parent_branch=config.branch,
        parent_agent_id=run_id,
        depth=1,
        declared_manifest=child_manifest,
    )
    child_slices = _initial_slices(child_plan, child_config, root_store.run_dir, "architecture")
    create(
        "architecture",
        {
            "slices": child_slices,
            "owner_branch": child_config.branch,
            "parent_branch": child_config.parent_branch,
            "parent_run_id": run_id,
            "parent_agent_id": child_config.parent_agent_id,
            "depth": 1,
        },
        root_dir=root_store.run_dir,
    )
    child_store.set_plan_manifest(child_manifest, slices=child_slices)
    child_state = child_store.load()
    dispatched_leaf = replace(
        child_state.slices["leaf-a"],
        status=SliceStatus.SPAWNED,
        attempts=1,
        dispatch_intent_id="leaf-a-intent",
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id="inv-1",
        dispatch_authoritative_event_seq=0,
        dispatch_generation=1,
        dispatch_last_boundary="agent.spawned",
    )
    child_store.checkpoint(
        TLWaiting({"leaf-a": ChildHandle("leaf-a", dispatched_leaf.branch or "", "worker")}),
        {**child_state.slices, "leaf-a": dispatched_leaf},
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    return root_plan, root_store, child_store


def _three_level_blocked_owner_stores(
    root_dir: Path,
    run_id: str,
) -> tuple[WorkPlan, RunStore, RunStore]:
    """Persist root -> outer -> inner -> leaf ownership for routing tests."""
    leaf_plan = WorkPlan.from_mapping(
        {"leaves": [{"name": "leaf-a", "task": "deep blocked leaf", "chainlink_issue_id": None}]}
    )
    inner_task = SubTLTask("inner", leaf_plan, order=1)
    outer_plan = WorkPlan(sub_tls=(inner_task,))
    outer_task = SubTLTask("outer", outer_plan, order=1)
    plan = WorkPlan(sub_tls=(outer_task,))
    config = TLLoopConfig(root_dir=root_dir, run_id=run_id)
    root_manifest = driver_module._manifest_for_plan(plan, run_id, config)
    root_slices = _initial_slices(plan, config, root_dir, run_id)
    create(run_id, {"slices": root_slices}, root_dir=root_dir)
    root_store = RunStore(run_id, root_dir)
    root_store.set_plan_manifest(root_manifest, slices=root_slices)
    root_state = root_store.set_ordered_state(
        1,
        (driver_module.OrderedStageState(1, ("outer",)),),
        root_store.load().integration,
    )

    outer_node = next(node for node in root_manifest.nodes if node.name == "outer")
    outer_manifest = root_manifest.child_manifests[outer_node.node_id]
    outer_store = RunStore("outer", root_store.run_dir)
    outer_config = replace(
        config,
        root_dir=root_store.run_dir,
        run_id="outer",
        parent_run_id=run_id,
        depth=1,
        declared_manifest=outer_manifest,
    )
    outer_slices = _initial_slices(outer_plan, outer_config, root_store.run_dir, "outer")
    create(
        "outer",
        {"slices": outer_slices, "parent_run_id": run_id, "depth": 1},
        root_dir=root_store.run_dir,
    )
    outer_store.set_plan_manifest(outer_manifest, slices=outer_slices)
    outer_state = outer_store.set_ordered_state(
        1,
        (driver_module.OrderedStageState(1, ("inner",)),),
        outer_store.load().integration,
    )

    inner_node = next(node for node in outer_manifest.nodes if node.name == "inner")
    inner_manifest = outer_manifest.child_manifests[inner_node.node_id]
    inner_store = RunStore("inner", outer_store.run_dir)
    inner_config = replace(
        config,
        root_dir=outer_store.run_dir,
        run_id="inner",
        parent_run_id="outer",
        depth=2,
        declared_manifest=inner_manifest,
    )
    inner_slices = _initial_slices(leaf_plan, inner_config, outer_store.run_dir, "inner")
    create(
        "inner",
        {"slices": inner_slices, "parent_run_id": "outer", "depth": 2},
        root_dir=outer_store.run_dir,
    )
    inner_store.set_plan_manifest(inner_manifest, slices=inner_slices)
    inner_state = inner_store.load()

    for parent_store, parent_state, child_name in (
        (root_store, root_state, "outer"),
        (outer_store, outer_state, "inner"),
    ):
        child_state = parent_state.slices[child_name]
        started = replace(
            child_state,
            status=SliceStatus.SPAWNED,
            attempts=1,
            dispatch_intent_id=f"{child_name}-intent",
            dispatch_agent_id=child_name,
            dispatch_authoritative_event_seq=0,
            dispatch_last_boundary="sub_tl_started",
        )
        parent_store.checkpoint(
            parent_state.recursive_fsm or parent_state.fsm,
            {**parent_state.slices, child_name: started},
            parent_state.budgets,
            parent_state.events.last_consumed_offset,
            current_order=parent_state.current_order,
            ordered_stages=parent_state.ordered_stages,
            integration=parent_state.integration,
        )
    active_leaf = replace(
        inner_state.slices["leaf-a"],
        status=SliceStatus.SPAWNED,
        attempts=1,
        dispatch_intent_id="deep-leaf-intent",
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id="deep-leaf-invocation",
        dispatch_generation=1,
        dispatch_authoritative_event_seq=0,
        dispatch_last_boundary="agent.spawned",
    )
    inner_store.checkpoint(
        TLWaiting({"leaf-a": ChildHandle("leaf-a", active_leaf.branch or "", "worker")}),
        {**inner_state.slices, "leaf-a": active_leaf},
        inner_state.budgets,
        inner_state.events.last_consumed_offset,
    )
    return plan, root_store, inner_store


def _complete_nested_leaf(
    child_store: RunStore,
    *,
    attempt: int = 6,
    repository: Path | None = None,
) -> RunState:
    """Persist complete receipt-backed leaf history for blocker replay tests."""
    state = child_store.load()
    current = state.slices["leaf-a"]
    head_sha = "a" * 40
    parent_commit_sha = "b" * 40
    changelog_commit_sha = "c" * 40
    expected_base_sha = "c" * 40
    pushed_commit = "d" * 40
    if repository is not None:
        expected_base_sha = _test_git_repository(repository)
        head_sha = _test_git_commit(repository, "leaf-a.txt", "reviewed leaf\n", "leaf merge")
        parent_commit_sha = head_sha
        changelog_commit_sha = _test_git_commit(
            repository, "CHANGELOG.md", "leaf entry\n", "changelog bookkeeping"
        )
        pushed_commit = _test_git_commit(
            repository, "post-merge.txt", "bookkeeping receipt\n", "parent bookkeeping push"
        )
        _test_git_commit(repository, "aggregate.txt", "aggregate publication\n", "aggregate head")
    publication = PublicationBinding(
        pr_number=145,
        head_sha=head_sha,
        head_branch=current.branch or "main.leaf-a",
        base_branch=current.base_ref or "main",
        attempt=attempt,
        invocation_id="leaf-publication-invocation",
    )
    handoff = HandoffEvidence(
        pr_number=publication.pr_number,
        head_sha=publication.head_sha,
        attempt=attempt,
        invocation_id=publication.invocation_id or "leaf-handoff-invocation",
        agent_id=current.dispatch_agent_id or current.id,
        observed_at="2026-10-01T00:00:00Z",
    )
    closure = (
        {
            "issue_id": str(current.chainlink_issue_id),
            "issue_close_intent_id": "leaf-issue-close-intent",
            "issue_close_journal_id": "leaf-issue-close-journal",
            "issue_close_outcome": "closed",
        }
        if current.chainlink_issue_id is not None
        else {
            "issue_close_outcome": "not_applicable",
            "changelog_outcome": "not_applicable",
        }
    )
    evidence = {
        "child_id": current.id,
        "repository": "https://forge.example.test/test/repository.git",
        "parent_branch": publication.base_branch,
        "pr_number": str(publication.pr_number),
        "head_sha": publication.head_sha,
        "merge_journal_id": "leaf-merge-journal",
        "lane_epoch": "1",
        "parent_commit_sha": parent_commit_sha,
        **closure,
        "changelog_intent_id": "leaf-changelog-intent",
        "changelog_generation": "1",
        "changelog_commit_sha": changelog_commit_sha,
        "parent_push_intent_id": "leaf-parent-push-intent",
        "push_journal_id": "leaf-push-journal",
        "expected_base_sha": expected_base_sha,
        "push_receipt_id": "leaf-push-receipt",
        "pushed_commit": pushed_commit,
        "bookkeeping_commit": pushed_commit,
        "observed_remote_head": pushed_commit,
        "ancestry_proof": f"ancestor:{pushed_commit}->{pushed_commit}",
    }
    post_merge = PostMergeState(PostMergePhase.COMPLETE, evidence)
    completed = replace(
        current,
        status=SliceStatus.MERGED,
        attempts=0,
        pr_number=publication.pr_number,
        reviewed_head=head_sha,
        verdict=Verdict.GO,
        review_patch_digests={head_sha: "leaf-patch-digest"},
        ci_state={head_sha: "success"},
        publication=publication,
        handoff=handoff,
        post_merge=post_merge,
        dispatch_invocation_id=None,
        recovery=None,
        park_cause=None,
        park_issue_id=None,
        park_audit=None,
    )
    phase = driver_module._phase_after_slice_merge(state, current.id, completed)
    return child_store.checkpoint(
        phase,
        {**state.slices, current.id: completed},
        state.budgets,
        state.events.last_consumed_offset,
    )


def _complete_legacy_shape_nested_leaf(child_store: RunStore, *, event_seq: int) -> RunState:
    """Persist legacy counters, repo slug, and issue-close proof for replay tests."""
    state = child_store.load()
    repository_identity = RepositoryIdentity("https://forge.example.test/test/repository.git")
    if state.repository_identity is None:
        state = child_store.set_repository_identity(repository_identity)
    elif state.repository_identity != repository_identity:
        raise AssertionError("legacy completion fixture has conflicting repository identity")
    current = state.slices["leaf-a"]
    publication = PublicationBinding(
        pr_number=145,
        head_sha="a" * 40,
        head_branch=current.branch or "main.leaf-a",
        base_branch=current.base_ref or "main",
        attempt=1,
        invocation_id="legacy-completion-invocation",
    )
    handoff = HandoffEvidence(
        pr_number=publication.pr_number,
        head_sha=publication.head_sha,
        attempt=1,
        invocation_id=publication.invocation_id or "legacy-completion-invocation",
        agent_id=current.dispatch_agent_id or current.id,
        observed_at="2026-10-01T00:00:00Z",
    )
    close_intent_id = "legacy-issue-close-intent"
    close_receipt_id = "legacy-issue-close-receipt"
    evidence = {
        "child_id": current.id,
        "repository": "test/repository",
        "parent_branch": publication.base_branch,
        "pr_number": str(publication.pr_number),
        "head_sha": publication.head_sha,
        "merge_journal_id": "legacy-merge-journal",
        "lane_epoch": "1",
        "parent_commit_sha": "b" * 40,
        "issue_id": "42",
        "issue_close_intent_id": close_intent_id,
        "issue_close_journal_id": close_receipt_id,
        "changelog_intent_id": "legacy-changelog-intent",
        "changelog_generation": "1",
        "changelog_commit_sha": "c" * 40,
        "parent_push_intent_id": "legacy-parent-push-intent",
        "push_journal_id": "legacy-push-journal",
        "expected_base_sha": "c" * 40,
        "push_receipt_id": "legacy-push-receipt",
        "pushed_commit": "d" * 40,
        "bookkeeping_commit": "d" * 40,
        "observed_remote_head": "d" * 40,
        "ancestry_proof": f"ancestor:{'d' * 40}->{'d' * 40}",
    }
    completed = replace(
        current,
        status=SliceStatus.MERGED,
        attempts=6,
        pr_number=publication.pr_number,
        reviewed_head=publication.head_sha,
        verdict=Verdict.GO,
        publication=publication,
        handoff=handoff,
        post_merge=PostMergeState(PostMergePhase.COMPLETE, evidence),
        dispatch_invocation_id=None,
        recovery=None,
        park_cause=None,
        park_issue_id=None,
        park_audit=None,
    )
    intent = EffectIntent(
        "post_merge_issue_close",
        current.id,
        {
            "issue_id": 42,
            "force": True,
            "commit_changelog": False,
            "intent_id": close_intent_id,
        },
        True,
    )
    journal = EffectJournal(child_store.run_id, child_store.run_dir / "action-journal.json")
    journal.append(intent)
    journal.mark_result(
        intent,
        ToolResult.from_raw(
            {
                "success": True,
                "result": {"issue_id": 42, "receipt_id": close_receipt_id},
            }
        ),
    )
    phase = driver_module._phase_after_slice_merge(state, current.id, completed)
    return child_store.checkpoint(
        phase,
        {**state.slices, current.id: completed},
        state.budgets,
        max(event_seq + 1, state.events.last_consumed_offset),
    )


def _rewrite_legacy_completion_evidence(
    store: RunStore,
    **updates: str,
) -> RunState:
    """Change one copied fixture receipt field to exercise strict failures."""
    state = store.load()
    current = state.slices["leaf-a"]
    assert current.post_merge is not None
    evidence = {**current.post_merge.evidence, **updates}
    updated = replace(current, post_merge=PostMergeState(current.post_merge.phase, evidence))
    return store.checkpoint(
        state.recursive_fsm or state.fsm,
        {**state.slices, current.id: updated},
        state.budgets,
        state.events.last_consumed_offset,
    )


def test_nested_task_blocked_recovers_child_before_ack_and_keeps_ordered_stage_gated(
    tmp_path: Path,
) -> None:
    run_id = "nested-task-blocked"
    cancel_event = threading.Event()
    plan, root_store, child_store = _nested_blocked_owner_stores(tmp_path, run_id)
    assert child_store is not None

    class ObservingQueue(SyntheticQueue):
        def acknowledge(self, event: EventEnvelope) -> int:
            assert child_store.load().slices["leaf-a"].recovery is not None
            assert root_store.load().events.last_consumed_offset == event.run_seq
            result = super().acknowledge(event)
            cancel_event.set()
            return result

    source = ObservingQueue([_blocked_event(1, "leaf-a", run_id)])
    config = TLLoopConfig(
        root_dir=tmp_path,
        run_id=run_id,
        max_parallel_slices=1,
        poll_interval=0.001,
        cancel_event=cancel_event,
    )
    with pytest.raises(LoopCancelled):
        driver_module._run_loop(
            run_id,
            plan,
            source,
            EffectClient(RecordingTransport()),
            config,
            root_store,
            root_store.load(),
            [],
            TLEventDecoder(),
        )

    root_state = root_store.load()
    child_state = child_store.load()
    assert source.acknowledged == [1]
    assert child_state.slices["leaf-a"].recovery is not None
    assert not driver_module._child_handoff_ready(child_state, run_id)
    assert root_state.current_order == 1
    assert root_state.slices["kani"].status is SliceStatus.PENDING
    assert root_state.integration.sub_tl_recovery["architecture"].slice_id == "leaf-a"


@pytest.mark.parametrize("reporter", ("outer", "inner"))
def test_task_blocked_routes_through_multiple_validated_subtl_levels(
    tmp_path: Path,
    reporter: str,
) -> None:
    run_id = f"three-level-{reporter}"
    cancel_event = threading.Event()
    plan, root_store, inner_store = _three_level_blocked_owner_stores(tmp_path, run_id)
    event = _blocked_event(
        9,
        "leaf-a",
        run_id,
        reporter=reporter,
        invocation_id=None,
        generation=None,
        attempt=1,
        role="controller",
    )
    lookup = driver_module._resolve_task_blocked_owner(event, root_store.load(), root_store)
    assert lookup.owner is not None
    assert lookup.owner.path == (run_id, "outer", "inner")
    assert lookup.reporter_scope_path[-1] == reporter

    class ObservingQueue(SyntheticQueue):
        def acknowledge(self, observed: EventEnvelope) -> int:
            assert inner_store.load().slices["leaf-a"].recovery is not None
            assert root_store.load().events.last_consumed_offset == observed.run_seq
            result = super().acknowledge(observed)
            cancel_event.set()
            return result

    source = ObservingQueue([event])
    config = TLLoopConfig(
        root_dir=tmp_path,
        run_id=run_id,
        max_parallel_slices=1,
        poll_interval=0.001,
        cancel_event=cancel_event,
    )
    result = driver_module._run_loop(
        run_id,
        plan,
        source,
        EffectClient(RecordingTransport()),
        config,
        root_store,
        root_store.load(),
        [],
        TLEventDecoder(),
    )
    assert source.acknowledged == [9]
    assert result.final_state.events.last_consumed_offset == 9


def test_nested_task_blocked_owner_lookup_parks_missing_or_ambiguous_scope(
    tmp_path: Path,
) -> None:
    missing_run, ambiguous_run = "missing-child-owner", "ambiguous-child-owner"
    missing_plan, missing_store, _ = _nested_blocked_owner_stores(
        tmp_path, missing_run, create_child=False
    )
    del missing_plan
    missing_event = _blocked_event(1, "leaf-a", missing_run)
    missing_lookup = driver_module._resolve_task_blocked_owner(
        missing_event, missing_store.load(), missing_store
    )
    assert missing_lookup.reason_code == "child_state_missing"
    parked = driver_module._park_task_blocked_owner_issue(
        missing_event,
        missing_store.load(),
        missing_store,
        missing_lookup,
        EventDiagnostics(),
    )
    assert parked.events.last_consumed_offset == 0
    assert any(gate.name == "task-blocked-owner:1" for gate in parked.gates)
    assert missing_store.audited_events()[0]["reason_code"] == "child_state_missing"

    _, ambiguous_store, _ = _nested_blocked_owner_stores(tmp_path, ambiguous_run, direct_leaf=True)
    ambiguous_lookup = driver_module._resolve_task_blocked_owner(
        _blocked_event(1, "leaf-a", ambiguous_run),
        ambiguous_store.load(),
        ambiguous_store,
    )
    assert ambiguous_lookup.owner is None
    assert ambiguous_lookup.reason_code == "ambiguous_owner"


@pytest.mark.parametrize(
    ("failed_owner", "expected_reason"),
    (
        ("architecture", "recovery_record_failed"),
        ("nested-task-blocked-write", "parent_checkpoint_failed"),
    ),
)
def test_nested_task_blocked_persistence_failure_does_not_ack_unrecorded_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_owner: str,
    expected_reason: str,
) -> None:
    run_id = "nested-task-blocked-write"
    plan, root_store, child_store = _nested_blocked_owner_stores(tmp_path, run_id)
    assert child_store is not None
    failing_store = child_store if failed_owner == "architecture" else root_store
    original_checkpoint = RunStore.checkpoint

    def fail_checkpoint(self: RunStore, *args: Any, **kwargs: Any) -> RunState:
        if self.path == failing_store.path:
            raise OSError("injected checkpoint failure")
        return original_checkpoint(self, *args, **kwargs)

    monkeypatch.setattr(RunStore, "checkpoint", fail_checkpoint)
    source = SyntheticQueue([_blocked_event(1, "leaf-a", run_id)])
    config = TLLoopConfig(root_dir=tmp_path, run_id=run_id, max_parallel_slices=1)
    result = driver_module._run_loop(
        run_id,
        plan,
        source,
        EffectClient(RecordingTransport()),
        config,
        root_store,
        root_store.load(),
        [],
        TLEventDecoder(),
    )

    assert source.acknowledged == []
    assert root_store.load().events.last_consumed_offset == 0
    assert any(gate.name == "task-blocked-owner:1" for gate in result.final_state.gates)
    assert expected_reason in " ".join(result.diagnostics["unresolved_events"])


def test_durable_journal_failure_parks_the_affected_slice(tmp_path: Path, monkeypatch) -> None:
    class ParkingTransport(RecordingTransport):
        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            if tool_name == "chainlink_issue_create":
                self.calls.append((tool_name, arguments))
                return {"success": True, "result": {"issue_id": 9340}}
            return super().call_tool(role, name, tool_name, arguments)

    def fail_append(_journal: EffectJournal, intent: object) -> None:
        raise DurableWriteError(
            "mappingproxy",
            operation=getattr(intent, "operation", None),
            target=getattr(intent, "target", None),
        )

    monkeypatch.setattr(EffectJournal, "append", fail_append)
    transport = ParkingTransport()
    result = run_tl_loop(
        "durable-failure",
        _plan(),
        SyntheticQueue([]),
        EffectClient(transport),
        config=replace(_config(), ledger_run_id="durable-failure"),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLFailed
    parked = [
        slice_state
        for slice_state in result.final_state.slices.values()
        if slice_state.park_cause is not None
    ]
    assert len(parked) == 1
    assert parked[0].park_cause.value == "durable_write_failed"


def test_observability_failure_does_not_change_terminal_state(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    baseline_transport = RecordingTransport()
    baseline = run_tl_loop(
        "observability-baseline-run",
        _plan(),
        SyntheticQueue(_lifecycle_events("observability-baseline-run")),
        EffectClient(baseline_transport),
        config=_config(),
        root_dir=tmp_path / "baseline",
    )
    failed_transport = RecordingTransport(fail_observability=True)
    with caplog.at_level("WARNING"):
        result = run_tl_loop(
            "observability-failure-run",
            _plan(),
            SyntheticQueue(_lifecycle_events("observability-failure-run")),
            EffectClient(failed_transport),
            config=_config(),
            root_dir=tmp_path / "failed",
        )

    assert result.final_state.fsm.phase is baseline.final_state.fsm.phase is TLPhase.TLDone
    assert (
        result.final_state.slices["leaf-a"].status is baseline.final_state.slices["leaf-a"].status
    )
    assert result.final_state.slices["leaf-a"].status is SliceStatus.SPAWNED
    assert "merge_pr" not in _effect_names(failed_transport)
    assert _merge_decisions(failed_transport) == []
    assert _merge_decisions(baseline_transport) == []


def test_dispatch_waits_for_delayed_authoritative_confirmation(tmp_path: Path) -> None:
    run_id = "delayed-dispatch-run"
    transport = RecordingTransport()
    source = SyntheticQueue([])
    cancel_event = threading.Event()
    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            run_tl_loop(
                run_id,
                WorkPlan.from_mapping(
                    {"leaves": [{"name": "leaf-a", "task": "delayed", "chainlink_issue_id": None}]}
                ),
                source,
                EffectClient(transport),
                config=TLLoopConfig(
                    max_workers=0,
                    max_leaves=1,
                    poll_interval=0.001,
                    cancel_event=cancel_event,
                ),
                root_dir=tmp_path,
            )
        except BaseException as error:  # noqa: BLE001 - assert explicit cancellation
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while not any(name == "spawn_leaf" for name, _ in transport.calls):
        if time.monotonic() >= deadline:
            pytest.fail("spawn request was not issued")
        time.sleep(0.001)
    time.sleep(0.02)
    source.events.append(
        _canonical_event(
            1,
            "agent.spawned",
            "leaf-a",
            run_id,
            intent_id=_dispatch_intent(run_id, "leaf-a"),
        )
    )
    deadline = time.monotonic() + 1.0
    while source.acknowledged != [1] and time.monotonic() < deadline:
        time.sleep(0.001)
    cancel_event.set()
    thread.join(timeout=2)

    assert source.acknowledged == [1]
    assert isinstance(outcome.get("error"), LoopCancelled)
    state = RunStore(run_id, tmp_path).load()
    assert state.fsm.phase is TLPhase.TLWaiting
    assert state.slices["leaf-a"].status is SliceStatus.SPAWNED
    assert not state.gates


def test_rejected_spawn_is_persisted_as_dispatch_failure(tmp_path: Path) -> None:
    transport = RecordingTransport(reject_spawns=True)
    plan = WorkPlan.from_mapping(
        {"leaves": [{"name": "leaf-a", "task": "implement the change", "chainlink_issue_id": None}]}
    )
    result = run_tl_loop(
        "dispatch-failure-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=TLLoopConfig(poll_interval=0.001),
        root_dir=tmp_path,
    )

    slice_state = result.final_state.slices["leaf-a"]
    assert result.final_state.fsm.phase is TLPhase.TLFailed
    assert slice_state.status is SliceStatus.DISPATCH_FAILED
    assert slice_state.park_cause.value == "dispatch_failed"
    assert slice_state.dispatch_last_boundary == "spawn_request_failed"
    assert "tmux launch rejected" in (slice_state.dispatch_error or "")
    assert any(
        arguments["event_type"] == "tl.spawn_request_failed"
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    )
    assert not any(
        arguments["event_type"] == "tl.gate_opened"
        and arguments["payload"].get("gate_name") == "tl-timeout"
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    )


def test_restart_reconciles_dispatch_without_duplicate_spawn(tmp_path: Path) -> None:
    run_id = "dispatch-restart-run"
    plan = WorkPlan.from_mapping(
        {"leaves": [{"name": "leaf-a", "task": "implement the change", "chainlink_issue_id": None}]}
    )
    cancel_event = threading.Event()
    config = TLLoopConfig(
        poll_interval=0.001,
        cancel_event=cancel_event,
    )
    initial = _initial_slices(plan, config, tmp_path, run_id)
    initial["leaf-a"].update(
        {
            "status": "dispatching",
            "attempts": 1,
            "dispatch_intent_id": "intent-restart-1",
            "dispatch_started_at": time.time(),
            "dispatch_last_boundary": "dispatch_intended",
        }
    )
    transport = RecordingTransport()

    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            run_tl_loop(
                run_id,
                plan,
                SyntheticQueue([]),
                EffectClient(transport),
                config=config,
                root_dir=tmp_path,
                initial_slices=initial,
            )
        except BaseException as error:  # noqa: BLE001 - assert explicit cancellation
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while "tl.dispatch_reconciliation_completed" not in [
        arguments["event_type"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    ]:
        if time.monotonic() >= deadline:
            pytest.fail("dispatch reconciliation did not complete")
        time.sleep(0.001)
    cancel_event.set()
    thread.join(timeout=2)

    slice_state = RunStore(run_id, tmp_path).load().slices["leaf-a"]
    event_types = [
        arguments["event_type"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    ]
    assert "spawn_leaf" not in _effect_names(transport)
    assert slice_state.dispatch_intent_id == "intent-restart-1"
    assert slice_state.status is SliceStatus.DISPATCH_UNCONFIRMED
    assert "tl.dispatch_reconciliation_started" in event_types
    assert "tl.dispatch_reconciliation_completed" in event_types
    assert isinstance(outcome.get("error"), LoopCancelled)
    assert not RunStore(run_id, tmp_path).load().gates


def test_restart_adopts_owner_by_intent_without_duplicate_spawn(tmp_path: Path) -> None:
    run_id = "dispatch-adopt-run"
    plan = WorkPlan.from_mapping(
        {"leaves": [{"name": "leaf-a", "task": "implement", "chainlink_issue_id": None}]}
    )
    config = TLLoopConfig(poll_interval=0.001)
    initial = _initial_slices(plan, config, tmp_path, run_id)
    initial["leaf-a"].update(
        {
            "status": "dispatching",
            "attempts": 1,
            "dispatch_intent_id": "intent-adopt-1",
            "dispatch_started_at": time.time(),
            "dispatch_last_boundary": "dispatch_intended",
        }
    )
    transport = RecordingTransport(
        listed_agents=[
            {"agent_id": "leaf-a-codex", "intent_id": "intent-adopt-1", "is_alive": True}
        ]
    )

    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue(
            [
                _event(1, "child_completed", "leaf-a", run_id=run_id),
                _event(2, "all_children_done", run_id=run_id),
            ]
        ),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
        initial_slices=initial,
    )

    adopted = result.final_state.slices["leaf-a"]
    assert "spawn_leaf" not in _effect_names(transport)
    assert adopted.dispatch_agent_id == "leaf-a-codex"
    assert adopted.dispatch_authoritative_event_seq is not None
    assert adopted.dispatch_last_boundary == "owner_adopted"


def test_stale_spawn_intent_cannot_confirm_new_attempt(tmp_path: Path) -> None:
    run_id = "dispatch-stale-intent-run"
    plan = WorkPlan.from_mapping(
        {"leaves": [{"name": "leaf-a", "task": "implement", "chainlink_issue_id": None}]}
    )
    cancel_event = threading.Event()
    config = TLLoopConfig(
        poll_interval=0.001,
        cancel_event=cancel_event,
    )
    initial = _initial_slices(plan, config, tmp_path, run_id)
    initial["leaf-a"].update(
        {
            "status": "dispatching",
            "attempts": 2,
            "dispatch_intent_id": "intent-new-2",
            "dispatch_started_at": time.time(),
            "dispatch_last_boundary": "dispatch_intended",
        }
    )

    source = SyntheticQueue(
        [
            _canonical_event(
                1,
                "agent.spawned",
                "leaf-a",
                run_id,
                intent_id="intent-stale-1",
            )
        ]
    )
    outcome: dict[str, BaseException] = {}
    transport = RecordingTransport()

    def run() -> None:
        try:
            run_tl_loop(
                run_id,
                plan,
                source,
                EffectClient(transport),
                config=config,
                root_dir=tmp_path,
                initial_slices=initial,
            )
        except BaseException as error:  # noqa: BLE001 - assert explicit cancellation
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while source.acknowledged != [1] and time.monotonic() < deadline:
        time.sleep(0.001)
    cancel_event.set()
    thread.join(timeout=2)

    stale = RunStore(run_id, tmp_path).load().slices["leaf-a"]
    assert stale.status is SliceStatus.DISPATCH_UNCONFIRMED
    assert stale.dispatch_intent_id == "intent-new-2"
    assert stale.dispatch_authoritative_event_seq is None
    assert stale.park_cause.value == "dispatch_unconfirmed"
    assert source.acknowledged == [1]
    assert isinstance(outcome.get("error"), LoopCancelled)


def test_ledger_run_id_mismatch_reaches_driver_diagnostics(tmp_path: Path) -> None:
    run_id = "production-mismatch-run"
    current_swarm = "current-swarm"
    stale_swarm = "stale-swarm"
    segments = tmp_path / "segments"
    segments.mkdir()
    (segments / "segment-000000000001.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_id": "stale-spawn",
                "id": "stale-spawn",
                "event_time": "2026-08-11T00:00:00Z",
                "observed_at": "2026-08-11T00:00:00Z",
                "run_seq": 1,
                "type": "agent.spawned",
                "agent_id": "leaf-a",
                "run_id": stale_swarm,
                "session_id": "session-1",
                "lifecycle_state": "observed",
                "data": {
                    "child_agent": "leaf-a",
                    "agent_type": "codex",
                    "branch": "main.leaf-a",
                    "intent_id": "stale-intent",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    segment = segments / "segment-000000000001.jsonl"
    source = LedgerQueue(
        LedgerReader(
            segments,
            run_id=run_id,
            state_root=tmp_path,
            ledger_run_id=current_swarm,
        ),
        poll_interval=0.001,
    ).start()
    transport = RecordingTransport()
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = run_tl_loop(
                run_id,
                WorkPlan.from_mapping(
                    {
                        "leaves": [
                            {"name": "leaf-a", "task": "implement", "chainlink_issue_id": None}
                        ]
                    }
                ),
                source,
                EffectClient(transport),
                config=TLLoopConfig(
                    poll_interval=0.001,
                    ledger_run_id=current_swarm,
                ),
                root_dir=tmp_path,
            )
        except BaseException as error:  # noqa: BLE001 - fail the test with the worker error
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while not source.findings and time.monotonic() < deadline:
        time.sleep(0.001)
    assert source.findings
    state_path = tmp_path / run_id / "run.json"
    current_intent: str | None = None
    while current_intent is None and time.monotonic() < deadline:
        time.sleep(0.001)
        if not state_path.exists():
            continue
        current_intent = RunStore(run_id, tmp_path).load().slices["leaf-a"].dispatch_intent_id
    assert current_intent
    rows = [
        {
            "schema_version": 1,
            "event_id": f"event-{sequence}",
            "id": f"event-{sequence}",
            "event_time": "2026-08-11T00:00:00Z",
            "observed_at": "2026-08-11T00:00:00Z",
            "run_seq": sequence,
            "type": event_type,
            "agent_id": "leaf-a",
            "run_id": current_swarm,
            "session_id": "session-1",
            "lifecycle_state": "observed",
            "data": data,
        }
        for sequence, event_type, data in (
            (
                2,
                "agent.spawned",
                {
                    "child_agent": "leaf-a",
                    "agent_type": "codex",
                    "branch": "main.leaf-a",
                    "intent_id": current_intent,
                },
            ),
            (3, "agent.completed", {"status": "completed"}),
            (4, "agent.notify_parent", {"shadow_event": {"kind": "all_children_done"}}),
        )
    ]
    with segment.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    thread.join(timeout=2)
    source.close(timeout=2)

    assert "error" not in outcome
    result = cast(TLRunResult, outcome["result"])

    assert result.diagnostics["received"] == 4
    assert result.diagnostics["filtered"] == 1
    assert result.diagnostics["reader_findings"] == [
        "ignored ledger event with run_id 'stale-swarm'; expected 'current-swarm'",
    ]


def test_pr_head_change_clears_per_head_gate_state() -> None:
    current = SliceState(
        id="leaf-a",
        status=SliceStatus.IN_REVIEW,
        paths=("src/leaf.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just tl-loop-test",),
        agent_type="codex",
        model="gpt-5",
        branch="main.leaf-a",
        worktree=".worktrees/leaf-a",
        pr_number=42,
        reviewed_head="head-a",
        attempts=2,
        verdict=Verdict.GO,
        review_findings={
            "head-a": (
                {
                    "severity": "blocking",
                    "path": "src/leaf.py",
                    "rationale": "old finding",
                },
            )
        },
        ci_state={"head-a": "success"},
        reviewer_attempt={"head-a": 1},
        repair_attempts=3,
        publication=PublicationBinding(
            pr_number=42,
            head_sha="head-a",
            head_branch="main.leaf-a",
            base_branch="main",
            attempt=2,
        ),
        handoff=HandoffEvidence(
            pr_number=42,
            head_sha="head-a",
            attempt=2,
            invocation_id="inv-a",
            agent_id="leaf-a",
            observed_at="2026-08-12T00:00:00Z",
        ),
    )

    updated = _update_slices({"leaf-a": current}, PRUpdated(42, "head-b", "leaf-a"))["leaf-a"]

    assert updated.status is SliceStatus.IN_REVIEW
    assert updated.pr_number == 42
    assert updated.reviewed_head == "head-b"
    assert updated.review_findings == {}
    assert updated.ci_state == {}
    assert updated.reviewer_attempt == {}
    assert updated.repair_attempts == 3
    assert updated.publication is None
    assert updated.handoff is None

    assert updated.verdict is None
    assert updated.verdict_at is None


def test_decoder_maps_wire_pr_filed_and_pr_updated_events() -> None:
    def raw(event_type: str, sequence: int, head_sha: str) -> dict[str, object]:
        return {
            "type": event_type,
            "run_seq": sequence,
            "run_id": "run-1",
            "agent_id": "leaf-a",
            "lifecycle_state": "emitted",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": head_sha,
            },
        }

    filed = TLEventDecoder().decode(project(raw("pr.filed", 1, "head-a")))
    updated = TLEventDecoder().decode(project(raw("pr.updated", 2, "head-b")))

    assert filed == PRFiled(42, "head-a", "leaf-a")
    assert updated == PRUpdated(42, "head-b", "leaf-a")


def test_pr_published_is_acknowledged_without_a_publication_transition(
    tmp_path: Path,
) -> None:
    run_id = "publication-telemetry-run"
    base: dict[str, object] = {
        "run_id": run_id,
        "agent_id": "leaf-a",
        "invocation_id": "inv-a",
        "lifecycle_state": "emitted",
        "observed_at": "2026-08-12T00:00:00Z",
    }

    def wire(event_type: str, sequence: int) -> EventEnvelope:
        return project(
            {
                **base,
                "type": event_type,
                "run_seq": sequence,
                "data": {
                    "slice_id": "leaf-a",
                    "pr_number": 42,
                    "head_sha": "head-a",
                    "head_branch": "main.leaf-a",
                    "base_branch": "main",
                    "verified": True,
                },
            }
        )

    source = SyntheticQueue(
        [
            wire("pr.published", 1),
            wire("pr.filed", 2),
            _event(3, "all_children_done", run_id=run_id),
        ]
    )
    plan = WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": "leaf-a",
                    "task": "implement the change",
                    "chainlink_issue_id": None,
                    "boundary": ["src/leaf.py"],
                    "verify": ["just tl-loop-test"],
                }
            ]
        }
    )

    result = run_tl_loop(
        run_id,
        plan,
        source,
        EffectClient(
            RecordingTransport(
                spawned_agent_id="leaf-a",
                spawned_invocation_id="inv-a",
            )
        ),
        config=TLLoopConfig(max_workers=0, max_leaves=1, max_events=3, poll_interval=0.001),
        root_dir=tmp_path,
    )

    # The telemetry row is acknowledged...
    assert 1 in source.acknowledged
    # ...but never produces an FSM transition of its own...
    assert all(transition.event_type != "pr.published" for transition in result.transitions)
    # ...and the definitive pr.filed still produces exactly one transition.
    assert sum(transition.event_type == "pr.filed" for transition in result.transitions) == 1
    assert result.final_state.slices["leaf-a"].pr_number == 42


def test_opt_in_reviewer_spawn_claims_attempt_and_injects_criteria(tmp_path: Path) -> None:
    run_id = "reviewer-spawn-run"
    raw_pr_filed = {
        "type": "pr.filed",
        "run_seq": 1,
        "run_id": run_id,
        "agent_id": "tunable-operator-body-opencode",
        "invocation_id": "invocation-a",
        "lifecycle_state": "emitted",
        "observed_at": "2026-08-12T00:00:00Z",
        "data": {
            "pr_number": 42,
            "head_sha": "head-a",
            "branch": "main.tunable-operator-body-opencode",
            "head_branch": "main.tunable-operator-body-opencode",
            "base_branch": "main",
        },
    }
    source = SyntheticQueue(
        [
            project(cast(dict[str, object], raw_pr_filed)),
            project(
                {
                    **raw_pr_filed,
                    "run_seq": 2,
                }
            ),
            _event(3, "all_children_done", run_id=run_id),
        ]
    )
    transport = RecordingTransport(
        spawned_agent_id="tunable-operator-body-opencode",
        spawned_invocation_id="invocation-a",
    )
    plan = WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": "tunable-operator-body",
                    "task": "implement the change",
                    "chainlink_issue_id": None,
                    "boundary": ["src/leaf.py"],
                    "verify": ["just tl-loop-test"],
                    "done_criteria": ["the changed behavior is covered"],
                }
            ]
        }
    )

    result = run_tl_loop(
        run_id,
        plan,
        source,
        EffectClient(transport),
        config=TLLoopConfig(
            enable_reviewer_spawn=True,
            max_workers=0,
            max_leaves=1,
            max_events=2,
            poll_interval=0.001,
        ),
        root_dir=tmp_path,
    )

    assert _effect_names(transport) == ["spawn_leaf", "spawn_reviewer", "root_branch_finalize"]
    assert sum(name == "spawn_reviewer" for name, _ in transport.calls) == 1
    reviewer_args = next(
        arguments for name, arguments in transport.calls if name == "spawn_reviewer"
    )
    assert reviewer_args["pr_number"] == 42
    assert reviewer_args["head_sha"] == "head-a"
    assert reviewer_args["force"] is False
    criteria = cast(list[object], reviewer_args["acceptance_criteria"])
    assert "Run-state test plan: just tl-loop-test" in criteria
    slice_state = result.final_state.slices["tunable-operator-body"]
    assert slice_state.reviewer_attempt == {"head-a": 1}
    assert source.acknowledged == [1, 2, 3]


def test_reviewer_unknown_outcome_persists_gate_without_retry(tmp_path: Path) -> None:
    store = _review_store(tmp_path)

    class UnknownReviewerTransport(RecordingTransport):
        def call_tool(
            self, role: str, name: str, tool_name: str, arguments: JsonObject
        ) -> JsonObject:
            if tool_name == "spawn_reviewer":
                raise RuntimeError("reviewer service disconnected")
            return super().call_tool(role, name, tool_name, arguments)

    journal = EffectJournal("review-run", tmp_path / "action-journal.json")
    transport = UnknownReviewerTransport()
    intent = ExternalIntent(
        "spawn_reviewer",
        "leaf-a",
        {"pr_number": 42, "head_sha": "head-a"},
    )

    with pytest.raises(RuntimeError, match="disconnected"):
        _execute_direct_reviewer_intent(
            store.load(),
            intent,
            ConvergenceTracker(),
            store,
            TLLoopConfig(active=True, enable_reviewer_spawn=True),
            EffectClient(transport),
            journal,
        )

    recovered = store.load().slices["leaf-a"]
    assert recovered.action is not None
    assert recovered.action.phase is ActionPhase.UNKNOWN
    assert any(gate.name.startswith("tl-action-journal-") for gate in store.load().gates)
    assert journal.pending_entries()[0]["status"] == "unknown"


def test_binding_review_findings_adjudicate_and_resume_same_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ReviewBackend(
        [
            RlmResponse(
                {
                    "verdict": "NO-GO",
                    "reviewed_head": "head-a",
                    "reasons": [
                        {
                            "severity": "blocking",
                            "file": "src/leaf.py",
                            "line": 8,
                            "claim": "The failure path is unhandled",
                        }
                    ],
                    "blocking_count": 1,
                }
            ),
        ]
    )
    transport = ReviewRepairTransport()
    store = _review_store(tmp_path)
    event = _review_event()
    state = store.load()
    original = state.slices["leaf-a"]
    state = store.checkpoint(
        state.fsm,
        {
            **state.slices,
            "leaf-a": replace(
                original,
                dispatch_agent_id="leaf-a-opencode",
                dispatch_started_at=1.0,
                task_timeout_seconds=3600.0,
                task_timeout_source="project",
            ),
        },
        state.budgets,
        state.events.last_consumed_offset,
    )
    monkeypatch.setattr("tl_loop.loop.driver.time.time", lambda: 2000.0)
    config = TLLoopConfig(
        active=True,
        review_model_choice=_review_choice(backend),
        review_policy_path=Path(".exo/review-policy.toml"),
    )
    effects_log: list[object] = []

    _route_review_event(
        WorkPlan.from_mapping(
            {
                "leaves": [
                    {
                        "name": "leaf-a",
                        "task": "implement the requested change",
                        "chainlink_issue_id": None,
                        "boundary": ["src/leaf.py"],
                        "verify": ["just tl-loop-test"],
                        "done_criteria": ["the failure path is covered"],
                    }
                ]
            }
        ),
        store,
        state,
        TLPlanning(),
        event,
        1,
        config,
        EffectClient(transport),
        effects_log,
    )

    restored = store.load().slices["leaf-a"]
    assert [request.name for request in backend.requests] == ["adjudicate_review"]
    assert _effect_names(transport) == [
        "watcher_pr_state",
        "resume_pr",
    ]
    resume = next(arguments for name, arguments in transport.calls if name == "resume_pr")
    assert "The failure path is unhandled" in str(resume["task"])
    assert restored.status is SliceStatus.REPAIRING
    assert restored.reviewed_head == "head-a"
    assert restored.verdict is Verdict.NO_GO
    assert restored.repair_attempts == 1
    assert restored.dispatch_started_at == 2000.0
    assert restored.dispatch_agent_id == "leaf-a-opencode"
    assert restored.dispatch_invocation_id is None
    assert restored.review_findings["head-a"][0]["path"] == "src/leaf.py"
    assert all(name != "spawn_leaf" for name, _ in transport.calls)


def test_parent_aggregate_review_event_does_not_create_approval(tmp_path: Path) -> None:
    backend = ReviewBackend(
        [
            RlmResponse(
                {
                    "verdict": "GO",
                    "reviewed_head": "head-a",
                    "reasons": [],
                    "blocking_count": 0,
                }
            )
        ]
    )
    transport = ReviewRepairTransport()
    store = _review_store(tmp_path)
    current = store.load().slices["leaf-a"]
    current = replace(
        current,
        dispatch_agent_id="review-run:leaf-a:integration",
        dispatch_last_boundary="aggregate_pr_open",
    )
    candidate = IntegrationCandidateState(
        lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
        aggregate_pr_number=42,
        aggregate_head_sha="head-a",
        aggregate_patch_digest="patch-a",
        aggregate_original_base_sha="main",
        integration_owner_id="review-run:leaf-a:integration",
        integration_owner_run_id="leaf-a",
        integration_owner_branch="main.leaf-a",
        integration_owner_worktree=".worktrees/leaf-a",
        head_sha="head-a",
        patch_digest="patch-a",
    )
    state = store.checkpoint(
        TLPlanning(),
        {"leaf-a": current},
        BudgetLedger(0, 0),
        offset=0,
        integration=IntegrationRuntimeState(
            lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
            sub_tl_states={"leaf-a": IntegrationLifecycle.AGGREGATE_PR_OPEN},
            candidates={"leaf-a": candidate},
        ),
    )
    _route_review_event(
        WorkPlan(sub_tls=(SubTLTask("leaf-a", WorkPlan(), source=SyntheticQueue([])),)),
        store,
        state,
        TLPlanning(),
        _review_event(),
        1,
        TLLoopConfig(
            active=True,
            review_model_choice=_review_choice(backend),
            review_policy_path=Path(".exo/review-policy.toml"),
        ),
        EffectClient(transport),
        [],
    )

    restored = store.load()
    assert (
        restored.integration.candidates["leaf-a"].lifecycle
        is IntegrationLifecycle.AGGREGATE_PR_OPEN
    )
    assert restored.slices["leaf-a"] == state.slices["leaf-a"]
    assert backend.requests == []
    assert _effect_names(transport) == []


def _watcher_approval_row() -> EventEnvelope:
    """The row the watcher writes for an approval it read off the forge.

    Verbatim in the shape that matters: a clean approval carries no ``findings``
    key at all, so a row that is re-derived as a first verdict has nothing to
    bind, and its head lives in the event data next to ``review_head_sha``.
    """
    return project(
        {
            "type": "pr.review",
            "run_seq": 13,
            "run_id": "review-run",
            "agent_id": "reviewer-unresolved",
            "lifecycle_state": "emitted",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-a",
                "review_head_sha": "head-a",
                "review_id": 1,
                "kind": "approved",
                "verdict": "approved",
                "ci_status": "unknown",
                "reviewer_account_authenticated": False,
                "reviewer_identity_reason": "Forgejo review author did not resolve",
                "reviewer_identity_unresolved": True,
            },
        }
    )


def _validated_approval() -> DurableReviewEvidence:
    return DurableReviewEvidence(
        review_id=1,
        pr_number=42,
        head_sha="head-a",
        reviewer_agent_id="leaf-a:reviewer",
        verdict=Verdict.GO,
        submitted_at="2026-08-12T00:00:00Z",
        validated_at="2026-08-12T00:00:00Z",
    )


def _aggregate_candidate(lifecycle: IntegrationLifecycle) -> IntegrationCandidateState:
    return IntegrationCandidateState(
        lifecycle=lifecycle,
        aggregate_pr_number=42,
        aggregate_head_sha="head-a",
        aggregate_patch_digest="patch-a",
        aggregate_original_base_sha="main",
        integration_owner_id="leaf-a",
        integration_owner_run_id="leaf-a",
        integration_owner_branch="main.leaf-a",
        integration_owner_worktree=".worktrees/leaf-a",
        head_sha="head-a",
        patch_digest="patch-a",
    )


def _route_watcher_approval(store: RunStore, state: RunState) -> RunState:
    return _route_review_event(
        WorkPlan(sub_tls=(SubTLTask("leaf-a", WorkPlan(), source=SyntheticQueue([])),)),
        store,
        state,
        TLPlanning(),
        _watcher_approval_row(),
        13,
        TLLoopConfig(active=True),
        EffectClient(RecordingTransport()),
        [],
    )


def test_replayed_parent_aggregate_approval_does_not_advance_lifecycle(
    tmp_path: Path,
) -> None:
    """Historical aggregate review rows cannot grant the unsupported gate."""
    store = _review_store(tmp_path, verdict=Verdict.GO)
    owner = replace(
        store.load().slices["leaf-a"],
        dispatch_agent_id="leaf-a",
        dispatch_last_boundary="aggregate_pr_open",
        review_evidence=_validated_approval(),
    )
    state = store.checkpoint(
        TLPlanning(),
        {"leaf-a": owner},
        BudgetLedger(0, 0),
        offset=0,
        integration=IntegrationRuntimeState(
            lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
            sub_tl_states={"leaf-a": IntegrationLifecycle.AGGREGATE_PR_OPEN},
            candidates={"leaf-a": _aggregate_candidate(IntegrationLifecycle.AGGREGATE_PR_OPEN)},
        ),
    )

    routed = _route_watcher_approval(store, state)

    assert (
        routed.integration.candidates["leaf-a"].lifecycle is IntegrationLifecycle.AGGREGATE_PR_OPEN
    )
    assert routed.slices["leaf-a"] == state.slices["leaf-a"]


def test_an_aggregate_approval_the_candidate_already_holds_binds_nothing(
    tmp_path: Path,
) -> None:
    """A repeat past the binding writes no checkpoint that advances nothing."""
    store = _review_store(tmp_path, verdict=Verdict.GO)
    owner = replace(
        store.load().slices["leaf-a"],
        dispatch_agent_id="leaf-a",
        dispatch_last_boundary="aggregate_pr_open",
        review_evidence=_validated_approval(),
    )
    state = store.checkpoint(
        TLPlanning(),
        {"leaf-a": owner},
        BudgetLedger(0, 0),
        offset=0,
        integration=IntegrationRuntimeState(
            lifecycle=IntegrationLifecycle.READY_FOR_INTEGRATION,
            sub_tl_states={"leaf-a": IntegrationLifecycle.READY_FOR_INTEGRATION},
            candidates={"leaf-a": _aggregate_candidate(IntegrationLifecycle.READY_FOR_INTEGRATION)},
        ),
    )

    assert _route_watcher_approval(store, state) == state


def test_a_repeated_approval_on_a_direct_review_leaf_binds_nothing(tmp_path: Path) -> None:
    """The repeat guard's original answer still holds for a slice that owns no aggregate."""
    store = _review_store(tmp_path, verdict=Verdict.GO)
    state = store.checkpoint(
        TLPlanning(),
        {"leaf-a": replace(store.load().slices["leaf-a"], review_evidence=_validated_approval())},
        BudgetLedger(0, 0),
        offset=0,
    )

    assert _route_watcher_approval(store, state) == state


@pytest.mark.parametrize(
    "boundary",
    [
        "aggregate_pr_open",
        "base_revalidation",
        "integration_revalidation",
        "integration_conflict",
        "integration_gate",
    ],
)
def test_an_aggregate_owner_stays_recognisable_at_every_boundary_it_reaches(
    boundary: str,
) -> None:
    """Revalidation overwrites the publication's boundary label, not the identity.

    Recognising only the label the aggregate PR was opened with loses the owner
    the moment its base or head evidence is invalidated -- and an aggregate the
    reducer cannot recognise is one whose approval is re-derived as a first
    review instead of binding.
    """
    owner = SliceState(
        id="leaf-a",
        status=SliceStatus.IN_REVIEW,
        paths=(),
        depends_on=(),
        base_ref="main",
        test_plan=(),
        agent_type=None,
        model=None,
        branch="main.leaf-a",
        worktree=None,
        pr_number=42,
        reviewed_head="head-a",
        attempts=1,
        verdict=Verdict.GO,
        dispatch_agent_id="leaf-a",
    )

    assert _is_aggregate_slice(replace(owner, dispatch_last_boundary=boundary))
    assert not _is_aggregate_slice(replace(owner, dispatch_last_boundary="spawn_request_accepted"))


def _ownership_publication_snapshot(
    *, merged: bool, ownership: str, pr_number: int = 42
) -> JsonObject:
    snapshot: JsonObject = {
        "found": True,
        "merged": merged,
        "pr_number": pr_number,
        "head_sha": "head-a",
        "head_branch": "main.leaf-a",
        "base_branch": "main",
        "base_sha": "base-a",
        "patch_digest": "patch-a",
        "merge_tree_sha": "tree-a",
        "ci_status": "success",
        "publication": {
            "invocation_id": "inv-current",
            "slice_id": "leaf-a",
            "author_agent": "leaf-a",
            "succession_invocation_ids": [],
        },
    }
    if ownership == "unresolved":
        # Explicitly unresolved ownership: the park path owns this case, but a
        # merge/recovery path may never resolve it.
        snapshot["publication_ownership_verified"] = False
        snapshot["publication_ownership_error"] = "publication ownership is unverified"
    elif ownership == "malformed":
        # verified=True with the ownership error field omitted: the complete
        # ownership contract is not satisfied.
        snapshot["publication_ownership_verified"] = True
    else:
        raise AssertionError(f"unknown ownership mode {ownership!r}")
    return snapshot


@pytest.mark.parametrize("ownership", ["unresolved", "malformed"])
def test_aggregate_candidate_untrusted_ownership_is_not_merged(
    tmp_path: Path, ownership: str
) -> None:
    store = _review_store(tmp_path)
    state = store.load()
    current = replace(
        state.slices["leaf-a"],
        pr_number=42,
        reviewed_head="head-a",
        action=ActionState(
            ActionKind.MERGE,
            ActionPhase.UNKNOWN,
            intent_id="merge-intent",
            head_sha="head-a",
        ),
    )
    candidate = IntegrationCandidateState(
        lifecycle=IntegrationLifecycle.MERGING,
        aggregate_pr_number=42,
        aggregate_head_sha="head-a",
        aggregate_patch_digest="patch-a",
        aggregate_original_base_sha="main",
        head_sha="head-a",
        patch_digest="patch-a",
        validated_base_sha="base-a",
        merge_tree_sha="tree-a",
        ci_status="success",
    )
    state = store.checkpoint(
        state.fsm,
        {"leaf-a": current},
        state.budgets,
        state.events.last_consumed_offset,
        integration=IntegrationRuntimeState(
            lifecycle=IntegrationLifecycle.MERGING,
            sub_tl_states={"leaf-a": IntegrationLifecycle.MERGING},
            head_sha="head-a",
            patch_digest="patch-a",
            validated_base_sha="base-a",
            merge_tree_sha="tree-a",
            ci_status="success",
            candidates={"leaf-a": candidate},
        ),
    )
    transport = IntegrationTransport(
        snapshots=[_ownership_publication_snapshot(merged=True, ownership=ownership)]
    )
    task = SubTLTask("leaf-a", WorkPlan(), source=SyntheticQueue([]))
    config = TLLoopConfig(
        active=True,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )

    result = _integrate_one_candidate(task, state, config, EffectClient(transport), store, [])

    # The untrusted snapshot may not reconcile an aggregate merge.
    assert result.slices["leaf-a"].status is SliceStatus.IN_REVIEW
    assert result.slices["leaf-a"].post_merge is None
    assert not any(name == "merge_pr" for name, _ in transport.calls)


@pytest.mark.parametrize("ownership", ["unresolved", "malformed"])
def test_pending_merge_entry_untrusted_ownership_is_not_resolved(
    tmp_path: Path, ownership: str
) -> None:
    store = _review_store(tmp_path)
    state = store.load()
    journal = EffectJournal("review-run", tmp_path / "action-journal.json")
    intent = EffectIntent("merge_pr", "leaf-a", {"pr_number": 42}, True)
    journal.append(intent)
    entry = next(
        candidate
        for candidate in journal.pending_entries()
        if candidate.get("operation") == "merge_pr"
    )
    transport = IntegrationTransport(
        snapshots=[_ownership_publication_snapshot(merged=True, ownership=ownership)]
    )

    resolved = _reconcile_pending_merge_entry(entry, state, store, journal, EffectClient(transport))

    assert resolved is False
    # The pending merge stays pending; it was not confirmed from the snapshot.
    assert journal.pending_entries()


def test_go_with_nits_persists_follow_up_in_per_head_state(tmp_path: Path) -> None:
    backend = ReviewBackend(
        [
            RlmResponse(
                {
                    "verdict": "GO-WITH-NITS",
                    "reviewed_head": "head-a",
                    "reasons": [
                        {
                            "severity": "nit",
                            "file": "src/leaf.py",
                            "line": 7,
                            "claim": "Clarify this name",
                        }
                    ],
                    "blocking_count": 0,
                }
            )
        ]
    )
    store = _review_store(tmp_path)
    _route_review_event(
        WorkPlan.from_mapping(
            {
                "leaves": [
                    {
                        "name": "leaf-a",
                        "task": "implement the requested change",
                        "chainlink_issue_id": None,
                        "boundary": ["src/leaf.py"],
                        "verify": ["just tl-loop-test"],
                    }
                ]
            }
        ),
        store,
        store.load(),
        TLPlanning(),
        _review_event(finding_severity="nit"),
        1,
        TLLoopConfig(
            active=True,
            review_model_choice=_review_choice(backend),
            review_policy_path=Path(".exo/review-policy.toml"),
        ),
        EffectClient(RecordingTransport()),
        [],
    )

    restored = store.load().slices["leaf-a"]
    assert restored.verdict is Verdict.GO_WITH_NITS
    assert {
        "severity": "nit",
        "path": "src/leaf.py:7",
        "rationale": "Clarify this name",
    } in restored.review_findings["head-a"]


def test_ci_failure_records_head_and_resumes_same_pr(tmp_path: Path) -> None:
    transport = ReviewRepairTransport()
    store = _review_store(tmp_path, verdict=Verdict.GO)
    state = store.load()
    _route_ci_event(
        store,
        state,
        TLPlanning(),
        _ci_failure_event(),
        1,
        TLLoopConfig(
            active=True,
            review_policy_path=Path(".exo/review-policy.toml"),
        ),
        EffectClient(transport),
        [],
    )

    restored = store.load().slices["leaf-a"]
    assert restored.ci_state == {"head-a": "failure"}
    assert restored.status is SliceStatus.REPAIRING
    assert restored.verdict is Verdict.NO_GO
    assert _effect_names(transport) == [
        "watcher_pr_state",
        "resume_pr",
    ]


def test_ci_failure_before_review_records_ci_state_without_verdict(tmp_path: Path) -> None:
    """The exact crash shape: a CI failure lands before any head has been reviewed.

    ``reviewed_head`` is None, so the schema invariant that a verdict requires
    a bound reviewed head must not be violated. Before the fix this call raised
    a schema error out of ``store.checkpoint`` and the event cursor never
    advanced, so every restart replayed the same event forever.
    """
    transport = RecordingTransport()
    store = _review_store(tmp_path, reviewed_head=None, verdict=None)
    state = store.load()

    result = _route_ci_event(
        store,
        state,
        TLPlanning(),
        _ci_failure_event(),
        1,
        TLLoopConfig(active=True, review_policy_path=Path(".exo/review-policy.toml")),
        EffectClient(transport),
        [],
    )

    assert result.slices["leaf-a"].verdict is None
    restored = store.load().slices["leaf-a"]
    assert restored.ci_state == {"head-a": "failure"}
    assert restored.verdict is None
    assert restored.verdict_at is None
    assert restored.status is SliceStatus.IN_REVIEW
    assert _effect_names(transport) == []


def test_ci_failure_for_stale_head_does_not_override_verdict(tmp_path: Path) -> None:
    """A late CI result for a head that isn't the currently reviewed head must not
    clobber the verdict bound to the reviewed head."""
    transport = RecordingTransport()
    store = _review_store(tmp_path, reviewed_head="head-b", verdict=Verdict.GO)
    state = store.load()

    result = _route_ci_event(
        store,
        state,
        TLPlanning(),
        _ci_failure_event(),
        1,
        TLLoopConfig(active=True, review_policy_path=Path(".exo/review-policy.toml")),
        EffectClient(transport),
        [],
    )

    assert result.slices["leaf-a"].verdict is Verdict.GO
    restored = store.load().slices["leaf-a"]
    assert restored.ci_state == {"head-a": "failure"}
    assert restored.verdict is Verdict.GO
    assert restored.reviewed_head == "head-b"
    assert _effect_names(transport) == []


def test_ci_failure_after_review_binds_head_then_still_repairs(tmp_path: Path) -> None:
    """A CI failure before review is recorded harmlessly; once review binds the
    same head, a subsequent CI failure on it still sets NO_GO and repairs."""
    pre_review_event = project(
        {
            "type": "ci.status_changed",
            "run_seq": 1,
            "run_id": "review-run",
            "agent_id": "leaf-a",
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-x",
                "status": "failure",
                "message": "tests failed",
            },
        }
    )
    pre_review_transport = RecordingTransport()
    store = _review_store(tmp_path, reviewed_head=None, verdict=None)
    state = store.load()
    state = _route_ci_event(
        store,
        state,
        TLPlanning(),
        pre_review_event,
        1,
        TLLoopConfig(active=True, review_policy_path=Path(".exo/review-policy.toml")),
        EffectClient(pre_review_transport),
        [],
    )
    assert state.slices["leaf-a"].verdict is None
    assert state.slices["leaf-a"].ci_state == {"head-x": "failure"}
    assert _effect_names(pre_review_transport) == []

    reviewed = replace(state.slices["leaf-a"], reviewed_head="head-a", verdict=Verdict.GO)
    state = store.checkpoint(TLPlanning(), {**state.slices, "leaf-a": reviewed}, state.budgets, 1)

    repair_transport = ReviewRepairTransport()
    state = _route_ci_event(
        store,
        state,
        TLPlanning(),
        _ci_failure_event(),
        2,
        TLLoopConfig(
            active=True,
            review_policy_path=Path(".exo/review-policy.toml"),
        ),
        EffectClient(repair_transport),
        [],
    )

    restored = store.load().slices["leaf-a"]
    assert restored.ci_state == {"head-x": "failure", "head-a": "failure"}
    assert restored.status is SliceStatus.REPAIRING
    assert restored.verdict is Verdict.NO_GO
    assert _effect_names(repair_transport) == [
        "watcher_pr_state",
        "resume_pr",
    ]


def _review_choice(backend: ReviewBackend) -> RlmModelChoice:
    return RlmModelChoice(
        model_id="test-model",
        backend=backend,
        store=RlmCallStore(),
        context_length=10_000,
    )


def _tl_choice(backend: ReviewBackend) -> RlmModelChoice:
    return RlmModelChoice(
        model_id="tl-model",
        backend=backend,
        role="tl",
        store=RlmCallStore(),
        context_length=10_000,
    )


def test_requested_model_requires_catalog() -> None:
    with pytest.raises(ValueError, match="requested_model requires a model catalog"):
        TLLoopConfig(requested_model="gpt-5-mini")


def _review_store(
    tmp_path: Path,
    *,
    verdict: Verdict | None = None,
    reviewed_head: str | None = "head-a",
    chainlink_issue_id: int | None = None,
) -> RunStore:
    store = RunStore("review-run", tmp_path)
    manifest = build_plan_manifest(
        {
            "leaves": [
                {
                    "name": "leaf-a",
                    "task": "review leaf",
                    "boundary": ["src/leaf.py"],
                    "verify": ["just tl-loop-test"],
                    "chainlink_issue_id": chainlink_issue_id,
                }
            ]
        },
        scope_id="review-run",
    )
    create(
        "review-run",
        {
            "plan_manifest": manifest.to_document(),
            "repository_identity": {
                "remote_url": "https://forge.example.com/org/repo.git",
            },
        },
        root_dir=tmp_path,
    )
    store.checkpoint(
        TLPlanning(),
        {
            "leaf-a": SliceState(
                id="leaf-a",
                status=SliceStatus.IN_REVIEW,
                paths=("src/leaf.py",),
                depends_on=(),
                base_ref="main",
                test_plan=("just tl-loop-test",),
                agent_type="codex",
                model="test-model",
                branch="main.leaf-a",
                worktree=".worktrees/leaf-a",
                pr_number=42,
                reviewed_head=reviewed_head,
                attempts=1,
                verdict=verdict,
                chainlink_issue_id=chainlink_issue_id,
            )
        },
        BudgetLedger(0, 0),
        offset=0,
    )
    return store


def _direct_reviewer_event(
    *,
    verdict: str,
    agent_id: str = "review-pr-42-codex",
    reviewer_agent_id: str | None = None,
    reviewer_account_authenticated: bool = True,
    reviewer_identity_unresolved: bool = False,
) -> EventEnvelope:
    data: dict[str, object] = {
        "slice_id": "leaf-a",
        "pr_number": 42,
        "head_sha": "head-a",
        "kind": "approved" if verdict == "GO" else "changes_requested",
        "verdict": verdict,
        "review_id": 403,
        "review_head_sha": "head-a",
        "reviewer_account_authenticated": reviewer_account_authenticated,
        "reviewer_identity_unresolved": reviewer_identity_unresolved,
        "findings": []
        if verdict == "GO"
        else [
            {
                "severity": "blocking",
                "path": "src/leaf.py",
                "rationale": "The failure path is unhandled",
            }
        ],
    }
    if reviewer_agent_id is not None:
        data["reviewer_agent_id"] = reviewer_agent_id
    return project(
        {
            "type": "pr.review",
            "run_seq": 1,
            "run_id": "review-run",
            "agent_id": agent_id,
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": data,
        }
    )


def test_direct_reviewer_approval_is_reduced_for_exact_head_without_model(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    current = store.load().slices["leaf-a"]
    store.checkpoint(
        TLPlanning(),
        {
            "leaf-a": replace(
                current,
                reviewer_attempt={"head-a": 1},
                reviewer_agent_id="review-pr-42-codex",
            )
        },
        BudgetLedger(0, 0),
        offset=0,
    )

    _route_review_event(
        WorkPlan.from_mapping(
            {"leaves": [{"name": "leaf-a", "task": "implement", "chainlink_issue_id": None}]}
        ),
        store,
        store.load(),
        TLPlanning(),
        _direct_reviewer_event(
            verdict="GO",
            agent_id="leaf-owner-opencode",
            reviewer_agent_id="review-pr-42-codex",
        ),
        1,
        TLLoopConfig(active=True),
        EffectClient(RecordingTransport()),
        [],
    )

    restored = store.load().slices["leaf-a"]
    assert restored.verdict is Verdict.GO
    assert restored.reviewed_head == "head-a"
    assert restored.reviewer_agent_id == "review-pr-42-codex"


def test_direct_reviewer_requested_changes_routes_one_same_owner_repair(tmp_path: Path) -> None:
    backend = ReviewBackend([])
    transport = ReviewRepairTransport()
    store = _review_store(tmp_path)
    current = store.load().slices["leaf-a"]
    store.checkpoint(
        TLPlanning(),
        {
            "leaf-a": replace(
                current,
                reviewer_attempt={"head-a": 1},
                reviewer_agent_id="review-pr-42-codex",
                publication=PublicationBinding(42, "head-a", "main.leaf-a", "main", 1, "inv-a"),
                handoff=HandoffEvidence(
                    42,
                    "head-a",
                    1,
                    "inv-a",
                    "leaf-a",
                    "2026-08-12T00:00:00Z",
                ),
            )
        },
        BudgetLedger(0, 0),
        offset=0,
    )
    plan = WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": "leaf-a",
                    "task": "implement",
                    "chainlink_issue_id": None,
                    "boundary": ["src/leaf.py"],
                    "verify": ["just tl-loop-test"],
                }
            ]
        }
    )
    config = TLLoopConfig(
        active=True,
        review_model_choice=_review_choice(backend),
        review_policy_path=Path(".exo/review-policy.toml"),
    )
    event = _direct_reviewer_event(
        verdict="NO-GO",
        reviewer_agent_id="review-pr-42-codex",
    )
    _route_review_event(
        plan, store, store.load(), TLPlanning(), event, 1, config, EffectClient(transport), []
    )
    _apply_convergence(
        store.load(),
        ConvergenceTracker(),
        store,
        config,
        EffectClient(transport),
        [],
    )
    _route_review_event(
        plan, store, store.load(), TLPlanning(), event, 2, config, EffectClient(transport), []
    )

    restored = store.load().slices["leaf-a"]
    assert restored.verdict is Verdict.NO_GO
    assert restored.status is SliceStatus.REPAIRING
    assert len([name for name, _ in transport.calls if name == "resume_pr"]) == 1
    assert backend.requests == []


def test_review_round_exhaustion_parks_gate_and_emits_once(tmp_path: Path) -> None:
    class IssueTransport(RecordingTransport):
        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            if tool_name == "chainlink_issue_create":
                self.calls.append((tool_name, arguments))
                return {"success": True, "result": {"issue_id": 1010}}
            return super().call_tool(role, name, tool_name, arguments)

    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        verdict=Verdict.NO_GO,
        review_rounds=2,
        reviewer_attempt={"head-a": 1},
    )
    store.checkpoint(
        TLPlanning(),
        {"leaf-a": current},
        BudgetLedger(0, 0),
        offset=0,
    )
    store.set_review_policy(2, "policy_file")
    transport = IssueTransport()
    journal = EffectJournal("review-run", store.run_dir / "action-journal.json")
    backend = ReviewBackend(
        [RlmResponse({"action": "escalate", "reason": "the reviewer and owner disagree"})]
    )
    config = TLLoopConfig(
        active=True,
        policy=validate_policy(_selector_policy(1)),
        tl_model_choice=_tl_choice(backend),
    )

    result = _apply_convergence(
        store.load(),
        ConvergenceTracker(reviewer_max_rounds=2),
        store,
        config,
        EffectClient(transport),
        journal,
    )

    parked = result.slices["leaf-a"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.REVIEW_ROUNDS_EXHAUSTED
    assert any(
        gate.name == blocked_gate_name("review-run", "leaf-a", 1, "review_rounds_exhausted")
        and gate.status is GateStatus.PENDING
        for gate in result.gates
    )
    exhaustion_events = [
        arguments
        for name, arguments in transport.calls
        if name == "emit_controller_event"
        and arguments.get("event_type") == "tl.review_rounds_exhausted"
    ]
    assert len(exhaustion_events) == 1
    assert [request.name for request in backend.requests] == ["review_stuck"]
    assert not any(name in {"resume_pr", "spawn_reviewer"} for name, _ in transport.calls)

    _apply_convergence(
        result,
        ConvergenceTracker(reviewer_max_rounds=2),
        store,
        config,
        EffectClient(transport),
        journal,
    )
    assert (
        len(
            [
                arguments
                for name, arguments in transport.calls
                if name == "emit_controller_event"
                and arguments.get("event_type") == "tl.review_rounds_exhausted"
            ]
        )
        == 1
    )


def _mergeable_review_store(tmp_path: Path) -> RunStore:
    store = _review_store(tmp_path, verdict=Verdict.GO)
    current = store.load().slices["leaf-a"]
    store.checkpoint(
        TLPlanning(),
        {
            "leaf-a": replace(
                current,
                publication=PublicationBinding(42, "head-a", "main.leaf-a", "main", 1),
                handoff=HandoffEvidence(42, "head-a", 1, "inv-1", "leaf-a", "2026-08-12T00:00:00Z"),
                reviewer_attempt={"head-a": 1},
                ci_state={"head-a": "success"},
            )
        },
        BudgetLedger(0, 0),
        offset=0,
    )
    return store


class RepeatedActionTransport(RecordingTransport):
    """Watcher compare evidence is never complete, so the merge intent repeats."""

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        if tool_name == "chainlink_issue_create":
            self.calls.append((tool_name, arguments))
            return {"success": True, "result": {"issue_id": 1011}}
        if tool_name == "watcher_pr_state":
            self.calls.append((tool_name, arguments))
            return {
                "success": True,
                "result": {
                    "merged": False,
                    "head_sha": "head-a",
                    "base_sha": "base-a",
                    # patch_digest/merge_tree_sha omitted: compare evidence incomplete
                    "ci_status": "success",
                },
            }
        return super().call_tool(role, name, tool_name, arguments)


def test_repeated_action_parks_with_gate_instead_of_crashing(tmp_path: Path) -> None:
    store = _mergeable_review_store(tmp_path)
    transport = RepeatedActionTransport()
    journal = EffectJournal("review-run", store.run_dir / "action-journal.json")
    tracker = ConvergenceTracker()
    config = TLLoopConfig(active=True)

    first = _apply_convergence(
        store.load(), tracker, store, config, EffectClient(transport), journal
    )
    assert first.slices["leaf-a"].status is SliceStatus.IN_REVIEW

    parked = _apply_convergence(
        store.load(), tracker, store, config, EffectClient(transport), journal
    )

    parked_slice = parked.slices["leaf-a"]
    assert parked_slice.status is SliceStatus.PARKED
    assert parked_slice.park_cause is ParkCause.REPEATED_ACTION_NO_PROGRESS
    audit = parked_slice.park_audit
    assert audit["invariant"] == "repeated_state_version_action"
    assert audit["action"] == "merge effect"
    assert audit["target_id"] == "leaf-a"
    assert "without durable state advancing" in str(audit["reason"])
    assert any(
        gate.name == blocked_gate_name("review-run", "leaf-a", 1, "repeated_action_no_progress")
        and gate.status is GateStatus.PENDING
        for gate in parked.gates
    )
    assert not any(name == "merge_pr" for name, _ in transport.calls)
    parked_events = [
        arguments
        for name, arguments in transport.calls
        if name == "emit_controller_event"
        and arguments.get("event_type") == "tl.repeated_action_parked"
    ]
    assert len(parked_events) == 1


def test_parked_repeated_action_survives_restart_idempotently(tmp_path: Path) -> None:
    store = _mergeable_review_store(tmp_path)
    transport = RepeatedActionTransport()
    journal = EffectJournal("review-run", store.run_dir / "action-journal.json")
    config = TLLoopConfig(active=True)
    tracker = ConvergenceTracker()
    _apply_convergence(store.load(), tracker, store, config, EffectClient(transport), journal)
    parked = _apply_convergence(
        store.load(), tracker, store, config, EffectClient(transport), journal
    )
    assert parked.slices["leaf-a"].status is SliceStatus.PARKED
    issues_before = [call for call in transport.calls if call[0] == "chainlink_issue_create"]

    resumed = _apply_convergence(
        store.load(), ConvergenceTracker(), store, config, EffectClient(transport), journal
    )

    assert resumed.slices["leaf-a"].status is SliceStatus.PARKED
    assert resumed.slices["leaf-a"].park_cause is ParkCause.REPEATED_ACTION_NO_PROGRESS
    assert [
        call for call in transport.calls if call[0] == "chainlink_issue_create"
    ] == issues_before
    assert not any(name == "merge_pr" for name, _ in transport.calls)


def test_repeated_action_without_slice_target_opens_run_gate(tmp_path: Path) -> None:
    store = _mergeable_review_store(tmp_path)
    transport = RecordingTransport()
    error = ConvergenceInvariantError("repeated_state_version_action", "abc123", target=None)

    result = _park_repeated_action(
        store.load(), error, None, store, TLLoopConfig(active=True), EffectClient(transport), []
    )

    gate_name = f"{REPEATED_ACTION_GATE_PREFIX}abc123"
    assert any(
        gate.name == gate_name and gate.status is GateStatus.PENDING for gate in result.gates
    )
    assert result.slices["leaf-a"].status is SliceStatus.IN_REVIEW
    parked_events = [
        arguments
        for name, arguments in transport.calls
        if name == "emit_controller_event"
        and arguments.get("event_type") == "tl.repeated_action_parked"
    ]
    assert len(parked_events) == 1


def test_repeated_action_run_gate_is_idempotent_and_respects_answer(tmp_path: Path) -> None:
    store = _mergeable_review_store(tmp_path)
    state = store.load()
    decision = ExternalIntent(
        "merge_aggregate", "aggregate-owner", {"pr_number": 7, "head_sha": "head-a"}
    )
    error = ConvergenceInvariantError(
        "repeated_state_version_action", action_key(decision), target="aggregate-owner"
    )
    gate_name = repeated_action_gate_name(decision)
    config = TLLoopConfig(active=True)

    first_transport = RecordingTransport()
    first = _park_repeated_action(
        state, error, decision, store, config, EffectClient(first_transport), []
    )
    assert any(gate.name == gate_name and gate.status is GateStatus.PENDING for gate in first.gates)
    assert (
        len(
            [
                arguments
                for name, arguments in first_transport.calls
                if name == "emit_controller_event"
                and arguments.get("event_type") == "tl.repeated_action_parked"
            ]
        )
        == 1
    )

    # A later cycle must not reset a pending gate or re-record the incident.
    later_transport = RecordingTransport()
    later = _park_repeated_action(
        first, error, decision, store, config, EffectClient(later_transport), []
    )
    assert any(gate.name == gate_name and gate.status is GateStatus.PENDING for gate in later.gates)
    assert not [
        arguments
        for name, arguments in later_transport.calls
        if name == "emit_controller_event"
        and arguments.get("event_type") == "tl.repeated_action_parked"
    ]

    # An operator rejection is preserved, never clobbered back to pending.
    rejected = store.answer_gate(gate_name, GateStatus.REJECTED)
    rejected_transport = RecordingTransport()
    preserved = _park_repeated_action(
        rejected, error, decision, store, config, EffectClient(rejected_transport), []
    )
    assert any(
        gate.name == gate_name and gate.status is GateStatus.REJECTED for gate in preserved.gates
    )

    # An approved retry that repeats re-arms one fresh pending occurrence.
    approved = store.answer_gate(gate_name, GateStatus.APPROVED)
    approved_transport = RecordingTransport()
    rearmed = _park_repeated_action(
        approved, error, decision, store, config, EffectClient(approved_transport), []
    )
    assert any(
        gate.name == gate_name and gate.status is GateStatus.PENDING for gate in rearmed.gates
    )
    assert (
        len(
            [
                arguments
                for name, arguments in approved_transport.calls
                if name == "emit_controller_event"
                and arguments.get("event_type") == "tl.repeated_action_parked"
            ]
        )
        == 1
    )


def test_derivation_holds_action_behind_repeated_action_gate(tmp_path: Path) -> None:
    store = _mergeable_review_store(tmp_path)
    state = store.load()
    decision = derive_next_action(state)
    assert isinstance(decision, ExternalIntent)
    gate_name = repeated_action_gate_name(decision)

    state = store.set_gate(gate_name, GateStatus.PENDING)
    assert derive_next_action(state) == Quiescent("await_repeated_action_gate")

    state = store.answer_gate(gate_name, GateStatus.REJECTED)
    assert derive_next_action(state) == Quiescent("repeated_action_rejected")

    state = store.answer_gate(gate_name, GateStatus.APPROVED)
    assert derive_next_action(state) == decision


def test_gate_answer_advances_the_convergence_epoch(tmp_path: Path) -> None:
    store = _mergeable_review_store(tmp_path)
    tracker = ConvergenceTracker()
    state = store.load()
    first = tracker.reduce(state)
    assert isinstance(first.decision, ExternalIntent)
    with pytest.raises(ConvergenceInvariantError):
        tracker.reduce(state)

    gate_name = repeated_action_gate_name(derive_next_action(state))
    store.set_gate(gate_name, GateStatus.PENDING)
    before_version = store.load().state_version
    approved = store.answer_gate(gate_name, GateStatus.APPROVED)

    assert approved.state_version == before_version + 1
    # The same tracker sees a new epoch, so the released action is not a repeat.
    allowed = tracker.reduce(approved)
    assert allowed.decision == first.decision


def test_approved_run_gate_retries_in_live_controller(tmp_path: Path) -> None:
    store = _mergeable_review_store(tmp_path)
    journal = EffectJournal("review-run", store.run_dir / "action-journal.json")
    tracker = ConvergenceTracker()
    config = TLLoopConfig(active=True)

    # Cycle 1: incomplete compare evidence clears the action without merging,
    # so the tracker now remembers (target, state_version, action).
    _apply_convergence(
        store.load(),
        tracker,
        store,
        config,
        EffectClient(
            IntegrationTransport(
                snapshots=[
                    {
                        "merged": False,
                        "head_sha": "head-a",
                        "base_sha": "base-a",
                        "ci_status": "success",
                    }
                ]
            )
        ),
        journal,
    )
    decision = derive_next_action(store.load())
    assert isinstance(decision, ExternalIntent) and decision.operation == "merge"
    gate_name = repeated_action_gate_name(decision)
    store.set_gate(gate_name, GateStatus.PENDING)

    # Cycle 2: the pending gate holds the action, so the same tracker is never
    # asked to repeat it (no raise, no re-park).
    held = _apply_convergence(
        store.load(), tracker, store, config, EffectClient(RecordingTransport()), journal
    )
    assert derive_next_action(held) == Quiescent("await_repeated_action_gate")

    before_version = held.state_version
    approved = store.answer_gate(gate_name, GateStatus.APPROVED)
    assert approved.state_version == before_version + 1

    # Cycle 3: with the SAME tracker, the released action executes once the
    # merge evidence is complete, instead of being re-detected and re-parked.
    resumed_transport = IntegrationTransport(
        snapshots=[
            {
                "merged": False,
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "merged": True,
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
                "pr_state": "closed",
            },
        ],
        merge_response={"success": True, "result": {"merged": True}},
    )
    resumed = _apply_convergence(
        approved, tracker, store, config, EffectClient(resumed_transport), journal
    )

    assert resumed.slices["leaf-a"].status is SliceStatus.MERGED
    assert any(name == "merge_pr" for name, _ in resumed_transport.calls)
    assert any(
        gate.name == gate_name and gate.status is GateStatus.APPROVED for gate in resumed.gates
    )


def test_duplicate_gate_approval_does_not_grant_a_second_retry(tmp_path: Path) -> None:
    store = _mergeable_review_store(tmp_path)
    tracker = ConvergenceTracker()
    state = store.load()
    first = tracker.reduce(state)
    assert isinstance(first.decision, ExternalIntent)
    with pytest.raises(ConvergenceInvariantError):
        tracker.reduce(state)

    gate_name = repeated_action_gate_name(first.decision)
    store.set_gate(gate_name, GateStatus.PENDING)
    approved = store.answer_gate(gate_name, GateStatus.APPROVED)
    # One approval advances the epoch and releases exactly one retry.
    assert tracker.reduce(approved).decision == first.decision

    duplicate = store.answer_gate(gate_name, GateStatus.APPROVED)
    assert duplicate.state_version == approved.state_version
    # The duplicate same-status answer gives no fresh epoch, so the same
    # tracker still detects the repeat instead of retrying a second time.
    with pytest.raises(ConvergenceInvariantError):
        tracker.reduce(duplicate)


def test_quiescent_convergence_is_not_parked(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    transport = RecordingTransport()

    result = _apply_convergence(
        store.load(),
        ConvergenceTracker(),
        store,
        TLLoopConfig(active=True),
        EffectClient(transport),
        [],
    )

    assert result.slices["leaf-a"].status is SliceStatus.IN_REVIEW
    assert not any(name == "chainlink_issue_create" for name, _ in transport.calls)


def test_valid_exact_head_review_still_merges_without_parking(tmp_path: Path) -> None:
    store = _mergeable_review_store(tmp_path)
    transport = IntegrationTransport(
        spawned_agent_id="aggregate-child",
        spawned_invocation_id="aggregate-child-invocation",
        snapshots=[
            {
                "merged": False,
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "merged": True,
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
                "pr_state": "closed",
            },
        ],
        merge_response={"success": True, "result": {"merged": True}},
    )
    journal = EffectJournal("review-run", store.run_dir / "action-journal.json")

    result = _apply_convergence(
        store.load(),
        ConvergenceTracker(),
        store,
        TLLoopConfig(active=True),
        EffectClient(transport),
        journal,
    )

    assert result.slices["leaf-a"].status is SliceStatus.MERGED
    assert not any(name == "chainlink_issue_create" for name, _ in transport.calls)
    assert not any(
        name == "emit_controller_event"
        and arguments.get("event_type") == "tl.repeated_action_parked"
        for name, arguments in transport.calls
    )


def test_direct_reviewer_verdict_rejects_unregistered_actor(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    current = store.load().slices["leaf-a"]
    store.checkpoint(
        TLPlanning(),
        {
            "leaf-a": replace(
                current,
                reviewer_attempt={"head-a": 1},
                reviewer_agent_id="review-pr-42-codex",
            )
        },
        BudgetLedger(0, 0),
        offset=0,
    )

    _route_review_event(
        WorkPlan.from_mapping(
            {"leaves": [{"name": "leaf-a", "task": "implement", "chainlink_issue_id": None}]}
        ),
        store,
        store.load(),
        TLPlanning(),
        _direct_reviewer_event(verdict="GO", agent_id="leaf-a"),
        1,
        TLLoopConfig(active=True),
        EffectClient(RecordingTransport()),
        [],
    )

    assert store.load().slices["leaf-a"].verdict is None


def test_direct_reviewer_verdict_uses_shared_account_not_event_agent_slug(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    current = store.load().slices["leaf-a"]
    store.checkpoint(
        TLPlanning(),
        {
            "leaf-a": replace(
                current,
                reviewer_attempt={"head-a": 1},
                reviewer_agent_id="review-pr-42-codex",
            )
        },
        BudgetLedger(0, 0),
        offset=0,
    )

    event = replace(
        _direct_reviewer_event(
            verdict="GO",
            agent_id="exomonad-reviewer",
            reviewer_agent_id="review-pr-42-codex",
        ),
        agent_id="exomonad-reviewer",
    )
    _route_review_event(
        WorkPlan.from_mapping(
            {"leaves": [{"name": "leaf-a", "task": "implement", "chainlink_issue_id": None}]}
        ),
        store,
        store.load(),
        TLPlanning(),
        event,
        1,
        TLLoopConfig(active=True),
        EffectClient(RecordingTransport()),
        [],
    )

    assert store.load().slices["leaf-a"].verdict is Verdict.GO


def test_direct_reviewer_verdict_rejects_unverified_shared_account(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    current = store.load().slices["leaf-a"]
    store.checkpoint(
        TLPlanning(),
        {
            "leaf-a": replace(
                current,
                reviewer_attempt={"head-a": 1},
                reviewer_agent_id="review-pr-42-codex",
            )
        },
        BudgetLedger(0, 0),
        offset=0,
    )

    _route_review_event(
        WorkPlan.from_mapping(
            {"leaves": [{"name": "leaf-a", "task": "implement", "chainlink_issue_id": None}]}
        ),
        store,
        store.load(),
        TLPlanning(),
        _direct_reviewer_event(
            verdict="GO",
            agent_id="exomonad-reviewer",
            reviewer_agent_id="review-pr-42-codex",
            reviewer_account_authenticated=False,
            reviewer_identity_unresolved=True,
        ),
        1,
        TLLoopConfig(active=True),
        EffectClient(RecordingTransport()),
        [],
    )

    assert store.load().slices["leaf-a"].verdict is None


def test_convergence_executes_journaled_direct_merge_with_compare_evidence(tmp_path: Path) -> None:
    store = _review_store(tmp_path, verdict=Verdict.GO)
    current = store.load().slices["leaf-a"]
    store.checkpoint(
        TLPlanning(),
        {
            "leaf-a": replace(
                current,
                publication=PublicationBinding(42, "head-a", "main.leaf-a", "main", 1),
                handoff=HandoffEvidence(
                    42,
                    "head-a",
                    1,
                    "inv-1",
                    "leaf-a",
                    "2026-08-12T00:00:00Z",
                ),
                reviewer_attempt={"head-a": 1},
                ci_state={"head-a": "success"},
            )
        },
        BudgetLedger(0, 0),
        offset=0,
    )
    transport = IntegrationTransport(
        snapshots=[
            {
                "merged": False,
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "merged": True,
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
                "pr_state": "closed",
            },
        ],
        merge_response={"success": True, "result": {"merged": True}},
    )
    journal = EffectJournal("review-run", store.run_dir / "action-journal.json")

    result = _apply_convergence(
        store.load(),
        ConvergenceTracker(),
        store,
        TLLoopConfig(active=True),
        EffectClient(transport),
        journal,
    )

    assert result.slices["leaf-a"].status is SliceStatus.MERGED
    merge_arguments = next(args for name, args in transport.calls if name == "merge_pr")
    assert merge_arguments["expected_base_sha"] == "base-a"
    assert merge_arguments["expected_head_sha"] == "head-a"
    assert merge_arguments["expected_patch_digest"] == "patch-a"
    assert merge_arguments["expected_merge_tree_sha"] == "tree-a"
    merge_entries = journal.confirmed_entries("merge_pr", "leaf-a")
    assert len(merge_entries) == 1


def test_slice_recovery_is_projected_into_the_canonical_scope_before_release(
    tmp_path: Path,
) -> None:
    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        status=SliceStatus.MERGED,
        post_merge=PostMergeState(
            PostMergePhase.REMOTE_MERGE_ADOPTED,
            {
                "child_id": "leaf-a",
                "repository": "repo",
                "parent_branch": "main",
                "pr_number": "42",
                "head_sha": "head-a",
                "merge_journal_id": "merge-journal",
                "lane_epoch": "1",
            },
        ),
    )
    canonical = RecursiveTLRunning(
        current_order=0,
        pending_by_order={},
        parallel_pending=(ChildRecord("leaf-a", ChildKind.LEAF),),
        post_merge={"leaf-a": PostMergeState()},
    )
    state = replace(store.load(), recursive_fsm=canonical)

    projected = _recursive_phase_after_slice_update(state, "leaf-a", current)

    assert isinstance(projected, RecursiveTLRunning)
    assert projected.post_merge["leaf-a"].phase is PostMergePhase.REMOTE_MERGE_ADOPTED
    assert tuple(record.child_id for record in projected.parallel_pending) == ("leaf-a",)


def test_legacy_release_waits_for_each_slice_post_merge_completion(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    current = replace(store.load().slices["leaf-a"], status=SliceStatus.MERGED)
    sibling = replace(current, id="leaf-b", status=SliceStatus.MERGED)
    state = replace(
        store.load(),
        fsm=FSMState(TLPhase.TLWaiting, ("leaf-a", "leaf-b")),
        slices={"leaf-a": current, "leaf-b": sibling},
    )

    next_phase = _phase_after_slice_merge(state, "leaf-a", current)

    assert isinstance(next_phase, TLWaiting)


def test_unknown_direct_merge_reconciles_authoritative_merged_snapshot(tmp_path: Path) -> None:
    store = _review_store(tmp_path, verdict=Verdict.GO)
    current = store.load().slices["leaf-a"]
    store.checkpoint(
        TLPlanning(),
        {
            "leaf-a": replace(
                current,
                publication=PublicationBinding(42, "head-a", "main.leaf-a", "main", 1),
                handoff=HandoffEvidence(
                    42,
                    "head-a",
                    1,
                    "inv-1",
                    "leaf-a",
                    "2026-08-12T00:00:00Z",
                ),
                reviewer_attempt={"head-a": 1},
                ci_state={"head-a": "success"},
            )
        },
        BudgetLedger(0, 0),
        offset=0,
    )

    class UnknownMergeTransport(IntegrationTransport):
        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            if tool_name == "merge_pr":
                raise RuntimeError("transport lost after merge dispatch")
            return super().call_tool(role, name, tool_name, arguments)

    transport = UnknownMergeTransport(
        snapshots=[
            {
                "merged": False,
                "head_sha": "head-a",
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {"merged": True, "head_sha": "head-a", "pr_state": "closed"},
        ]
    )
    journal = EffectJournal("review-run", store.run_dir / "action-journal.json")

    result = _apply_convergence(
        store.load(),
        ConvergenceTracker(),
        store,
        TLLoopConfig(active=True),
        EffectClient(transport),
        journal,
    )

    assert result.slices["leaf-a"].status is SliceStatus.MERGED
    assert len(journal.confirmed_entries("merge_pr", "leaf-a")) == 1


def test_publication_derives_handoff_without_completion_event(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id="inv-1",
    )
    event = project(
        {
            "type": "pr.filed",
            "run_seq": 1,
            "run_id": "review-run",
            "agent_id": "leaf-a",
            "invocation_id": "inv-1",
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-a",
                "head_branch": "main.leaf-a",
                "base_branch": "main",
            },
        }
    )
    reduced = _update_slices({"leaf-a": current}, PRFiled(42, "head-a", "leaf-a"))
    bound = _bind_publication_evidence(reduced, PRFiled(42, "head-a", "leaf-a"), event, "leaf-a")

    slice_state = bound["leaf-a"]
    assert slice_state.publication is not None
    assert slice_state.handoff is not None
    assert slice_state.handoff.head_sha == "head-a"
    assert _update_slices(bound, ChildCompleted("leaf-a")) == bound


def test_reconciliation_backfills_handoff_from_host_publication_provenance(
    tmp_path: Path,
) -> None:
    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id="inv-host",
        publication=None,
        handoff=None,
        verdict=Verdict.GO,
        reviewer_attempt={"head-a": 1},
    )
    result = ReconciliationResult(
        slice_id="leaf-a",
        confirmed_stage="review",
        authoritative_evidence=("published_pr",),
        missing_evidence=("handoff",),
        conflicts=(),
        next_action="await_handoff",
    )
    watcher = {
        "found": True,
        "pr_number": 42,
        "head_sha": "head-a",
        "head_branch": "main.leaf-a",
        "base_branch": "main",
        "review_state": "approved",
        "ci_status": "success",
        "publication_ownership_verified": True,
        "publication_ownership_error": "",
        "head_reachable": True,
        "publication": {
            "invocation_id": "inv-host",
            "slice_id": "leaf-a",
            "author_agent": "leaf-a",
            "succession_invocation_ids": [],
        },
    }

    updated = _apply_reconciliation_observations(current, result, watcher, None)

    assert updated.publication is not None
    assert updated.publication.invocation_id == "inv-host"
    assert updated.handoff is not None
    assert updated.handoff.invocation_id == "inv-host"
    assert updated.handoff.agent_id == "leaf-a"
    assert updated.status is SliceStatus.IN_REVIEW
    merge_ready = reconcile_merge_observation(updated, watcher)
    assert merge_ready.reconciliation is not None
    assert merge_ready.reconciliation["next_action"] == "queue_merge"


def test_reconciliation_without_active_invocation_refuses_host_publication(
    tmp_path: Path,
) -> None:
    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id=None,
        publication=None,
        handoff=None,
        verdict=Verdict.GO,
        reviewer_attempt={"head-a": 1},
    )
    result = ReconciliationResult(
        slice_id="leaf-a",
        confirmed_stage="review",
        authoritative_evidence=("published_pr",),
        missing_evidence=("handoff",),
        conflicts=(),
        next_action="await_handoff",
    )
    watcher = {
        "found": True,
        "pr_number": 42,
        "head_sha": "head-a",
        "head_branch": "main.leaf-a",
        "base_branch": "main",
        "review_state": "approved",
        "ci_status": "success",
        "publication_ownership_verified": True,
        "publication_ownership_error": "",
        "publication": {
            "invocation_id": "inv-host",
            "slice_id": "leaf-a",
            "author_agent": "leaf-a",
            "succession_invocation_ids": [],
        },
    }

    updated = _apply_reconciliation_observations(current, result, watcher, None)

    # Without an active invocation or a durable binding, the host record is a
    # historical record that cannot bind on its own.
    assert updated.publication is None
    assert updated.handoff is None
    assert updated.status is SliceStatus.IN_REVIEW


def test_reconciliation_malformed_ownership_does_not_bind_or_backfill(
    tmp_path: Path,
) -> None:
    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id="inv-host",
        publication=None,
        handoff=None,
        verdict=Verdict.GO,
        reviewer_attempt={"head-a": 1},
    )
    result = ReconciliationResult(
        slice_id="leaf-a",
        confirmed_stage="review",
        authoritative_evidence=("published_pr",),
        missing_evidence=("handoff",),
        conflicts=(),
        next_action="await_handoff",
    )
    watcher = {
        "found": True,
        "pr_number": 42,
        "head_sha": "head-a",
        "head_branch": "main.leaf-a",
        "base_branch": "main",
        "review_state": "approved",
        "ci_status": "success",
        # verified=True with the ownership error field omitted: the complete
        # ownership contract is not satisfied.
        "publication_ownership_verified": True,
        "publication": {
            "invocation_id": "inv-host",
            "slice_id": "leaf-a",
            "author_agent": "leaf-a",
            "succession_invocation_ids": [],
        },
    }

    updated = _apply_reconciliation_observations(current, result, watcher, None)

    # Malformed ownership metadata can never bind or backfill evidence.
    assert updated.publication is None
    assert updated.handoff is None
    assert updated.status is SliceStatus.IN_REVIEW


def test_reconciliation_refused_watcher_does_not_backfill_handoff(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        status=SliceStatus.SPAWNED,
        pr_number=45,
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id="inv-current",
        publication=None,
        handoff=None,
    )
    result = ReconciliationResult(
        slice_id="leaf-a",
        confirmed_stage="review",
        authoritative_evidence=("published_pr",),
        missing_evidence=("handoff",),
        conflicts=(),
        next_action="await_handoff",
    )
    watcher = {
        "found": True,
        "pr_number": 45,
        "head_sha": "head-45",
        "head_branch": "main.leaf-a",
        "base_branch": "main",
        "review_state": "approved",
        "ci_status": "success",
        "publication_ownership_verified": True,
        "publication_ownership_error": "",
        # The record invocation is unrelated to the current dispatch, so the
        # watcher binding is refused.
        "publication": {
            "invocation_id": "inv-other",
            "slice_id": "leaf-a",
            "author_agent": "leaf-a",
            "succession_invocation_ids": [],
        },
    }

    updated = _apply_reconciliation_observations(current, result, watcher, None)

    # A refused binding must not relabel the current dispatch as handoff
    # evidence for a stale publication.
    assert updated.publication is None
    assert updated.handoff is None


def test_reconciliation_refused_watcher_does_not_apply_merge_or_head(
    tmp_path: Path,
) -> None:
    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        status=SliceStatus.SPAWNED,
        pr_number=None,
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id="inv-current",
        publication=None,
        handoff=None,
        reviewed_head=None,
        ci_state={},
    )
    result = ReconciliationResult(
        slice_id="leaf-a",
        confirmed_stage="review",
        authoritative_evidence=(),
        missing_evidence=("published_pr",),
        conflicts=(),
        next_action="await_authoritative_evidence",
    )
    watcher = {
        "found": True,
        "pr_number": 45,
        "head_sha": "head-45",
        "head_branch": "main.leaf-a",
        "base_branch": "main",
        "review_state": "approved",
        "ci_status": "success",
        "merged": True,
        "publication_ownership_verified": True,
        "publication_ownership_error": "",
        # Unrelated to the current dispatch, so the watcher binding is refused.
        "publication": {
            "invocation_id": "inv-other",
            "slice_id": "leaf-a",
            "author_agent": "leaf-a",
            "succession_invocation_ids": [],
        },
    }

    updated = _apply_reconciliation_observations(current, result, watcher, None)

    # A refused provenance mismatch must not set the PR number, head/CI
    # evidence, or merge the slice.
    assert updated.status is SliceStatus.SPAWNED
    assert updated.pr_number is None
    assert updated.reviewed_head is None
    assert updated.ci_state == {}
    assert updated.publication is None
    assert updated.handoff is None


def test_pr_filed_binds_host_verified_publication_to_owner(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id="inv-1",
    )
    event = project(
        {
            "type": "pr.filed",
            "run_seq": 1,
            "run_id": "review-run",
            "agent_id": "leaf-a",
            "invocation_id": "inv-1",
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-a",
                "head_branch": "main.leaf-a",
                "base_branch": "main",
            },
        }
    )
    reduced = _update_slices({"leaf-a": current}, PRFiled(42, "head-a", "leaf-a"))

    bound = _bind_publication_evidence(reduced, PRFiled(42, "head-a", "leaf-a"), event, "leaf-a")

    publication = bound["leaf-a"].publication
    assert publication is not None
    assert publication.pr_number == 42
    assert publication.head_branch == "main.leaf-a"
    assert publication.base_branch == "main"
    assert publication.invocation_id == "inv-1"


def _pr_filed_event(
    envelope_invocation_id: str | None,
    pr_number: int,
    head_sha: str,
    run_seq: int,
) -> EventEnvelope:
    document: dict[str, object] = {
        "type": "pr.filed",
        "run_seq": run_seq,
        "run_id": "review-run",
        "agent_id": "leaf-a",
        "lifecycle_state": "observed",
        "observed_at": "2026-09-22T00:00:00Z",
        "data": {
            "slice_id": "leaf-a",
            "pr_number": pr_number,
            "head_sha": head_sha,
            "head_branch": "main.leaf-a",
            "base_branch": "main",
        },
    }
    if envelope_invocation_id is not None:
        document["invocation_id"] = envelope_invocation_id
    return project(document)


def test_pr_filed_refuses_historical_publication_across_recreate(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id="inv-current",
    )
    reduced = _update_slices({"leaf-a": current}, PRFiled(44, "head-44", "leaf-a"))
    quarantined: list[str] = []

    # Historical #44 at ledger seq 39829 from the predecessor run replays into
    # the recreated run and must stay audit-only.
    historical = _pr_filed_event("inv-historical", 44, "head-44", 39829)
    bound = _bind_publication_evidence(
        reduced,
        PRFiled(44, "head-44", "leaf-a"),
        historical,
        "leaf-a",
        controller_epoch="epoch-current",
        quarantine=quarantined.append,
    )

    assert bound["leaf-a"].publication is None
    assert bound["leaf-a"].handoff is None
    assert quarantined == [
        (
            "publication invocation does not match the current dispatch "
            "invocation or a recorded recovery succession"
        )
    ]

    # Current #45 at ledger seq 42329 binds only with matching invocation and
    # exact publication identity.
    current_pr = _pr_filed_event("inv-current", 45, "head-45", 42329)
    bound = _bind_publication_evidence(
        reduced,
        PRFiled(45, "head-45", "leaf-a"),
        current_pr,
        "leaf-a",
        controller_epoch="epoch-current",
    )

    publication = bound["leaf-a"].publication
    assert publication is not None
    assert publication.pr_number == 45
    assert publication.head_sha == "head-45"
    assert publication.invocation_id == "inv-current"


def test_pr_filed_without_invocation_identity_is_quarantined(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id="inv-current",
    )
    reduced = _update_slices({"leaf-a": current}, PRFiled(44, "head-44", "leaf-a"))
    quarantined: list[str] = []
    legacy = _pr_filed_event(None, 44, "head-44", 39829)

    bound = _bind_publication_evidence(
        reduced,
        PRFiled(44, "head-44", "leaf-a"),
        legacy,
        "leaf-a",
        controller_epoch="epoch-current",
        quarantine=quarantined.append,
    )

    assert bound["leaf-a"].publication is None
    assert quarantined == ["publication event carries no invocation identity"]


def test_pr_filed_refuses_prior_controller_epoch(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id="inv-current",
    )
    reduced = _update_slices({"leaf-a": current}, PRFiled(45, "head-45", "leaf-a"))
    document = {
        "type": "pr.filed",
        "run_seq": 42329,
        "run_id": "review-run",
        "agent_id": "leaf-a",
        "invocation_id": "inv-current",
        "lifecycle_state": "observed",
        "observed_at": "2026-09-22T00:00:00Z",
        "data": {
            "slice_id": "leaf-a",
            "controller_epoch": "epoch-previous",
            "pr_number": 45,
            "head_sha": "head-45",
            "head_branch": "main.leaf-a",
            "base_branch": "main",
        },
    }

    bound = _bind_publication_evidence(
        reduced,
        PRFiled(45, "head-45", "leaf-a"),
        project(document),
        "leaf-a",
        controller_epoch="epoch-current",
    )

    assert bound["leaf-a"].publication is None


def test_historical_publication_rejected_before_reduction_across_recreate(
    tmp_path: Path,
) -> None:
    run_id = "recreate-publication-run"
    agent_id = "tunable-operator-body-opencode"
    leaf = "tunable-operator-body"

    def pr_filed(seq: int, invocation: str, number: int, head: str) -> EventEnvelope:
        return project(
            {
                "type": "pr.filed",
                "run_seq": seq,
                "run_id": run_id,
                "agent_id": agent_id,
                "invocation_id": invocation,
                "lifecycle_state": "observed",
                "observed_at": "2026-09-22T00:00:00Z",
                "data": {
                    "slice_id": leaf,
                    "pr_number": number,
                    "head_sha": head,
                    "head_branch": f"main.{agent_id}",
                    "base_branch": "main",
                },
            }
        )

    source = SyntheticQueue(
        [
            pr_filed(1, "inv-current", 45, "head-45"),
            # Establish a real active recovery before the historical replay.
            _blocked_event(2, leaf, run_id, invocation_id="inv-current"),
            # Historical #44 from the predecessor run replays after #45 and the
            # active recovery; it must disturb neither.
            pr_filed(3, "inv-historical", 44, "head-44"),
            _event(4, "all_children_done", run_id=run_id),
        ]
    )
    transport = RecordingTransport(
        spawned_agent_id=agent_id,
        spawned_invocation_id="inv-current",
    )
    plan = WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": leaf,
                    "task": "implement the change",
                    "chainlink_issue_id": None,
                    "boundary": ["src/leaf.py"],
                    "verify": ["just tl-loop-test"],
                    "done_criteria": ["covered"],
                }
            ]
        }
    )

    result = run_tl_loop(
        run_id,
        plan,
        source,
        EffectClient(transport),
        config=TLLoopConfig(
            max_workers=0,
            max_leaves=1,
            max_events=4,
            poll_interval=0.001,
        ),
        root_dir=tmp_path,
    )

    # The rejected historical event must not have reached the reducer: #45's
    # publication, head, handoff, PR number, and the seeded active recovery all
    # survive.
    final = result.final_state.slices[leaf]
    assert final.publication is not None
    assert final.publication.pr_number == 45
    assert final.publication.head_sha == "head-45"
    assert final.pr_number == 45
    assert final.recovery is not None
    assert final.recovery.cause == ParkCause.BASE_CI_UNSTABLE.value
    assert final.handoff is not None and final.handoff.head_sha == "head-45"

    # The refusal is permanent audit evidence, never pending replay work.
    store = RunStore(run_id, tmp_path)
    assert store.quarantined_events() == ()
    assert any(entry.get("run_seq") == 3 for entry in store.audited_events())
    # A restart reloads the audit row but never replays or releases it.
    reloaded = RunStore(run_id, tmp_path)
    assert reloaded.quarantined_events() == ()
    assert any(entry.get("run_seq") == 3 for entry in reloaded.audited_events())


def test_conflicting_publication_refused_before_reduction_preserves_owner(
    tmp_path: Path,
) -> None:
    """A refused same-invocation conflict must never reach the slice reducer.

    The reducer clears publication, handoff, and recovery state and rewrites the
    PR number and head, so a conflicting PR/head, a wrong owner, or a wrong
    branch that is refused by the binder must be stopped before reduction.
    """
    run_id = "conflicting-publication-run"
    agent_id = "tunable-operator-body-opencode"
    leaf = "tunable-operator-body"

    def pr_filed(
        seq: int,
        invocation: str,
        number: int,
        head: str,
        *,
        owner: str = agent_id,
        branch: str | None = None,
    ) -> EventEnvelope:
        return project(
            {
                "type": "pr.filed",
                "run_seq": seq,
                "run_id": run_id,
                "agent_id": owner,
                "invocation_id": invocation,
                "lifecycle_state": "observed",
                "observed_at": "2026-09-22T00:00:00Z",
                "data": {
                    "slice_id": leaf,
                    "pr_number": number,
                    "head_sha": head,
                    "head_branch": branch or f"main.{agent_id}",
                    "base_branch": "main",
                },
            }
        )

    plan = WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": leaf,
                    "task": "implement the change",
                    "chainlink_issue_id": None,
                    "boundary": ["src/leaf.py"],
                    "verify": ["just tl-loop-test"],
                    "done_criteria": ["covered"],
                }
            ]
        }
    )

    def run(events: list[EventEnvelope], root: Path) -> TLRunResult:
        transport = RecordingTransport(
            spawned_agent_id=agent_id,
            spawned_invocation_id="inv-current",
        )
        return run_tl_loop(
            run_id,
            plan,
            SyntheticQueue(events),
            EffectClient(transport),
            config=TLLoopConfig(
                max_workers=0,
                max_leaves=1,
                max_events=len(events),
                poll_interval=0.001,
            ),
            root_dir=root,
        )

    # Baseline: #45 is accepted, then a real active recovery is established.
    baseline = run(
        [
            pr_filed(1, "inv-current", 45, "head-45"),
            _blocked_event(2, leaf, run_id, invocation_id="inv-current"),
            _event(3, "all_children_done", run_id=run_id),
        ],
        tmp_path / "baseline",
    )

    # Same accepted #45 and recovery, then three refused publications that must
    # not reduce: a same-invocation conflicting PR/head, a wrong owner, and a
    # wrong head branch.
    conflicting = run(
        [
            pr_filed(1, "inv-current", 45, "head-45"),
            _blocked_event(2, leaf, run_id, invocation_id="inv-current"),
            pr_filed(3, "inv-current", 46, "head-46"),
            pr_filed(4, "inv-current", 47, "head-47", owner="someone-else"),
            pr_filed(5, "inv-current", 48, "head-48", branch="main.other"),
            _event(6, "all_children_done", run_id=run_id),
        ],
        tmp_path / "conflicting",
    )

    baseline_slice = baseline.final_state.slices[leaf]
    final = conflicting.final_state.slices[leaf]

    # The accepted owner state and the phase are unchanged by the refusals.
    assert conflicting.final_state.fsm.phase == baseline.final_state.fsm.phase
    assert final.publication == baseline_slice.publication
    assert final.publication is not None
    assert final.publication.pr_number == 45
    assert final.publication.head_sha == "head-45"
    assert final.pr_number == baseline_slice.pr_number == 45
    assert final.reviewed_head == baseline_slice.reviewed_head == "head-45"
    assert final.handoff == baseline_slice.handoff
    assert final.handoff is not None and final.handoff.head_sha == "head-45"
    assert final.recovery is not None
    assert final.recovery.cause == baseline_slice.recovery.cause
    assert final.recovery.phase == baseline_slice.recovery.phase
    assert final.status == baseline_slice.status

    # Ordinary refusals are acknowledged, not retained as replayable work.
    store = RunStore(run_id, tmp_path / "conflicting")
    assert store.quarantined_events() == ()
    assert store.audited_events() == ()


def _watcher_publication(
    invocation_id: str | None, succession: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "found": True,
        "pr_number": 45,
        "head_sha": "head-45",
        "base_sha": "base-45",
        "patch_digest": "patch-45",
        "head_branch": "main.leaf-a",
        "base_branch": "main",
        "head_reachable": True,
        "publication_ownership_verified": True,
        "publication_ownership_error": "",
        "publication": {
            "invocation_id": invocation_id,
            "slice_id": "leaf-a",
            "author_agent": "leaf-a",
            "succession_invocation_ids": list(succession),
        },
    }


def _watcher_current(root: Path, invocation_id: str | None) -> SliceState:
    store = _review_store(root)
    return replace(
        store.load().slices["leaf-a"],
        pr_number=45,
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id=invocation_id,
    )


def test_aggregate_watcher_accepts_resumed_invocation_in_durable_lineage(
    tmp_path: Path,
) -> None:
    current = _watcher_current(tmp_path, "inv-root")
    watcher = _watcher_publication("inv-root", ("inv-resumed",))
    observation = WatcherObservation.from_response(watcher)

    assert _aggregate_watcher_identity_matches(
        SubTLTask("leaf-a", WorkPlan()),
        current,
        observation,
        "main.leaf-a",
        "leaf-a",
        "inv-resumed",
        "main",
    )


def test_publication_from_watcher_refuses_invocation_mismatch(tmp_path: Path) -> None:
    current = _watcher_current(tmp_path, "inv-current")
    watcher = _watcher_publication("inv-host")
    watcher["review_body"] = "review content must not enter refusal diagnostics"
    validation = validate_publication_from_watcher(current, watcher, "head-45", "leaf-a")
    assert validation.binding is None
    assert validation.rejection is not None
    assert validation.rejection.reason_code == "invocation_lineage_mismatch"
    assert dict(validation.rejection.expected)["invocation_ids"] == ("inv-current",)
    assert dict(validation.rejection.observed)["invocation_id"] == "inv-host"
    assert dict(validation.rejection.observed)["succession_invocation_ids"] == ()
    assert "review content" not in repr(validation.rejection)

    assert _publication_from_watcher(current, watcher, "head-45", "leaf-a") is None
    publication = _publication_from_watcher(
        current, _watcher_publication("inv-current"), "head-45", "leaf-a"
    )
    assert publication is not None
    assert publication.invocation_id == "inv-current"


def test_publication_from_watcher_reports_wrong_pr_number(tmp_path: Path) -> None:
    current = _watcher_current(tmp_path, "inv-current")
    watcher = _watcher_publication("inv-current")
    watcher["pr_number"] = 46

    validation = validate_publication_from_watcher(current, watcher, "head-45", "leaf-a")

    assert validation.binding is None
    assert validation.rejection is not None
    assert validation.rejection.reason_code == "pr_number_mismatch"
    assert dict(validation.rejection.expected)["pr_number"] == 45
    assert dict(validation.rejection.observed)["pr_number"] == 46


def test_publication_from_watcher_refuses_stale_head_evidence(tmp_path: Path) -> None:
    current = _watcher_current(tmp_path, "inv-current")
    watcher = _watcher_publication("inv-current")
    watcher["head_sha"] = "head-stale"

    validation = validate_publication_from_watcher(current, watcher, "head-45", "leaf-a")

    assert validation.binding is None
    assert validation.rejection is not None
    assert validation.rejection.reason_code == "head_sha_missing_or_mismatched"
    assert dict(validation.rejection.expected)["head_sha"] == "head-45"
    assert dict(validation.rejection.observed)["head_sha"] == "head-stale"


def test_publication_from_watcher_reports_evidence_failures_without_error_text(
    tmp_path: Path,
) -> None:
    current = _watcher_current(tmp_path, "inv-current")
    watcher = _watcher_publication("inv-current")
    watcher["head_reachable"] = False
    watcher["evidence_error"] = "forge access token must never enter diagnostics"

    validation = validate_publication_from_watcher(current, watcher, "head-45", "leaf-a")

    assert validation.binding is None
    assert validation.rejection is not None
    assert validation.rejection.reason_code == "head_unreachable"
    assert dict(validation.rejection.observed)["evidence_error_present"] is True
    assert "forge access token" not in repr(validation.rejection)


def test_publication_from_watcher_refuses_missing_invocation_provenance(
    tmp_path: Path,
) -> None:
    current = _watcher_current(tmp_path, "inv-current")
    assert (
        _publication_from_watcher(current, _watcher_publication(None), "head-45", "leaf-a") is None
    )
    assert _publication_from_watcher(current, _watcher_publication(""), "head-45", "leaf-a") is None


def test_publication_from_watcher_refuses_unrelated_succession(tmp_path: Path) -> None:
    current = _watcher_current(tmp_path, "inv-current")
    watcher = _watcher_publication("inv-origin", ("inv-other", "inv-third"))
    assert _publication_from_watcher(current, watcher, "head-45", "leaf-a") is None


def test_publication_from_watcher_refuses_without_active_invocation(tmp_path: Path) -> None:
    current = _watcher_current(tmp_path, None)
    assert current.dispatch_invocation_id is None
    assert current.publication is None
    # No active dispatch/recovery invocation and no durable publication
    # binding: a historical record may not bind by itself.
    assert (
        _publication_from_watcher(
            current, _watcher_publication("inv-historical"), "head-45", "leaf-a"
        )
        is None
    )


def test_publication_from_watcher_binds_without_active_invocation_when_durable(
    tmp_path: Path,
) -> None:
    current = _watcher_current(tmp_path, None)
    durable = replace(
        current,
        publication=PublicationBinding(
            pr_number=45,
            head_sha="head-45",
            head_branch="main.leaf-a",
            base_branch="main",
            attempt=1,
            invocation_id="inv-historical",
        ),
    )
    publication = _publication_from_watcher(
        durable, _watcher_publication("inv-historical"), "head-45", "leaf-a"
    )
    assert publication is not None
    assert publication.invocation_id == "inv-historical"
    # A durable binding proves only its own invocation.
    assert (
        _publication_from_watcher(durable, _watcher_publication("inv-other"), "head-45", "leaf-a")
        is None
    )


def test_malformed_publication_is_refused_not_absent(tmp_path: Path) -> None:
    current = _watcher_current(tmp_path, "inv-current")
    watcher = {
        "found": True,
        "pr_number": 45,
        "head_sha": "head-45",
        "head_branch": "main.leaf-a",
        "base_branch": "main",
        "publication_ownership_verified": True,
        "publication_ownership_error": "",
        # Present but not a publication object: this must be refused, never
        # treated as a genuinely absent record eligible for the fallback.
        "publication": "not-a-publication-record",
    }
    assert watcher_presents_publication(watcher) is True
    assert publication_refused(current, watcher, "head-45", "leaf-a") is True
    assert _publication_from_watcher(current, watcher, "head-45", "leaf-a") is None


def test_absent_publication_record_retains_compatibility(tmp_path: Path) -> None:
    current = _watcher_current(tmp_path, "inv-current")
    watcher = {
        "found": True,
        "pr_number": 45,
        "head_sha": "head-45",
        "head_branch": "main.leaf-a",
        "base_branch": "main",
        "merged": False,
    }
    # A genuinely absent record is not a refusal and not malformed/unresolved;
    # the established durable-merge compatibility behavior still applies.
    assert watcher_presents_publication(watcher) is False
    assert publication_refused(current, watcher, "head-45", "leaf-a") is False
    assert publication_ownership_malformed(watcher) is False
    assert publication_ownership_unresolved(watcher) is False


def test_publication_from_watcher_refuses_branch_mismatch(tmp_path: Path) -> None:
    current = _watcher_current(tmp_path, "inv-current")
    watcher = _watcher_publication("inv-current")
    watcher["head_branch"] = "main.other"
    assert _publication_from_watcher(current, watcher, "head-45", "leaf-a") is None


def test_publication_from_watcher_refuses_base_branch_mismatch(tmp_path: Path) -> None:
    current = _watcher_current(tmp_path, "inv-current")
    watcher = _watcher_publication("inv-current")
    watcher["base_branch"] = "develop"
    assert _publication_from_watcher(current, watcher, "head-45", "leaf-a") is None


def test_publication_from_watcher_refuses_unrelated_owner(tmp_path: Path) -> None:
    current = _watcher_current(tmp_path, "inv-current")
    watcher = _watcher_publication("inv-current")
    publication = cast(dict[str, object], watcher["publication"])
    publication["author_agent"] = "someone-else"
    assert _publication_from_watcher(current, watcher, "head-45", "leaf-a") is None


def test_publication_from_watcher_refuses_missing_author(tmp_path: Path) -> None:
    current = _watcher_current(tmp_path, "inv-current")
    watcher = _watcher_publication("inv-current")
    publication = cast(dict[str, object], watcher["publication"])
    publication.pop("author_agent")
    assert _publication_from_watcher(current, watcher, "head-45", "leaf-a") is None


def test_publication_from_watcher_refuses_when_no_expected_owner(tmp_path: Path) -> None:
    current = replace(_watcher_current(tmp_path, "inv-current"), dispatch_agent_id=None)
    assert (
        _publication_from_watcher(current, _watcher_publication("inv-current"), "head-45", None)
        is None
    )


def test_publication_from_watcher_binds_succession_in_origin_direction(
    tmp_path: Path,
) -> None:
    # One hop: current invocation is reachable from the recorded origin.
    one_hop = _watcher_current(tmp_path / "one-hop", "inv-hop1")
    publication = _publication_from_watcher(
        one_hop,
        _watcher_publication("inv-origin", ("inv-hop1",)),
        "head-45",
        "leaf-a",
    )
    assert publication is not None
    assert publication.invocation_id == "inv-origin"

    # A root RunStore may still name the original dispatch after a resume; the
    # durable owner record retains that origin and lists the fresh invocation.
    resumed = _watcher_current(tmp_path / "resume", "75d73f864bd893c3c9d45ab712237499")
    publication = _publication_from_watcher(
        resumed,
        _watcher_publication(
            "75d73f864bd893c3c9d45ab712237499",
            ("10948a5a-e48a-4df2-851c-c25872ff8cde",),
        ),
        "head-45",
        "leaf-a",
    )
    assert publication is not None
    assert publication.invocation_id == "75d73f864bd893c3c9d45ab712237499"

    # Multi hop: a later invocation is still reachable from the origin.
    multi_hop = _watcher_current(tmp_path / "multi-hop", "inv-hop2")
    publication = _publication_from_watcher(
        multi_hop,
        _watcher_publication("inv-origin", ("inv-hop1", "inv-hop2")),
        "head-45",
        "leaf-a",
    )
    assert publication is not None
    assert publication.invocation_id == "inv-origin"

    # The origin itself remains valid.
    origin = _watcher_current(tmp_path / "origin", "inv-origin")
    publication = _publication_from_watcher(
        origin, _watcher_publication("inv-origin"), "head-45", "leaf-a"
    )
    assert publication is not None
    assert publication.invocation_id == "inv-origin"


def test_publication_from_watcher_binds_active_recovery_invocation(
    tmp_path: Path,
) -> None:
    store = _review_store(tmp_path)
    current = replace(
        store.load().slices["leaf-a"],
        pr_number=45,
        dispatch_agent_id="leaf-a",
        dispatch_invocation_id="inv-stale",
        recovery=RecoveryState(
            cause="stall_detected",
            phase=RecoveryPhase.WAITING_SIGNAL,
            recovery_round=1,
            next_action="wait_for_signal",
            owner_run_id="review-run",
            entered_at=0.0,
            slice_attempt=1,
            owner_agent_id="leaf-a",
            evidence={"invocation_id": "inv-recovery"},
        ),
    )
    publication = _publication_from_watcher(
        current,
        _watcher_publication("inv-origin", ("inv-recovery",)),
        "head-45",
        "leaf-a",
    )
    assert publication is not None
    assert publication.invocation_id == "inv-origin"


def test_audit_marked_pending_row_migrates_to_permanent_audit(tmp_path: Path) -> None:
    run_id = "audit-migration-run"
    store = RunStore(run_id, tmp_path)
    store.quarantine_event(
        {
            "type": "pr.filed",
            "run_seq": 39829,
            "run_id": run_id,
            "agent_id": "leaf-a",
            "invocation_id": "inv-historical",
            "lifecycle_state": "observed",
            "observed_at": "2026-09-22T00:00:00Z",
            "correlation": "publication_history_audit",
            "correlation_reason": "invocation mismatch",
            "data": {"slice_id": "leaf-a", "pr_number": 44, "head_sha": "head-44"},
        }
    )
    # A non-audit pending row must stay replayable.
    store.quarantine_event(
        {
            "type": "pr.filed",
            "run_seq": 7,
            "run_id": run_id,
            "agent_id": "leaf-a",
            "lifecycle_state": "observed",
            "observed_at": "2026-09-22T00:00:00Z",
            "data": {"slice_id": "leaf-a", "pr_number": 45, "head_sha": "head-45"},
        }
    )
    assert len(store.quarantined_events()) == 2

    _migrate_audit_marked_quarantine(store)

    pending = store.quarantined_events()
    assert [entry.get("run_seq") for entry in pending] == [7]
    assert any(entry.get("run_seq") == 39829 for entry in store.audited_events())
    # The audit row survives a reload and never returns to the pending queue.
    reloaded = RunStore(run_id, tmp_path)
    assert [entry.get("run_seq") for entry in reloaded.quarantined_events()] == [7]
    assert any(entry.get("run_seq") == 39829 for entry in reloaded.audited_events())


def test_append_audit_event_rejects_conflicting_run_seq(tmp_path: Path) -> None:
    store = RunStore("audit-conflict-run", tmp_path)
    row = {"type": "pr.filed", "run_seq": 5, "head_sha": "head-a"}
    store.append_audit_event(row)
    # Re-appending an identical row is idempotent.
    store.append_audit_event(row)
    assert len(store.audited_events()) == 1
    # A different row reusing the same run_seq is rejected.
    with pytest.raises(QuarantineStorageError):
        store.append_audit_event({"type": "pr.filed", "run_seq": 5, "head_sha": "head-b"})
    assert len(store.audited_events()) == 1


def test_task_blocked_disposition_sidecar_is_immutable_and_idempotent(tmp_path: Path) -> None:
    store = RunStore("blocker-disposition-ledger", tmp_path)
    disposition = {
        "disposition_id": "agent.task_blocked:7:event-7",
        "decision": "superseded_by_durable_completion",
        "run_seq": 7,
        "event_id": "event-7",
        "event_type": "agent.task_blocked",
        "reporter_id": "architecture",
        "reporter_scope_path": ["blocker-disposition-ledger", "architecture"],
        "subject_slice_id": "leaf-a",
        "owner_path": ["blocker-disposition-ledger", "architecture"],
        "attempt": 2,
        "proof": ["attempt=2", "merge_journal_id=merge-journal"],
        "evidence_digest": "e" * 64,
    }

    assert store.record_task_blocked_disposition(disposition)
    assert not store.record_task_blocked_disposition(disposition)
    assert len(store.task_blocked_dispositions()) == 1
    with pytest.raises(QuarantineStorageError, match="conflicting disposition"):
        store.record_task_blocked_disposition({**disposition, "evidence_digest": "f" * 64})
    assert len(store.task_blocked_dispositions()) == 1


def test_audit_migration_retains_conflicting_pending_row(tmp_path: Path) -> None:
    run_id = "audit-conflict-migration"
    store = RunStore(run_id, tmp_path)
    # Permanent audit already holds a row for run_seq 5.
    store.append_audit_event(
        {
            "type": "pr.filed",
            "run_seq": 5,
            "head_sha": "head-a",
            "correlation": "publication_history_audit",
        }
    )
    # A conflicting audit-marked pending row reuses the same run_seq.
    store.quarantine_event(
        {
            "type": "pr.filed",
            "run_seq": 5,
            "head_sha": "head-b",
            "correlation": "publication_history_audit",
        }
    )

    _migrate_audit_marked_quarantine(store)

    # The conflicting pending row is retained, not released into a conflict.
    assert [entry.get("run_seq") for entry in store.quarantined_events()] == [5]
    assert len(store.audited_events()) == 1
    # A retained audit-marked row is never eligible for replay.
    assert _replayable_quarantine_documents(store) == []


def test_worker_self_approval_is_not_gate_evidence(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    current = replace(store.load().slices["leaf-a"], dispatch_agent_id="leaf-a")
    store.checkpoint(TLPlanning(), {"leaf-a": current}, BudgetLedger(0, 0), offset=0)
    event = project(
        {
            "type": "pr.review",
            "run_seq": 1,
            "run_id": "review-run",
            "agent_id": "leaf-a",
            "role": "worker",
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-a",
                "kind": "approved",
                "findings": [],
            },
        }
    )

    _record_review_event(store, store.load(), TLPlanning(), event, 1)

    assert store.load().slices["leaf-a"].verdict is None


def test_tool_unavailable_recovery_parks_without_consuming_attempts(tmp_path: Path) -> None:
    class IssueTransport(RecordingTransport):
        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            if tool_name == "chainlink_issue_create":
                self.calls.append((tool_name, arguments))
                return {"success": True, "result": {"issue_id": 9411}}
            return super().call_tool(role, name, tool_name, arguments)

    store = _review_store(tmp_path)
    before = store.load()
    effects = EffectClient(IssueTransport())
    error = ToolUnavailableError(
        "resolve_live_pr_for_slice",
        ToolResult(
            raw={
                "success": False,
                "error": "reworded missing tool",
                "error_kind": "tool_unavailable",
                "error_context": {
                    "tool_name": "resolve_live_pr_for_slice",
                    "role": "tl",
                    "wasm_path": "/project/.exo/wasm/wasm-guest-devswarm.wasm",
                    "wasm_mtime": 1787440000,
                },
            },
            success=False,
            result=None,
            error="reworded missing tool",
            error_kind="tool_unavailable",
            error_context={
                "tool_name": "resolve_live_pr_for_slice",
                "role": "tl",
                "wasm_path": "/project/.exo/wasm/wasm-guest-devswarm.wasm",
                "wasm_mtime": 1787440000,
            },
        ),
        target="leaf-a",
    )

    result = _recover_tool_unavailable(store, effects, [], error)

    parked = result.final_state.slices["leaf-a"]
    assert result.final_state.fsm.phase is TLPhase.TLFailed
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.TOOL_UNAVAILABLE
    assert parked.attempts == before.slices["leaf-a"].attempts
    assert result.final_state.budgets == before.budgets


def _review_event(*, finding_severity: str = "blocking") -> EventEnvelope:
    return project(
        {
            "type": "pr.review",
            "run_seq": 1,
            "run_id": "review-run",
            "agent_id": "leaf-a",
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-a",
                "kind": "changes_requested",
                "findings": [
                    {
                        "severity": finding_severity,
                        "path": "src/leaf.py",
                        "rationale": "The failure path is unhandled",
                    }
                ],
                "diff": {
                    "diff": "@@ -1 +1 @@\\n-old\\n+new\\n",
                    "lines_changed": 1,
                    "paths": ["src/leaf.py"],
                    "review_rounds": 1,
                },
            },
        }
    )


def test_review_stall_classification_is_persisted_by_tl_projection(tmp_path: Path) -> None:
    store = _review_store(tmp_path)
    event = project(
        {
            "type": "pr.review",
            "run_seq": 2,
            "run_id": "review-run",
            "agent_id": "leaf-a",
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:01:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-a",
                "kind": "timeout",
                "last_review_state": "changes_requested",
                "reviewer_registered": True,
                "forgejo_review_present": True,
                "addressed_changes": False,
                "wait_seconds": 900,
            },
        }
    )

    _record_review_event(store, store.load(), TLPlanning(), event, 2)

    assert store.load().slices["leaf-a"].stall_classification == "dev_not_pushing"


def _ci_failure_event() -> EventEnvelope:
    return project(
        {
            "type": "ci.status_changed",
            "run_seq": 1,
            "run_id": "review-run",
            "agent_id": "leaf-a",
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:00:00Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-a",
                "status": "failure",
                "message": "tests failed",
            },
        }
    )


def _aggregate_ci_store(tmp_path: Path, *, interventions: int = 0) -> RunStore:
    """Seed one aggregate PR whose parent-branch CI can fail after child merges."""
    store = _review_store(tmp_path, verdict=Verdict.GO)
    state = store.load()
    current = replace(
        state.slices["leaf-a"],
        dispatch_agent_id="review-run:leaf-a:integration",
        dispatch_last_boundary="aggregate_pr_open",
        ci_state={"head-a": "success"},
    )
    candidate = IntegrationCandidateState(
        lifecycle=IntegrationLifecycle.CODE_REVIEWED,
        aggregate_pr_number=42,
        aggregate_head_sha="head-a",
        aggregate_patch_digest="patch-a",
        aggregate_original_base_sha="main",
        integration_owner_id="review-run:leaf-a:integration",
        integration_owner_run_id="leaf-a",
        integration_owner_branch="main.leaf-a",
        integration_owner_worktree=".worktrees/leaf-a",
        head_sha="head-a",
        patch_digest="patch-a",
        ci_status="success",
        tl_intervention_attempts=interventions,
    )
    store.checkpoint(
        state.fsm,
        {"leaf-a": current},
        state.budgets,
        state.events.last_consumed_offset,
        integration=IntegrationRuntimeState(
            lifecycle=IntegrationLifecycle.CODE_REVIEWED,
            sub_tl_states={"leaf-a": IntegrationLifecycle.CODE_REVIEWED},
            candidates={"leaf-a": candidate},
        ),
    )
    return store


def _integration_judgment_config(backend: ReviewBackend, *, ceiling: int = 1) -> TLLoopConfig:
    return TLLoopConfig(
        active=True,
        policy=validate_policy(_selector_policy(ceiling)),
        tl_model_choice=_tl_choice(backend),
    )


def test_parent_branch_ci_failure_judges_once_and_resumes_aggregate_owner(
    tmp_path: Path,
) -> None:
    backend = ReviewBackend(
        [
            RlmResponse(
                {
                    "action": "delegate",
                    "reason": "The existing aggregate owner can fix this within its boundary",
                    "handoff": {
                        "root_cause": "The integrated parent branch fails its test",
                        "proposed_solution": "Fix the failing test on the aggregate PR",
                        "read_first": ["src/leaf.py"],
                        "steps": ["Repair the failing test"],
                        "verify": ["just tl-loop-test"],
                        "boundary": ["Only edit src/leaf.py"],
                        "done_criteria": ["Parent branch CI passes"],
                    },
                },
                input_tokens=10,
                output_tokens=5,
            )
        ]
    )
    transport = ReviewRepairTransport()
    store = _aggregate_ci_store(tmp_path)

    _route_ci_event(
        store,
        store.load(),
        TLPlanning(),
        _ci_failure_event(),
        1,
        _integration_judgment_config(backend),
        EffectClient(transport),
        [],
    )
    _route_ci_event(
        store,
        store.load(),
        TLPlanning(),
        _ci_failure_event(),
        2,
        _integration_judgment_config(backend),
        EffectClient(transport),
        [],
    )

    restored = store.load()
    assert [request.name for request in backend.requests] == ["integration_failure"]
    assert restored.slices["leaf-a"].status is SliceStatus.REPAIRING
    assert restored.integration.candidates["leaf-a"].lifecycle is (
        IntegrationLifecycle.REPAIRING_AGGREGATE
    )
    assert restored.integration.candidates["leaf-a"].tl_intervention_attempts == 1
    assert restored.budgets.role_spent["tl"] == 15
    assert [name for name, _ in transport.calls if name == "resume_pr"] == ["resume_pr"]


def test_aggregate_ci_recovery_resets_intervention_allowance(tmp_path: Path) -> None:
    store = _aggregate_ci_store(tmp_path, interventions=1)
    success = project(
        {
            "type": "ci.status_changed",
            "run_seq": 2,
            "run_id": "review-run",
            "agent_id": "leaf-a",
            "lifecycle_state": "observed",
            "observed_at": "2026-08-12T00:00:01Z",
            "data": {
                "slice_id": "leaf-a",
                "pr_number": 42,
                "head_sha": "head-a",
                "status": "success",
            },
        }
    )

    _route_ci_event(
        store,
        store.load(),
        TLPlanning(),
        success,
        2,
        TLLoopConfig(active=True),
        EffectClient(RecordingTransport()),
        [],
    )

    assert store.load().integration.candidates["leaf-a"].tl_intervention_attempts == 0


def test_parent_branch_ci_failure_escalates_to_named_gate(tmp_path: Path) -> None:
    backend = ReviewBackend(
        [RlmResponse({"action": "escalate", "reason": "The failure crosses the slice boundary"})]
    )
    transport = RecordingTransport()
    store = _aggregate_ci_store(tmp_path)

    _route_ci_event(
        store,
        store.load(),
        TLPlanning(),
        _ci_failure_event(),
        1,
        _integration_judgment_config(backend),
        EffectClient(transport),
        [],
    )

    restored = store.load()
    assert [request.name for request in backend.requests] == ["integration_failure"]
    assert GateState(name=INTEGRATION_FAILURE_GATE_NAME, status=GateStatus.PENDING) in (
        restored.gates
    )
    assert not any(name == "resume_pr" for name, _ in transport.calls)


def test_parent_branch_ci_failure_ceiling_opens_gate_without_another_judgment(
    tmp_path: Path,
) -> None:
    backend = ReviewBackend([])
    transport = RecordingTransport()
    store = _aggregate_ci_store(tmp_path, interventions=1)

    _route_ci_event(
        store,
        store.load(),
        TLPlanning(),
        _ci_failure_event(),
        1,
        _integration_judgment_config(backend, ceiling=1),
        EffectClient(transport),
        [],
    )

    restored = store.load()
    assert backend.requests == []
    assert GateState(name=INTEGRATION_FAILURE_GATE_NAME, status=GateStatus.PENDING) in (
        restored.gates
    )
    assert restored.integration.candidates["leaf-a"].tl_intervention_attempts == 1


def test_tl_run_integrates_selection_model_and_atomic_charge(tmp_path: Path) -> None:
    transport = RecordingTransport()
    run_id = "selector-run"
    source = SyntheticQueue(_lifecycle_events(run_id))
    policy = validate_policy(_selector_policy())
    config = TLLoopConfig(
        source=source,
        effects=EffectClient(transport),
        root_dir=tmp_path,
        policy=policy,
        capabilities=CapabilityMap(
            {"codex/gpt-luna": Difficulty.STANDARD, "claude/sonnet": Difficulty.HARD}
        ),
        catalog=ModelCatalog.from_fixture(
            Path(__file__).parent / "fixtures" / "model_catalog.json"
        ),
        requested_model="gpt-5.5",
        max_workers=1,
        max_leaves=1,
        max_events=5,
        poll_interval=0.001,
    )

    result = tl_run({"run_id": run_id, "plan": _plan()}, config, BudgetLedger(0, 0))

    assert _effect_names(transport) == [
        "spawn_worker",
        "spawn_leaf",
        "root_branch_finalize",
    ]
    spawn_worker_call = next(
        arguments for name, arguments in transport.calls if name == "spawn_worker"
    )
    assert spawn_worker_call["agent_type"] == "codex"
    assert spawn_worker_call["model"] == "gpt-5.5"
    assert result.final_state.budgets.role_reserved == {"worker": 500}
    assert result.final_state.budgets.harness_reserved == {"codex/gpt-luna": 500}
    assert result.final_state.slices["worker-a"].model == "gpt-5.5"
    assert result.final_state.fsm.phase is TLPhase.TLDone


def test_tl_run_width_gate_dispatches_next_ready_slice_after_completion(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport()
    run_id = "width-run"
    source = SyntheticQueue(_serial_worker_events(run_id))
    policy = validate_policy(_selector_policy())
    config = TLLoopConfig(
        source=source,
        effects=EffectClient(transport),
        root_dir=tmp_path,
        policy=policy,
        capabilities=CapabilityMap(
            {"codex/gpt-luna": Difficulty.STANDARD, "claude/sonnet": Difficulty.HARD}
        ),
        max_workers=2,
        max_leaves=0,
        max_parallel_slices=1,
        max_events=5,
        poll_interval=0.001,
    )
    plan = WorkPlan(
        workers=(
            WorkerTask("worker-a", "first"),
            WorkerTask("worker-b", "second"),
        )
    )

    result = tl_run({"run_id": run_id, "plan": plan}, config, BudgetLedger(0, 0))

    assert _effect_names(transport) == ["spawn_worker", "spawn_worker", "root_branch_finalize"]
    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert result.final_state.budgets.role_reserved == {"worker": 500}


def test_shadow_loop_uses_the_same_driver_without_mutating_transport(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport()
    source = SyntheticQueue(_lifecycle_events("shadow-run"))
    result = run_tl_loop(
        "shadow-run",
        _plan(),
        source,
        ReadOnlyEffectClient(EffectClient(transport)),
        config=_config(active=False),
        root_dir=tmp_path,
    )

    assert transport.calls == []
    assert _effect_operations(result) == [
        "spawn_worker",
        "spawn_leaf",
    ]
    assert not any(intent.executed for intent in result.effects)
    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert result.final_state.events.last_consumed_offset == 5


def test_canonical_completion_and_parent_notification_are_idempotent(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport()
    source = SyntheticQueue(_canonical_lifecycle("canonical-run"))
    result = run_tl_loop(
        "canonical-run",
        _plan(),
        source,
        EffectClient(transport),
        config=TLLoopConfig(
            max_workers=1,
            max_leaves=1,
            max_events=6,
            poll_interval=0.001,
        ),
        root_dir=tmp_path,
    )

    assert _effect_names(transport) == [
        "spawn_worker",
        "spawn_leaf",
        "root_branch_finalize",
    ]
    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert source.acknowledged == [1, 2, 3, 4, 5, 6]


def test_loop_rejects_a_plan_over_its_worker_ceiling(tmp_path: Path) -> None:
    source = SyntheticQueue([])
    with pytest.raises(LoopLimitExceeded, match="max_workers"):
        run_tl_loop(
            "bounded-run",
            WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "bounded"}]}),
            source,
            EffectClient(RecordingTransport()),
            config=TLLoopConfig(max_workers=0),
            root_dir=tmp_path,
        )


def test_loop_rejects_an_event_stream_over_its_event_ceiling(tmp_path: Path) -> None:
    source = SyntheticQueue(_lifecycle_events("event-bounded-run"))
    with pytest.raises(LoopLimitExceeded, match="event limit"):
        run_tl_loop(
            "event-bounded-run",
            _plan(),
            source,
            EffectClient(RecordingTransport()),
            config=TLLoopConfig(
                max_events=1,
                test_harness=True,
                poll_interval=0.001,
            ),
            root_dir=tmp_path,
        )


def test_idle_silence_preserves_spawned_state_until_explicit_cancellation(
    tmp_path: Path,
) -> None:
    run_id = "long-running-run"
    cancel_event = threading.Event()
    source = SyntheticQueue(
        [
            _canonical_event(
                1,
                "agent.spawned",
                "leaf-a",
                run_id,
                intent_id=_dispatch_intent(run_id, "leaf-a"),
            )
        ]
    )
    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            run_tl_loop(
                run_id,
                WorkPlan.from_mapping(
                    {
                        "leaves": [
                            {"name": "leaf-a", "task": "long work", "chainlink_issue_id": None}
                        ]
                    }
                ),
                source,
                EffectClient(RecordingTransport()),
                config=TLLoopConfig(
                    max_workers=0,
                    max_leaves=1,
                    poll_interval=0.001,
                    cancel_event=cancel_event,
                ),
                root_dir=tmp_path,
            )
        except BaseException as error:  # noqa: BLE001 - assert explicit cancellation
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while not source.acknowledged and time.monotonic() < deadline:
        time.sleep(0.001)
    assert source.acknowledged == [1]
    cancel_event.set()
    thread.join(timeout=2)

    assert isinstance(outcome.get("error"), LoopCancelled)
    state = RunStore(run_id, tmp_path).load()
    assert state.fsm.phase is TLPhase.TLWaiting
    assert state.slices["leaf-a"].status is SliceStatus.SPAWNED
    assert not state.gates


def _selector_policy(escalate_after_attempts: int = 1) -> dict[str, object]:
    role = {
        "allow": ["codex/gpt-luna", "claude/sonnet"],
        "cost_rank": {"codex/gpt-luna": 1, "claude/sonnet": 2},
        "token_budget": 120000,
        "per_harness_budget": {"codex/gpt-luna": 80000, "claude/sonnet": 40000},
        "escalate_after_attempts": escalate_after_attempts,
    }
    return {"roles": {"tl": dict(role), "worker": dict(role), "reviewer": dict(role)}}


def _plan() -> WorkPlan:
    return WorkPlan.from_mapping(
        {
            "workers": [{"name": "worker-a", "task": "inspect the repository"}],
            "leaves": [
                {
                    "name": "leaf-a",
                    "task": "implement the requested change",
                    "chainlink_issue_id": None,
                    "agent_type": "codex",
                    "verify": ["just tl-loop-test"],
                }
            ],
        }
    )


def _config(*, active: bool = True) -> TLLoopConfig:
    return TLLoopConfig(
        active=active,
        max_workers=1,
        max_leaves=1,
        max_events=5,
        poll_interval=0.001,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )


def _lifecycle_events(run_id: str) -> list[EventEnvelope]:
    return [
        _canonical_event(1, "agent.spawned", "worker-a", run_id),
        _canonical_event(2, "agent.spawned", "leaf-a", run_id),
        _event(3, "child_completed", "worker-a", run_id=run_id),
        _event(4, "child_completed", "leaf-a", pr_number=42, head_sha="head-a", run_id=run_id),
        _event(5, "all_children_done", run_id=run_id),
    ]


def _multi_leaf_events(run_id: str) -> list[EventEnvelope]:
    return [
        _canonical_event(1, "agent.spawned", "leaf-a", run_id),
        _canonical_event(2, "agent.spawned", "leaf-b", run_id),
        _event(3, "child_completed", "leaf-a", pr_number=10, head_sha="leaf-a-head", run_id=run_id),
        _event(4, "child_completed", "leaf-b", pr_number=11, head_sha="leaf-b-head", run_id=run_id),
        _event(5, "all_children_done", run_id=run_id),
    ]


def _complete_ordered_child_receipts(
    child_store: RunStore,
    scope: str,
    *,
    repository: Path | None = None,
) -> RunState:
    """Persist exact reviewed leaf merges for one synthetic ordered child."""
    state = child_store.load()
    expected_base = _test_git_repository(repository) if repository is not None else None
    completed: dict[str, SliceState] = {}
    for index, (slice_id, current) in enumerate(sorted(state.slices.items()), start=1):
        pr_number = current.pr_number or 200 + index
        head_sha = f"{scope}-{slice_id}-reviewed-head"
        parent_commit_sha = f"{scope}-{slice_id}-parent"
        changelog_commit_sha = f"{scope}-{slice_id}-changelog"
        pushed_commit = f"{scope}-{slice_id}-pushed"
        expected_base_sha = f"{scope}-{slice_id}-expected-base"
        if repository is not None:
            assert expected_base is not None
            expected_base_sha = expected_base
            head_sha = _test_git_commit(
                repository,
                f"{scope}-{slice_id}.txt",
                f"reviewed {scope} {slice_id}\n",
                f"{scope} {slice_id} reviewed merge",
            )
            parent_commit_sha = head_sha
            changelog_commit_sha = _test_git_commit(
                repository,
                f"{scope}-CHANGELOG.md",
                f"{scope} {slice_id} changelog\n",
                f"{scope} {slice_id} changelog",
            )
            pushed_commit = _test_git_commit(
                repository,
                f"{scope}-post-merge-{slice_id}.txt",
                f"{scope} {slice_id} bookkeeping\n",
                f"{scope} {slice_id} bookkeeping push",
            )
            expected_base = pushed_commit
        base_branch = current.base_ref or "main"
        owner = current.dispatch_agent_id or slice_id
        invocation_id = f"{scope}-{slice_id}-publication"
        publication = PublicationBinding(
            pr_number=pr_number,
            head_sha=head_sha,
            head_branch=current.branch or f"main.{slice_id}",
            base_branch=base_branch,
            attempt=1,
            invocation_id=invocation_id,
        )
        handoff = HandoffEvidence(
            pr_number=pr_number,
            head_sha=head_sha,
            attempt=1,
            invocation_id=invocation_id,
            agent_id=owner,
            observed_at="2026-10-14T00:00:00Z",
        )
        receipt: dict[str, str] = {
            "child_id": slice_id,
            "repository": "https://forge.example.test/test/repository.git",
            "parent_branch": base_branch,
            "pr_number": str(pr_number),
            "head_sha": head_sha,
            "merge_journal_id": f"{scope}-{slice_id}-merge-journal",
            "lane_epoch": "1",
            "parent_commit_sha": parent_commit_sha,
        }
        if current.chainlink_issue_id is None:
            receipt.update(
                {
                    "issue_close_outcome": "not_applicable",
                    "changelog_outcome": "not_applicable",
                }
            )
        else:
            receipt.update(
                {
                    "issue_id": str(current.chainlink_issue_id),
                    "issue_close_intent_id": f"{scope}-{slice_id}-issue-close-intent",
                    "issue_close_journal_id": f"{scope}-{slice_id}-issue-close-journal",
                    "issue_close_outcome": "closed",
                }
            )
        receipt.update(
            {
                "changelog_intent_id": f"{scope}-{slice_id}-changelog-intent",
                "changelog_generation": "1",
                "changelog_commit_sha": changelog_commit_sha,
                "parent_push_intent_id": f"{scope}-{slice_id}-parent-push-intent",
                "push_journal_id": f"{scope}-{slice_id}-push-journal",
                "expected_base_sha": expected_base_sha,
                "push_receipt_id": f"{scope}-{slice_id}-push-receipt",
                "pushed_commit": pushed_commit,
                "bookkeeping_commit": pushed_commit,
                "observed_remote_head": pushed_commit,
                "ancestry_proof": f"ancestor:{pushed_commit}->{pushed_commit}",
            }
        )
        completed[slice_id] = replace(
            current,
            status=SliceStatus.MERGED,
            dispatch_agent_id=owner,
            pr_number=pr_number,
            reviewed_head=head_sha,
            verdict=Verdict.GO,
            review_patch_digests={head_sha: f"{scope}-{slice_id}-patch"},
            ci_state={head_sha: "success"},
            publication=publication,
            handoff=handoff,
            post_merge=PostMergeState(PostMergePhase.COMPLETE, receipt),
            recovery=None,
        )
    return child_store.checkpoint(
        FSMState(TLPhase.TLAllMerged, ()),
        {**state.slices, **completed},
        state.budgets,
        state.events.last_consumed_offset,
    )


def _canonical_lifecycle(run_id: str) -> list[EventEnvelope]:
    return [
        _canonical_event(1, "agent.spawned", "worker-a", run_id),
        _canonical_event(2, "agent.spawned", "leaf-a", run_id),
        _canonical_event(3, "agent.completed", "worker-a", run_id),
        _canonical_event(4, "agent.notify_parent", "worker-a", run_id),
        _canonical_event(5, "agent.completed", "leaf-a", run_id, pr_number=42),
        _event(6, "all_children_done", run_id=run_id),
    ]


def _serial_worker_events(run_id: str) -> list[EventEnvelope]:
    return [
        _event(1, "child_spawned", "worker-a", run_id=run_id),
        _event(2, "child_completed", "worker-a", run_id=run_id),
        _event(3, "child_spawned", "worker-b", run_id=run_id),
        _event(4, "child_completed", "worker-b", run_id=run_id),
        _event(5, "all_children_done", run_id=run_id),
    ]


def _canonical_event(
    run_seq: int,
    event_type: str,
    slug: str,
    run_id: str,
    *,
    pr_number: int | None = None,
    intent_id: str | None = None,
) -> EventEnvelope:
    data: dict[str, object] = {}
    if event_type == "agent.spawned":
        data.update(
            {
                "child_agent": slug,
                "agent_type": "codex",
                "branch": f"main.{slug}",
                "intent_id": intent_id or _dispatch_intent(run_id, slug),
            }
        )
    else:
        data.update({"status": "success", "message": "completed"})
    if pr_number is not None:
        data["pr_number"] = pr_number
    raw = {
        "schema_version": 1,
        "event_id": f"canonical-{run_seq}",
        "id": f"canonical-{run_seq}",
        "event_time": "2026-08-11T00:00:00Z",
        "observed_at": "2026-08-11T00:00:00Z",
        "run_seq": run_seq,
        "type": event_type,
        "agent_id": slug,
        "run_id": run_id,
        "session_id": "session-1",
        "lifecycle_state": "observed",
        "data": data,
    }
    return project(cast(dict[str, object], raw))


def _event(
    run_seq: int,
    kind: str,
    slug: str | None = None,
    *,
    pr_number: int | None = None,
    head_sha: str | None = None,
    run_id: str,
) -> EventEnvelope:
    shadow_event: dict[str, object] = {"kind": kind}
    if slug is not None:
        shadow_event["slug"] = slug
    if kind == "child_spawned":
        shadow_event["branch"] = f"main.{slug}"
        shadow_event["agent_type"] = "codex"
        shadow_event["intent_id"] = _dispatch_intent(run_id, slug or "")
    data: dict[str, object] = {"shadow_event": shadow_event}
    if pr_number is not None:
        data["pr_number"] = pr_number
    if head_sha is not None:
        data["head_sha"] = head_sha
    raw = {
        "schema_version": 1,
        "event_id": f"event-{run_seq}",
        "id": f"event-{run_seq}",
        "event_time": "2026-08-11T00:00:00Z",
        "observed_at": "2026-08-11T00:00:00Z",
        "run_seq": run_seq,
        "type": "agent.notify_parent",
        "agent_id": slug,
        "run_id": run_id,
        "session_id": "session-1",
        "lifecycle_state": "observed",
        "data": data,
    }
    return project(cast(dict[str, object], raw))


def _dispatch_intent(run_id: str, slug: str, epoch: str | None = None) -> str:
    """Mirror `_new_dispatch_attempt`'s attempt-1 identity for one controller epoch."""
    if epoch is None:
        epoch = hashlib.sha256(f"controller:{run_id}".encode()).hexdigest()[:32]
    return hashlib.sha256(f"{run_id}:{slug}:1:{epoch}".encode()).hexdigest()[:32]


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("pr.review", {"findings": [], "head_sha": "aggregate-head"}),
        ("ci.status_changed", {"ci_status": "success", "head_sha": "aggregate-head"}),
    ],
)
def test_aggregate_review_and_ci_events_route_by_persisted_pr(
    event_type: str, payload: dict[str, object]
) -> None:
    payload = {**payload, "pr_number": 77}
    event = project(
        {
            "schema_version": 1,
            "event_id": "aggregate-event",
            "id": "aggregate-event",
            "event_time": "2026-08-11T00:00:00Z",
            "observed_at": "2026-08-11T00:00:00Z",
            "run_seq": 1,
            "type": event_type,
            "agent_id": "aggregate-run:alpha:integration",
            "run_id": "aggregate-run",
            "session_id": "session-1",
            "lifecycle_state": "observed",
            "data": payload,
        }
    )
    state = SimpleNamespace(
        slices={
            "alpha": SimpleNamespace(
                pr_number=77,
                reviewed_head="aggregate-head",
                dispatch_agent_id="aggregate-run:alpha:integration",
            )
        }
    )

    assert _event_belongs_to_plan(event, set(), state)


def test_recursive_sub_tls_isolate_state_and_branch_coordinates(tmp_path: Path) -> None:
    transport = RecordingTransport()
    grand_source = SyntheticQueue([])
    child_source = SyntheticQueue([])
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "child",
                WorkPlan(
                    sub_tls=(
                        SubTLTask(
                            "grandchild",
                            WorkPlan(),
                            source=grand_source,
                            integration=IntegrationContract(aggregate_pr_required=False),
                        ),
                    )
                ),
                source=child_source,
                integration=IntegrationContract(aggregate_pr_required=False),
            ),
        )
    )

    result = run_tl_loop(
        "recursive-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=_config(),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert set(result.final_state.slices) == {"child"}
    assert result.final_state.slices["child"].branch == "main.child"
    assert result.final_state.slices["child"].base_ref == "main"
    child = load_state(tmp_path / "recursive-run" / "child" / "run.json")
    assert set(child.slices) == {"grandchild"}
    assert child.owner_branch == "main.child"
    assert child.parent_branch == "main"
    assert child.slices["grandchild"].branch == "main.child.grandchild"
    assert child.slices["grandchild"].base_ref == "main.child"
    assert result.final_state.repository_identity == RepositoryIdentity(
        "https://forge.example.com/org/repo.git"
    )
    assert child.repository_identity == result.final_state.repository_identity
    grandchild = load_state(tmp_path / "recursive-run" / "child" / "grandchild" / "run.json")
    assert grandchild.repository_identity == result.final_state.repository_identity
    assert not any(name == "repository_identity" for name, _ in transport.calls)
    assert all(
        name in {"emit_controller_event", "repository_identity"} for name, _ in transport.calls
    )


def test_root_finalization_checkpoint_precedes_terminal_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint_types: list[str] = []
    original_checkpoint = RunStore.checkpoint

    def record_checkpoint(self: RunStore, *args: object, **kwargs: object) -> object:
        if args:
            checkpoint_types.append(type(args[0]).__name__)
        return original_checkpoint(self, *args, **kwargs)

    monkeypatch.setattr(RunStore, "checkpoint", record_checkpoint)
    run_tl_loop(
        "root-finalization-run",
        WorkPlan(),
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=_config(),
        root_dir=tmp_path,
    )

    finalizing = checkpoint_types.index("TLFinalizing")
    done = checkpoint_types.index("TLDone")
    assert finalizing < done


def test_recursive_depth_ceiling_parks_schedule_deadlock(tmp_path: Path) -> None:
    with pytest.raises(DepthLimitExceeded):
        run_tl_loop(
            "depth-run",
            WorkPlan(sub_tls=(SubTLTask("child", WorkPlan(), source=SyntheticQueue([])),)),
            SyntheticQueue([]),
            EffectClient(RecordingTransport()),
            config=TLLoopConfig(max_depth=0, poll_interval=0.001),
            root_dir=tmp_path,
        )

    state = load_state(tmp_path / "depth-run" / "run.json")
    assert state.fsm.phase is TLPhase.TLFailed
    assert state.slices["child"].status.value == "parked"


def test_same_order_sub_tls_overlap_and_wait_for_prior_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = 0
    maximum = 0
    lock = threading.Lock()
    first_stage = threading.Barrier(2)
    timeline: list[tuple[str, str]] = []

    def fake_tl_run(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del config, budgets
        name = cast(dict[str, object], root_spec)["run_id"]
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            timeline.append(("start", cast(str, name)))
        if name in {"stage-one-a", "stage-one-b"}:
            first_stage.wait(timeout=2)
        time.sleep(0.01)
        with lock:
            active -= 1
            timeline.append(("end", cast(str, name)))
        return SimpleNamespace(
            final_state=SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLDone), slices={})
        )

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", fake_tl_run)
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "stage-one-a",
                WorkPlan(),
                order=1,
                integration=IntegrationContract(aggregate_pr_required=False),
            ),
            SubTLTask(
                "stage-one-b",
                WorkPlan(),
                order=1,
                integration=IntegrationContract(aggregate_pr_required=False),
            ),
            SubTLTask(
                "stage-two",
                WorkPlan(),
                order=2,
                integration=IntegrationContract(aggregate_pr_required=False),
            ),
        )
    )
    result = run_tl_loop(
        "ordered-concurrency-run",
        plan,
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=TLLoopConfig(max_parallel_slices=2, poll_interval=0.001),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert maximum == 2
    assert timeline.index(("start", "stage-two")) > timeline.index(("end", "stage-one-a"))
    assert timeline.index(("start", "stage-two")) > timeline.index(("end", "stage-one-b"))


def test_active_parent_stays_alive_for_later_recursive_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_at = time.monotonic() + 0.02

    class DelayedQueue(SyntheticQueue):
        def get(self, timeout: float | None = None) -> EventEnvelope:
            if not self.events and time.monotonic() < release_at:
                raise queue.Empty
            return super().get(timeout)

    def waiting_child(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del root_spec, config, budgets
        return SimpleNamespace(
            final_state=SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLWaiting), slices={})
        )

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", waiting_child)
    run_id = "waiting-parent"
    result = run_tl_loop(
        run_id,
        WorkPlan(
            sub_tls=(
                SubTLTask(
                    "waiting-child",
                    WorkPlan(),
                    order=1,
                    integration=IntegrationContract(aggregate_pr_required=False),
                ),
            )
        ),
        DelayedQueue([_event(1, "all_children_done", run_id=run_id)]),
        EffectClient(RecordingTransport()),
        config=TLLoopConfig(
            active=True,
            keep_alive_on_waiting=True,
            max_parallel_slices=1,
            max_events=1,
            poll_interval=0.001,
        ),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert result.consumed_events == (1,)


def test_recursive_ordered_lifecycle_integrates_sequential_children_without_root_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_plans = {
        "alpha": WorkPlan.from_mapping(
            {
                "leaves": [
                    {
                        "name": "leaf-a",
                        "task": "implement alpha part one",
                        "chainlink_issue_id": 1052,
                    },
                    {
                        "name": "leaf-b",
                        "task": "implement alpha part two",
                        "chainlink_issue_id": 1053,
                    },
                ]
            }
        ),
        "beta": WorkPlan.from_mapping(
            {
                "leaves": [
                    {
                        "name": "leaf-a",
                        "task": "implement beta part one",
                        "chainlink_issue_id": 1054,
                    },
                    {
                        "name": "leaf-b",
                        "task": "implement beta part two",
                        "chainlink_issue_id": 1055,
                    },
                ]
            }
        ),
    }
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "alpha",
                child_plans["alpha"],
                source=SyntheticQueue(_multi_leaf_events("alpha")),
                order=1,
                chainlink_issue_id=1057,
            ),
            SubTLTask(
                "beta",
                child_plans["beta"],
                source=SyntheticQueue(_multi_leaf_events("beta")),
                order=2,
                chainlink_issue_id=1056,
            ),
        )
    )
    config = TLLoopConfig(
        max_parallel_slices=2,
        max_leaves=2,
        max_workers=1,
        max_events=5,
        poll_interval=0.001,
        keep_alive_on_waiting=False,
        ledger_run_id="ordered-e2e-ledger",
        repository_identity=RepositoryIdentity("https://forge.example.test/test/repository.git"),
        agent_id="root-controller",
    )
    _install_ordered_invocation(monkeypatch, "beta-invocation")

    def seed_child_scopes(
        parent_root: Path,
        transport: OrderedIntegrationTransport,
        *,
        with_ordered_recovery: bool = False,
        with_rejected_merge: bool = False,
    ) -> None:
        root_manifest = driver_module._manifest_for_plan(plan, "ordered-e2e-run", config)
        root_slices = _initial_slices(plan, config, parent_root, "ordered-e2e-run")
        create(
            "ordered-e2e-run",
            {"slices": root_slices, "ledger_run_id": "ordered-e2e-ledger"},
            root_dir=parent_root,
        )
        root_store = RunStore("ordered-e2e-run", parent_root)
        root_store.set_plan_manifest(root_manifest, slices=root_slices)
        ordered_stages = tuple(
            driver_module.OrderedStageState(stage.order, stage.sub_tls)
            for stage in plan.ordered_stages
        )
        state = root_store.set_ordered_state(1, ordered_stages, root_store.load().integration)
        for task in plan.sub_tls:
            sub_tl_id = task.name
            child_node = next(node for node in root_manifest.nodes if node.name == sub_tl_id)
            child_manifest = root_manifest.child_manifests[child_node.node_id]
            branch = driver_module.derive_child_branch(
                config.branch, driver_module._child_controller_name(task)
            )
            worktree = str(
                driver_module._sub_tl_worktree(config, root_store.root_dir, root_store.run_id, task)
            )
            child_config = driver_module._child_config(
                config,
                task,
                task.source or SyntheticQueue([]),
                EffectClient(transport),
                root_store,
                branch,
                worktree,
                keep_alive_on_waiting=True,
            )
            child_slices = _initial_slices(
                child_plans[sub_tl_id], child_config, root_store.run_dir, sub_tl_id
            )
            create(
                sub_tl_id,
                {
                    "slices": child_slices,
                    "owner_branch": branch,
                    "owner_worktree": worktree,
                    "parent_branch": config.branch,
                    "parent_run_id": root_store.run_id,
                    "parent_agent_id": root_store.run_id,
                    "depth": 1,
                    "ledger_run_id": "ordered-e2e-ledger",
                },
                root_dir=root_store.run_dir,
            )
            child_store = RunStore(sub_tl_id, root_store.run_dir)
            child_store.set_plan_manifest(child_manifest, slices=child_slices)
            repository = Path(worktree)
            _complete_ordered_child_receipts(
                child_store,
                sub_tl_id,
                repository=repository,
            )
            aggregate_head = _test_git_output(repository, "rev-parse", "HEAD")
            assert aggregate_head is not None
            transport.aggregate_git_evidence[sub_tl_id] = (
                aggregate_head,
                _test_git_tree(repository, aggregate_head),
            )
        current = state.slices["alpha"]
        alpha_head, alpha_tree = transport.aggregate_git_evidence["alpha"]
        alpha_publication = PublicationBinding(
            pr_number=101,
            head_sha=alpha_head,
            head_branch=current.branch or "main.alpha",
            base_branch="main",
            attempt=1,
            invocation_id="alpha-invocation",
        )
        aggregate = IntegrationCandidateState(
            lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
            aggregate_pr_number=101,
            aggregate_head_sha=alpha_head,
            aggregate_patch_digest="patch-alpha",
            aggregate_original_base_sha="base-alpha",
            integration_owner_id="ordered-e2e-run:alpha:integration",
            integration_owner_run_id="alpha",
            integration_owner_branch=current.branch,
            integration_owner_worktree=current.worktree,
            head_sha=alpha_head,
            patch_digest="patch-alpha",
        )
        alpha = replace(
            current,
            status=SliceStatus.IN_REVIEW,
            attempts=1,
            pr_number=101,
            publication=alpha_publication,
            dispatch_intent_id="alpha-dispatch-intent",
            dispatch_agent_id="alpha",
            dispatch_invocation_id="alpha-invocation",
            dispatch_generation=1,
            dispatch_authoritative_event_seq=0,
            dispatch_last_boundary="aggregate_pr_open",
        )
        if with_rejected_merge:
            rejected_intent = EffectIntent(
                "merge_pr",
                "alpha",
                {"pr_number": 101, "attempt": 1},
                True,
            )
            journal = EffectJournal(
                "ordered-e2e-run",
                root_store.run_dir / "action-journal.json",
            )
            journal.append(rejected_intent)
            journal.mark_result(
                rejected_intent,
                ToolResult(
                    raw={
                        "success": False,
                        "error": "No Forgejo reviewer response yet on PR #101.",
                    },
                    success=False,
                    result={
                        "success": False,
                        "error": "No Forgejo reviewer response yet on PR #101.",
                    },
                    error="No Forgejo reviewer response yet on PR #101.",
                ),
            )
            alpha = replace(
                alpha,
                action=ActionState(
                    ActionKind.MERGE,
                    ActionPhase.IN_FLIGHT,
                    intent_id=journal.key_for(rejected_intent),
                    head_sha=alpha_head,
                    attempt=1,
                ),
            )
            aggregate = replace(
                aggregate,
                lifecycle=IntegrationLifecycle.MERGING,
                validated_base_sha="base-alpha",
                merge_tree_sha="tree-alpha",
                integration_evidence_at="2026-10-16T00:00:00Z",
                ci_status="success",
                stage_verification="passed",
            )
        if with_ordered_recovery:
            alpha = replace(
                alpha,
                recovery=begin_recovery(
                    cause="ordered_child_pane_exited",
                    owner_run_id="ordered-e2e-run",
                    slice_attempt=1,
                    owner_agent_id="alpha",
                    invocation_generation=1,
                    evidence={
                        "invocation_id": "alpha-invocation",
                        "slice_id": "alpha",
                        "authorization_source": "human",
                    },
                    next_action="resume_same_owner",
                ),
            )
        publication_record = {
            "invocation_id": "alpha-invocation",
            "slice_id": "alpha",
            "author_agent": "alpha",
            "succession_invocation_ids": [],
        }
        transport.aggregate_reconciliations["alpha"] = {
            "pr_number": 101,
            "publication": publication_record,
            "watcher": {
                "found": True,
                "pr_number": 101,
                "merged": False,
                "head_sha": alpha_head,
                "base_sha": "base-alpha",
                "patch_digest": "patch-alpha",
                "pr_head_tree_sha": alpha_tree,
                "merge_tree_sha": "tree-alpha",
                "ci_status": "success",
                "head_branch": alpha.branch,
                "base_branch": "main",
                "head_reachable": True,
                "publication_ownership_verified": True,
                "publication_ownership_error": "",
                "publication": publication_record,
            },
        }
        root_store.checkpoint(
            state.recursive_fsm or state.fsm,
            {**state.slices, "alpha": alpha},
            state.budgets,
            state.events.last_consumed_offset,
            current_order=1,
            ordered_stages=state.ordered_stages,
            integration=replace(
                state.integration,
                candidates={**state.integration.candidates, "alpha": aggregate},
            ),
        )
        transport.calls.clear()

    uninterrupted_root = tmp_path / "uninterrupted"
    uninterrupted_transport = OrderedIntegrationTransport(
        spawned_agent_id="beta",
        spawned_invocation_id="beta-invocation",
    )
    seed_child_scopes(
        uninterrupted_root,
        uninterrupted_transport,
        with_rejected_merge=True,
    )
    uninterrupted_transport.observed_root_store = RunStore("ordered-e2e-run", uninterrupted_root)
    uninterrupted = run_tl_loop(
        "ordered-e2e-run",
        plan,
        SyntheticQueue([]),
        EffectClient(uninterrupted_transport),
        config=replace(config, max_workers=1),
        root_dir=uninterrupted_root,
    )

    replayed_root = tmp_path / "replayed"
    replayed_transport = OrderedIntegrationTransport(
        spawned_agent_id="beta",
        spawned_invocation_id="beta-invocation",
    )
    seed_child_scopes(
        replayed_root,
        replayed_transport,
        with_ordered_recovery=True,
        with_rejected_merge=True,
    )
    replayed_transport.observed_root_store = RunStore("ordered-e2e-run", replayed_root)
    original_checkpoint = RunStore.checkpoint
    approval_checkpoint_written = False

    def crash_after_alpha_approval(
        store: RunStore,
        *args: Any,
        **checkpoint_args: Any,
    ) -> RunState:
        nonlocal approval_checkpoint_written
        state = original_checkpoint(store, *args, **checkpoint_args)
        if (
            store.run_id == "ordered-e2e-run"
            and not approval_checkpoint_written
            and state.integration is not None
            and "alpha" in state.integration.candidates
        ):
            candidate = state.integration.candidates["alpha"]
            if candidate.active_aggregate_approval_id and candidate.aggregate_approval_receipts:
                approval_checkpoint_written = True
                raise SystemExit("simulated crash after alpha approval before merge")
        return state

    monkeypatch.setattr(RunStore, "checkpoint", crash_after_alpha_approval)
    with pytest.raises(SystemExit, match="after alpha approval before merge"):
        run_tl_loop(
            "ordered-e2e-run",
            plan,
            SyntheticQueue([]),
            EffectClient(replayed_transport),
            config=replace(config, max_workers=1),
            root_dir=replayed_root,
        )

    assert approval_checkpoint_written
    checkpointed = RunStore("ordered-e2e-run", replayed_root).load()
    alpha_approval = checkpointed.integration.candidates["alpha"]
    assert alpha_approval.lifecycle is IntegrationLifecycle.INTEGRATION_VALIDATED
    assert len(alpha_approval.aggregate_approval_receipts) == 1
    alpha_approval_id = alpha_approval.active_aggregate_approval_id
    assert alpha_approval_id is not None
    assert checkpointed.slices["alpha"].status is SliceStatus.IN_REVIEW
    assert not any(name == "merge_pr" for name, _ in replayed_transport.calls)

    monkeypatch.setattr(RunStore, "checkpoint", original_checkpoint)
    restart_cancel = threading.Event()
    restart_deadline = threading.Timer(10, restart_cancel.set)
    restart_deadline.start()
    try:
        resumed = run_tl_loop(
            "ordered-e2e-run",
            plan,
            SyntheticQueue([]),
            EffectClient(replayed_transport),
            config=replace(config, max_workers=1, cancel_event=restart_cancel),
            root_dir=replayed_root,
        )
    finally:
        restart_deadline.cancel()

    for result, transport in (
        (uninterrupted, uninterrupted_transport),
        (resumed, replayed_transport),
    ):
        assert result.final_state.fsm.phase is TLPhase.TLDone
        assert result.final_state.current_order == 2
        assert [args["pr_number"] for name, args in transport.calls if name == "merge_pr"] == [
            101,
            102,
        ]
        merge_arguments = [args for name, args in transport.calls if name == "merge_pr"]
        approval_references = [args.get("aggregate_approval") for args in merge_arguments]
        assert [reference["candidate_key"] for reference in approval_references] == [
            "alpha",
            "beta",
        ]
        assert all(
            reference["parent_run_id"] == "ordered-e2e-run"
            and reference["parent_scope_path"] == ["ordered-e2e-run"]
            and reference["receipt_id"]
            for reference in approval_references
        )
        if result is resumed:
            assert result.final_state.integration.candidates[
                "alpha"
            ].active_aggregate_approval_id == (alpha_approval_id)
            assert (
                len(result.final_state.integration.candidates["alpha"].aggregate_approval_receipts)
                == 1
            )
        aggregate_titles = [
            str(args["title"])
            for name, args in transport.calls
            if name == "file_pr" and str(args.get("title", "")).startswith("Aggregate ")
        ]
        assert len(aggregate_titles) == 1
        assert "beta" in aggregate_titles[0]
        assert not any(name == "spawn_reviewer" for name, _ in transport.calls)
        assert not any(
            arguments.get("event_type") in {"tl.review_verdict", "tl.aggregate_review_accepted"}
            for name, arguments in transport.calls
            if name == "emit_controller_event"
        )
        assert (
            len(
                [
                    arguments
                    for name, arguments in transport.calls
                    if name == "emit_controller_event"
                    and arguments.get("event_type") == "tl.dispatch_confirmed"
                    and isinstance(arguments.get("payload"), Mapping)
                    and arguments["payload"].get("slice_id") == "beta"
                ]
            )
            == 1
        )
        assert all(
            result.final_state.slices[name].post_merge is not None
            and result.final_state.slices[name].post_merge.phase is PostMergePhase.COMPLETE
            for name in ("alpha", "beta")
        )
        retired_attempts = [
            arguments
            for name, arguments in transport.calls
            if name == "emit_controller_event"
            and arguments.get("event_type") == "tl.merge_attempt_retired"
        ]
        assert len(retired_attempts) == 1
        assert retired_attempts[0]["payload"]["slice_id"] == "alpha"
        assert result.final_state.integration.candidates["alpha"].merge_attempts == 2
    assert resumed.final_state.slices["alpha"].recovery is None
    assert any(
        arguments.get("event_type") == "tl.ordered_recovery_superseded"
        for name, arguments in replayed_transport.calls
        if name == "emit_controller_event"
    )
    assert (
        uninterrupted.final_state.current_order,
        uninterrupted.final_state.fsm.phase,
        uninterrupted.final_state.slices["alpha"].pr_number,
        uninterrupted.final_state.slices["beta"].pr_number,
    ) == (
        resumed.final_state.current_order,
        resumed.final_state.fsm.phase,
        resumed.final_state.slices["alpha"].pr_number,
        resumed.final_state.slices["beta"].pr_number,
    )
    assert len([name for name, _ in replayed_transport.calls if name == "file_pr"]) == 1
    for root in (uninterrupted_root, replayed_root):
        journal = EffectJournal(
            "ordered-e2e-run",
            RunStore("ordered-e2e-run", root).run_dir / "action-journal.json",
        )
        entries = journal.snapshot()
        rejected = [
            entry
            for entry in entries
            if entry.get("operation") == "merge_pr"
            and entry.get("target") == "alpha"
            and entry.get("status") == "rejected"
        ]
        successful = [
            entry
            for entry in entries
            if entry.get("operation") == "merge_pr"
            and entry.get("target") == "alpha"
            and entry.get("status") == "confirmed"
        ]
        assert len(rejected) == 1
        assert rejected[0]["error"] == "No Forgejo reviewer response yet on PR #101."
        assert rejected[0]["result"]["success"] is False
        assert len(successful) == 1
        assert successful[0]["arguments"]["attempt"] == 2
    resumed_again = run_tl_loop(
        "ordered-e2e-run",
        plan,
        SyntheticQueue([]),
        EffectClient(replayed_transport),
        config=replace(config, max_workers=1),
        root_dir=replayed_root,
    )
    assert resumed_again.final_state.fsm.phase is TLPhase.TLDone
    assert [args["pr_number"] for name, args in replayed_transport.calls if name == "merge_pr"] == [
        101,
        102,
    ]
    assert (
        len(
            [
                arguments
                for name, arguments in replayed_transport.calls
                if name == "file_pr" and str(arguments.get("title", "")).startswith("Aggregate ")
            ]
        )
        == 1
    )
    assert (
        sum(
            1
            for name, arguments in replayed_transport.calls
            if name == "emit_controller_event"
            and arguments.get("event_type") == "tl.dispatch_confirmed"
            and isinstance(arguments.get("payload"), Mapping)
            and arguments["payload"].get("slice_id") == "beta"
        )
        == 1
    )


def test_failed_ordered_sub_tl_blocks_higher_order_work(tmp_path: Path) -> None:
    failing_child = WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": "leaf-fails",
                    "task": "fail without a completion event",
                    "chainlink_issue_id": None,
                }
            ]
        }
    )
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "failed-stage",
                failing_child,
                source=SyntheticQueue([]),
                effects=EffectClient(RecordingTransport(reject_spawns=True)),
                order=1,
            ),
            SubTLTask("later-stage", WorkPlan(), source=SyntheticQueue([]), order=2),
        )
    )
    transport = RecordingTransport()

    result = run_tl_loop(
        "ordered-failure-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=TLLoopConfig(
            max_parallel_slices=1,
            max_workers=0,
            max_events=5,
            poll_interval=0.001,
        ),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLFailed
    assert result.final_state.slices["failed-stage"].status is SliceStatus.FAILED
    assert result.final_state.slices["later-stage"].status is SliceStatus.PENDING
    assert not any(
        arguments.get("name") == "later-stage"
        for name, arguments in transport.calls
        if name in {"spawn_leaf", "spawn_worker"}
    )


def test_nested_ordered_stages_round_trip_the_recursive_checkpoint(tmp_path: Path) -> None:
    nested = WorkPlan(
        sub_tls=(
            SubTLTask(
                "inner-one",
                WorkPlan(),
                source=SyntheticQueue([]),
                order=1,
                integration=IntegrationContract(aggregate_pr_required=False),
            ),
            SubTLTask(
                "inner-two",
                WorkPlan(),
                source=SyntheticQueue([]),
                order=2,
                integration=IntegrationContract(aggregate_pr_required=False),
            ),
        )
    )
    transport = RecordingTransport()

    result = run_tl_loop(
        "nested-ordered-run",
        WorkPlan(
            sub_tls=(
                SubTLTask(
                    "outer",
                    nested,
                    source=SyntheticQueue([]),
                    order=1,
                    integration=IntegrationContract(aggregate_pr_required=False),
                ),
            )
        ),
        SyntheticQueue([]),
        EffectClient(transport),
        config=TLLoopConfig(max_parallel_slices=2, poll_interval=0.001),
        root_dir=tmp_path,
    )

    child = load_state(tmp_path / "nested-ordered-run" / "outer" / "run.json")
    assert result.final_state.slices["outer"].status is SliceStatus.MERGED
    assert child.ordered_stages == (
        OrderedStageState(1, ("inner-one",)),
        OrderedStageState(2, ("inner-two",)),
    )
    assert child.slices["inner-one"].status is SliceStatus.MERGED
    assert child.slices["inner-two"].status is SliceStatus.MERGED


def test_ordered_sub_tl_restart_does_not_rerun_merged_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_tl_run(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del config, budgets
        calls.append(cast(str, cast(dict[str, object], root_spec)["run_id"]))
        return SimpleNamespace(
            final_state=SimpleNamespace(fsm=SimpleNamespace(phase=TLPhase.TLDone), slices={})
        )

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", fake_tl_run)
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "restart-a",
                WorkPlan(),
                order=1,
                integration=IntegrationContract(aggregate_pr_required=False),
            ),
            SubTLTask(
                "restart-b",
                WorkPlan(),
                order=1,
                integration=IntegrationContract(aggregate_pr_required=False),
            ),
        )
    )
    config = TLLoopConfig(max_parallel_slices=2, poll_interval=0.001)
    run_tl_loop(
        "restart-run",
        plan,
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=config,
        root_dir=tmp_path,
    )
    run_tl_loop(
        "restart-run",
        plan,
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=config,
        root_dir=tmp_path,
    )

    assert sorted(calls) == ["restart-a", "restart-b"]


def test_ordered_sub_tl_restart_adopts_terminal_child_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_dir = tmp_path / "adopt-run"
    run_tl_loop(
        "adopt-child",
        WorkPlan(),
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=_config(),
        root_dir=parent_dir,
    )

    def unexpected_child_run(root_spec: object, config: TLLoopConfig, budgets: object) -> object:
        del root_spec, config, budgets
        raise AssertionError("a terminal child checkpoint must be adopted")

    monkeypatch.setattr("tl_loop.loop.driver.tl_run", unexpected_child_run)
    result = run_tl_loop(
        "adopt-run",
        WorkPlan(
            sub_tls=(
                SubTLTask(
                    "adopt-child",
                    WorkPlan(),
                    order=1,
                    integration=IntegrationContract(aggregate_pr_required=False),
                ),
            )
        ),
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=_config(),
        root_dir=tmp_path,
    )

    assert result.final_state.slices["adopt-child"].status is SliceStatus.MERGED


def test_sub_tl_aggregate_pr_is_persisted_and_reused_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ordered_invocation(monkeypatch, "aggregate-child-invocation")
    transport = NamedRecordingTransport()
    child_plan = _plan()
    child_plan = replace(
        child_plan,
        leaves=(replace(child_plan.leaves[0], chainlink_issue_id=811),),
    )
    parent_dir = tmp_path / "aggregate-run"
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(transport),
        config=replace(
            _config(),
            branch="main.aggregate-child",
            parent_branch="main",
            parent_run_id="aggregate-run",
            depth=1,
            keep_alive_on_waiting=False,
        ),
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    child_state = child_store.load()
    child_slices = dict(child_state.slices)
    child_slices["leaf-a"] = replace(child_slices["leaf-a"], pr_number=45, reviewed_head="head-a")
    child_store.checkpoint(
        child_state.fsm,
        child_slices,
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    parent_plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "aggregate-child",
                child_plan,
                source=SyntheticQueue([]),
                agent_id="child-service",
                order=1,
                chainlink_issue_id=1052,
            ),
            SubTLTask("later-stage", WorkPlan(), source=SyntheticQueue([]), order=2),
        )
    )
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        keep_alive_on_waiting=False,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
        agent_id="root-controller",
    )

    first = run_tl_loop(
        "aggregate-run",
        parent_plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    second = run_tl_loop(
        "aggregate-run",
        parent_plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    file_pr_calls = [arguments for name, arguments in transport.calls if name == "file_pr"]
    assert len(file_pr_calls) == 1
    assert [
        (role, name) for role, name, tool_name in transport.identities if tool_name == "file_pr"
    ] == [("tl", "child-service")]
    assert file_pr_calls[0]["base_branch"] == "main"
    assert "Refs #811" in str(file_pr_calls[0]["body"])
    assert "Related PR: #45" in str(file_pr_calls[0]["body"])
    assert "Closes #811" not in str(file_pr_calls[0]["body"])
    assert "Refs #1052" not in str(file_pr_calls[0]["body"])
    assert isinstance(first.final_state.recursive_fsm, RecursiveTLRunning)
    assert isinstance(second.final_state.recursive_fsm, RecursiveTLRunning)
    parent_slice = second.final_state.slices["aggregate-child"]
    assert parent_slice.status is SliceStatus.IN_REVIEW, parent_slice.park_audit
    assert parent_slice.pr_number == 43
    assert parent_slice.dispatch_agent_id == "child-service"
    assert second.final_state.slices["later-stage"].status is SliceStatus.PENDING
    child = load_state(parent_dir / "aggregate-child" / "run.json")
    assert isinstance(child.recursive_fsm, RecursiveTLPRFiled)
    assert child.integration.aggregate_pr_number == parent_slice.pr_number
    assert child.integration.integration_owner_id == "aggregate-run:aggregate-child:integration"
    assert child.integration.integration_owner_run_id == "aggregate-child"
    assert child.integration.integration_owner_branch == "main.aggregate-child"
    assert child.integration.integration_owner_worktree
    candidate = second.final_state.integration.candidates["aggregate-child"]
    assert candidate.integration_owner_run_id == "aggregate-child"
    assert candidate.integration_owner_branch == "main.aggregate-child"
    assert candidate.integration_owner_worktree == child.integration.integration_owner_worktree


@pytest.mark.parametrize(
    ("references", "expected_lines"),
    [
        ((_LeafIssueProvenance(811, 45),), ("- Refs #811 · Related PR: #45",)),
        (
            (_LeafIssueProvenance(812, 46), _LeafIssueProvenance(811, 45)),
            (
                "- Refs #811 · Related PR: #45",
                "- Refs #812 · Related PR: #46",
            ),
        ),
        ((_LeafIssueProvenance(811, None),), ("- Refs #811",)),
        ((_LeafIssueProvenance(None, 45),), ("- Related PR: #45",)),
        ((), ()),
    ],
    ids=(
        "single-leaf",
        "sorted-multiple-leaves",
        "closed-issue-reference",
        "pr-only",
        "no-issues",
    ),
)
def test_aggregate_pr_body_preserves_non_closing_leaf_provenance(
    references: tuple[_LeafIssueProvenance, ...],
    expected_lines: tuple[str, ...],
) -> None:
    body = _aggregate_pr_body(
        "architecture-stage", "main", "owner", "head-sha", "digest", references
    )

    rendered_lines = tuple(line for line in body.splitlines() if line.startswith("- "))
    assert rendered_lines == expected_lines
    assert not any(keyword in body for keyword in ("Closes #", "Fixes #", "Resolves #"))
    if not references:
        assert "Leaf issue references:" not in body
        assert "Refs #" not in body


def test_aggregate_pr_title_and_body_escape_supplied_text_without_credentials() -> None:
    task_name = "stage`\nCloses #999"
    title = _aggregate_pr_title(task_name, "main`\nbranch")
    body = _aggregate_pr_body(task_name, "main", "owner", "head-sha", "patch-digest", ())

    assert "\n" not in title
    assert "``stage` Closes #999``" in body
    assert "ghp_test_secret" not in title + body
    assert "Authorization" not in title + body


def test_aggregate_file_pr_journal_replays_after_checkpoint_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "aggregate-crash-run"
    _install_ordered_invocation(monkeypatch, "aggregate-child-invocation")
    parent_dir = tmp_path / run_id
    child_plan = _plan()
    child_config = replace(
        _config(),
        branch="main.aggregate-child",
        parent_branch="main",
        parent_run_id=run_id,
        depth=1,
        keep_alive_on_waiting=False,
    )
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(RecordingTransport()),
        config=child_config,
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    child_state = child_store.load()
    child_slices = dict(child_state.slices)
    child_slices["leaf-a"] = replace(child_slices["leaf-a"], pr_number=42, reviewed_head="head-a")
    child_store.checkpoint(
        child_state.fsm,
        child_slices,
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    plan = WorkPlan(
        sub_tls=(SubTLTask("aggregate-child", child_plan, source=SyntheticQueue([]), order=1),)
    )
    config = replace(_config(), ledger_run_id=run_id, keep_alive_on_waiting=False)
    transport = RecordingTransport()
    original_set_ordered_state = RunStore.set_ordered_state

    def crash_after_file_pr(
        store: RunStore,
        current_order: int,
        ordered_stages: object,
        integration: object,
    ) -> RunState:
        if store.run_id == "aggregate-child":
            assert any(name == "file_pr" for name, _ in transport.calls)
            raise RuntimeError("simulated crash after file_pr")
        return original_set_ordered_state(store, current_order, ordered_stages, integration)

    monkeypatch.setattr(RunStore, "set_ordered_state", crash_after_file_pr)
    with pytest.raises(RuntimeError, match="simulated crash after file_pr"):
        run_tl_loop(
            run_id,
            plan,
            SyntheticQueue([]),
            EffectClient(transport),
            config=config,
            root_dir=tmp_path,
        )

    monkeypatch.setattr(RunStore, "set_ordered_state", original_set_ordered_state)
    parent_store = RunStore(run_id, tmp_path)
    journal = EffectJournal(run_id, parent_store.run_dir / "action-journal.json")
    assert len(journal.confirmed_entries("file_pr", "aggregate-child")) == 1

    resumed = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert len([name for name, _ in transport.calls if name == "file_pr"]) == 1
    assert resumed.final_state.slices["aggregate-child"].status is SliceStatus.IN_REVIEW
    assert resumed.final_state.slices["aggregate-child"].pr_number == 43
    assert len(journal.confirmed_entries("file_pr", "aggregate-child")) == 1


def test_aggregate_watcher_observation_replays_after_checkpoint_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PartialResponseTransport(RecordingTransport):
        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            title = str(arguments.get("title", ""))
            if tool_name == "file_pr" and title.startswith("Aggregate aggregate-child"):
                self.calls.append((tool_name, arguments))
                self.aggregate_reconciliation = _aggregate_watcher_fixture(
                    "aggregate-child-invocation"
                )
                return {"success": True, "result": {"pr_number": 43}}
            return super().call_tool(role, name, tool_name, arguments)

    run_id = "aggregate-observation-crash"
    _install_ordered_invocation(monkeypatch, "aggregate-child-invocation")
    transport = PartialResponseTransport()
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "aggregate-child",
                _plan(),
                source=SyntheticQueue(_lifecycle_events(run_id)),
                order=1,
            ),
            SubTLTask("later-stage", WorkPlan(), order=2),
        )
    )
    config = replace(
        _config(),
        ledger_run_id=run_id,
        keep_alive_on_waiting=False,
    )
    original_set_ordered_state = RunStore.set_ordered_state

    def crash_after_watcher_observation(
        store: RunStore,
        current_order: int,
        ordered_stages: object,
        integration: object,
    ) -> RunState:
        observed_watcher = any(name == "watcher_pr_state" for name, _ in transport.calls)
        if store.run_id == "aggregate-child" and observed_watcher:
            raise RuntimeError("simulated crash after watcher observation")
        return original_set_ordered_state(store, current_order, ordered_stages, integration)

    monkeypatch.setattr(RunStore, "set_ordered_state", crash_after_watcher_observation)
    with pytest.raises(RuntimeError, match="simulated crash after watcher observation"):
        run_tl_loop(
            run_id,
            plan,
            SyntheticQueue([]),
            EffectClient(transport),
            config=config,
            root_dir=tmp_path,
        )

    initial_leaf_effects = sum(
        name in {"spawn_worker", "spawn_leaf"} for name, _ in transport.calls
    )
    monkeypatch.setattr(RunStore, "set_ordered_state", original_set_ordered_state)
    for _ in range(2):
        resumed = run_tl_loop(
            run_id,
            plan,
            SyntheticQueue([]),
            EffectClient(transport),
            config=config,
            root_dir=tmp_path,
        )
        aggregate = resumed.final_state.slices["aggregate-child"]
        assert aggregate.status is SliceStatus.IN_REVIEW
        assert aggregate.pr_number == 43
        assert resumed.final_state.slices["later-stage"].status is SliceStatus.PENDING

    assert sum(name == "file_pr" for name, _ in transport.calls) == 1
    assert (
        sum(name in {"spawn_worker", "spawn_leaf"} for name, _ in transport.calls)
        == initial_leaf_effects
    )


def test_aggregate_file_pr_result_decoder_requires_complete_correlation() -> None:
    complete, missing = _aggregate_candidate_from_result(
        "aggregate-child",
        {
            "pr_number": 43,
            "head_sha": "head-a",
            "base_sha": "base-a",
            "patch_digest": "patch-a",
            "head_branch": "main.aggregate-child",
            "base_branch": "main",
            "slice_id": "aggregate-child",
            "invocation_id": "aggregate-child-invocation",
            "author_agent": "aggregate-child",
        },
        "main.aggregate-child",
        "main",
        "aggregate-child",
        "aggregate-child-invocation",
    )

    assert complete is not None
    assert complete.pr_number == 43
    assert complete.original_base_sha == "base-a"
    assert missing == ()

    partial, missing = _aggregate_candidate_from_result(
        "aggregate-child",
        {"pr_number": 43, "head_sha": "head-a"},
        "main.aggregate-child",
        "main",
        "aggregate-child",
        "aggregate-child-invocation",
    )

    assert partial is None
    assert missing == (
        "base_sha",
        "patch_digest",
        "head_branch",
        "base_branch",
        "slice_id",
        "invocation_id",
        "author_agent",
    )


@pytest.mark.parametrize("response_failed", [False, True])
def test_partial_aggregate_file_pr_response_adopts_watcher_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_failed: bool,
) -> None:
    class PartialResponseTransport(RecordingTransport):
        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            title = str(arguments.get("title", ""))
            if tool_name == "file_pr" and title.startswith("Aggregate aggregate-child"):
                self.calls.append((tool_name, arguments))
                self.aggregate_reconciliation = _aggregate_watcher_fixture(
                    "aggregate-child-invocation"
                )
                if response_failed:
                    return {
                        "success": False,
                        "error": "publication response was lost",
                        "error_kind": "internal",
                    }
                return {"success": True, "result": {"pr_number": 43}}
            return super().call_tool(role, name, tool_name, arguments)

    run_id = "aggregate-partial-response"
    _install_ordered_invocation(monkeypatch, "aggregate-child-invocation")
    transport = PartialResponseTransport()
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "aggregate-child",
                _plan(),
                source=SyntheticQueue(_lifecycle_events(run_id)),
                order=1,
            ),
        )
    )
    config = replace(
        _config(),
        ledger_run_id=run_id,
        keep_alive_on_waiting=False,
    )

    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert result.final_state.slices["aggregate-child"].status is SliceStatus.IN_REVIEW, (
        result.final_state.slices["aggregate-child"].park_audit
    )
    assert result.final_state.slices["aggregate-child"].pr_number == 43
    assert (
        len(
            [
                arguments
                for name, arguments in transport.calls
                if name == "file_pr" and str(arguments.get("title", "")).startswith("Aggregate ")
            ]
        )
        == 1
    )


def test_aggregate_publication_rehydrates_durable_invocation_before_file_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = driver_module._prepare_sub_tl_stage

    def prepare(tasks, state, config, source, effects, store, effects_log, order):
        state, prepared = original(tasks, state, config, source, effects, store, effects_log, order)
        invocation_path = (
            Path(config.project_root) / ".exo" / "agents" / "aggregate-child" / "invocation.json"
        )
        invocation_path.parent.mkdir(parents=True, exist_ok=True)
        worktree = str(
            driver_module._sub_tl_worktree(config, store.root_dir, store.run_id, tasks[0])
        )
        invocation_path.write_text(
            json.dumps(
                {
                    "invocation_id": "aggregate-child-invocation",
                    "trigger": "spawn",
                    "slice_id": "aggregate-child",
                    "branch": "main.aggregate-child",
                    "worktree": worktree,
                }
            ),
            encoding="utf-8",
        )
        return state, prepared

    monkeypatch.setattr(driver_module, "_prepare_sub_tl_stage", prepare)
    run_id = "aggregate-invocation-rehydration"
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "aggregate-child",
                _plan(),
                source=SyntheticQueue(_lifecycle_events(run_id)),
                order=1,
            ),
        )
    )
    config = replace(
        _config(),
        project_root=tmp_path,
        ledger_run_id=run_id,
        keep_alive_on_waiting=False,
    )
    transport = RecordingTransport()

    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert result.final_state.slices["aggregate-child"].status is SliceStatus.IN_REVIEW
    assert (
        result.final_state.slices["aggregate-child"].dispatch_invocation_id
        == "aggregate-child-invocation"
    )
    assert any(name == "file_pr" for name, _ in transport.calls)


def test_unrecoverable_aggregate_evidence_parks_with_missing_fields(
    tmp_path: Path,
) -> None:
    class PartialResponseTransport(RecordingTransport):
        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            title = str(arguments.get("title", ""))
            if tool_name == "file_pr" and title.startswith("Aggregate aggregate-child"):
                self.calls.append((tool_name, arguments))
                return {"success": True, "result": {"pr_number": 43}}
            return super().call_tool(role, name, tool_name, arguments)

    run_id = "aggregate-evidence-park"
    transport = PartialResponseTransport()
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "aggregate-child",
                _plan(),
                source=SyntheticQueue(_lifecycle_events(run_id)),
                order=1,
            ),
        )
    )
    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=replace(_config(), ledger_run_id=run_id, keep_alive_on_waiting=False),
        root_dir=tmp_path,
    )

    parked = result.final_state.slices["aggregate-child"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.PUBLICATION_BLOCKED
    assert parked.park_audit is not None
    assert parked.park_audit["retryable"] is True
    assert parked.park_audit["missing_evidence"] == [
        "head_sha",
        "base_sha",
        "patch_digest",
        "head_branch",
        "base_branch",
        "slice_id",
        "invocation_id",
        "author_agent",
    ]
    assert result.final_state.current_order == 1


def test_missing_durable_invocation_parks_before_file_pr(
    tmp_path: Path,
) -> None:
    run_id = "aggregate-missing-invocation"
    transport = RecordingTransport()
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "aggregate-child",
                _plan(),
                source=SyntheticQueue(_lifecycle_events(run_id)),
                order=1,
            ),
        )
    )
    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=replace(
            _config(),
            project_root=tmp_path,
            ledger_run_id=run_id,
            keep_alive_on_waiting=False,
        ),
        root_dir=tmp_path,
    )

    parked = result.final_state.slices["aggregate-child"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.PUBLICATION_BLOCKED
    assert parked.park_audit is not None
    assert parked.park_audit["error_kind"] == "missing_invocation_registration"
    assert parked.park_audit["missing_evidence"] == [
        "invocation_id",
        "slice_id",
        "head_branch",
    ]
    assert not any(name == "file_pr" for name, _ in transport.calls)


@pytest.mark.parametrize("publication_available", [True, False])
def test_aggregate_file_pr_timeout_continue_reconciles_without_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_available: bool,
) -> None:
    class CrashAfterPublishTransport(RecordingTransport):
        crashed = False

        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            title = str(arguments.get("title", ""))
            if (
                tool_name == "file_pr"
                and title.startswith("Aggregate aggregate-child")
                and not self.crashed
            ):
                self.calls.append((tool_name, arguments))
                self.aggregate_reconciliation = _aggregate_watcher_fixture(
                    "aggregate-child-invocation"
                )
                self.crashed = True
                raise SystemExit("simulated controller crash after Forgejo publication")
            return super().call_tool(role, name, tool_name, arguments)

    run_id = "aggregate-timeout-restart"
    _install_ordered_invocation(monkeypatch, "aggregate-child-invocation")
    transport = CrashAfterPublishTransport()
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "aggregate-child",
                _plan(),
                source=SyntheticQueue(_lifecycle_events(run_id)),
                order=1,
            ),
        )
    )
    config = replace(
        _config(),
        ledger_run_id=run_id,
        keep_alive_on_waiting=False,
    )
    with pytest.raises(SystemExit, match="simulated controller crash"):
        run_tl_loop(
            run_id,
            plan,
            SyntheticQueue([]),
            EffectClient(transport),
            config=config,
            root_dir=tmp_path,
        )

    if not publication_available:
        transport.aggregate_reconciliation = None
    child_store = RunStore("aggregate-child", tmp_path / run_id)
    child_revision = child_store.load().revision
    resumed = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    if publication_available:
        assert resumed.final_state.slices["aggregate-child"].status is SliceStatus.IN_REVIEW
        assert resumed.final_state.slices["aggregate-child"].pr_number == 43
    else:
        parked = resumed.final_state.slices["aggregate-child"]
        assert parked.status is SliceStatus.PARKED
        assert parked.park_cause is ParkCause.PUBLICATION_BLOCKED
        assert parked.park_audit is not None
        assert parked.park_audit["error_kind"] == "prior_file_pr_outcome_unresolved"
    assert child_store.load().revision == child_revision
    assert (
        len(
            [
                arguments
                for name, arguments in transport.calls
                if name == "file_pr" and str(arguments.get("title", "")).startswith("Aggregate ")
            ]
        )
        == 1
    )


def test_plan_reconciliation_revision_and_aggregate_adoption_preserve_ordered_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "root"
    architecture = replace(_plan(), workers=())
    architecture = replace(
        architecture,
        leaves=(
            replace(
                architecture.leaves[0],
                task="accepted architecture task",
                chainlink_issue_id=None,
            ),
        ),
    )
    future_issue_ids = (814, 812, 813, 815)
    future_names = tuple(f"future-stage-{index}" for index in range(1, 5))
    accepted_future_plans = tuple(
        WorkPlan.from_mapping(
            {
                "leaves": [
                    {
                        "name": "future-leaf",
                        "task": f"unchanged future task {index}",
                        "chainlink_issue_id": None,
                    }
                ]
            }
        )
        for index in range(1, 5)
    )
    stages = (
        SubTLTask(
            "architecture",
            architecture,
            source=SyntheticQueue(_lifecycle_events(run_id)),
            order=1,
        ),
        *tuple(
            SubTLTask(name, child_plan, source=SyntheticQueue([]), order=index + 2)
            for index, (name, child_plan) in enumerate(
                zip(future_names, accepted_future_plans, strict=True)
            )
        ),
    )
    accepted_plan = WorkPlan(sub_tls=stages)
    candidate_stages = (
        stages[0],
        *tuple(
            replace(
                stage,
                plan=replace(
                    stage.plan,
                    leaves=(
                        replace(
                            stage.plan.leaves[0],
                            chainlink_issue_id=issue_id,
                        ),
                    ),
                ),
            )
            for stage, issue_id in zip(stages[1:], future_issue_ids, strict=True)
        ),
    )
    candidate_plan = WorkPlan(sub_tls=candidate_stages)

    def public_plan_document(plan: WorkPlan) -> dict[str, object]:
        canonical = dict(driver_module._manifest_plan(plan))
        for leaf in cast(list[dict[str, object]], canonical["leaves"]):
            leaf.setdefault("chainlink_issue_id", None)
        canonical["sub_tls"] = [
            {
                "name": stage.name,
                "order": stage.order,
                **({"agent_id": stage.agent_id} if stage.agent_id is not None else {}),
                "plan": public_plan_document(
                    stage.plan
                    if isinstance(stage.plan, WorkPlan)
                    else WorkPlan.from_mapping(stage.plan)
                ),
            }
            for stage in plan.sub_tls
        ]
        return canonical

    def plan_bytes(plan: WorkPlan) -> bytes:
        return json.dumps(
            {"run_id": run_id, "plan": public_plan_document(plan)},
            sort_keys=True,
        ).encode()

    state_root = tmp_path / ".exo" / "tl-loop"
    state_root.mkdir(parents=True)
    accepted_bytes = plan_bytes(accepted_plan)
    (state_root / "plan.json").write_bytes(accepted_bytes)
    (state_root / "plan.snapshot").write_bytes(accepted_bytes)
    (state_root / "plan.snapshot.sha256").write_text(
        hashlib.sha256(accepted_bytes).hexdigest() + "\n",
        encoding="ascii",
    )

    invocation_id = "architecture-invocation"
    created_invocation_scopes: list[str] = []
    original_prepare = driver_module._prepare_sub_tl_stage

    def prepare(tasks, state, config, source, effects, store, effects_log, order):
        state, prepared = original_prepare(
            tasks, state, config, source, effects, store, effects_log, order
        )
        invocation_path = (
            Path(config.project_root) / ".exo" / "agents" / tasks[0].name / "invocation.json"
        )
        if not invocation_path.exists():
            stage_invocation_id = f"{tasks[0].name}-invocation"
            _checkpoint_ordered_sub_tl_invocation(store, tasks[0].name, stage_invocation_id)
            worktree = str(
                driver_module._sub_tl_worktree(config, store.root_dir, store.run_id, tasks[0])
            )
            created_invocation_scopes.append(tasks[0].name)
            invocation_path.parent.mkdir(parents=True, exist_ok=True)
            invocation_path.write_text(
                json.dumps(
                    {
                        "invocation_id": stage_invocation_id,
                        "runtime_agent_id": tasks[0].agent_id or tasks[0].name,
                        "trigger": "spawn",
                        "slice_id": tasks[0].name,
                        "branch": f"main.{tasks[0].name}",
                        "worktree": worktree,
                    }
                ),
                encoding="utf-8",
            )
        return store.load(), prepared

    monkeypatch.setattr(driver_module, "_prepare_sub_tl_stage", prepare)

    class CrashAfterAggregatePublication(RecordingTransport):
        def call_tool(self, role, name, tool_name, arguments):
            if tool_name == "post_merge_changelog_context":
                self.calls.append((tool_name, arguments))
                return {"success": True, "result": {"changelog_present": False}}
            if tool_name in {"spawn_leaf", "spawn_worker"}:
                self.calls.append((tool_name, arguments))
                agent_id = str(arguments["name"])
                return {
                    "success": True,
                    "result": {
                        "agent_id": agent_id,
                        "invocation_id": f"{agent_id}-invocation",
                    },
                }
            title = str(arguments.get("title", ""))
            if tool_name == "file_pr" and title.startswith("Aggregate architecture into "):
                self.calls.append((tool_name, arguments))
                self.aggregate_reconciliation = _aggregate_watcher_fixture(
                    invocation_id,
                    slice_id="architecture",
                    author_agent="architecture",
                    head_branch="main.architecture",
                )
                aggregate_head, aggregate_tree = self.aggregate_git_evidence["architecture"]
                self.aggregate_reconciliation["watcher"]["head_sha"] = aggregate_head
                self.aggregate_reconciliation["watcher"]["pr_head_tree_sha"] = aggregate_tree
                self.aggregate_reconciliation["head_sha"] = aggregate_head
                raise SystemExit("simulated controller crash after aggregate publication")
            return super().call_tool(role, name, tool_name, arguments)

    transport = CrashAfterAggregatePublication()
    config = replace(
        _config(),
        root_dir=state_root,
        project_root=tmp_path,
        ledger_run_id=run_id,
        keep_alive_on_waiting=False,
        policy=validate_policy(_selector_policy()),
        tl_model_choice=_tl_choice(ReviewBackend([])),
        agent_id=run_id,
        capabilities=CapabilityMap(
            {"codex/gpt-luna": Difficulty.STANDARD, "claude/sonnet": Difficulty.HARD}
        ),
        catalog=ModelCatalog.from_fixture(
            Path(__file__).parent / "fixtures" / "model_catalog.json"
        ),
    )
    repository_path = Path(driver_module._sub_tl_worktree(config, state_root, run_id, stages[0]))
    repository_base = _test_git_repository(repository_path)
    reviewed_commit = _test_git_commit(
        repository_path, "issue-811.txt", "reviewed architecture work\n", "PR 45 merge"
    )
    changelog_commit = _test_git_commit(
        repository_path, "CHANGELOG.md", "issue 811 completed\n", "issue 811 changelog"
    )
    push_commit = _test_git_commit(
        repository_path, "issue-811-bookkeeping.txt", "bookkeeping\n", "issue 811 push"
    )
    aggregate_head = _test_git_commit(
        repository_path, "aggregate.txt", "architecture aggregate\n", "aggregate PR 46 head"
    )
    aggregate_tree = _test_git_tree(repository_path, aggregate_head)
    transport.aggregate_git_evidence["architecture"] = (aggregate_head, aggregate_tree)
    with pytest.raises(SystemExit, match="after aggregate publication"):
        run_tl_loop(
            run_id,
            accepted_plan,
            SyntheticQueue([]),
            EffectClient(transport),
            config=config,
            root_dir=state_root,
        )

    root_store = RunStore(run_id, state_root)
    child_store = RunStore("architecture", root_store.run_dir)
    child_state = child_store.load()
    completed_leaf = child_state.slices["leaf-a"]
    assert completed_leaf.chainlink_issue_id is None
    assert isinstance(child_state.recursive_fsm, RecursiveTLAllMerged)
    receipt = PostMergeState(
        PostMergePhase.COMPLETE,
        {
            "child_id": "leaf-a",
            "repository": "https://forge.example.com/org/repo.git",
            "parent_branch": "main.architecture",
            "pr_number": "45",
            "head_sha": reviewed_commit,
            "merge_journal_id": "merge-45",
            "lane_epoch": "1",
            "parent_commit_sha": reviewed_commit,
            "issue_id": "811",
            "issue_close_intent_id": "close-811",
            "issue_close_journal_id": "chainlink-close:811",
            "forgejo_head_sha": reviewed_commit,
            "forgejo_merged": "true",
            "forgejo_pr_number": "45",
            "changelog_intent_id": "changelog-811",
            "changelog_generation": "1",
            "changelog_commit_sha": changelog_commit,
            "parent_push_intent_id": "push-811",
            "push_journal_id": "push-journal-811",
            "expected_base_sha": repository_base,
            "push_receipt_id": "receipt-811",
            "pushed_commit": push_commit,
            "bookkeeping_commit": push_commit,
            "observed_remote_head": push_commit,
            "ancestry_proof": f"ancestor:{push_commit}->{push_commit}",
        },
    )
    leaf_publication = PublicationBinding(
        pr_number=45,
        head_sha=reviewed_commit,
        head_branch=completed_leaf.branch or "main.architecture.leaf-a",
        base_branch="main.architecture",
        attempt=1,
        invocation_id="leaf-811-publication-invocation",
    )
    leaf_handoff = HandoffEvidence(
        pr_number=45,
        head_sha=reviewed_commit,
        attempt=1,
        invocation_id="leaf-811-publication-invocation",
        agent_id=completed_leaf.dispatch_agent_id or "leaf-a",
        observed_at="2026-10-13T00:00:00Z",
    )
    completed_leaf = replace(
        completed_leaf,
        status=SliceStatus.MERGED,
        pr_number=45,
        reviewed_head=reviewed_commit,
        verdict=Verdict.GO,
        review_patch_digests={reviewed_commit: "leaf-811-patch"},
        ci_state={reviewed_commit: "success"},
        publication=leaf_publication,
        handoff=leaf_handoff,
        chainlink_issue_id=811,
        dispatch_invocation_id=None,
        dispatch_authoritative_event_seq=2,
        post_merge=receipt,
        park_cause=None,
        park_issue_id=None,
        park_audit=None,
    )
    merged_fsm = replace(
        child_state.recursive_fsm,
        completed_children={
            **child_state.recursive_fsm.completed_children,
            "leaf-a": receipt,
        },
    )
    child_store.checkpoint(
        merged_fsm,
        {
            **child_state.slices,
            "leaf-a": replace(completed_leaf, chainlink_issue_id=None),
        },
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    assert child_store.load().slices["leaf-a"].post_merge == receipt
    legacy_leaf = child_store.load().slices["leaf-a"]
    legacy_journal_path = child_store.run_dir / "action-journal.json"
    legacy_journal = json.loads(legacy_journal_path.read_text(encoding="utf-8"))
    spawn_record = next(
        row
        for row in legacy_journal
        if row.get("operation") == "spawn_leaf" and row.get("target") == "leaf-a"
    )
    spawn_result = spawn_record["result"]["result"]
    spawn_result.pop("agent_id", None)
    spawn_result["invocation_id"] = None
    spawn_result["branch_name"] = legacy_leaf.branch
    spawn_result["worktree_path"] = str(tmp_path / ".exo" / "worktrees" / "leaf-a-opencode")
    legacy_journal_path.write_text(
        json.dumps(legacy_journal, sort_keys=True),
        encoding="utf-8",
    )
    assert child_store.load().ledger_run_id is not None
    ledger_segments = tmp_path / ".exo" / "ledger" / "segments"
    ledger_segments.mkdir(parents=True)
    dispatch_event = {
        "agent_id": "architecture",
        "data": {
            "branch": legacy_leaf.branch,
            "child_agent": legacy_leaf.dispatch_agent_id,
            "intent_id": legacy_leaf.dispatch_intent_id,
        },
        "invocation_id": None,
        "lifecycle_state": "observed",
        "observed_at": "2026-10-13T00:00:00Z",
        "run_id": child_store.load().ledger_run_id,
        "run_seq": legacy_leaf.dispatch_authoritative_event_seq,
        "type": "agent.spawned",
    }
    (ledger_segments / "segment-000000000001.jsonl").write_text(
        json.dumps(dispatch_event, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    close_count = sum(
        name == "chainlink_issue_close" and str(args.get("issue_id")) == "811"
        for name, args in transport.calls
    )
    assert close_count == 0
    completed_leaf_effect_count = sum(
        name in {"spawn_worker", "spawn_leaf"} for name, _ in transport.calls
    )

    drifted_architecture = replace(
        architecture,
        leaves=(
            replace(
                architecture.leaves[0],
                task="rewritten completed task",
                chainlink_issue_id=811,
            ),
        ),
    )
    drifted_plan = WorkPlan(
        sub_tls=(replace(stages[0], plan=drifted_architecture), *candidate_stages[1:])
    )
    (state_root / "plan.json").write_bytes(plan_bytes(drifted_plan))
    drifted_manifest = driver_module._manifest_for_plan(
        drifted_plan,
        run_id,
        replace(config, plan_revision=1),
    )

    # Reproduce the live parked aggregate and legacy root declaration while
    # retaining accepted FSM digests and dispatch records.
    root_state = root_store.load()
    blocked_stage = replace(
        root_state.slices["architecture"],
        status=SliceStatus.PARKED,
        attempts=0,
        park_cause=ParkCause.PUBLICATION_BLOCKED,
        park_issue_id=None,
        park_audit={
            "error_kind": "publication_evidence_incomplete",
            "reason": "existing aggregate publication awaits reconciliation",
            "missing_evidence": ["durable_invocation_correlation"],
            "retryable": True,
        },
    )
    root_store.checkpoint(
        root_state.recursive_fsm,
        {**root_state.slices, "architecture": blocked_stage},
        root_state.budgets,
        root_state.events.last_consumed_offset,
        current_order=root_state.current_order,
        ordered_stages=root_state.ordered_stages,
        integration=root_state.integration,
    )
    root_document = json.loads(root_store.path.read_text(encoding="utf-8"))
    root_manifest_document = cast(JsonObject, drifted_manifest.to_document())
    architecture_node = next(
        node
        for node in cast(list[JsonObject], root_manifest_document["nodes"])
        if node["name"] == "architecture"
    )
    cast(JsonObject, architecture_node["declaration"]).pop("order_explicit", None)
    integration_contract = cast(JsonObject, architecture_node["integration_contract"])
    for field_name in (
        "leaf_review_owner",
        "aggregate_review_owner",
        "aggregate_repair_owner",
    ):
        integration_contract.pop(field_name, None)
    root_manifest_document.pop("digest", None)
    root_document["plan_manifest"] = PlanManifest.from_document(
        root_manifest_document
    ).to_document()
    root_store.path.write_text(json.dumps(root_document), encoding="utf-8")
    child_document = json.loads(child_store.path.read_text(encoding="utf-8"))
    child_manifest = drifted_manifest.child_manifests["root/sub_tl/architecture"]
    child_document["plan_manifest"] = child_manifest.to_document()
    child_document["slices"]["leaf-a"]["chainlink_issue_id"] = 811
    child_document["slices"]["leaf-a"]["dispatch_authoritative_event_seq"] = 2
    child_store.path.write_text(json.dumps(child_document), encoding="utf-8")
    root_store.set_gate(
        driver_module._aggregate_publication_gate_name("architecture"),
        GateStatus.PENDING,
    )
    action_journal = child_store.run_dir / "action-journal.json"
    action_journal_before_reconciliation = action_journal.read_bytes()

    reconciliation = analyze_plan_reconciliation(tmp_path)
    assert reconciliation.can_apply, [
        (
            scope["scope_id"],
            [
                (proof["reason_code"], proof["evidence"])
                for proof in scope["protected_declaration_proofs"]
            ],
        )
        for scope in reconciliation.to_document()["scopes"]
    ]
    reconciliation_document = reconciliation.to_document()
    root_scope_report = next(
        scope for scope in reconciliation_document["scopes"] if scope["scope_id"] == "root"
    )
    legacy_proof = root_scope_report["legacy_default_equivalences"][0]
    assert set(legacy_proof["fields"]) == {
        "declaration.order_explicit",
        "integration_contract.aggregate_repair_owner",
        "integration_contract.aggregate_review_owner",
        "integration_contract.leaf_review_owner",
    }
    child_scope_report = next(
        scope
        for scope in reconciliation_document["scopes"]
        if scope["scope_id"] == "root/sub_tl/architecture"
    )
    declaration_proof = child_scope_report["protected_declaration_proofs"][0]
    assert declaration_proof["fields"] == ["chainlink_issue_id", "task"]
    assert declaration_proof["disposition"] == "proven"
    assert "completed_issue_closure_receipt" in declaration_proof["evidence"]
    assert declaration_proof["correlation"]["dispatch_invocation_id"] is None
    assert declaration_proof["correlation"]["worktree_paths_match"] is False
    assert child_scope_report["scope_emitter_proof"]["correlation"]["parent_dispatch_attempts"] == 0
    assert (
        child_scope_report["scope_emitter_proof"]["correlation"][
            "parent_dispatch_attempt_condition"
        ]
        == "zero_with_complete_dispatch_proof"
    )
    applied_reconciliation = apply_plan_reconciliation(
        tmp_path,
        "Restore accepted plan bindings before updating future issue metadata",
        reconciliation.evidence_fingerprint,
        actor="operator",
    )
    assert applied_reconciliation.applied
    assert root_store.load().slices["architecture"].attempts == 0
    assert root_store.load().plan_manifest is not None
    assert (
        root_store.load().plan_manifest.digest
        == driver_module._manifest_for_plan(accepted_plan, run_id, config).digest
    )
    restored_leaf = child_store.load().slices["leaf-a"]
    assert restored_leaf.status is SliceStatus.MERGED
    assert restored_leaf.dispatch_invocation_id is None
    assert restored_leaf.chainlink_issue_id is None
    assert restored_leaf.pr_number == 45
    assert restored_leaf.post_merge == receipt
    assert restored_leaf.publication == leaf_publication
    assert restored_leaf.handoff == leaf_handoff
    assert restored_leaf.verdict is Verdict.GO
    assert action_journal.read_bytes() == action_journal_before_reconciliation

    # Only the future leaf metadata is proposed after history is restored.
    (state_root / "plan.json").write_bytes(plan_bytes(candidate_plan))

    revision = apply_plan_revision(
        tmp_path,
        "Bind Chainlink issues to future planned leaves",
        actor="operator",
    )
    assert revision.applied
    assert child_store.load().slices["leaf-a"].publication == leaf_publication
    assert child_store.load().slices["leaf-a"].handoff == leaf_handoff
    assert child_store.load().slices["leaf-a"].verdict is Verdict.GO
    revised_root = root_store.load()
    assert revised_root.plan_manifest is not None
    for stage, issue_id in zip(candidate_stages[1:], future_issue_ids, strict=True):
        stage_node = revised_root.plan_manifest.node(f"root/sub_tl/{stage.name}")
        child_manifest = revised_root.plan_manifest.child_manifests[stage_node.node_id]
        leaf = child_manifest.nodes[0]
        assert leaf.chainlink_issue_id == issue_id
        assert leaf.task == f"unchanged future task {future_names.index(stage.name) + 1}"

    # Match the historical Beast identity shape immediately before resume:
    # the root has no identity key and its completed child has the old
    # five-field identity while the authoritative URL remains unchanged.
    root_document = json.loads(root_store.path.read_text(encoding="utf-8"))
    root_document["version"] = 4
    root_document.pop("repository_identity", None)
    root_store.path.write_text(json.dumps(root_document), encoding="utf-8")
    child_document = json.loads(child_store.path.read_text(encoding="utf-8"))
    child_document["version"] = 4
    child_document["repository_identity"] = {
        "owner": "forge-example",
        "repo": "repository",
        "base_branch": "main",
        "forge_host": "forge.example.com",
        "remote_url": "https://forge.example.com/org/repo.git",
    }
    child_store.path.write_text(json.dumps(child_document), encoding="utf-8")

    resumed_config = replace(
        config,
        session_mode="continue",
        plan_revision=revised_root.plan_manifest.manifest_revision,
    )
    adopted = run_tl_loop(
        run_id,
        candidate_plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=resumed_config,
        root_dir=state_root,
    )
    architecture_slice = adopted.final_state.slices["architecture"]
    assert adopted.final_state.repository_identity == RepositoryIdentity(
        "https://forge.example.com/org/repo.git"
    )
    assert adopted.final_state.repository_identity_migration is not None
    adopted_child = child_store.load()
    assert adopted_child.version == SCHEMA_VERSION
    assert adopted_child.repository_identity == adopted.final_state.repository_identity
    assert adopted_child.repository_identity_migration is not None
    assert architecture_slice.status is SliceStatus.IN_REVIEW
    assert architecture_slice.pr_number == 43
    assert architecture_slice.dispatch_invocation_id == invocation_id
    aggregate_candidate = adopted.final_state.integration.candidates["architecture"]
    assert aggregate_candidate.aggregate_pr_number == 43
    assert aggregate_candidate.aggregate_head_sha == aggregate_head
    assert aggregate_candidate.aggregate_original_base_sha == "base-a"
    assert aggregate_candidate.aggregate_patch_digest == "patch-a"
    assert aggregate_candidate.integration_owner_id == "root:architecture:integration"
    assert aggregate_candidate.integration_owner_run_id == "architecture"
    assert aggregate_candidate.integration_owner_branch == "main.architecture"
    assert transport.aggregate_reconciliation is not None
    publication = cast(JsonObject, transport.aggregate_reconciliation["publication"])
    assert publication["invocation_id"] == invocation_id
    assert publication["author_agent"] == "architecture"
    assert not any(
        gate.name == driver_module._aggregate_publication_gate_name("architecture")
        for gate in adopted.final_state.gates
    )
    assert adopted.final_state.current_order == 1
    assert all(
        adopted.final_state.slices[name].status is SliceStatus.PENDING for name in future_names
    )
    assert sum(name == "file_pr" for name, _ in transport.calls) == 1
    assert (
        sum(name in {"spawn_worker", "spawn_leaf"} for name, _ in transport.calls)
        == completed_leaf_effect_count
    )
    assert (
        sum(
            name == "chainlink_issue_close" and str(args.get("issue_id")) == "811"
            for name, args in transport.calls
        )
        == close_count
    )
    assert child_store.load().slices["leaf-a"].post_merge == completed_leaf.post_merge

    root_state = root_store.load()
    readiness_candidate = driver_module._aggregate_candidate_from_state(
        "architecture", root_state.integration.candidates.get("architecture")
    )
    assert readiness_candidate is not None
    assert driver_module._child_handoff_ready(child_store.load(), run_id), (
        child_store.load().recursive_fsm
    )
    assert (
        driver_module._verified_leaf_merge_receipt(child_store.load().slices["leaf-a"]) is not None
    )
    assert (
        driver_module._verified_child_completion_evidence(
            candidate_plan.sub_tls[0],
            child_store.load(),
            readiness_candidate,
            watcher=cast(JsonObject, transport.aggregate_reconciliation["watcher"]),
            repository=repository_path,
        )
        is not None
    )
    waiting_for_review = root_state.slices["architecture"]
    assert waiting_for_review.verdict is None
    assert waiting_for_review.ci_state.get(aggregate_head) != "success"
    assert not any(name == "merge_pr" for name, _ in transport.calls)

    successful_snapshot = {
        "head_sha": aggregate_head,
        "base_sha": "base-a",
        "patch_digest": "patch-a",
        "merge_tree_sha": "tree-a",
        "pr_head_tree_sha": aggregate_tree,
        "ci_status": "success",
    }

    class ReconciledIntegrationTransport(IntegrationTransport):
        def call_tool(self, role, name, tool_name, arguments):
            if tool_name == "post_merge_changelog_context":
                self.calls.append((tool_name, arguments))
                return {"success": True, "result": {"changelog_present": False}}
            if tool_name == "watcher_pr_state":
                self.calls.append((tool_name, arguments))
                snapshot = self.snapshots[0]
                fixture = cast(JsonObject, self.aggregate_reconciliation)
                watcher = cast(JsonObject, fixture["watcher"])
                return {"success": True, "result": {**watcher, **snapshot}}
            return super().call_tool(role, name, tool_name, arguments)

    integration_transport = ReconciledIntegrationTransport(
        snapshots=[dict(successful_snapshot), dict(successful_snapshot)],
        aggregate_reconciliation=transport.aggregate_reconciliation,
        merge_response={"success": True, "result": {"merged": True}},
    )
    integrated = run_tl_loop(
        run_id,
        candidate_plan,
        SyntheticQueue([]),
        EffectClient(integration_transport),
        config=resumed_config,
        root_dir=state_root,
    )

    assert integrated.final_state.slices["architecture"].status is SliceStatus.MERGED
    assert integrated.final_state.slices["architecture"].verdict is None
    assert integrated.final_state.slices["architecture"].reviewed_head is None
    assert integrated.final_state.integration.candidates["architecture"].lifecycle is (
        IntegrationLifecycle.MERGED
    )
    assert not any(
        "review" in name.lower() and name != "review_pr" for name, _ in integration_transport.calls
    )
    root_state = root_store.load()
    assert root_state.slices["architecture"].post_merge is not None
    assert root_state.slices["architecture"].post_merge.phase is PostMergePhase.COMPLETE
    assert root_state.current_order == 2
    assert root_state.slices[future_names[0]].status is not SliceStatus.PENDING
    future_dispatch_intent = root_state.slices[future_names[0]].dispatch_intent_id
    future_invocation_path = tmp_path / ".exo" / "agents" / future_names[0] / "invocation.json"
    future_invocation_before = future_invocation_path.read_bytes()
    assert future_names[0] in created_invocation_scopes
    assert all(root_state.slices[name].status is SliceStatus.PENDING for name in future_names[1:])
    advanced = run_tl_loop(
        run_id,
        candidate_plan,
        SyntheticQueue([]),
        EffectClient(integration_transport),
        config=resumed_config,
        root_dir=state_root,
    )
    assert advanced.final_state.current_order == 2
    assert advanced.final_state.slices[future_names[0]].status is not SliceStatus.PENDING
    assert advanced.final_state.slices[future_names[0]].dispatch_intent_id == (
        future_dispatch_intent
    )
    assert future_invocation_path.read_bytes() == future_invocation_before
    assert created_invocation_scopes.count(future_names[0]) == 1
    assert all(
        advanced.final_state.slices[name].status is SliceStatus.PENDING for name in future_names[1:]
    )
    assert [name for name, _ in integration_transport.calls if name == "merge_pr"] == ["merge_pr"]
    assert future_dispatch_intent
    assert child_store.load().slices["leaf-a"].post_merge == completed_leaf.post_merge
    assert (
        sum(
            name == "chainlink_issue_close" and str(args.get("issue_id")) == "811"
            for name, args in transport.calls + integration_transport.calls
        )
        == close_count
    )


@pytest.mark.parametrize(
    ("failure_mode", "expected_reason"),
    [
        ("lookup_unavailable", "publication_lookup_unavailable"),
        ("ambiguous", "publication_lookup_unresolved"),
        ("malformed_lookup", "publication_lookup_malformed"),
        ("wrong_repo", "repository_identity_conflict"),
        ("repository_timeout", "repository_identity_timeout"),
        ("wrong_slice", "publication_lookup_slice_mismatch"),
        ("wrong_pr", "watcher_pr_mismatch"),
        ("wrong_owner", "publication_coordinates_mismatch"),
        ("wrong_invocation", "publication_lineage_rejected:invocation_lineage_mismatch"),
        ("wrong_branch", "publication_coordinates_mismatch"),
        ("wrong_base", "publication_coordinates_mismatch"),
        ("stale_head", "publication_evidence_incomplete"),
        ("stale_patch", "publication_lineage_rejected:watcher_evidence_error"),
        ("watcher_unavailable", "watcher_observation_unavailable"),
    ],
)
def test_aggregate_recovery_refuses_unresolved_or_mismatched_publications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    expected_reason: str,
) -> None:
    class RefusalTransport(RecordingTransport):
        def __init__(self) -> None:
            super().__init__()
            if failure_mode in {
                "wrong_owner",
                "wrong_invocation",
                "wrong_branch",
                "wrong_base",
                "stale_head",
                "stale_patch",
                "watcher_unavailable",
            }:
                self.aggregate_reconciliation = _aggregate_watcher_fixture(
                    "unrelated-invocation"
                    if failure_mode == "wrong_invocation"
                    else "aggregate-child-invocation",
                    author_agent="another-owner"
                    if failure_mode == "wrong_owner"
                    else "aggregate-child",
                    head_branch="other-branch"
                    if failure_mode == "wrong_branch"
                    else "main.aggregate-child",
                    base_branch="other-base" if failure_mode == "wrong_base" else "main",
                )
                if failure_mode == "stale_head":
                    self.aggregate_reconciliation["watcher"]["head_reachable"] = False
                if failure_mode == "stale_patch":
                    self.aggregate_reconciliation["watcher"]["evidence_error"] = (
                        "stale patch digest"
                    )

        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            if tool_name == "repository_identity" and failure_mode == "repository_timeout":
                raise TimeoutError("secret-token must be redacted")
            if tool_name == "repository_identity" and failure_mode == "wrong_repo":
                return {
                    "success": True,
                    "result": {"remote_url": "https://forge.example.com/other/repository.git"},
                }
            if tool_name == "resolve_live_pr_for_slice":
                if failure_mode == "lookup_unavailable":
                    return {"success": False, "error": "secret-token must be redacted"}
                if failure_mode == "ambiguous":
                    return {
                        "success": True,
                        "result": {
                            "resolution": "ambiguous",
                            "slice_id": "aggregate-child",
                            "candidate_pr_numbers": [43, 44],
                        },
                    }
                if failure_mode == "malformed_lookup":
                    return {
                        "success": True,
                        "result": {
                            "resolution": "live",
                            "slice_id": "aggregate-child",
                            "pr_number": "43",
                            "publication": _aggregate_watcher_fixture("aggregate-child-invocation")[
                                "publication"
                            ],
                        },
                    }
                if failure_mode == "wrong_pr":
                    return {
                        "success": True,
                        "result": {
                            "resolution": "live",
                            "slice_id": "aggregate-child",
                            "pr_number": 44,
                            "publication": _aggregate_watcher_fixture("aggregate-child-invocation")[
                                "publication"
                            ],
                        },
                    }
                if failure_mode == "wrong_slice":
                    return {
                        "success": True,
                        "result": {
                            "resolution": "live",
                            "slice_id": "another-slice",
                            "pr_number": 43,
                            "publication": _aggregate_watcher_fixture("aggregate-child-invocation")[
                                "publication"
                            ],
                        },
                    }
            if tool_name == "watcher_pr_state" and failure_mode == "watcher_unavailable":
                return {"success": False, "error": "secret-token must be redacted"}
            return super().call_tool(role, name, tool_name, arguments)

    run_id = f"aggregate-refusal-{failure_mode}"
    _install_ordered_invocation(monkeypatch, "aggregate-child-invocation")
    transport = RefusalTransport()
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "aggregate-child",
                _plan(),
                source=SyntheticQueue(_lifecycle_events(run_id)),
                order=1,
            ),
        )
    )

    def invoke_loop() -> TLRunResult:
        return run_tl_loop(
            run_id,
            plan,
            SyntheticQueue([]),
            EffectClient(transport),
            config=replace(
                _config(),
                ledger_run_id=run_id,
                keep_alive_on_waiting=False,
                repository_identity=(
                    None
                    if failure_mode in {"wrong_repo", "repository_timeout"}
                    else _config().repository_identity
                ),
            ),
            root_dir=tmp_path,
        )

    if failure_mode == "wrong_repo":
        create(
            run_id,
            {"repository_identity": {"remote_url": "https://forge.example.com/org/repo.git"}},
            root_dir=tmp_path,
        )
        with pytest.raises(TLLoopError, match=expected_reason):
            invoke_loop()
        assert RunStore(run_id, tmp_path).load().repository_identity == RepositoryIdentity(
            "https://forge.example.com/org/repo.git"
        )
        assert not any(
            name in {"spawn_leaf", "spawn_worker", "file_pr"} for name, _ in transport.calls
        )
        return
    if failure_mode == "repository_timeout":
        with pytest.raises(TLLoopError, match=expected_reason):
            invoke_loop()
        assert not RunStore(run_id, tmp_path).path.exists()
        assert not any(
            name in {"spawn_leaf", "spawn_worker", "file_pr"} for name, _ in transport.calls
        )
        return
    result = invoke_loop()

    parked = result.final_state.slices["aggregate-child"]
    assert parked.status is SliceStatus.PARKED
    assert parked.park_cause is ParkCause.PUBLICATION_BLOCKED
    assert parked.park_audit is not None
    assert expected_reason in str(parked.park_audit["error_kind"])
    assert "secret-token" not in repr(parked.park_audit)
    assert not any(name == "file_pr" for name, _ in transport.calls)


@pytest.mark.parametrize(
    ("resume_prior", "publication_succession"),
    [
        ("aggregate-child-invocation", ()),
        ("intermediate-resume", ("aggregate-child-invocation", "intermediate-resume")),
    ],
)
def test_aggregate_recovery_adopts_authorized_resume_without_replaying_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resume_prior: str,
    publication_succession: tuple[str, ...],
) -> None:
    original_prepare = driver_module._prepare_sub_tl_stage
    prior_invocation = "aggregate-child-invocation"
    resumed_invocation = "aggregate-child-resume"

    def prepare(tasks, state, config, source, effects, store, effects_log, order):
        state, prepared = original_prepare(
            tasks, state, config, source, effects, store, effects_log, order
        )
        invocation_path = (
            Path(config.project_root) / ".exo" / "agents" / "aggregate-child" / "invocation.json"
        )
        if not invocation_path.exists():
            _checkpoint_ordered_sub_tl_invocation(store, tasks[0].name, prior_invocation)
            worktree = str(
                driver_module._sub_tl_worktree(config, store.root_dir, store.run_id, tasks[0])
            )
            invocation_path.parent.mkdir(parents=True, exist_ok=True)
            invocation_path.write_text(
                json.dumps(
                    {
                        "invocation_id": prior_invocation,
                        "trigger": "spawn",
                        "slice_id": "aggregate-child",
                        "branch": "main.aggregate-child",
                        "worktree": worktree,
                    }
                ),
                encoding="utf-8",
            )
        return store.load(), prepared

    monkeypatch.setattr(driver_module, "_prepare_sub_tl_stage", prepare)

    class CrashAfterPublishTransport(RecordingTransport):
        crashed = False

        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            title = str(arguments.get("title", ""))
            if tool_name == "file_pr" and title.startswith("Aggregate aggregate-child"):
                self.calls.append((tool_name, arguments))
                self.aggregate_reconciliation = _aggregate_watcher_fixture(prior_invocation)
                self.crashed = True
                raise SystemExit("simulated controller crash after aggregate PR creation")
            return super().call_tool(role, name, tool_name, arguments)

    run_id = "aggregate-resume-lineage"
    transport = CrashAfterPublishTransport()
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "aggregate-child",
                _plan(),
                source=SyntheticQueue(_lifecycle_events(run_id)),
                order=1,
            ),
            SubTLTask("later-stage", WorkPlan(), order=2),
        )
    )
    config = replace(
        _config(),
        project_root=tmp_path,
        ledger_run_id=run_id,
        keep_alive_on_waiting=False,
    )
    with pytest.raises(SystemExit, match="simulated controller crash"):
        run_tl_loop(
            run_id,
            plan,
            SyntheticQueue([]),
            EffectClient(transport),
            config=config,
            root_dir=tmp_path,
        )

    store = RunStore(run_id, tmp_path)
    assert store.load().slices["aggregate-child"].dispatch_invocation_id == prior_invocation
    leaf_effects = sum(name in {"spawn_worker", "spawn_leaf"} for name, _ in transport.calls)
    invocation_path = tmp_path / ".exo" / "agents" / "aggregate-child" / "invocation.json"
    record = json.loads(invocation_path.read_text(encoding="utf-8"))
    record.update(
        {
            "invocation_id": resumed_invocation,
            "trigger": "resume_pr",
            "prior_invocation_id": resume_prior,
            "pr_number": 43,
            "head_sha": "head-a",
        }
    )
    invocation_path.write_text(json.dumps(record), encoding="utf-8")
    transport.aggregate_reconciliation = _aggregate_watcher_fixture(
        resumed_invocation, succession_invocation_ids=publication_succession
    )

    resumed = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    stage = resumed.final_state.slices["aggregate-child"]
    assert stage.status is SliceStatus.IN_REVIEW
    assert stage.pr_number == 43
    assert stage.dispatch_invocation_id == resumed_invocation
    assert resumed.final_state.slices["later-stage"].status is SliceStatus.PENDING
    assert (
        sum(name in {"spawn_worker", "spawn_leaf"} for name, _ in transport.calls) == leaf_effects
    )
    assert sum(name == "file_pr" for name, _ in transport.calls) == 1


def test_rejected_aggregate_publication_parks_one_slice_and_retries_after_approval(
    tmp_path: Path,
) -> None:
    child_plan = _plan()
    parent_dir = tmp_path / "blocked-aggregate-run"
    for child_name in ("aggregate-child", "sibling-child"):
        run_tl_loop(
            child_name,
            child_plan,
            SyntheticQueue(_lifecycle_events(child_name)),
            EffectClient(RecordingTransport()),
            config=replace(
                _config(),
                branch=f"main.{child_name}",
                parent_branch="main",
                parent_run_id="blocked-aggregate-run",
                depth=1,
                keep_alive_on_waiting=False,
            ),
            root_dir=parent_dir,
        )
        child_store = RunStore(child_name, parent_dir)
        child_state = child_store.load()
        child_slices = dict(child_state.slices)
        child_slices["leaf-a"] = replace(
            child_slices["leaf-a"], pr_number=42, reviewed_head="head-a"
        )
        child_store.checkpoint(
            child_state.fsm,
            child_slices,
            child_state.budgets,
            child_state.events.last_consumed_offset,
        )

    class RejectFirstTransport(RecordingTransport):
        aggregate_publication_calls = 0

        def call_tool(
            self,
            role: str,
            name: str,
            tool_name: str,
            arguments: JsonObject,
        ) -> JsonObject:
            title = str(arguments.get("title", ""))
            if tool_name == "file_pr" and "Aggregate aggregate-child" in title:
                self.calls.append((tool_name, arguments))
                self.aggregate_publication_calls += 1
                if self.aggregate_publication_calls == 1:
                    return {
                        "success": False,
                        "error": "missing invocation record",
                        "error_kind": "InvalidInput",
                    }
            return super().call_tool(role, name, tool_name, arguments)

    transport = RejectFirstTransport()
    parent_plan = WorkPlan(
        sub_tls=(
            SubTLTask("aggregate-child", child_plan, source=SyntheticQueue([]), order=1),
            SubTLTask("sibling-child", child_plan, source=SyntheticQueue([]), order=1),
            SubTLTask("later-stage", WorkPlan(), order=2),
        )
    )
    config = replace(_config(), keep_alive_on_waiting=False)

    initial = run_tl_loop(
        "blocked-aggregate-run",
        parent_plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    blocked = initial.final_state.slices["aggregate-child"]
    gate_name = f"{AGGREGATE_PUBLICATION_GATE_PREFIX}aggregate-child"
    assert blocked.status is SliceStatus.PARKED
    assert blocked.park_cause is ParkCause.PUBLICATION_BLOCKED
    assert blocked.park_audit is not None
    assert blocked.park_audit["error_kind"] == "InvalidInput"
    assert blocked.park_audit["needs_human"] is True
    assert blocked.park_audit["retryable"] is False
    assert blocked.park_audit["publication_attempt"] == 1
    assert initial.final_state.slices["sibling-child"].status is SliceStatus.IN_REVIEW
    assert any(
        gate.name == gate_name and gate.status is GateStatus.PENDING
        for gate in initial.final_state.gates
    )
    invariant_events = [
        arguments["payload"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
        and arguments.get("event_type") == "tl.transition_invariant_failed"
    ]
    assert len(invariant_events) == 1
    assert invariant_events[0]["target_id"] == "controller"

    store = RunStore("blocked-aggregate-run", tmp_path)
    store.clear_gate(gate_name)
    recovered = run_tl_loop(
        "blocked-aggregate-run",
        parent_plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    assert recovered.final_state.slices["aggregate-child"].status is SliceStatus.PARKED
    assert any(
        gate.name == gate_name and gate.status is GateStatus.PENDING
        for gate in recovered.final_state.gates
    )
    assert transport.aggregate_publication_calls == 1

    class GateAnswerQueue(SyntheticQueue):
        answered = False

        def get(self, timeout: float | None = None) -> EventEnvelope:
            if not self.answered:
                self.answered = True
                store.answer_gate(gate_name, GateStatus.APPROVED)
            return super().get(timeout)

    approval = project(
        cast(
            dict[str, object],
            {
                "schema_version": 1,
                "event_id": "aggregate-publication-approved",
                "id": "aggregate-publication-approved",
                "event_time": "2026-10-11T00:00:00Z",
                "observed_at": "2026-10-11T00:00:00Z",
                "run_seq": 1,
                "type": "tl.gate_answered",
                "agent_id": "controller",
                "run_id": "blocked-aggregate-run",
                "session_id": "session-1",
                "lifecycle_state": "emitted",
                "data": {
                    "gate_name": gate_name,
                    "decision": GateStatus.APPROVED.value,
                    "source": "operator",
                },
            },
        )
    )
    resumed = run_tl_loop(
        "blocked-aggregate-run",
        parent_plan,
        GateAnswerQueue([approval]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    assert resumed.final_state.slices["aggregate-child"].status is SliceStatus.IN_REVIEW
    assert not any(gate.name == gate_name for gate in resumed.final_state.gates)
    assert transport.aggregate_publication_calls == 2

    run_tl_loop(
        "blocked-aggregate-run",
        parent_plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    assert transport.aggregate_publication_calls == 2


@pytest.mark.parametrize("crash_after_approval", [False, True], ids=["uninterrupted", "restart"])
def test_parent_serializes_aggregate_merge_after_base_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_after_approval: bool,
) -> None:
    _install_ordered_invocation(monkeypatch, "aggregate-child-invocation")
    child_plan = _plan()
    parent_dir = tmp_path / "serialized-run"
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "aggregate-child",
                child_plan,
                source=SyntheticQueue([]),
                order=1,
                chainlink_issue_id=1052,
            ),
        )
    )
    parent_config = TLLoopConfig(
        root_dir=tmp_path,
        run_id="serialized-run",
        branch="main",
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )
    parent_manifest = driver_module._manifest_for_plan(plan, "serialized-run", parent_config)
    child_node = next(node for node in parent_manifest.nodes if node.name == "aggregate-child")
    child_manifest = parent_manifest.child_manifests[child_node.node_id]
    child_config = TLLoopConfig(
        root_dir=parent_dir,
        run_id="aggregate-child",
        branch=child_manifest.owned_branch,
        keep_alive_on_waiting=False,
        agent_id="aggregate-child",
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
        declared_manifest=child_manifest,
    )
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(RecordingTransport()),
        config=child_config,
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    setup_config = TLLoopConfig(root_dir=tmp_path, run_id="serialized-run")
    repository = Path(
        driver_module._sub_tl_worktree(setup_config, tmp_path, "serialized-run", plan.sub_tls[0])
    )
    _complete_ordered_child_receipts(child_store, "serialized", repository=repository)
    aggregate_head = _test_git_output(repository, "rev-parse", "HEAD")
    assert aggregate_head is not None
    aggregate_tree = _test_git_tree(repository, aggregate_head)

    class ReceiptCheckingTransport(IntegrationTransport):
        def call_tool(self, role, name, tool_name, arguments):
            if tool_name == "merge_pr":
                stored = RunStore("serialized-run", tmp_path).load()
                candidate = stored.integration.candidates["aggregate-child"]
                reference = cast(JsonObject, arguments["aggregate_approval"])
                assert candidate.active_aggregate_approval_id == reference["receipt_id"]
                assert candidate.aggregate_approval_receipts
            return super().call_tool(role, name, tool_name, arguments)

    transport = ReceiptCheckingTransport(
        snapshots=[
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
        ]
    )
    transport.aggregate_git_evidence["aggregate-child"] = (aggregate_head, aggregate_tree)
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        keep_alive_on_waiting=False,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
        agent_id="root-controller",
    )
    verified: list[tuple[str, str, str, str]] = []
    from tl_loop.loop import driver

    original_verify = driver.verify_integration

    def record_verify(state: object, **live: str) -> object:
        verified.append(
            (
                live["base_sha"],
                live["head_sha"],
                live["merge_tree_sha"],
                live["ci_status"],
            )
        )
        return original_verify(state, **live)

    monkeypatch.setattr(driver, "verify_integration", record_verify)
    original_checkpoint = RunStore.checkpoint
    approval_checkpoint_written = False

    def crash_after_approval_checkpoint(
        store: RunStore,
        *args: Any,
        **checkpoint_args: Any,
    ) -> RunState:
        nonlocal approval_checkpoint_written
        state = original_checkpoint(store, *args, **checkpoint_args)
        if (
            store.run_id == "serialized-run"
            and not approval_checkpoint_written
            and state.integration is not None
            and "aggregate-child" in state.integration.candidates
        ):
            candidate = state.integration.candidates["aggregate-child"]
            if candidate.active_aggregate_approval_id and candidate.aggregate_approval_receipts:
                approval_checkpoint_written = True
                raise SystemExit("simulated crash after aggregate approval checkpoint")
        return state

    receipt_id_before_restart: str | None = None
    if crash_after_approval:
        monkeypatch.setattr(RunStore, "checkpoint", crash_after_approval_checkpoint)
        with pytest.raises(SystemExit, match="after aggregate approval checkpoint"):
            run_tl_loop(
                "serialized-run",
                plan,
                SyntheticQueue([]),
                EffectClient(transport),
                config=config,
                root_dir=tmp_path,
            )

        assert approval_checkpoint_written
        state_after_crash = RunStore("serialized-run", tmp_path).load()
        approval_before_restart = state_after_crash.integration.candidates["aggregate-child"]
        assert len(approval_before_restart.aggregate_approval_receipts) == 1
        receipt_id_before_restart = approval_before_restart.active_aggregate_approval_id
        assert receipt_id_before_restart is not None
        assert state_after_crash.slices["aggregate-child"].status is SliceStatus.IN_REVIEW
        assert not any(name == "merge_pr" for name, _ in transport.calls)
        monkeypatch.setattr(RunStore, "checkpoint", original_checkpoint)

        result = run_tl_loop(
            "serialized-run",
            plan,
            SyntheticQueue([]),
            EffectClient(transport),
            config=config,
            root_dir=tmp_path,
        )
    else:
        result = run_tl_loop(
            "serialized-run",
            plan,
            SyntheticQueue([]),
            EffectClient(transport),
            config=config,
            root_dir=tmp_path,
        )

    assert result.final_state.slices["aggregate-child"].status is SliceStatus.MERGED
    assert result.final_state.slices["aggregate-child"].post_merge is not None
    assert result.final_state.slices["aggregate-child"].post_merge.phase is PostMergePhase.COMPLETE
    assert result.final_state.slices["aggregate-child"].verdict is None
    assert (
        result.final_state.slices["aggregate-child"].dispatch_invocation_id
        == "aggregate-child-invocation"
    )
    assert verified and all(
        item == ("base-a", aggregate_head, "tree-a", "success") for item in verified
    )
    if not crash_after_approval:
        assert verified == [("base-a", aggregate_head, "tree-a", "success")]
    candidate = result.final_state.integration.candidates["aggregate-child"]
    if receipt_id_before_restart is not None:
        assert candidate.active_aggregate_approval_id == receipt_id_before_restart
    assert len(candidate.aggregate_approval_receipts) == 1
    assert candidate.validated_base_sha == "base-a"
    assert candidate.head_sha == aggregate_head
    assert candidate.patch_digest == "patch-a"
    assert candidate.merge_tree_sha == "tree-a"
    assert candidate.ci_status == "success"
    assert [name for name, _ in transport.calls if name == "merge_pr"] == ["merge_pr"]
    assert next(arguments for name, arguments in transport.calls if name == "merge_pr") == {
        "pr_number": 43,
        "strategy": "merge",
        "working_dir": ".",
        "expected_base_sha": "base-a",
        "expected_head_sha": aggregate_head,
        "expected_patch_digest": "patch-a",
        "expected_merge_tree_sha": "tree-a",
        "aggregate_approval": {
            "receipt_id": result.final_state.integration.candidates[
                "aggregate-child"
            ].active_aggregate_approval_id,
            "parent_run_id": "serialized-run",
            "parent_scope_path": ["serialized-run"],
            "candidate_key": "aggregate-child",
            "controller_epoch": result.final_state.controller_epoch,
        },
    }
    approval = result.final_state.integration.candidates["aggregate-child"]
    assert len(approval.aggregate_approval_receipts) == 1
    assert (
        approval.active_aggregate_approval_id == approval.aggregate_approval_receipts[0].receipt_id
    )
    event_types = {
        arguments["event_type"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    }
    expected_events = {
        "tl.stage_started",
        "tl.aggregate_pr_opened",
        "tl.integration_validated",
        "tl.integration_revalidated",
        "tl.merge_decided",
    }
    if not crash_after_approval:
        expected_events.add("tl.stage_completed")
    assert expected_events <= event_types


def test_restart_during_merging_without_publication_lineage_stays_unresolved(
    tmp_path: Path,
) -> None:
    run_id = "merging-restart-run"
    plan = WorkPlan(
        sub_tls=(SubTLTask("aggregate-child", WorkPlan(), order=1, chainlink_issue_id=1052),)
    )
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        keep_alive_on_waiting=False,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )
    initial = _initial_slices(plan, config, tmp_path, run_id)
    initial["aggregate-child"].update(
        {
            "status": "in_review",
            "pr_number": 42,
            "reviewed_head": "head-a",
            "verdict": "GO",
            "review_patch_digests": {"head-a": "patch-a"},
            "ci_state": {"head-a": "success"},
            "action": {
                "kind": ActionKind.MERGE.value,
                "phase": ActionPhase.IN_FLIGHT.value,
                "state_version": 1,
                "intent_id": "merge-intent",
                "head_sha": "head-a",
                "attempt": 1,
                "contract_digest": None,
            },
        }
    )
    transport = IntegrationTransport(snapshots=[{}])
    first = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
        initial_slices=initial,
    )
    store = RunStore(run_id, tmp_path)
    state = store.load()
    store.checkpoint(
        state.fsm,
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=replace(
            state.integration,
            lifecycle=IntegrationLifecycle.MERGING,
            head_sha="head-a",
            patch_digest="patch-a",
            validated_base_sha="base-a",
            merge_tree_sha="tree-a",
            ci_status="success",
            stage_verification="passed",
        ),
    )
    transport.snapshots.append(
        {
            "merged": True,
            "head_sha": "head-a",
            "base_sha": "base-a",
        }
    )

    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert first.final_state.fsm.phase is TLPhase.TLWaiting
    assert result.final_state.fsm.phase is TLPhase.TLWaiting
    assert result.final_state.current_order == 1
    unresolved = result.final_state.slices["aggregate-child"]
    assert unresolved.status is SliceStatus.IN_REVIEW
    assert unresolved.action is not None and unresolved.action.phase is ActionPhase.IN_FLIGHT
    assert "published_pr" in unresolved.reconciliation["missing_evidence"]
    assert [name for name, _ in transport.calls if name == "merge_pr"] == []
    assert not any(
        arguments["event_type"] == "tl.merge_reconciled"
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    )


def test_external_merge_without_dispatch_and_publication_lineage_stays_unresolved(
    tmp_path: Path,
) -> None:
    plan = WorkPlan(sub_tls=(SubTLTask("aggregate-child", WorkPlan(), order=1),))
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        keep_alive_on_waiting=False,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )
    initial = _initial_slices(plan, config, tmp_path, "external-merge-run")
    initial["aggregate-child"].update(
        {
            "status": "in_review",
            "pr_number": 43,
            "reviewed_head": "head-external",
            "verdict": "GO",
            "review_patch_digests": {"head-external": "patch-external"},
            "ci_state": {"head-external": "success"},
            "action": {
                "kind": ActionKind.MERGE.value,
                "phase": ActionPhase.IN_FLIGHT.value,
                "state_version": 1,
                "intent_id": "external-merge-intent",
                "head_sha": "head-external",
                "attempt": 1,
                "contract_digest": None,
            },
        }
    )
    transport = IntegrationTransport(
        snapshots=[
            {
                "merged": True,
                "head_sha": "head-external",
                "base_sha": "base-main",
                "patch_digest": "patch-external",
                "merge_tree_sha": "tree-external",
                "ci_status": "success",
            }
        ]
    )

    result = run_tl_loop(
        "external-merge-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
        initial_slices=initial,
    )

    assert result.final_state.current_order == 1
    unresolved = result.final_state.slices["aggregate-child"]
    assert unresolved.status is SliceStatus.IN_REVIEW
    assert unresolved.action is not None and unresolved.action.phase is ActionPhase.IN_FLIGHT
    assert set(unresolved.reconciliation["missing_evidence"]) >= {
        "dispatch_owner",
        "published_pr",
    }
    assert [name for name, _ in transport.calls if name == "merge_pr"] == []
    assert not any(
        arguments["event_type"] == "tl.merge_reconciled"
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    )


def test_parent_requeues_aggregate_when_base_changes_before_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ordered_invocation(monkeypatch, "aggregate-child-invocation")
    child_plan = _plan()
    parent_dir = tmp_path / "serialized-run"
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(RecordingTransport()),
        config=_config(),
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "aggregate-child",
                child_plan,
                source=SyntheticQueue([]),
                order=1,
                chainlink_issue_id=1052,
            ),
        )
    )
    setup_config = TLLoopConfig(root_dir=tmp_path, run_id="serialized-run")
    repository = Path(
        driver_module._sub_tl_worktree(setup_config, tmp_path, "serialized-run", plan.sub_tls[0])
    )
    _complete_ordered_child_receipts(
        child_store,
        "base-revalidation",
        repository=repository,
    )
    aggregate_head = _test_git_output(repository, "rev-parse", "HEAD")
    assert aggregate_head is not None
    aggregate_tree = _test_git_tree(repository, aggregate_head)
    transport = IntegrationTransport(
        snapshots=[
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": aggregate_head,
                "base_sha": "base-b",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-b",
                "ci_status": "success",
            },
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": aggregate_head,
                "base_sha": "base-b",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-b",
                "ci_status": "success",
            },
        ]
    )
    transport.aggregate_git_evidence["aggregate-child"] = (aggregate_head, aggregate_tree)
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        max_base_revalidations=1,
        keep_alive_on_waiting=False,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )
    first_revalidation = run_tl_loop(
        "serialized-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )
    assert first_revalidation.final_state.integration.base_revalidation_count == 1
    result = run_tl_loop(
        "serialized-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert result.final_state.slices["aggregate-child"].status is SliceStatus.IN_REVIEW
    assert [name for name, _ in transport.calls if name == "merge_pr"] == []
    assert (
        result.final_state.integration.sub_tl_states["aggregate-child"]
        is IntegrationLifecycle.NEEDS_BASE_REVALIDATION
    )
    assert result.final_state.integration.validated_base_sha is None
    assert result.final_state.integration.base_revalidation_count == 1
    assert (
        GateState(name="tl-integration-revalidation", status=GateStatus.PENDING)
        in result.final_state.gates
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (("head_sha", "head-b"), ("patch_digest", "patch-b")),
)
def test_head_or_patch_mismatch_opens_conflict_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    _install_ordered_invocation(monkeypatch, "aggregate-child-invocation")
    parent_dir = tmp_path / "mismatch-run"
    child_plan = _plan()
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(RecordingTransport()),
        config=_config(),
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    plan = WorkPlan(
        sub_tls=(SubTLTask("aggregate-child", child_plan, source=SyntheticQueue([]), order=1),)
    )
    setup_config = TLLoopConfig(root_dir=tmp_path, run_id="mismatch-run")
    repository = Path(
        driver_module._sub_tl_worktree(setup_config, tmp_path, "mismatch-run", plan.sub_tls[0])
    )
    _complete_ordered_child_receipts(child_store, "head-mismatch", repository=repository)
    aggregate_head = _test_git_output(repository, "rev-parse", "HEAD")
    assert aggregate_head is not None
    aggregate_tree = _test_git_tree(repository, aggregate_head)
    second_snapshot = {
        "head_sha": aggregate_head,
        "base_sha": "base-a",
        "patch_digest": "patch-a",
        "merge_tree_sha": "tree-a",
        "ci_status": "success",
    }
    second_snapshot[field] = value
    transport = IntegrationTransport(
        snapshots=[
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            second_snapshot,
        ]
    )
    transport.aggregate_git_evidence["aggregate-child"] = (aggregate_head, aggregate_tree)
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        keep_alive_on_waiting=False,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )
    result = run_tl_loop(
        "mismatch-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert (
        GateState(name="tl-integration-conflict", status=GateStatus.PENDING)
        in result.final_state.gates
    )
    assert result.final_state.integration.lifecycle is IntegrationLifecycle.INTEGRATION_CONFLICT
    assert result.final_state.slices["aggregate-child"].dispatch_agent_id == "aggregate-child"
    assert (
        result.final_state.integration.candidates["aggregate-child"].integration_owner_id
        == "mismatch-run:aggregate-child:integration"
    )
    event_types = [
        arguments["event_type"]
        for name, arguments in transport.calls
        if name == "emit_controller_event"
    ]
    assert "tl.integration_base_invalidated" not in event_types
    assert "tl.gate_opened" in event_types


def test_integration_conflict_repairs_same_aggregate_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ordered_invocation(monkeypatch, "aggregate-child-invocation")
    child_plan = _plan()
    parent_dir = tmp_path / "conflict-run"
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "aggregate-child",
                child_plan,
                source=SyntheticQueue([]),
                order=1,
                chainlink_issue_id=1052,
            ),
        )
    )
    parent_config = TLLoopConfig(
        root_dir=tmp_path,
        run_id="conflict-run",
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )
    parent_manifest = driver_module._manifest_for_plan(plan, "conflict-run", parent_config)
    child_node = next(node for node in parent_manifest.nodes if node.name == "aggregate-child")
    child_config = TLLoopConfig(
        root_dir=parent_dir,
        run_id="aggregate-child",
        branch=parent_manifest.child_manifests[child_node.node_id].owned_branch,
        keep_alive_on_waiting=False,
        agent_id="aggregate-child",
        repository_identity=parent_config.repository_identity,
        declared_manifest=parent_manifest.child_manifests[child_node.node_id],
    )
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(RecordingTransport()),
        config=child_config,
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    setup_config = TLLoopConfig(root_dir=tmp_path, run_id="conflict-run")
    repository = Path(
        driver_module._sub_tl_worktree(setup_config, tmp_path, "conflict-run", plan.sub_tls[0])
    )
    _complete_ordered_child_receipts(child_store, "conflict", repository=repository)
    aggregate_head = _test_git_output(repository, "rev-parse", "HEAD")
    assert aggregate_head is not None
    aggregate_tree = _test_git_tree(repository, aggregate_head)
    backend = ReviewBackend(
        [
            RlmResponse(
                {
                    "action": "delegate",
                    "reason": "The merge conflict has a bounded repair in the aggregate slice",
                    "handoff": {
                        "root_cause": "The aggregate PR conflicts with the parent base",
                        "proposed_solution": "Resolve the conflict on the existing aggregate PR",
                        "read_first": ["tl-loop/aggregate-child"],
                        "steps": ["Resolve the aggregate conflict"],
                        "verify": ["just tl-loop-test"],
                        "boundary": ["Only edit tl-loop/aggregate-child"],
                        "done_criteria": ["The aggregate PR applies cleanly"],
                    },
                },
                input_tokens=10,
                output_tokens=5,
            )
        ]
    )
    transport = IntegrationTransport(
        snapshots=[
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "open": True,
                "merged": False,
                "head_branch": "main.aggregate-child",
                "head_sha": aggregate_head,
            },
        ],
        merge_response={"success": False, "error": "merge conflict with parent base"},
    )
    transport.aggregate_git_evidence["aggregate-child"] = (aggregate_head, aggregate_tree)
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        policy=validate_policy(_selector_policy()),
        tl_model_choice=_tl_choice(backend),
        keep_alive_on_waiting=False,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
        agent_id="root-controller",
    )
    result = run_tl_loop(
        "conflict-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    current = result.final_state.slices["aggregate-child"]
    assert current.status is SliceStatus.REPAIRING
    assert current.repair_attempts == 1
    assert current.dispatch_agent_id == "aggregate-child"
    assert current.verdict is None
    assert current.reviewed_head is None
    assert (
        result.final_state.integration.candidates["aggregate-child"].integration_owner_id
        == "conflict-run:aggregate-child:integration"
    )
    assert result.final_state.integration.lifecycle is IntegrationLifecycle.REPAIRING_AGGREGATE
    assert result.final_state.integration.head_sha is None
    assert [request.name for request in backend.requests] == ["integration_failure"]
    assert (
        result.final_state.integration.candidates["aggregate-child"].tl_intervention_attempts == 1
    )
    assert result.final_state.budgets.role_spent["tl"] == 15
    assert [name for name, _ in transport.calls if name == "resume_pr"] == ["resume_pr"]
    assert not any(name in {"spawn_leaf", "spawn_worker"} for name, _ in transport.calls)


def test_exhausted_integration_conflict_opens_human_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ordered_invocation(monkeypatch, "aggregate-child-invocation")
    child_plan = _plan()
    parent_dir = tmp_path / "gate-run"
    plan = WorkPlan(
        sub_tls=(SubTLTask("aggregate-child", child_plan, source=SyntheticQueue([]), order=1),)
    )
    parent_config = TLLoopConfig(
        root_dir=tmp_path,
        run_id="gate-run",
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
    )
    parent_manifest = driver_module._manifest_for_plan(plan, "gate-run", parent_config)
    child_node = next(node for node in parent_manifest.nodes if node.name == "aggregate-child")
    child_config = TLLoopConfig(
        root_dir=parent_dir,
        run_id="aggregate-child",
        branch=parent_manifest.child_manifests[child_node.node_id].owned_branch,
        keep_alive_on_waiting=False,
        agent_id="aggregate-child",
        repository_identity=parent_config.repository_identity,
        declared_manifest=parent_manifest.child_manifests[child_node.node_id],
    )
    run_tl_loop(
        "aggregate-child",
        child_plan,
        SyntheticQueue(_lifecycle_events("aggregate-child")),
        EffectClient(RecordingTransport()),
        config=child_config,
        root_dir=parent_dir,
    )
    child_store = RunStore("aggregate-child", parent_dir)
    setup_config = TLLoopConfig(root_dir=tmp_path, run_id="gate-run")
    repository = Path(
        driver_module._sub_tl_worktree(setup_config, tmp_path, "gate-run", plan.sub_tls[0])
    )
    _complete_ordered_child_receipts(child_store, "conflict-gate", repository=repository)
    aggregate_head = _test_git_output(repository, "rev-parse", "HEAD")
    assert aggregate_head is not None
    aggregate_tree = _test_git_tree(repository, aggregate_head)
    backend = ReviewBackend(
        [RlmResponse({"action": "escalate", "reason": "The repair needs a human choice"})]
    )
    transport = IntegrationTransport(
        snapshots=[
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "head_sha": aggregate_head,
                "base_sha": "base-a",
                "patch_digest": "patch-a",
                "merge_tree_sha": "tree-a",
                "ci_status": "success",
            },
            {
                "open": True,
                "merged": False,
                "head_branch": "main.aggregate-child",
                "head_sha": aggregate_head,
            },
        ],
        merge_response={"success": False, "error": "merge conflict with parent base"},
    )
    transport.aggregate_git_evidence["aggregate-child"] = (aggregate_head, aggregate_tree)
    config = TLLoopConfig(
        max_parallel_slices=1,
        poll_interval=0.001,
        keep_alive_on_waiting=False,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
        agent_id="root-controller",
        policy=validate_policy(_selector_policy()),
        tl_model_choice=_tl_choice(backend),
    )
    result = run_tl_loop(
        "gate-run",
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert (
        GateState(name="tl-integration-conflict", status=GateStatus.PENDING)
        in result.final_state.gates
    )
    assert result.final_state.integration.lifecycle is IntegrationLifecycle.INTEGRATION_CONFLICT
    assert result.final_state.slices["aggregate-child"].status is SliceStatus.IN_REVIEW
    assert [request.name for request in backend.requests] == ["integration_failure"]
    assert not any(name == "resume_pr" for name, _ in transport.calls)


def test_same_order_event_consumers_require_isolated_sources(tmp_path: Path) -> None:
    eventful = WorkPlan(workers=(WorkerTask("worker", "consume"),))
    plan = WorkPlan(
        sub_tls=(
            SubTLTask("event-a", eventful),
            SubTLTask("event-b", eventful),
        )
    )

    with pytest.raises(TLLoopError, match="isolated sources"):
        run_tl_loop(
            "routing-run",
            plan,
            SyntheticQueue([]),
            EffectClient(RecordingTransport()),
            config=TLLoopConfig(max_parallel_slices=2),
            root_dir=tmp_path,
        )


__all__ = [
    "RecordingTransport",
    "SyntheticQueue",
    "test_active_loop_dispatches_direct_children_and_merges_leaf",
]


def test_restart_internal_head_reset_is_persisted_before_next_action(tmp_path: Path) -> None:
    store = _review_store(tmp_path, verdict=Verdict.GO)
    current = store.load().slices["leaf-a"]
    store.checkpoint(
        TLPlanning(),
        {
            "leaf-a": replace(
                current,
                publication=PublicationBinding(42, "head-b", "main.leaf-a", "main", 2),
            )
        },
        BudgetLedger(0, 0),
        offset=0,
    )
    transport = RecordingTransport()
    journal = EffectJournal("review-run", store.run_dir / "action-journal.json")

    result = _apply_convergence(
        store.load(),
        ConvergenceTracker(),
        store,
        TLLoopConfig(active=True),
        EffectClient(transport),
        journal,
    )

    restored = result.slices["leaf-a"]
    assert result.state_version == 1
    assert restored.status is SliceStatus.IN_REVIEW
    assert restored.reviewed_head is None
    assert restored.verdict is None
    assert any(name == "emit_controller_event" for name, _ in transport.calls)


def test_child_readiness_rejects_unrelated_aggregate_head(tmp_path: Path) -> None:
    plan, root_store, child_store = _nested_blocked_owner_stores(
        tmp_path,
        "unrelated-aggregate-head",
        leaf_issue_id=811,
    )
    assert child_store is not None
    repository = Path(root_store.load().slices["architecture"].worktree or "")
    _complete_nested_leaf(child_store, repository=repository)
    _seed_nested_aggregate_in_review(root_store)
    candidate = driver_module._aggregate_candidate_from_state(
        "architecture", root_store.load().integration.candidates.get("architecture")
    )
    assert candidate is not None

    subprocess.run(
        ("git", "-C", str(repository), "checkout", "--orphan", "unrelated"),
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "rm", "-rf", "."),
        capture_output=True,
        check=True,
        text=True,
    )
    unrelated_head = _test_git_commit(repository, "unrelated.txt", "other work\n", "unrelated")
    unrelated = replace(candidate, head_sha=unrelated_head)
    watcher = {
        "found": True,
        "pr_number": unrelated.pr_number,
        "head_sha": unrelated.head_sha,
        "patch_digest": unrelated.patch_digest,
        "head_reachable": True,
        "pr_head_tree_sha": _test_git_tree(repository, unrelated_head),
    }
    evidence = driver_module._verified_child_completion_evidence(
        plan.sub_tls[0],
        child_store.load(),
        unrelated,
        watcher=watcher,
        repository=repository,
    )

    assert evidence is None


def test_child_readiness_rejects_contradictory_push_receipt(tmp_path: Path) -> None:
    plan, root_store, child_store = _nested_blocked_owner_stores(
        tmp_path,
        "contradictory-child-push",
        leaf_issue_id=811,
    )
    assert child_store is not None
    repository = Path(root_store.load().slices["architecture"].worktree or "")
    child_state = _complete_nested_leaf(child_store, repository=repository)
    current = child_state.slices["leaf-a"]
    assert current.post_merge is not None
    contradictory_receipt = {
        **current.post_merge.evidence,
        "observed_remote_head": "f" * 40,
    }
    child_state = replace(
        child_state,
        slices={
            **child_state.slices,
            "leaf-a": replace(
                current,
                post_merge=PostMergeState(PostMergePhase.COMPLETE, contradictory_receipt),
            ),
        },
    )
    _seed_nested_aggregate_in_review(root_store)
    candidate = driver_module._aggregate_candidate_from_state(
        "architecture", root_store.load().integration.candidates.get("architecture")
    )
    assert candidate is not None
    watcher = {
        "found": True,
        "pr_number": candidate.pr_number,
        "head_sha": candidate.head_sha,
        "patch_digest": candidate.patch_digest,
        "head_reachable": True,
        "pr_head_tree_sha": _test_git_tree(repository, candidate.head_sha),
    }

    evidence = driver_module._verified_child_completion_evidence(
        plan.sub_tls[0],
        child_state,
        candidate,
        watcher=watcher,
        repository=repository,
    )

    assert evidence is None


def test_driver_does_not_merge_contradictory_child_push_receipt(tmp_path: Path) -> None:
    plan, root_store, child_store = _nested_blocked_owner_stores(
        tmp_path,
        "driver-contradictory-child-push",
        leaf_issue_id=811,
    )
    assert child_store is not None
    repository = Path(root_store.load().slices["architecture"].worktree or "")
    child_state = _complete_nested_leaf(child_store, repository=repository)
    current_leaf = child_state.slices["leaf-a"]
    assert current_leaf.post_merge is not None
    bad_receipt = {
        **current_leaf.post_merge.evidence,
        "observed_remote_head": "f" * 40,
    }
    child_state = child_store.checkpoint(
        child_state.fsm,
        {
            **child_state.slices,
            "leaf-a": replace(
                current_leaf,
                post_merge=PostMergeState(PostMergePhase.COMPLETE, bad_receipt),
            ),
        },
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    _seed_nested_aggregate_in_review(root_store)
    state = root_store.load()
    current = state.slices["architecture"]
    candidate = state.integration.candidates["architecture"]
    assert current.publication is not None
    assert candidate.aggregate_head_sha is not None
    watcher = {
        "found": True,
        "pr_number": candidate.aggregate_pr_number,
        "merged": False,
        "head_sha": candidate.aggregate_head_sha,
        "base_sha": candidate.aggregate_original_base_sha,
        "patch_digest": candidate.aggregate_patch_digest,
        "merge_tree_sha": "aggregate-merge-tree",
        "pr_head_tree_sha": _test_git_tree(repository, candidate.aggregate_head_sha),
        "ci_status": "success",
        "head_branch": current.branch,
        "base_branch": current.base_ref,
        "head_reachable": True,
        "publication_ownership_verified": True,
        "publication_ownership_error": "",
        "publication": {
            "invocation_id": current.publication.invocation_id,
            "slice_id": "architecture",
            "author_agent": "architecture",
            "succession_invocation_ids": [],
        },
    }
    transport = IntegrationTransport(snapshots=[watcher])

    result = driver_module._integrate_one_candidate(
        plan.sub_tls[0],
        state,
        replace(_config(), working_dir=str(repository)),
        EffectClient(transport),
        root_store,
        [],
    )

    assert result.slices["architecture"].status is SliceStatus.IN_REVIEW
    assert result.integration.candidates["architecture"].lifecycle is (
        IntegrationLifecycle.AGGREGATE_PR_OPEN
    )
    assert not any(name == "merge_pr" for name, _ in transport.calls)


def test_driver_keeps_aggregate_parked_when_git_lineage_is_unavailable(tmp_path: Path) -> None:
    plan, root_store, child_store = _nested_blocked_owner_stores(
        tmp_path,
        "driver-unavailable-aggregate-git",
        leaf_issue_id=811,
    )
    assert child_store is not None
    repository = Path(root_store.load().slices["architecture"].worktree or "")
    _complete_nested_leaf(child_store, repository=repository)
    _seed_nested_aggregate_in_review(root_store)
    state = root_store.load()
    current = state.slices["architecture"]
    candidate = state.integration.candidates["architecture"]
    assert current.publication is not None
    assert candidate.aggregate_head_sha is not None
    unavailable_repository = tmp_path / "unavailable-git-worktree"
    unavailable_repository.mkdir()
    candidate = replace(candidate, integration_owner_worktree=str(unavailable_repository))
    state = root_store.checkpoint(
        state.fsm,
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=replace(
            state.integration,
            candidates={**state.integration.candidates, "architecture": candidate},
        ),
    )
    watcher = {
        "found": True,
        "pr_number": candidate.aggregate_pr_number,
        "merged": False,
        "head_sha": candidate.aggregate_head_sha,
        "base_sha": candidate.aggregate_original_base_sha,
        "patch_digest": candidate.aggregate_patch_digest,
        "merge_tree_sha": "aggregate-merge-tree",
        "pr_head_tree_sha": _test_git_tree(repository, candidate.aggregate_head_sha),
        "ci_status": "success",
        "head_branch": current.branch,
        "base_branch": current.base_ref,
        "head_reachable": True,
        "publication_ownership_verified": True,
        "publication_ownership_error": "",
        "publication": {
            "invocation_id": current.publication.invocation_id,
            "slice_id": "architecture",
            "author_agent": "architecture",
            "succession_invocation_ids": [],
        },
    }
    transport = IntegrationTransport(snapshots=[watcher])

    result = driver_module._integrate_one_candidate(
        plan.sub_tls[0],
        state,
        replace(_config(), working_dir=str(repository)),
        EffectClient(transport),
        root_store,
        [],
    )

    assert result.slices["architecture"].status is SliceStatus.IN_REVIEW
    assert result.integration.candidates["architecture"].lifecycle is (
        IntegrationLifecycle.AGGREGATE_PR_OPEN
    )
    assert not any(name == "merge_pr" for name, _ in transport.calls)


def test_driver_does_not_merge_unrelated_head_that_matches_candidate_and_watcher(
    tmp_path: Path,
) -> None:
    plan, root_store, child_store = _nested_blocked_owner_stores(
        tmp_path,
        "driver-unrelated-aggregate-head",
        leaf_issue_id=811,
    )
    assert child_store is not None
    repository = Path(root_store.load().slices["architecture"].worktree or "")
    _complete_nested_leaf(child_store, repository=repository)
    _seed_nested_aggregate_in_review(root_store)
    state = root_store.load()
    current = state.slices["architecture"]
    candidate = state.integration.candidates["architecture"]
    assert current.publication is not None

    subprocess.run(
        ("git", "-C", str(repository), "checkout", "--orphan", "unrelated"),
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "rm", "-rf", "."),
        capture_output=True,
        check=True,
        text=True,
    )
    unrelated_head = _test_git_commit(repository, "unrelated.txt", "other work\n", "unrelated")
    unrelated_tree = _test_git_tree(repository, unrelated_head)
    unrelated_publication = replace(current.publication, head_sha=unrelated_head)
    unrelated_slice = replace(current, publication=unrelated_publication)
    unrelated_candidate = replace(
        candidate,
        aggregate_head_sha=unrelated_head,
        head_sha=unrelated_head,
    )
    state = root_store.checkpoint(
        state.fsm,
        {**state.slices, "architecture": unrelated_slice},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=replace(
            state.integration,
            candidates={**state.integration.candidates, "architecture": unrelated_candidate},
        ),
    )
    assert unrelated_publication is not None
    watcher = {
        "found": True,
        "pr_number": unrelated_slice.pr_number,
        "merged": False,
        "head_sha": unrelated_head,
        "base_sha": "aggregate-base",
        "patch_digest": unrelated_candidate.aggregate_patch_digest,
        "merge_tree_sha": "aggregate-merge-tree",
        "pr_head_tree_sha": unrelated_tree,
        "ci_status": "success",
        "head_branch": unrelated_slice.branch,
        "base_branch": unrelated_slice.base_ref,
        "head_reachable": True,
        "publication_ownership_verified": True,
        "publication_ownership_error": "",
        "publication": {
            "invocation_id": unrelated_publication.invocation_id,
            "slice_id": "architecture",
            "author_agent": "architecture",
            "succession_invocation_ids": [],
        },
    }
    transport = IntegrationTransport(snapshots=[watcher])
    result = driver_module._integrate_one_candidate(
        plan.sub_tls[0],
        state,
        replace(_config(), working_dir=str(repository)),
        EffectClient(transport),
        root_store,
        [],
    )

    assert result.slices["architecture"].status is SliceStatus.IN_REVIEW
    assert result.current_order == 1
    assert result.slices["kani"].status is SliceStatus.PENDING
    assert result.integration.candidates["architecture"].lifecycle is (
        IntegrationLifecycle.AGGREGATE_PR_OPEN
    )
    assert not any(name == "merge_pr" for name, _ in transport.calls)


def test_recovery_boundary_is_recorded_while_quarantine_is_pending(tmp_path: Path) -> None:
    """Historical catch-up is an explicit boundary before live operation (#1225)."""
    run_id = "recovery-boundary-run"
    plan = WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "task"}]})
    case = _confirmed_worker(tmp_path, run_id=run_id)
    store = RunStore(run_id, tmp_path)
    store.set_plan_manifest(
        build_plan_manifest({"workers": [{"name": "worker-a", "task": "task"}]}, scope_id=run_id),
        slices=case.state.slices,
    )
    store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("worker-a",)),
        case.state.slices,
        case.state.budgets,
        8,
    )
    store.quarantine_event(
        {
            "type": "pr.filed",
            "run_seq": 901,
            "run_id": run_id,
            "agent_id": "foreign-agent",
            "lifecycle_state": "observed",
            "observed_at": "2026-01-01T00:00:00Z",
            "data": {"slice_id": "foreign-slice", "pr_number": 77, "head_sha": "head-77"},
        }
    )

    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(_worker_transport()),
        config=replace(
            _config(),
            run_id=run_id,
            ledger_run_id=run_id,
            repository_identity=None,
            keep_alive_on_waiting=False,
            max_parallel_slices=None,
            policy=None,
        ),
        root_dir=tmp_path,
    )

    boundary = [
        entry
        for entry in result.journal_entries
        if entry.get("operation") == "emit_controller_event"
        and cast("dict[str, Any]", entry["arguments"]).get("event_type") == "tl.recovery_boundary"
    ]
    assert boundary, "the recovery boundary was not recorded"
    payload = cast("dict[str, Any]", cast("dict[str, Any]", boundary[0]["arguments"])["payload"])
    assert payload["reason"] == "historical_catch_up_pending"
    assert payload["pending_quarantine"] == 1
    assert payload["cursor"] == 8
    # The pending history was retained, not skipped to reach live mode.
    assert store.quarantined_events()


def test_recovery_consistency_hold_blocks_live_dispatch(tmp_path: Path) -> None:
    """An inconsistent recovered projection holds behind a named gate (#1225)."""
    run_id = "recovery-consistency-run"
    plan = WorkPlan(
        sub_tls=(SubTLTask("architecture", WorkPlan(), order=1, chainlink_issue_id=1052),)
    )
    config = replace(
        _config(),
        run_id=run_id,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
        keep_alive_on_waiting=False,
        max_parallel_slices=None,
        policy=None,
    )
    manifest = driver_module._manifest_for_plan(plan, run_id, config)
    store = RunStore(run_id, tmp_path)
    slices = _initial_slices(plan, config, tmp_path, run_id)
    create(
        run_id,
        {
            "slices": slices,
            "repository_identity": {"remote_url": "https://forge.example.com/org/repo.git"},
        },
        root_dir=tmp_path,
    )
    store.set_plan_manifest(manifest)
    # A candidate with no backing slice is exactly the kind of projection
    # disagreement recovery must refuse to schedule on.
    store.set_ordered_state(
        1,
        (OrderedStageState(1, ("architecture",)),),
        IntegrationRuntimeState(
            sub_tl_states={"architecture": IntegrationLifecycle.RUNNING},
            candidates={"ghost": IntegrationCandidateState()},
        ),
    )
    transport = RecordingTransport()

    result = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    assert any(
        gate.name == driver_module.RECOVERY_CONSISTENCY_GATE_NAME
        and gate.status is GateStatus.PENDING
        for gate in result.final_state.gates
    )
    assert "ghost" in result.diagnostics["recovery_consistency"]
    assert not any(
        name in {"spawn_leaf", "spawn_worker", "spawn_reviewer", "merge_pr"}
        for name, _ in transport.calls
    )


def test_finite_startup_prefix_drains_before_dispatch_and_live_events(
    tmp_path: Path,
) -> None:
    """The production driver drains its captured prefix before effects or live reads."""
    run_id = "finite-startup-prefix"
    plan = WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "task"}]})
    historical = _canonical_event(1, "inbox.message", "worker-a", run_id)
    live_events = [_canonical_event(2, "agent.spawned", "worker-a", run_id)] + [
        _event(3, "child_completed", "worker-a", run_id=run_id),
        _event(4, "all_children_done", run_id=run_id),
    ]

    class FiniteSnapshotQueue(SyntheticQueue):
        snapshot_count = 0

        def capture_snapshot(self, cursor: int) -> SimpleNamespace:
            self.snapshot_count += 1
            return SimpleNamespace(
                events=(historical,) if cursor < 1 else (),
                boundary_run_seq=1,
                sequence_status=SequenceStatus.COMPLETE,
                findings=(),
                active_tail=None,
            )

        def acknowledge(self, observed: EventEnvelope) -> int:
            if observed.run_seq == 1:
                assert not any(name == "spawn_worker" for name, _ in transport.calls)
            elif observed.run_seq in {2, 3, 4}:
                assert any(name == "spawn_worker" for name, _ in transport.calls)
            return super().acknowledge(observed)

    source = FiniteSnapshotQueue([historical, *live_events])
    transport = RecordingTransport()
    result = run_tl_loop(
        run_id,
        plan,
        source,
        EffectClient(transport),
        config=replace(_config(), keep_alive_on_waiting=False),
        root_dir=tmp_path,
    )

    assert source.snapshot_count == 1
    assert source.acknowledged == [1, 2, 3, 4]
    assert [name for name, _ in transport.calls].count("spawn_worker") == 1
    assert result.final_state.fsm.phase is TLPhase.TLDone
    boundary_index = next(
        index
        for index, (name, arguments) in enumerate(transport.calls)
        if name == "emit_controller_event"
        and arguments.get("event_type") == "tl.recovery_boundary"
        and cast(dict[str, Any], arguments["payload"]).get("reason")
        == "historical_catch_up_complete"
    )
    dispatch_index = next(
        index for index, (name, _) in enumerate(transport.calls) if name == "spawn_worker"
    )
    assert boundary_index < dispatch_index


def test_production_driver_accepts_contiguous_interior_supersession_history(
    tmp_path: Path,
) -> None:
    """Startup uses raw ledger continuity even when projection removes a row."""
    run_id = "driver-interior-supersession"
    segments = tmp_path / "segments"
    segments.mkdir()
    rows = [
        {
            "event_id": f"source-{sequence}",
            "id": f"source-{sequence}",
            "schema_version": 1,
            "event_time": "2026-10-16T00:00:00Z",
            "observed_at": "2026-10-16T00:00:00Z",
            "run_seq": sequence,
            "type": "test.unmapped_history",
            "run_id": run_id,
            "session_id": "session-test",
            "source": "test",
            "lifecycle_state": "emitted",
            "data": {},
        }
        for sequence in range(101, 104)
    ]
    rows.append(
        {
            **rows[-1],
            "event_id": "source-104",
            "id": "source-104",
            "run_seq": 104,
            "type": "event.superseded",
            "data": {"superseded_event_id": "source-102", "reason": "corrected"},
        }
    )
    (segments / "segment-000000000001.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    reader = LedgerReader(segments, ledger_run_id=run_id)

    class ReaderBackedSource:
        def capture_snapshot(self, cursor: int):
            return reader.capture_snapshot(cursor)

        def get(self, timeout: float | None = None) -> EventEnvelope:
            del timeout
            raise queue.Empty

        def acknowledge(self, event: EventEnvelope) -> int:
            assert event.run_seq is not None
            return event.run_seq

    snapshot = reader.capture_snapshot(cursor=0)
    assert snapshot.events == ()
    assert snapshot.boundary_run_seq == 104
    assert snapshot.sequence_status is SequenceStatus.COMPLETE

    transport = RecordingTransport()
    result = run_tl_loop(
        run_id,
        WorkPlan(),
        ReaderBackedSource(),
        EffectClient(transport),
        config=replace(_config(), keep_alive_on_waiting=False),
        root_dir=tmp_path,
    )

    assert result.final_state.fsm.phase is TLPhase.TLDone
    assert result.diagnostics.get("recovery_consistency") is None
    assert any(
        name == "emit_controller_event"
        and arguments.get("event_type") == "tl.recovery_boundary"
        and cast(dict[str, Any], arguments["payload"]).get("reason")
        == "historical_catch_up_complete"
        for name, arguments in transport.calls
    )
    assert result.final_state.events.last_consumed_offset <= 104


def test_incomplete_startup_ledger_holds_before_live_dispatch(tmp_path: Path) -> None:
    """A gap in the captured ledger prefix cannot release startup work."""
    run_id = "incomplete-startup-ledger"
    plan = WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "task"}]})

    class IncompleteSnapshotQueue(SyntheticQueue):
        def capture_snapshot(self, cursor: int) -> SimpleNamespace:
            return SimpleNamespace(
                events=(),
                boundary_run_seq=cursor + 2,
                sequence_status=SequenceStatus.PARTIAL,
                findings=(),
                active_tail=None,
            )

    transport = RecordingTransport()
    result = run_tl_loop(
        run_id,
        plan,
        IncompleteSnapshotQueue([]),
        EffectClient(transport),
        config=replace(_config(), keep_alive_on_waiting=False),
        root_dir=tmp_path,
    )

    assert result.diagnostics["recovery_consistency"] == "ledger_sequence_gap_or_duplicate"
    assert any(
        gate.name == driver_module.RECOVERY_CONSISTENCY_GATE_NAME
        and gate.status is GateStatus.PENDING
        for gate in result.final_state.gates
    )
    assert not any(
        name in {"spawn_worker", "spawn_leaf", "spawn_reviewer", "merge_pr"}
        for name, _ in transport.calls
    )


def test_missing_startup_snapshot_capability_holds_before_dispatch(tmp_path: Path) -> None:
    """A source without a durable startup boundary cannot start runnable work."""
    run_id = "missing-startup-snapshot"
    plan = WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "task"}]})

    class MissingSnapshotQueue:
        def get(self, timeout: float | None = None) -> EventEnvelope:
            del timeout
            raise queue.Empty

        def acknowledge(self, event: EventEnvelope) -> int:
            del event
            raise AssertionError("an empty source cannot acknowledge an event")

    transport = RecordingTransport()
    result = run_tl_loop(
        run_id,
        plan,
        MissingSnapshotQueue(),
        EffectClient(transport),
        config=replace(_config(), keep_alive_on_waiting=False),
        root_dir=tmp_path,
    )

    assert result.diagnostics["recovery_consistency"] == (
        "ledger_snapshot_unavailable: event source has no snapshot"
    )
    assert not any(name == "spawn_worker" for name, _ in transport.calls)


def test_unavailable_startup_snapshot_preserves_reader_error_before_dispatch(
    tmp_path: Path,
) -> None:
    run_id = "unavailable-startup-snapshot"
    plan = WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "task"}]})

    class UnavailableSnapshotQueue(SyntheticQueue):
        def capture_snapshot(self, cursor: int) -> SimpleNamespace:
            raise QueueError(
                cursor=cursor,
                sequence_status=SequenceStatus.UNKNOWN,
                cause=OSError("ledger unavailable"),
            )

    transport = RecordingTransport()
    with pytest.raises(QueueError, match="OSError: ledger unavailable"):
        run_tl_loop(
            run_id,
            plan,
            UnavailableSnapshotQueue([]),
            EffectClient(transport),
            config=replace(_config(), keep_alive_on_waiting=False),
            root_dir=tmp_path,
        )
    assert not any(name == "spawn_worker" for name, _ in transport.calls)


def test_startup_snapshot_queue_mismatch_holds_without_acknowledging(tmp_path: Path) -> None:
    run_id = "startup-snapshot-queue-mismatch"
    plan = WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "task"}]})
    expected = _canonical_event(1, "inbox.message", "worker-a", run_id)
    observed = _canonical_event(2, "agent.spawned", "worker-a", run_id)

    class MismatchedQueue(SyntheticQueue):
        def capture_snapshot(self, cursor: int) -> SimpleNamespace:
            return SimpleNamespace(
                events=(expected,),
                boundary_run_seq=1,
                sequence_status=SequenceStatus.COMPLETE,
                findings=(),
                active_tail=None,
            )

    transport = RecordingTransport()
    source = MismatchedQueue([observed])
    result = run_tl_loop(
        run_id,
        plan,
        source,
        EffectClient(transport),
        config=replace(_config(), keep_alive_on_waiting=False),
        root_dir=tmp_path,
    )

    assert result.diagnostics["recovery_consistency"] == "ledger_snapshot_queue_mismatch"
    assert source.acknowledged == []
    assert not any(name == "spawn_worker" for name, _ in transport.calls)


def test_startup_snapshot_drain_timeout_parks_before_dispatch(tmp_path: Path, monkeypatch) -> None:
    run_id = "startup-snapshot-drain-timeout"
    plan = WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "task"}]})
    historical = _canonical_event(1, "inbox.message", "worker-a", run_id)

    class MissingRowQueue(SyntheticQueue):
        def capture_snapshot(self, cursor: int) -> SimpleNamespace:
            return SimpleNamespace(
                events=(historical,),
                boundary_run_seq=1,
                sequence_status=SequenceStatus.COMPLETE,
                findings=(),
                active_tail=None,
            )

    monkeypatch.setattr(driver_module, "STARTUP_DRAIN_EMPTY_POLL_LIMIT", 1)
    transport = RecordingTransport()
    result = run_tl_loop(
        run_id,
        plan,
        MissingRowQueue([]),
        EffectClient(transport),
        config=replace(_config(), keep_alive_on_waiting=False),
        root_dir=tmp_path,
    )

    assert result.diagnostics["recovery_consistency"] == "ledger_snapshot_drain_timeout"
    assert not any(name == "spawn_worker" for name, _ in transport.calls)


class _PollWorkersTransport(RecordingTransport):
    """RecordingTransport that answers heartbeat liveness polling."""

    cancel_event: object | None = None

    def call_tool(
        self, role: str, name: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if tool_name == "poll_workers":
            self.calls.append((tool_name, arguments))
            agents = arguments.get("agents", [])
            rows = [
                {"name": str(agent), "pane_alive": True}
                for agent in agents
                if isinstance(agent, str)
            ]
            if self.cancel_event is not None:
                self.cancel_event.set()  # type: ignore[attr-defined]
            return {"success": True, "result": {"workers": rows, "missing_agents": []}}
        return super().call_tool(role, name, tool_name, arguments)


def test_live_heartbeat_resumes_when_startup_quarantine_never_resolves(
    tmp_path: Path,
) -> None:
    """A startup row that never resolves must not disable live operation (#1225).

    The recovery gate is bounded by the startup catch-up pass, not by whatever
    is still sitting in the live quarantine list. A stray foreign row left at
    startup (and any resolvable-impossible row appended later) therefore cannot
    switch off the heartbeat, finalization, or convergence for the run.
    """
    from tl_loop.loop.heartbeat import HeartbeatConfig

    run_id = "recovery-unresolvable-run"
    plan = WorkPlan.from_mapping({"workers": [{"name": "worker-a", "task": "task"}]})
    case = _confirmed_worker(tmp_path, run_id=run_id)
    store = RunStore(run_id, tmp_path)
    store.set_plan_manifest(
        build_plan_manifest({"workers": [{"name": "worker-a", "task": "task"}]}, scope_id=run_id),
        slices=case.state.slices,
    )
    store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("worker-a",)),
        case.state.slices,
        case.state.budgets,
        8,
    )
    store.quarantine_event(
        {
            "type": "pr.filed",
            "run_seq": 902,
            "run_id": run_id,
            "agent_id": "foreign-agent",
            "lifecycle_state": "observed",
            "observed_at": "2026-01-01T00:00:00Z",
            "data": {"slice_id": "foreign-slice", "pr_number": 78, "head_sha": "head-78"},
        }
    )
    cancel = threading.Event()
    transport = _PollWorkersTransport()
    transport.cancel_event = cancel
    # Fallback so a regressed (gated) heartbeat cannot hang the test.
    fallback = threading.Timer(3.0, cancel.set)
    fallback.start()
    try:
        run_tl_loop(
            run_id,
            plan,
            SyntheticQueue([]),
            EffectClient(transport),
            config=replace(
                _config(),
                run_id=run_id,
                ledger_run_id=run_id,
                repository_identity=None,
                keep_alive_on_waiting=True,
                max_parallel_slices=None,
                policy=None,
                project_root=tmp_path,
                heartbeat=HeartbeatConfig(
                    interval_seconds=0.001,
                    stall_threshold_seconds=1000.0,
                    task_timeout_seconds=0.0,
                ),
                cancel_event=cancel,
            ),
            root_dir=tmp_path,
        )
    except LoopCancelled:
        pass
    finally:
        fallback.cancel()

    assert any(name == "poll_workers" for name, _ in transport.calls), (
        "live heartbeat never ran while an unresolvable startup row stayed quarantined"
    )


def test_replay_does_not_read_work_clock_before_live_heartbeat(
    tmp_path: Path,
) -> None:
    """A replayable historical row cannot start or consume active-work time."""
    from tl_loop.loop.heartbeat import HeartbeatConfig

    run_id = "work-clock-replay-boundary"
    plan_document = {"workers": [{"name": "worker-a", "task": "task"}]}
    case = _confirmed_worker(tmp_path, run_id=run_id)
    store = RunStore(run_id, tmp_path)
    store.set_plan_manifest(
        build_plan_manifest(plan_document, scope_id=run_id), slices=case.state.slices
    )
    current = replace(
        case.state.slices["worker-a"],
        dispatch_started_at=time.time() - 23 * 24 * 60 * 60,
        task_timeout_seconds=3600.0,
        task_timeout_source="project",
    )
    store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("worker-a",)),
        {"worker-a": current},
        case.state.budgets,
        8,
    )
    store.quarantine_event(
        {
            "schema_version": 1,
            "event_id": "replayed-spawn-confirmation",
            "id": "replayed-spawn-confirmation",
            "event_time": "2026-09-30T16:22:12Z",
            "observed_at": "2026-09-30T16:22:12Z",
            "run_seq": 9,
            "type": "agent.spawned",
            "agent_id": "root",
            "run_id": run_id,
            "session_id": "session-1",
            "invocation_id": None,
            "generation": None,
            "lifecycle_state": "emitted",
            "data": {
                "agent_type": "worker",
                "intent_id": case.intent_id,
                "slug": "worker-a",
                "task_summary": "task",
            },
        }
    )

    class _AfterCatchupQueue(SyntheticQueue):
        live_poll_started = False

        def get(self, timeout: float | None = None) -> EventEnvelope:
            self.live_poll_started = True
            return super().get(timeout)

    source = _AfterCatchupQueue([])

    class _ReplayGuardClock:
        calls = 0

        def __call__(self) -> float:
            assert source.live_poll_started, "work clock was read before historical catch-up"
            self.calls += 1
            return self.calls * 0.01

    work_clock = _ReplayGuardClock()
    cancel = threading.Event()
    transport = _PollWorkersTransport()
    transport.cancel_event = cancel
    fallback = threading.Timer(3.0, cancel.set)
    fallback.start()
    try:
        with pytest.raises(LoopCancelled):
            run_tl_loop(
                run_id,
                WorkPlan.from_mapping(plan_document),
                cast(Any, source),
                EffectClient(transport),
                config=replace(
                    _config(),
                    run_id=run_id,
                    ledger_run_id=run_id,
                    repository_identity=None,
                    keep_alive_on_waiting=True,
                    max_parallel_slices=None,
                    policy=None,
                    project_root=tmp_path,
                    work_clock=work_clock,
                    heartbeat=HeartbeatConfig(
                        interval_seconds=0.001,
                        stall_threshold_seconds=1000.0,
                        task_timeout_seconds=3600.0,
                    ),
                    cancel_event=cancel,
                ),
                root_dir=tmp_path,
            )
    finally:
        fallback.cancel()

    assert source.live_poll_started
    assert work_clock.calls > 0, "the live active worker did not start its invocation clock"
    assert not store.quarantined_events(), "the historical spawn confirmation was not replayed"
    assert store.load().slices["worker-a"].status is SliceStatus.SPAWNED
    assert not any(name == "close_worker_pane" for name, _ in transport.calls)


def test_recovery_consistency_gate_clears_when_projection_is_restored(tmp_path: Path) -> None:
    """A transient inconsistency closes its own hold on the next recovery."""
    run_id = "recovery-consistency-restore-run"
    plan = WorkPlan(
        sub_tls=(SubTLTask("architecture", WorkPlan(), order=1, chainlink_issue_id=1052),)
    )
    config = replace(
        _config(),
        run_id=run_id,
        ledger_run_id=run_id,
        repository_identity=RepositoryIdentity("https://forge.example.com/org/repo.git"),
        keep_alive_on_waiting=False,
        max_parallel_slices=None,
        policy=None,
    )
    manifest = driver_module._manifest_for_plan(plan, run_id, config)
    store = RunStore(run_id, tmp_path)
    slices = _initial_slices(plan, config, tmp_path, run_id)
    create(
        run_id,
        {
            "slices": slices,
            "repository_identity": {"remote_url": "https://forge.example.com/org/repo.git"},
        },
        root_dir=tmp_path,
    )
    store.set_plan_manifest(manifest)
    store.set_ordered_state(
        1,
        (OrderedStageState(1, ("architecture",)),),
        IntegrationRuntimeState(
            sub_tl_states={"architecture": IntegrationLifecycle.RUNNING},
            candidates={"ghost": IntegrationCandidateState()},
        ),
    )

    held = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=config,
        root_dir=tmp_path,
    )
    assert any(
        gate.name == driver_module.RECOVERY_CONSISTENCY_GATE_NAME
        and gate.status is GateStatus.PENDING
        for gate in held.final_state.gates
    )

    # The backing slice appears / the bogus candidate is gone: consistent again.
    loaded = store.load()
    store.checkpoint(
        loaded.fsm,
        loaded.slices,
        loaded.budgets,
        loaded.events.last_consumed_offset,
        integration=replace(loaded.integration, candidates={}),
    )
    restored = run_tl_loop(
        run_id,
        plan,
        SyntheticQueue([]),
        EffectClient(RecordingTransport()),
        config=config,
        root_dir=tmp_path,
    )

    assert not any(
        gate.name == driver_module.RECOVERY_CONSISTENCY_GATE_NAME
        for gate in restored.final_state.gates
    )
    reasons = [
        cast("dict[str, Any]", entry["arguments"])["payload"]["reason"]
        for entry in restored.journal_entries
        if entry.get("operation") == "emit_controller_event"
        and cast("dict[str, Any]", entry["arguments"]).get("event_type") == "tl.recovery_boundary"
    ]
    assert "consistency_restored" in reasons
