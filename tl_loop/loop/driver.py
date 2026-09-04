"""Bounded active and shadow execution for the programmatic TL."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import multiprocessing
import queue as queue_module
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

from tl_loop.client.effects import EffectClient, ToolResult, ToolUnavailableError
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import JsonObject, TransportClient
from tl_loop.events.envelope import BlockCause, EnvelopeError, EventEnvelope, EventKind, project
from tl_loop.events.identity import (
    IdentityResolution,
    envelope_document,
    resolve_event_slice,
)
from tl_loop.events.reader import FindingKind, LedgerFinding, LedgerReader, LedgerReadError
from tl_loop.fsm.child import ChildKind, ChildRecord
from tl_loop.fsm.event import (
    AllChildrenDone,
    ChildCompleted,
    ChildFailed,
    ChildSpawned,
    PRFiled,
    PRMerged,
    PRUpdated,
    TLEvent,
)
from tl_loop.fsm.lane import (
    LaneAbandoned,
    LaneBookkeepingStarted,
    LaneIntegrationStarted,
    LanePhase,
    LaneRecoveryRequested,
    LaneRecoveryResolved,
    LaneReleased,
    LaneReserved,
    LaneState,
    transition_lane,
)
from tl_loop.fsm.orchestration import transition as scope_transition
from tl_loop.fsm.phase import (
    ChildHandle,
    PhaseValue,
    TLAllMerged,
    TLDone,
    TLFailed,
    TLMerging,
    TLPhase,
    TLPlanning,
    TLWaiting,
)
from tl_loop.fsm.post_merge import PostMergePhase
from tl_loop.fsm.post_merge_events import (
    ChangelogCommitted,
    ChangelogPending,
    IssueCloseConfirmed,
    IssueClosePending,
    MergeAdopted,
    ParentBranchSynced,
    ParentPushPending,
    PostMergeComplete,
    PostMergeRebuildRequested,
)
from tl_loop.fsm.post_merge_evidence import PushReceipt
from tl_loop.fsm.recovery import RecoveryPhase, begin_recovery, transition_recovery
from tl_loop.fsm.scope import (
    TLAllMerged as RecursiveTLAllMerged,
)
from tl_loop.fsm.scope import (
    TLDone as RecursiveTLDone,
)
from tl_loop.fsm.scope import (
    TLFailed as RecursiveTLFailed,
)
from tl_loop.fsm.scope import (
    TLFinalizing as RecursiveTLFinalizing,
)
from tl_loop.fsm.scope import (
    TLParked as RecursiveTLParked,
)
from tl_loop.fsm.scope import (
    TLPlanning as RecursiveTLPlanning,
)
from tl_loop.fsm.scope import (
    TLPRFiled as RecursiveTLPRFiled,
)
from tl_loop.fsm.scope import (
    TLRunning as RecursiveTLRunning,
)
from tl_loop.fsm.scope_events import (
    FailureRecorded as ScopeFailureRecorded,
)
from tl_loop.fsm.scope_events import (
    FinalizationComplete as ScopeFinalizationComplete,
)
from tl_loop.fsm.scope_events import (
    FinalizationRequested as ScopeFinalizationRequested,
)
from tl_loop.fsm.scope_events import (
    LeafCompleted as ScopeLeafCompleted,
)
from tl_loop.fsm.scope_events import (
    ScopeRole,
)
from tl_loop.fsm.scope_events import (
    StageReleased as ScopeStageReleased,
)
from tl_loop.fsm.scope_events import (
    WorkerCompleted as ScopeWorkerCompleted,
)
from tl_loop.fsm.scope_projection import active_child_ids
from tl_loop.fsm.transition import IllegalTransition
from tl_loop.fsm.transition import transition as phase_transition
from tl_loop.loop.convergence import (
    ConvergenceInvariantError,
    ConvergenceTracker,
)
from tl_loop.loop.escalate import park
from tl_loop.loop.heartbeat import HeartbeatConfig, SyntheticHeartbeatEvent, heartbeat_once
from tl_loop.loop.observability import emit_controller_event
from tl_loop.loop.recovery_policy import policy_for_cause
from tl_loop.loop.review import (
    IntegrationEvidenceMismatch,
    ReviewContract,
    ReviewGateError,
    ReviewPolicySnapshot,
    compose_acceptance_criteria,
    compose_review_contract,
    invalidate_integration_evidence,
    load_freshness_window,
    load_reviewer_max_rounds,
    load_reviewer_policy_snapshot,
    verdict_is_stale,
    verify_integration,
    verify_review,
)
from tl_loop.loop.schedule import ScheduleDeadlock, ready, suspend_dependents
from tl_loop.ordered import (
    AggregateCandidate,
    ChildRecoverySummary,
    IntegrationContract,
    IntegrationLifecycle,
    IntegrationState,
    IntegrationTransition,
    IntegrationTransitionError,
    OrderedStage,
    ReviewOwner,
    SubTLLifecycle,
    transition_integration,
)
from tl_loop.rlm.adjudicate import adjudicate_review
from tl_loop.rlm.repair import RepairError, RepairHandoff, compose_repair
from tl_loop.select.agent_type import parse_harness_identifier, select_agent_type, selection_failure
from tl_loop.select.capability import CapabilityMap, load_capability
from tl_loop.select.classify import Difficulty
from tl_loop.select.learned_policy import LearnedPolicy
from tl_loop.select.ledger import apply_spawn_and_charge
from tl_loop.select.model import ModelCatalog, select_model, select_model_for_difficulty
from tl_loop.select.policy import HarnessPolicy, load_policy
from tl_loop.state.legacy_manifest import (
    LegacyManifestReconciliation,
    reconcile_legacy_manifest,
)
from tl_loop.state.plan_manifest import (
    ManifestError,
    PlanManifest,
    build_plan_manifest,
)
from tl_loop.state.review_validation import review_validation_disposition
from tl_loop.state.schema import (
    CI_STATUS_VALUES,
    REDUCER_VERSION,
    ActionKind,
    ActionPhase,
    ActionState,
    BudgetLedger,
    GateStatus,
    GoalState,
    HandoffEvidence,
    IntegrationCandidateState,
    IntegrationRuntimeState,
    OrderedStageState,
    ParkCause,
    PublicationBinding,
    RepositoryIdentity,
    ReviewValidationDisposition,
    ReviewValidationObservation,
    RunState,
    SliceState,
    SliceStatus,
    Verdict,
)
from tl_loop.state.serialization import DurableWriteError
from tl_loop.state.slice_transition import (
    ActionChanged,
    CIStatusObserved,
    HeadChanged,
    HeadEvidenceObserved,
    MergeCompleted,
    PostMergeEventObserved,
    RepairQueued,
    RevalidateReview,
    ReviewDiscarded,
    ReviewerDispatched,
    ReviewerIdentityObserved,
    ReviewRoundsExhausted,
    ReviewValidated,
    ReviewValidationFailed,
    ReviewVerdictObserved,
    SliceStatusChanged,
    StallClassificationObserved,
    slice_transition,
)
from tl_loop.state.store import DEFAULT_ROOT, RunStore, create

from .journal import MUTATING_OPERATIONS, ActionJournalError, EffectJournal, stable_action_key
from .observation import WatcherObservation
from .reconcile import (
    ExternalIntent,
    InternalTransition,
    Quiescent,
    ReconciliationResult,
    _publication_ownership_status,
    derive_next_action,
    reconcile_merge_observation,
    reconcile_slice,
)
from .shadow import TLEventDecoder, _phase_from_state, _phase_tag, _update_slices

LOGGER = logging.getLogger(__name__)
DISPATCH_FAILURE_GATE_NAME = "tl-dispatch-failed"
INTEGRATION_REVALIDATION_GATE_NAME = "tl-integration-revalidation"
INTEGRATION_CONFLICT_GATE_NAME = "tl-integration-conflict"
INTEGRITY_RECONCILIATION_GATE_NAME = "tl-integrity-reconciliation"
REPOSITORY_IDENTITY_GATE_NAME = "tl-repository-identity"
MERGE_RECOVERY_GATE_PREFIX = "tl-merge-recovery-"
MAX_CONVERGENCE_STEPS = 8
# A per-call fairness cap on _drain_direct_scope_convergence, not a
# correctness bound: that function raises the moment a step makes no
# progress, so exhausting this many *progressing* steps in one call just
# means the next "no event" poll picks up and continues draining -- it is
# not evidence of being stuck. Distinct from MAX_CONVERGENCE_STEPS, which
# bounds internal steps *within* a single _apply_convergence call to reach
# one action or wait state, not the number of action boundaries a whole
# direct-leaf/worker scope may need.
DIRECT_SCOPE_DRAIN_STEP_LIMIT = 64
DISPATCHING_STATUSES = frozenset({SliceStatus.DISPATCHING, SliceStatus.DISPATCH_UNCONFIRMED})
REMOTE_ADVANCE_FAILURE_MARKERS = (
    "force-with-lease",
    "stale info",
    "non-fast-forward",
    "non fast-forward",
    "fetch first",
)


def _transition_sub_tl_lifecycle(
    current: IntegrationLifecycle, event: IntegrationTransition
) -> IntegrationLifecycle:
    """Apply the centralized ordered-integration contract to one candidate."""
    return transition_integration(IntegrationState(lifecycle=current), event).lifecycle


def _candidate_runtime(
    integration: IntegrationRuntimeState, candidate_id: str
) -> IntegrationRuntimeState:
    """Read one candidate record, falling back to legacy single-candidate state."""
    candidate = integration.candidates.get(candidate_id)
    if candidate is None:
        return integration
    if (
        len(integration.candidates) == 1
        and candidate == IntegrationCandidateState()
        and integration.lifecycle is not IntegrationLifecycle.RUNNING
    ):
        return replace(integration, candidates={})
    return replace(
        integration,
        lifecycle=candidate.lifecycle,
        aggregate_pr_number=candidate.aggregate_pr_number,
        aggregate_head_sha=candidate.aggregate_head_sha,
        aggregate_patch_digest=candidate.aggregate_patch_digest,
        aggregate_original_base_sha=candidate.aggregate_original_base_sha,
        integration_owner_id=candidate.integration_owner_id,
        integration_owner_run_id=candidate.integration_owner_run_id,
        integration_owner_branch=candidate.integration_owner_branch,
        integration_owner_worktree=candidate.integration_owner_worktree,
        head_sha=candidate.head_sha,
        patch_digest=candidate.patch_digest,
        validated_base_sha=candidate.validated_base_sha,
        merge_tree_sha=candidate.merge_tree_sha,
        integration_evidence_at=candidate.integration_evidence_at,
        ci_status=candidate.ci_status,
        merge_attempts=candidate.merge_attempts,
        base_revalidation_count=candidate.base_revalidation_count,
        stage_verification=candidate.stage_verification,
        candidates={},
    )


def _persist_candidate_runtime(
    integration: IntegrationRuntimeState,
    candidate_id: str,
    candidate: IntegrationRuntimeState,
) -> IntegrationRuntimeState:
    """Write one candidate without discarding sibling candidate records."""
    candidates = dict(integration.candidates)
    candidates[candidate_id] = IntegrationCandidateState(
        lifecycle=candidate.lifecycle,
        aggregate_pr_number=candidate.aggregate_pr_number,
        aggregate_head_sha=candidate.aggregate_head_sha,
        aggregate_patch_digest=candidate.aggregate_patch_digest,
        aggregate_original_base_sha=candidate.aggregate_original_base_sha,
        integration_owner_id=candidate.integration_owner_id,
        integration_owner_run_id=candidate.integration_owner_run_id,
        integration_owner_branch=candidate.integration_owner_branch,
        integration_owner_worktree=candidate.integration_owner_worktree,
        head_sha=candidate.head_sha,
        patch_digest=candidate.patch_digest,
        validated_base_sha=candidate.validated_base_sha,
        merge_tree_sha=candidate.merge_tree_sha,
        integration_evidence_at=candidate.integration_evidence_at,
        ci_status=candidate.ci_status,
        merge_attempts=candidate.merge_attempts,
        base_revalidation_count=candidate.base_revalidation_count,
        stage_verification=candidate.stage_verification,
    )
    # Keep legacy fields as a compatibility view; all ordered production reads
    # use _candidate_runtime and therefore cannot mix sibling evidence.
    return replace(
        integration,
        lifecycle=candidate.lifecycle,
        aggregate_pr_number=candidate.aggregate_pr_number,
        aggregate_head_sha=candidate.aggregate_head_sha,
        aggregate_patch_digest=candidate.aggregate_patch_digest,
        aggregate_original_base_sha=candidate.aggregate_original_base_sha,
        integration_owner_id=candidate.integration_owner_id,
        integration_owner_run_id=candidate.integration_owner_run_id,
        integration_owner_branch=candidate.integration_owner_branch,
        integration_owner_worktree=candidate.integration_owner_worktree,
        head_sha=candidate.head_sha,
        patch_digest=candidate.patch_digest,
        validated_base_sha=candidate.validated_base_sha,
        merge_tree_sha=candidate.merge_tree_sha,
        integration_evidence_at=candidate.integration_evidence_at,
        ci_status=candidate.ci_status,
        merge_attempts=candidate.merge_attempts,
        base_revalidation_count=candidate.base_revalidation_count,
        stage_verification=candidate.stage_verification,
        candidates=candidates,
    )


def _sub_tls_waiting_for_integration(plan: WorkPlan, state: RunState) -> bool:
    """Report recursive work that must remain alive until a later event."""
    tasks = {task.name: task for task in plan.sub_tls}
    stage_ids = {
        task_id
        for stage in plan.ordered_stages
        if stage.order == state.current_order
        for task_id in stage.sub_tls
    }
    return any(
        task_id in state.slices
        and not _ordered_child_complete(tasks[task_id], state.slices[task_id])
        for task_id in stage_ids
    )


def _ordered_slice_complete(current: SliceState) -> bool:
    """Check a persisted slice's post-merge release condition."""
    return current.status is SliceStatus.MERGED and (
        current.pr_number is None
        or (current.post_merge is not None and current.post_merge.phase is PostMergePhase.COMPLETE)
    )


def _ordered_child_complete(task: SubTLTask, current: SliceState) -> bool:
    """Require aggregate post-merge recovery before releasing a child barrier."""
    if not task.integration.aggregate_pr_required:
        return _ordered_slice_complete(current)
    return (
        current.status is SliceStatus.MERGED
        and current.pr_number is not None
        and current.post_merge is not None
        and current.post_merge.phase is PostMergePhase.COMPLETE
    )


def _ordered_stage_complete(
    tasks: Sequence[SubTLTask],
    slices: Mapping[str, SliceState],
) -> bool:
    """Check one stage using its child-specific completion contract."""
    return all(_ordered_child_complete(task, slices[task.name]) for task in tasks)


class TLLoopError(RuntimeError):
    """The TL loop cannot continue without operator intervention."""


class LoopLimitExceeded(TLLoopError):
    """The loop reached its event ceiling before reaching a terminal state."""


class LoopCancelled(TLLoopError):
    """The caller explicitly cancelled the controller without changing state."""


class DepthLimitExceeded(TLLoopError):
    """A recursive child reached the configured depth ceiling."""


class EffectFailed(TLLoopError):
    """An active effect returned an explicit failure."""


@dataclass(frozen=True)
class DispatchAttempt:
    """Durable identity assigned to one external child-spawn attempt."""

    intent_id: str
    started_at: float
    harness: str
    agent_type: str = ""
    model: str | None = None
    attempt: int = 0
    controller_epoch: str | None = None
    dispatch_generation: int = 0


@dataclass(frozen=True)
class DispatchCorrelation:
    """Classify a spawn observation without inferring ownership from history."""

    classification: str
    slice_id: str | None = None
    reason: str | None = None


DISPATCH_CORRELATED = "correlated"
DISPATCH_HISTORICAL_AUDIT = "historical_audit"
DISPATCH_INTEGRITY_CONFLICT = "integrity_conflict"


@dataclass
class EventDiagnostics:
    """Counters explaining what the controller did with projected events."""

    controller_started_at: float = field(default_factory=time.time)
    task_started_at: dict[str, float] = field(default_factory=dict)
    received: int = 0
    acknowledged: int = 0
    filtered: int = 0
    correlated: int = 0
    rejected: int = 0
    last_event_seq: int | None = None
    last_authoritative_event_seq: int | None = None
    last_observed_progress_at: float | None = None
    reader_finding_keys: set[tuple[str, str, str, int]] = field(default_factory=set)
    reader_findings: list[str] = field(default_factory=list)
    unresolved_event_keys: set[tuple[int, str]] = field(default_factory=set)
    unresolved_events: list[str] = field(default_factory=list)

    def record_reader_findings(self, findings: Sequence[LedgerFinding]) -> None:
        for finding in findings:
            key = (
                finding.kind.value,
                finding.event_type,
                str(finding.segment),
                finding.line_number,
            )
            if key in self.reader_finding_keys:
                continue
            self.reader_finding_keys.add(key)
            self.reader_findings.append(finding.message)
            if finding.kind is FindingKind.RUN_ID_MISMATCH:
                self.received += 1

    def record_unresolved_event(self, event: EventEnvelope, resolution: IdentityResolution) -> None:
        """Record a valid observation retained until ownership is proven."""
        key = (event.run_seq or 0, event.event_type)
        if key in self.unresolved_event_keys:
            return
        self.unresolved_event_keys.add(key)
        details = (
            f"{event.event_type} seq={event.run_seq} reason={resolution.reason} "
            f"candidates={list(resolution.candidates)}"
        )
        self.unresolved_events.append(details)
        self.rejected += 1

    def snapshot(self) -> Mapping[str, object]:
        return {
            "controller_started_at": self.controller_started_at,
            "elapsed_seconds": max(0.0, time.time() - self.controller_started_at),
            "task_started_at": dict(self.task_started_at),
            "received": self.received,
            "acknowledged": self.acknowledged,
            "filtered": self.filtered,
            "correlated": self.correlated,
            "rejected": self.rejected,
            "last_event_seq": self.last_event_seq,
            "last_authoritative_event_seq": self.last_authoritative_event_seq,
            "last_observed_progress_at": self.last_observed_progress_at,
            "reader_findings": list(self.reader_findings),
            "unresolved_events": list(self.unresolved_events),
        }


class EventQueue(Protocol):
    """Queue capability consumed by both active and shadow loops."""

    def get(self, timeout: float | None = None) -> EventEnvelope:
        """Return the next projected event."""

    def acknowledge(self, event: EventEnvelope) -> int:
        """Persist consumption of one event sequence."""


@dataclass(frozen=True)
class WorkerTask:
    """One ephemeral worker task dispatched by the TL."""

    name: str
    task: str
    agent_type: str | None = None
    task_timeout_seconds: float | None = None
    task_timeout_declared: bool = False

    def __post_init__(self) -> None:
        _require_text(self.name, "worker name")
        _require_text(self.task, "worker task")
        _optional_text(self.agent_type, "worker agent_type")
        _validate_task_timeout(self.task_timeout_seconds, "worker task_timeout_seconds")


@dataclass(frozen=True)
class LeafTask:
    """One PR-producing dev-leaf task dispatched by the TL."""

    name: str
    task: str
    agent_type: str | None = None
    boundary: tuple[str, ...] = ()
    context: str | None = None
    read_first: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    verify: tuple[str, ...] = ()
    done_criteria: tuple[str, ...] = ()
    task_timeout_seconds: float | None = None
    task_timeout_declared: bool = False

    def __post_init__(self) -> None:
        _require_text(self.name, "leaf name")
        _require_text(self.task, "leaf task")
        _optional_text(self.agent_type, "leaf agent_type")
        _optional_text(self.context, "leaf context")
        _validate_task_timeout(self.task_timeout_seconds, "leaf task_timeout_seconds")
        for field_name, values in (
            ("boundary", self.boundary),
            ("read_first", self.read_first),
            ("steps", self.steps),
            ("verify", self.verify),
            ("done_criteria", self.done_criteria),
        ):
            _text_tuple(values, f"leaf {field_name}")


@dataclass(frozen=True)
class WorkPlan:
    """Direct children the TL may dispatch for one bounded run."""

    workers: tuple[WorkerTask, ...] = ()
    leaves: tuple[LeafTask, ...] = ()
    sub_tls: tuple[SubTLTask, ...] = ()

    def __post_init__(self) -> None:
        names = (
            [task.name for task in self.workers]
            + [task.name for task in self.leaves]
            + [task.name for task in self.sub_tls]
        )
        if len(names) != len(set(names)):
            raise ValueError("worker and leaf names must be unique")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], *, path: str = "plan") -> WorkPlan:
        """Parse the small, closed plan shape used by the TL entry point."""
        unknown = sorted(set(value) - {"workers", "leaves", "sub_tls"})
        if unknown:
            raise ValueError(f"work plan contains unknown keys: {', '.join(unknown)}")
        parsed = cls(
            workers=_workers(value.get("workers", ())),
            leaves=_leaves(value.get("leaves", ())),
            sub_tls=_sub_tls(value.get("sub_tls", ()), path=f"{path}.sub_tls"),
        )
        return normalize_work_plan(parsed, path=path)

    @property
    def ordered_stages(self) -> tuple[OrderedStage, ...]:
        """Group direct sub-TLs by positive sibling order, lowest first."""
        grouped: dict[int, list[str]] = {}
        for task in self.sub_tls:
            grouped.setdefault(task.order, []).append(task.name)
        return tuple(
            OrderedStage(order, tuple(sorted(grouped[order]))) for order in sorted(grouped)
        )


@dataclass(frozen=True)
class SubTLTask:
    """One recursive child TL executed with an isolated nested run-state."""

    name: str
    plan: WorkPlan | Mapping[str, object]
    source: EventQueue | None = None
    effects: EffectClient | ReadOnlyEffectClient | None = None
    agent_type: str | None = None
    worktree: str | Path | None = None
    agent_id: str | None = None
    order: int = 1
    integration: IntegrationContract = field(default_factory=IntegrationContract)
    order_explicit: bool = True
    task_timeout_seconds: float | None = None
    task_timeout_declared: bool = False

    def __post_init__(self) -> None:
        _require_text(self.name, "sub-TL name")
        if not isinstance(self.plan, (WorkPlan, Mapping)):
            raise TypeError("sub-TL plan must be a WorkPlan or object")
        _optional_text(self.agent_type, "sub-TL agent_type")
        _optional_text(self.agent_id, "sub-TL agent_id")
        if self.worktree is not None:
            _require_text(str(self.worktree), "sub-TL worktree")
        if type(self.order) is not int or self.order <= 0:
            raise ValueError("sub-TL order must be a positive integer")
        if not isinstance(self.integration, IntegrationContract):
            raise TypeError("sub-TL integration must be an IntegrationContract")
        if not isinstance(self.order_explicit, bool):
            raise TypeError("sub-TL order_explicit must be a boolean")
        _validate_task_timeout(self.task_timeout_seconds, "sub-TL task_timeout_seconds")


@dataclass(frozen=True)
class TLLoopConfig:
    """Safety ceilings and effect mode for one TL invocation."""

    active: bool = True
    max_workers: int = 8
    max_leaves: int = 8
    max_parallel_slices: int | None = None
    max_events: int = 256
    test_harness: bool = False
    cancel_event: threading.Event | None = None
    poll_interval: float = 0.1
    keep_alive_on_waiting: bool = True
    task_timeout_seconds: float | None = 3600.0
    task_timeout_source: str = "built_in"
    max_base_revalidations: int = 3
    max_integration_repairs: int = 3
    heartbeat: HeartbeatConfig | None = None
    goals: GoalState | None = None
    chainlink_issue_id: int | None = None
    merge_strategy: str | None = None
    working_dir: str | None = None
    repository_identity: RepositoryIdentity | None = None
    source: EventQueue | None = None
    effects: EffectClient | ReadOnlyEffectClient | None = None
    root_dir: str | Path = DEFAULT_ROOT
    project_root: str | Path | None = None
    run_id: str = "tl-run"
    session_mode: str | None = None
    ledger_run_id: str | None = None
    policy: HarnessPolicy | None = None
    learned_policy: LearnedPolicy | None = None
    capabilities: CapabilityMap | None = None
    catalog: ModelCatalog | None = None
    requested_model: str | None = None
    role: str = "worker"
    review_policy_path: str | Path | None = None
    review_clock: Callable[[], datetime] | None = None
    enable_reviewer_spawn: bool = False
    dispatch_names: Mapping[str, str] = field(default_factory=dict)
    review_model_choice: object | None = None
    branch: str = "main"
    worktree: str | Path | None = None
    agent_id: str | None = None
    parent_branch: str | None = None
    parent_run_id: str | None = None
    parent_agent_id: str | None = None
    depth: int = 0
    max_depth: int = 3
    plan_revision: int = 1

    def __post_init__(self) -> None:
        if self.project_root is not None:
            object.__setattr__(self, "project_root", Path(self.project_root))
        for name in (
            "max_workers",
            "max_leaves",
            "max_events",
            "max_base_revalidations",
            "max_integration_repairs",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_parallel_slices is not None and (
            type(self.max_parallel_slices) is not int or self.max_parallel_slices < 0
        ):
            raise ValueError("max_parallel_slices must be null or non-negative")
        if self.max_events == 0:
            raise ValueError("max_events must be positive")
        if type(self.test_harness) is not bool:
            raise ValueError("test_harness must be a boolean")
        if not isinstance(self.dispatch_names, Mapping):
            raise TypeError("dispatch_names must be a mapping")
        for slice_id, runtime_name in self.dispatch_names.items():
            _require_text(slice_id, "dispatch_names slice id")
            _require_text(runtime_name, "dispatch_names runtime name")
        if self.poll_interval < 0:
            raise ValueError("poll_interval must be non-negative")
        if self.heartbeat is not None and self.project_root is None:
            raise ValueError("project_root is required when heartbeat reconciliation is enabled")
        if type(self.keep_alive_on_waiting) is not bool:
            raise ValueError("keep_alive_on_waiting must be a boolean")
        _validate_task_timeout(self.task_timeout_seconds, "task_timeout_seconds")
        _require_text(self.task_timeout_source, "task_timeout_source")
        if type(self.enable_reviewer_spawn) is not bool:
            raise ValueError("enable_reviewer_spawn must be a boolean")
        if self.chainlink_issue_id is not None and self.chainlink_issue_id <= 0:
            raise ValueError("chainlink_issue_id must be positive")
        _optional_text(self.merge_strategy, "merge_strategy")
        _optional_text(self.working_dir, "working_dir")
        if self.repository_identity is not None and not isinstance(
            self.repository_identity, RepositoryIdentity
        ):
            raise TypeError("repository_identity must be a RepositoryIdentity or null")
        _require_text(self.run_id, "run_id")
        if self.session_mode is not None and self.session_mode not in {
            "start",
            "continue",
            "recreate",
        }:
            raise ValueError("session_mode must be start, continue, recreate, or null")
        _optional_text(self.ledger_run_id, "ledger_run_id")
        _require_text(self.role, "role")
        for name in ("depth", "max_depth"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if type(self.plan_revision) is not int or self.plan_revision < 1:
            raise ValueError("plan_revision must be a positive integer")
        _require_text(self.branch, "branch")
        _optional_text(self.agent_id, "agent_id")
        _optional_text(self.parent_branch, "parent_branch")
        _optional_text(self.parent_run_id, "parent_run_id")
        _optional_text(self.parent_agent_id, "parent_agent_id")
        if self.worktree is not None:
            _require_text(str(self.worktree), "worktree")
        _optional_text(self.requested_model, "requested_model")
        if self.requested_model is not None and self.catalog is None:
            raise ValueError("requested_model requires a model catalog")


@dataclass(frozen=True)
class EffectIntent:
    """An effect requested by the loop, whether executed or shadowed."""

    operation: str
    target: str
    arguments: Mapping[str, object]
    executed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True)
class LoopTransition:
    """One durable event-to-phase transition."""

    event_seq: int
    event_type: str
    before: TLPhase
    after: TLPhase


@dataclass(frozen=True)
class TLRunResult:
    """The durable result and audit trail of one bounded invocation."""

    final_state: RunState
    effects: tuple[EffectIntent, ...]
    transitions: tuple[LoopTransition, ...]
    consumed_events: tuple[int, ...]
    heartbeat_events: tuple[SyntheticHeartbeatEvent, ...] = ()
    diagnostics: Mapping[str, object] = field(default_factory=dict)
    journal_entries: tuple[Mapping[str, object], ...] = ()

    @property
    def cursor(self) -> int:
        """Return the exact durable ledger cursor reached by this invocation."""
        return self.final_state.events.last_consumed_offset

    @property
    def reducer_version(self) -> int:
        """Return the reducer contract used to produce this checkpoint."""
        return self.final_state.reducer_version

    @property
    def recursive_position(self) -> object | None:
        """Return the complete recursive FSM position, if this is recursive."""
        return self.final_state.recursive_fsm


def tl_run(
    root_spec: WorkPlan | Mapping[str, object],
    cfg: TLLoopConfig,
    budgets: BudgetLedger | Mapping[str, object],
) -> TLRunResult:
    """Run one selector-integrated wave through the shared active/shadow body."""
    if not isinstance(cfg, TLLoopConfig):
        raise TypeError("cfg must be a TLLoopConfig")
    plan, run_id, source, effects = _root_inputs(root_spec, cfg)
    policy = cfg.policy or load_policy()
    capabilities = cfg.capabilities or load_capability()
    selected = replace(cfg, policy=policy, capabilities=capabilities)
    return run_tl_loop(
        run_id,
        plan,
        source,
        effects,
        config=selected,
        root_dir=selected.root_dir,
        budgets=budgets,
        initial_slices=_initial_slices(plan, selected) if plan is not None else None,
    )


def run_tl_loop(
    run_id: str,
    plan: WorkPlan | Mapping[str, object] | None,
    source: EventQueue,
    effects: EffectClient | ReadOnlyEffectClient,
    *,
    config: TLLoopConfig | None = None,
    root_dir: str | Path = DEFAULT_ROOT,
    decoder: TLEventDecoder | None = None,
    budgets: BudgetLedger | Mapping[str, object] | None = None,
    initial_slices: Mapping[str, Mapping[str, object]] | None = None,
) -> TLRunResult:
    """Dispatch direct children and run one bounded active/shadow event loop."""
    selected = config or TLLoopConfig()
    _validate_mode(selected, effects)
    store = RunStore(run_id, Path(root_dir))
    existing_state = store.load() if store.path.exists() else None
    if existing_state is not None and _is_terminal_phase(_phase_from_state(existing_state)):
        if existing_state.reducer_version != REDUCER_VERSION:
            raise TLLoopError(
                f"checkpoint reducer_version {existing_state.reducer_version} is incompatible "
                f"with reducer {REDUCER_VERSION}"
            )
        terminal_ledger_id = selected.ledger_run_id or existing_state.ledger_run_id
        terminal_log: list[EffectIntent] = (
            EffectJournal(run_id, store.run_dir / "action-journal.json")
            if terminal_ledger_id is not None
            else []
        )
        return TLRunResult(
            existing_state,
            (),
            (),
            (),
            (),
            {
                "reducer_version": existing_state.reducer_version,
                "cursor": existing_state.events.last_consumed_offset,
            },
            _journal_entries(terminal_log),
        )
    manifest: PlanManifest
    if existing_state is not None and existing_state.plan_manifest is not None:
        persisted_manifest = existing_state.plan_manifest
        if _manifest_is_legacy(persisted_manifest):
            if plan is None:
                raise TLLoopError(
                    "continuation cannot reconstruct a legacy checkpoint; "
                    "supply an explicit external WorkPlan"
                )
            supplied_plan = plan if isinstance(plan, WorkPlan) else WorkPlan.from_mapping(plan)
            candidate = _manifest_for_plan(supplied_plan, run_id, selected)
            reconciliation = None
            if selected.active:
                journal_path = store.run_dir / "action-journal.json"
                migration_journal: list[EffectIntent] = (
                    EffectJournal(run_id, journal_path)
                    if selected.ledger_run_id is not None or journal_path.exists()
                    else []
                )
                reconciliation = reconcile_legacy_manifest(
                    persisted_manifest,
                    candidate,
                    existing_state,
                    migration_journal,
                    child_checkpoint_root=store.run_dir,
                )
                if not reconciliation.proven:
                    store.set_gate(reconciliation.gate_name())
                    raise TLLoopError(
                        "continuation cannot replace the legacy manifest: "
                        f"{reconciliation.disposition.value}: {reconciliation.reason}"
                    )
            proofs = (
                {proof.slice_id: proof for proof in reconciliation.proofs} if reconciliation else {}
            )
            rebound: dict[str, SliceState] = {}
            for slice_id, slice_state in existing_state.slices.items():
                proof = proofs.get(slice_id)
                rebound[slice_id] = replace(
                    slice_state,
                    branch=(proof.branch if proof else None) or slice_state.branch,
                    worktree=(proof.worktree if proof else None) or slice_state.worktree,
                    legacy_manifest_migration=proof.to_document() if proof else None,
                )
            generated = _initial_slices(supplied_plan, selected, root_dir, run_id)
            initial_slices = {
                **rebound,
                **{
                    name: value
                    for name, value in generated.items()
                    if name not in existing_state.slices
                },
            }
            try:
                existing_state = store.set_plan_manifest(
                    candidate,
                    slices=initial_slices,
                )
            except ManifestError as error:
                raise TLLoopError(f"legacy plan manifest replacement rejected: {error}") from error
            existing_state = _clear_resolved_migration_gate(
                store,
                existing_state,
                reconciliation,
            )
            work_plan = _work_plan_from_manifest(candidate)
            manifest = candidate
            initial_slices = None
        elif plan is None:
            work_plan = _work_plan_from_manifest(persisted_manifest)
            manifest = persisted_manifest
            initial_slices = None
        else:
            supplied_plan = plan if isinstance(plan, WorkPlan) else WorkPlan.from_mapping(plan)
            candidate = _manifest_for_plan(supplied_plan, run_id, selected)
            if candidate.digest == persisted_manifest.digest:
                work_plan = _work_plan_from_manifest(persisted_manifest)
                manifest = persisted_manifest
                initial_slices = None
            else:
                if candidate.manifest_revision <= persisted_manifest.manifest_revision:
                    raise TLLoopError(
                        "external plan differs from the persisted manifest; "
                        "increase plan_revision with an explicit compatible revision"
                    )
                protected = {
                    slice_state.manifest_node_id
                    for slice_state in existing_state.slices.values()
                    if slice_state.status not in {SliceStatus.PENDING, SliceStatus.READY}
                    and slice_state.manifest_node_id is not None
                }
                generated = _initial_slices(supplied_plan, selected, root_dir, run_id)
                initial_slices = {
                    **dict(existing_state.slices),
                    **{
                        name: value
                        for name, value in generated.items()
                        if name not in existing_state.slices
                    },
                }
                try:
                    existing_state = store.set_plan_manifest(
                        candidate,
                        slices=initial_slices,
                        protected_node_ids=protected,
                    )
                except ManifestError as error:
                    raise TLLoopError(f"plan manifest revision rejected: {error}") from error
                work_plan = _work_plan_from_manifest(candidate)
                manifest = candidate
                initial_slices = None
    else:
        if plan is None:
            if existing_state is None:
                raise TLLoopError("a new run requires an external WorkPlan")
            raise TLLoopError(
                "continuation cannot reconstruct a legacy checkpoint without an immutable plan manifest"
            )
        work_plan = plan if isinstance(plan, WorkPlan) else WorkPlan.from_mapping(plan)
        manifest = _manifest_for_plan(work_plan, run_id, selected)
        if initial_slices is None:
            initial_slices = _initial_slices(work_plan, selected, root_dir, run_id)
        initial_slices = _bind_initial_slices(initial_slices, manifest)

    if len(work_plan.workers) > selected.max_workers:
        raise LoopLimitExceeded("work plan exceeds max_workers")
    if len(work_plan.leaves) > selected.max_leaves:
        raise LoopLimitExceeded("work plan exceeds max_leaves")
    epoch_enabled = selected.active and selected.review_clock is None

    if existing_state is None:
        root_state: dict[str, object] = {}
        if (
            work_plan.sub_tls
            or selected.parent_branch is not None
            or selected.worktree is not None
            or selected.depth > 0
        ):
            root_state = {
                "owner_branch": selected.branch,
                "owner_worktree": _effective_worktree(selected, Path(root_dir), run_id),
                "parent_branch": selected.parent_branch,
                "parent_run_id": selected.parent_run_id,
                "parent_agent_id": selected.parent_agent_id,
                "depth": selected.depth,
            }
        if selected.goals is not None:
            root_state["goals"] = _encode_goals(selected.goals)
        if initial_slices is not None:
            root_state["slices"] = copy.deepcopy(dict(initial_slices))
        root_state["plan_manifest"] = manifest.to_document()
        root_state["fsm"] = _canonical_planning_from_manifest(manifest, initial_slices or {})
        if budgets is not None:
            root_state["budgets"] = _budget_root(budgets)
        if selected.ledger_run_id is not None:
            root_state["ledger_run_id"] = selected.ledger_run_id
        if selected.repository_identity is not None:
            root_state["repository_identity"] = {
                "owner": selected.repository_identity.owner,
                "repo": selected.repository_identity.repo,
                "base_branch": selected.repository_identity.base_branch,
                "forge_host": selected.repository_identity.forge_host,
                "remote_url": selected.repository_identity.remote_url,
            }
        if selected.session_mode is not None:
            root_state["session_mode"] = selected.session_mode
        if epoch_enabled:
            root_state["controller_epoch"] = _controller_epoch(store.root_dir, run_id)
        create(run_id, root_state, root_dir=store.root_dir)
    state = store.load()
    if state.reducer_version != REDUCER_VERSION:
        raise TLLoopError(
            f"checkpoint reducer_version {state.reducer_version} is incompatible "
            f"with reducer {REDUCER_VERSION}"
        )
    if selected.repository_identity is not None:
        if (
            state.repository_identity is not None
            and state.repository_identity != selected.repository_identity
        ):
            raise TLLoopError("continuation repository identity differs from the checkpoint")
        if state.repository_identity is None:
            state = store.set_repository_identity(selected.repository_identity)
    if state.plan_manifest is None:
        node_ids_by_name = {node.name: node.node_id for node in manifest.nodes}
        protected = {
            node_ids_by_name[next_node]
            for next_node, slice_state in state.slices.items()
            if slice_state.status not in {SliceStatus.PENDING, SliceStatus.READY}
            and next_node in node_ids_by_name
        }
        if protected and selected.active:
            state = store.set_gate(
                "plan-manifest-migration: existing active run has no immutable manifest"
            )
            raise TLLoopError("continuation cannot infer a plan manifest for an active legacy run")
        state = store.set_plan_manifest(
            manifest,
            slices=initial_slices if existing_state is None else None,
        )
    if state.session_mode is None and selected.session_mode is not None:
        state = store.set_session_mode(selected.session_mode)
    if epoch_enabled:
        current_controller_epoch = _controller_epoch(store.root_dir, run_id)
        if state.controller_epoch is None or (
            selected.session_mode == "continue"
            and state.controller_epoch != current_controller_epoch
        ):
            state = store.set_controller_epoch(current_controller_epoch)
    if (
        state.ledger_run_id
        and selected.ledger_run_id
        and (state.ledger_run_id != selected.ledger_run_id)
    ):
        raise TLLoopError(
            f"checkpoint ledger_run_id {state.ledger_run_id!r} does not match "
            f"active swarm {selected.ledger_run_id!r}"
        )
    effective_ledger_run_id = selected.ledger_run_id or state.ledger_run_id
    if effective_ledger_run_id is not None:
        selected = replace(selected, ledger_run_id=effective_ledger_run_id)
    state = _ensure_canonical_scope(state, manifest, store)
    state = _release_canonical_scope(state, store)
    effects_log: list[EffectIntent] = (
        EffectJournal(run_id, store.run_dir / "action-journal.json")
        if selected.ledger_run_id is not None or (store.run_dir / "action-journal.json").exists()
        else []
    )
    state = _reconcile_legacy_parked_lanes(state, store, effects_log)
    state = _initialize_ordered_runtime(work_plan, state, store)
    try:
        state = _reconcile_action_journal(
            state,
            store,
            effects_log,
            effects=effects,
            project_root=selected.project_root,
            ledger_run_id=effective_ledger_run_id,
        )
        state = _reconcile_confirmed_repair_actions(state, store, effects_log)
        state = _reconcile_dispatches(state, selected, effects, store, effects_log)
        state = _reconcile_nonterminal_slices(
            work_plan, state, selected, effects, store, effects_log
        )
        state = _dispatch_children(work_plan, state, selected, effects, effects_log, store)
        state = _run_sub_tls(work_plan, state, selected, source, effects, store, effects_log)
        return _run_loop(
            run_id,
            work_plan,
            source,
            effects,
            selected,
            store,
            state,
            effects_log,
            decoder or TLEventDecoder(),
        )
    except ToolUnavailableError as error:
        return _recover_tool_unavailable(store, effects, effects_log, error)
    except DurableWriteError as error:
        return _recover_durable_write_failure(store, effects, effects_log, error)


def _clear_resolved_migration_gate(
    store: RunStore,
    state: RunState,
    reconciliation: LegacyManifestReconciliation | None,
) -> RunState:
    """Clear only the gate whose exact proof has just been resolved."""
    if reconciliation is None:
        return state
    gate_name = reconciliation.gate_name()
    if not any(gate.name == gate_name for gate in state.gates):
        return state
    return store.clear_gate(gate_name)


def _failure_phase(state: RunState, reason: str) -> PhaseValue:
    """Build a failure value in the active FSM domain."""
    phase = state.recursive_fsm
    if phase is not None:
        return scope_transition(phase, ScopeFailureRecorded(reason))
    return TLFailed(reason)


def _recover_durable_write_failure(
    store: RunStore,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    error: DurableWriteError,
) -> TLRunResult:
    """Park the affected slice when lifecycle journaling cannot be trusted."""
    if not isinstance(effects, EffectClient):
        store.record_exit_reason("durable write failed in shadow mode", error=error)
        raise error
    state = store.load()
    slice_state = state.slices.get(error.target or "")
    if slice_state is None:
        slice_state = next(
            (
                candidate
                for candidate in state.slices.values()
                if candidate.status
                not in {
                    SliceStatus.MERGED,
                    SliceStatus.FAILED,
                    SliceStatus.PARKED,
                    SliceStatus.BLOCKED,
                }
            ),
            None,
        )
    if slice_state is None:
        store.record_exit_reason("durable write failed without an active slice", error=error)
        raise error
    park(
        slice_state,
        ParkCause.DURABLE_WRITE_FAILED,
        store=store,
        issue_creator=effects,
        audit={"reason": str(error)},
    )
    state = store.load()
    if state.fsm.phase not in {TLPhase.TLDone, TLPhase.TLFailed}:
        state = store.checkpoint(
            _failure_phase(state, f"durable write failed for {slice_state.id!r}"),
            state.slices,
            state.budgets,
            state.events.last_consumed_offset,
        )
    return TLRunResult(
        state,
        tuple(effects_log),
        (),
        (),
        journal_entries=_journal_entries(effects_log),
    )


def _recover_tool_unavailable(
    store: RunStore,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    error: ToolUnavailableError,
) -> TLRunResult:
    """Park deployment skew instead of terminating the controller."""
    if not isinstance(effects, EffectClient):
        store.record_exit_reason("tool unavailable in shadow mode", error=error)
        raise error
    state = store.load()
    slice_state = state.slices.get(error.target or "")
    if slice_state is None:
        slice_state = next(
            (
                candidate
                for candidate in state.slices.values()
                if candidate.status
                not in {
                    SliceStatus.MERGED,
                    SliceStatus.FAILED,
                    SliceStatus.PARKED,
                    SliceStatus.BLOCKED,
                }
            ),
            None,
        )
    if slice_state is None:
        store.record_exit_reason("tool unavailable without an active slice", error=error)
        raise error
    payload = {
        "slice_id": slice_state.id,
        "tool_name": error.tool_name,
        "role": error.role,
        "wasm_path": error.wasm_path,
        "wasm_mtime": error.wasm_mtime,
        "remediation": error.remediation,
        "message": str(error),
        "error_kind": "tool_unavailable",
    }
    emit_controller_event(effects, "tl.tool_unavailable", payload)
    park(
        slice_state,
        ParkCause.TOOL_UNAVAILABLE,
        store=store,
        issue_creator=effects,
        audit=payload,
    )
    state = store.load()
    if state.fsm.phase not in {TLPhase.TLDone, TLPhase.TLFailed}:
        state = store.checkpoint(
            _failure_phase(state, f"tool unavailable for {slice_state.id!r}"),
            state.slices,
            state.budgets,
            state.events.last_consumed_offset,
        )
    return TLRunResult(
        state,
        tuple(effects_log),
        (),
        (),
        journal_entries=_journal_entries(effects_log),
    )


def _initialize_ordered_runtime(plan: WorkPlan, state: RunState, store: RunStore) -> RunState:
    """Create ordered restart metadata once, without resetting progress."""
    if not plan.sub_tls:
        return state
    stages = tuple(OrderedStageState(stage.order, stage.sub_tls) for stage in plan.ordered_stages)
    if state.ordered_stages:
        if state.ordered_stages != stages:
            raise TLLoopError("persisted ordered stages do not match the normalized plan")
        return state
    integration = IntegrationRuntimeState(
        sub_tl_states={task.name: IntegrationLifecycle.RUNNING for task in plan.sub_tls}
    )
    return store.set_ordered_state(1, stages, integration)


def _canonical_planning_from_manifest(
    manifest: PlanManifest,
    slices: Mapping[str, object],
) -> RecursiveTLPlanning:
    """Build the one canonical planning value from immutable declarations."""
    records = {
        node.node_id: _canonical_child_record(
            node, slices.get(node.name), manifest.manifest_revision
        )
        for node in manifest.nodes
    }
    parallel = tuple(
        records[node.node_id] for node in manifest.nodes if node.kind in {"worker", "leaf"}
    )
    ordered = tuple(
        (order, tuple(records[node_id] for node_id in node_ids))
        for order, node_ids in manifest.ordered_stages
    )
    return RecursiveTLPlanning(
        ordered_children=ordered,
        scope_path=(manifest.scope_id,),
        plan_digest=manifest.digest or "",
        parallel_children=parallel,
    )


def _canonical_child_record(
    node: object,
    slice_value: object,
    manifest_revision: int,
) -> ChildRecord:
    """Bind one runtime child record to its immutable manifest identity."""
    kind = ChildKind(node.kind)
    action = _runtime_field(slice_value, "action")
    intent_id = _runtime_field(action, "intent_id")
    invocation_id = _runtime_field(slice_value, "dispatch_invocation_id")
    return ChildRecord(
        child_id=node.name,
        kind=kind,
        dispatch_intent_id=intent_id if isinstance(intent_id, str) else None,
        invocation_id=invocation_id if isinstance(invocation_id, str) else None,
        evidence={
            "manifest_node_id": node.node_id,
            "manifest_revision": str(manifest_revision),
        },
        manifest_node_id=node.node_id,
        manifest_revision=manifest_revision,
    )


def _runtime_field(value: object, field_name: str) -> object:
    """Read a field from either a decoded state value or an input mapping."""
    if isinstance(value, Mapping):
        return value.get(field_name)
    return getattr(value, field_name, None)


def _ensure_canonical_scope(
    state: RunState,
    manifest: PlanManifest,
    store: RunStore,
) -> RunState:
    """Upgrade only unambiguous legacy planning checkpoints."""
    if state.recursive_fsm is not None:
        return state
    if state.fsm.phase is TLPhase.TLPlanning:
        planning = _canonical_planning_from_manifest(manifest, state.slices)
        return store.checkpoint(
            planning,
            state.slices,
            state.budgets,
            state.events.last_consumed_offset,
            plan_manifest=manifest,
        )
    if state.fsm.phase is TLPhase.TLDone:
        return store.checkpoint(
            RecursiveTLDone(
                scope_path=(manifest.scope_id,),
                plan_digest=manifest.digest or "",
                finalization_evidence={"legacy_terminal": "true"},
            ),
            state.slices,
            state.budgets,
            state.events.last_consumed_offset,
        )
    if state.fsm.phase is TLPhase.TLFailed:
        return store.checkpoint(
            RecursiveTLFailed("legacy failure checkpoint"),
            state.slices,
            state.budgets,
            state.events.last_consumed_offset,
        )
    if state.fsm.phase in {TLPhase.TLWaiting, TLPhase.TLMerging} and not manifest.ordered_stages:
        planning = _canonical_planning_from_manifest(manifest, state.slices)
        active_ids = tuple(state.fsm.waiting)
        declared_ids = tuple(record.child_id for record in planning.parallel_children)
        if set(active_ids) == set(declared_ids) and len(active_ids) == len(declared_ids):
            running = scope_transition(
                planning,
                ScopeStageReleased(
                    order=0,
                    child_ids=declared_ids,
                    scope_path=planning.scope_path,
                ),
            )
            return store.checkpoint(
                running,
                state.slices,
                state.budgets,
                state.events.last_consumed_offset,
            )
    store.set_gate(
        "recursive-fsm-migration: legacy active phase cannot be upgraded without dispatch evidence"
    )
    raise TLLoopError(
        f"legacy active phase {state.fsm.phase.value!r} cannot be resumed safely; "
        "dispatch evidence is ambiguous"
    )


def _release_canonical_scope(state: RunState, store: RunStore) -> RunState:
    """Release the first canonical block exactly once at startup."""
    phase = state.recursive_fsm
    if not isinstance(phase, RecursiveTLPlanning):
        return state
    if phase.ordered_children:
        order, records = phase.ordered_children[0]
    else:
        order, records = 0, phase.parallel_children
    event = ScopeStageReleased(
        order=order,
        child_ids=tuple(record.child_id for record in records),
        scope_path=phase.scope_path,
    )
    try:
        next_phase = scope_transition(phase, event)
    except Exception as error:
        raise TLLoopError(f"canonical scope release failed: {error}") from error
    return store.checkpoint(
        next_phase,
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
    )


def _is_terminal_phase(phase: object) -> bool:
    """Recognize terminal values from both the canonical and legacy views."""
    return isinstance(
        phase,
        (
            TLDone,
            TLFailed,
            RecursiveTLDone,
            RecursiveTLPRFiled,
            RecursiveTLFailed,
            RecursiveTLParked,
        ),
    )


def _checkpoint_scope_phase(
    phase: PhaseValue,
    state: RunState,
    store: RunStore,
) -> RunState:
    """Persist one canonical scope transition with exactly one version step."""
    return store.checkpoint(
        phase,
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        state_version=state.state_version + 1,
    )


def _maybe_finalize_root_scope(
    state: RunState,
    store: RunStore,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    """Fast-forward the root branch before durably completing finalization."""
    phase = state.recursive_fsm
    manifest = state.plan_manifest
    if manifest is None or manifest.role != "root":
        return state
    if not isinstance(phase, (RecursiveTLAllMerged, RecursiveTLFinalizing)):
        return state
    evidence = {
        "root_branch": state.owner_branch or manifest.owned_branch,
        "local_checkout": state.owner_worktree or manifest.owned_branch,
    }
    if isinstance(phase, RecursiveTLAllMerged):
        finalizing = scope_transition(phase, ScopeFinalizationRequested(ScopeRole.ROOT))
        finalizing = replace(finalizing, evidence=evidence)
        state = _checkpoint_scope_phase(finalizing, state, store)
    else:
        finalizing = phase
        if not finalizing.evidence:
            finalizing = replace(finalizing, evidence=evidence)
            state = _checkpoint_scope_phase(finalizing, state, store)
    if not config.active:
        completed = scope_transition(
            finalizing,
            ScopeFinalizationComplete(ScopeRole.ROOT, evidence),
        )
        return _checkpoint_scope_phase(completed, state, store)

    branch = evidence["root_branch"]
    if not isinstance(branch, str) or not branch:
        return store.set_gate("tl-root-finalization", GateStatus.PENDING)
    result = _post_merge_effect(
        "root_branch_finalize",
        state.run_id,
        {"branch": branch, "working_dir": config.working_dir},
        effects,
        effects_log,
        "root_branch_finalize",
        active=True,
        retryable_failure=False,
    )
    payload = _merge_result_payload(result)
    if payload is None:
        return store.set_gate("tl-root-finalization", GateStatus.PENDING)
    if (
        payload.get("branch") != branch
        or payload.get("fast_forward") is not True
        or not isinstance(payload.get("local_head_sha"), str)
        or not isinstance(payload.get("remote_head_sha"), str)
        or payload["local_head_sha"] != payload["remote_head_sha"]
        or not isinstance(payload.get("ancestry_proof"), str)
        or not payload["ancestry_proof"]
    ):
        return store.set_gate("tl-root-finalization", GateStatus.PENDING)
    completion_evidence = {
        **evidence,
        "local_head_sha": payload["local_head_sha"],
        "remote_head_sha": payload["remote_head_sha"],
        "ancestry_proof": payload["ancestry_proof"],
        "fast_forward": "true",
    }
    completed = scope_transition(
        finalizing,
        ScopeFinalizationComplete(ScopeRole.ROOT, completion_evidence),
    )
    return _checkpoint_scope_phase(completed, state, store)


def _non_root_finalization_evidence(
    state: RunState,
    manifest: PlanManifest,
) -> Mapping[str, str] | None:
    """Recover a parent-targeted aggregate candidate from durable runtime data."""
    integration = state.integration
    aggregate_pr = integration.aggregate_pr_number
    head_sha = integration.aggregate_head_sha or integration.head_sha
    base_sha = integration.aggregate_original_base_sha or state.parent_branch
    parent_branch = state.parent_branch or manifest.parent_integration_target
    handoff = integration.integration_owner_id
    if not all((aggregate_pr, head_sha, base_sha, parent_branch, handoff)):
        return None
    return {
        "aggregate_pr": str(aggregate_pr),
        "head_sha": str(head_sha),
        "base_sha": str(base_sha),
        "parent_branch": str(parent_branch),
        "handoff": str(handoff),
    }


def _maybe_finalize_non_root_scope(state: RunState, store: RunStore) -> RunState:
    """Persist and complete a non-root aggregate publication handoff."""
    phase = state.recursive_fsm
    manifest = state.plan_manifest
    if manifest is None or manifest.role != "non_root":
        return state
    if isinstance(phase, RecursiveTLAllMerged):
        evidence = _non_root_finalization_evidence(state, manifest)
        if evidence is None:
            return state
        finalizing = scope_transition(phase, ScopeFinalizationRequested(ScopeRole.NON_ROOT))
        finalizing = replace(finalizing, evidence=evidence)
        state = _checkpoint_scope_phase(finalizing, state, store)
    elif isinstance(phase, RecursiveTLFinalizing):
        finalizing = phase
        if not finalizing.evidence:
            evidence = _non_root_finalization_evidence(state, manifest)
            if evidence is None:
                return state
            finalizing = replace(finalizing, evidence=evidence)
            state = _checkpoint_scope_phase(finalizing, state, store)
    else:
        return state
    completed = scope_transition(
        finalizing,
        ScopeFinalizationComplete(ScopeRole.NON_ROOT, finalizing.evidence),
    )
    return _checkpoint_scope_phase(completed, state, store)


def _maybe_finalize_scope(
    state: RunState,
    store: RunStore,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    """Select the role-specific finalization reducer for one scope."""
    manifest = state.plan_manifest
    if manifest is None:
        return state
    if state.recursive_fsm is None and state.fsm.phase is TLPhase.TLAllMerged:
        return store.checkpoint(
            TLDone(),
            state.slices,
            state.budgets,
            state.events.last_consumed_offset,
            current_order=state.current_order,
            ordered_stages=state.ordered_stages,
            integration=state.integration,
        )
    if manifest.role == "root":
        return _maybe_finalize_root_scope(state, store, config, effects, effects_log)
    return _maybe_finalize_non_root_scope(state, store)


def _run_loop(
    run_id: str,
    plan: WorkPlan,
    source: EventQueue,
    effects: EffectClient | ReadOnlyEffectClient,
    config: TLLoopConfig,
    store: RunStore,
    state: RunState,
    effects_log: list[EffectIntent],
    decoder: TLEventDecoder,
) -> TLRunResult:
    """Shared loop body; active mode changes only effect execution."""
    phase = _phase_from_state(state)
    expected = (
        {task.name for task in plan.workers}
        | {task.name for task in plan.leaves}
        | {task.name for task in plan.sub_tls}
    )
    transitions: list[LoopTransition] = []
    consumed: list[int] = []
    policy = _review_policy_for_state(state, config.review_policy_path, store)
    state = store.load()
    freshness_window_secs = (
        load_freshness_window(config.review_policy_path)
        if config.review_policy_path is not None
        else None
    )
    convergence = ConvergenceTracker(
        reviewer_max_rounds=policy.reviewer_max_rounds,
        review_freshness_window_secs=freshness_window_secs,
        review_now=config.review_clock() if config.review_clock is not None else None,
    )
    heartbeat_events: list[SyntheticHeartbeatEvent] = []
    diagnostics = EventDiagnostics(
        task_started_at={
            slice_id: slice_state.dispatch_started_at
            for slice_id, slice_state in state.slices.items()
            if slice_state.dispatch_started_at is not None
        }
    )
    quarantined: list[EventEnvelope] = []
    for document in store.quarantined_events():
        try:
            event = project(document)
        except EnvelopeError as error:
            LOGGER.error("Ignoring malformed quarantined event: %s", error)
            continue
        quarantined.append(event)
    if state.goals.controller_started_at is None:
        state = store.set_goals(
            replace(state.goals, controller_started_at=diagnostics.controller_started_at)
        )
    before_finalization_phase = _phase_from_state(state)
    state = _maybe_finalize_scope(state, store, config, effects, effects_log)
    after_finalization_phase = _phase_from_state(state)
    if after_finalization_phase != before_finalization_phase:
        _emit_phase_change(
            run_id,
            before_finalization_phase,
            after_finalization_phase,
            config,
            effects,
            effects_log,
        )
    phase = _phase_from_state(state)
    if isinstance(state.recursive_fsm, RecursiveTLAllMerged) and not _source_has_pending(source):
        return TLRunResult(
            state,
            tuple(effects_log),
            tuple(transitions),
            tuple(consumed),
            diagnostics=diagnostics.snapshot(),
            journal_entries=_journal_entries(effects_log),
        )
    if not expected and not plan.sub_tls and not _is_terminal_phase(phase):
        if not isinstance(phase, RecursiveTLAllMerged):
            raise TLLoopError("empty scope did not reach a canonical completion boundary")
        return TLRunResult(
            state,
            tuple(effects_log),
            tuple(transitions),
            tuple(consumed),
            diagnostics=diagnostics.snapshot(),
            journal_entries=_journal_entries(effects_log),
        )

    if not isinstance(
        derive_next_action(
            state,
            reviewer_max_rounds=convergence.reviewer_max_rounds,
            review_freshness_window_secs=convergence.review_freshness_window_secs,
            now=convergence.review_now,
        ),
        Quiescent,
    ):
        state = _apply_convergence(state, convergence, store, config, effects, effects_log)
        phase = _phase_from_state(state)

    while not config.test_harness or len(consumed) < config.max_events:
        if not (
            isinstance(_phase_from_state(state), RecursiveTLAllMerged)
            and _source_has_pending(source)
        ):
            before_finalization_phase = _phase_from_state(state)
            state = _maybe_finalize_scope(state, store, config, effects, effects_log)
            after_finalization_phase = _phase_from_state(state)
            if after_finalization_phase != before_finalization_phase:
                _emit_phase_change(
                    run_id,
                    before_finalization_phase,
                    after_finalization_phase,
                    config,
                    effects,
                    effects_log,
                )
        phase = _phase_from_state(state)
        if _is_terminal_phase(phase):
            break
        if (
            isinstance(convergence.last_decision, Quiescent)
            and not config.keep_alive_on_waiting
            and not _source_has_pending(source)
        ):
            return TLRunResult(
                state,
                tuple(effects_log),
                tuple(transitions),
                tuple(consumed),
                tuple(heartbeat_events),
                diagnostics.snapshot(),
                _journal_entries(effects_log),
            )
        if config.cancel_event is not None and config.cancel_event.is_set():
            raise LoopCancelled(f"TL controller {run_id!r} was cancelled")
        _record_reader_findings(source, diagnostics)
        replaying = False
        replay_index = _replayable_event_index(quarantined, state, expected)
        if replay_index is not None:
            event = quarantined.pop(replay_index)
            replaying = True
        else:
            event = _next_event(source, config)
        _record_reader_findings(source, diagnostics)
        if event is None:
            if config.heartbeat is not None:
                heartbeat = heartbeat_once(
                    state,
                    store,
                    effects,
                    config.heartbeat,
                    project_root=config.project_root,
                    review_replay=lambda replay_state, replay_slice, replay_watcher: (
                        _replay_watcher_review_if_needed(
                            plan,
                            replay_state,
                            replay_slice,
                            replay_watcher,
                            config,
                            effects,
                            store,
                            effects_log,
                        )
                    ),
                )
                if heartbeat.fired:
                    before_phase = phase
                    state = heartbeat.state
                    heartbeat_events.extend(heartbeat.events)
                    diagnostics.last_observed_progress_at = state.goals.last_progress_at
                    phase = _phase_from_state(state)
                    LOGGER.info(
                        "[TL loop] waiting observation run_id=%s elapsed=%.3fs "
                        "last_authoritative_event_seq=%s last_progress_at=%s",
                        run_id,
                        time.time() - diagnostics.controller_started_at,
                        state.goals.last_authoritative_event_seq,
                        state.goals.last_progress_at,
                    )
                    _emit_phase_change(run_id, before_phase, phase, config, effects, effects_log)
                    state = _apply_convergence(
                        state, convergence, store, config, effects, effects_log
                    )
                    phase = _phase_from_state(state)
                    if heartbeat.parked_slice_ids and _all_expected_terminal(state, expected):
                        before_phase = phase
                        state = store.checkpoint(
                            _failure_phase(
                                state,
                                "heartbeat parked the remaining active slices",
                            ),
                            state.slices,
                            state.budgets,
                            state.events.last_consumed_offset,
                        )
                        phase = _phase_from_state(state)
                        _emit_phase_change(
                            run_id, before_phase, phase, config, effects, effects_log
                        )
            if _sub_tls_waiting_for_integration(plan, state) and not config.keep_alive_on_waiting:
                return TLRunResult(
                    state,
                    tuple(effects_log),
                    tuple(transitions),
                    tuple(consumed),
                    tuple(heartbeat_events),
                    diagnostics.snapshot(),
                    _journal_entries(effects_log),
                )
            # A direct-leaf/worker scope (no sub_tls) has no recursive
            # sub-child dispatch to await, so an empty poll carries no new
            # information: any remaining work (post-merge boundary steps,
            # scope finalization) is entirely internal to already-persisted
            # state. Drain it here instead of relying on the next ledger
            # event, which will never come.
            if not plan.sub_tls:
                state = _drain_direct_scope_convergence(
                    state, convergence, store, config, effects, effects_log
                )
            continue
        event_seq = event.run_seq
        if event_seq is None:
            raise TLLoopError(f"{event.event_type!r} has no run_seq")
        if not replaying and event_seq <= state.events.last_consumed_offset:
            diagnostics.filtered += 1
            _ack_event(source, event, replaying, diagnostics)
            state = store.load()
            continue
        if not replaying:
            consumed.append(event_seq)
            diagnostics.received += 1
        diagnostics.last_event_seq = event_seq
        checkpoint_seq = max(event_seq, state.events.last_consumed_offset)
        ledger_run_id = config.ledger_run_id or run_id
        if event.run_id not in {None, ledger_run_id}:
            diagnostics.filtered += 1
            _checkpoint_and_ack(store, source, event, state, phase, acknowledge=not replaying)
            if not replaying:
                diagnostics.acknowledged += 1
            _release_replayed_event(store, event, replaying)
            state = store.load()
            LOGGER.warning(
                "Ignoring event from stale swarm run_id=%s expected=%s local_checkpoint=%s event_seq=%s",
                event.run_id,
                ledger_run_id,
                run_id,
                event_seq,
            )
            continue
        resolution = resolve_event_slice(event, state, allowed_ids=expected)
        if not _event_belongs_to_plan(event, expected, state):
            if _is_reconcilable_identity_event(event):
                if not replaying:
                    quarantined.append(event)
                    store.quarantine_event(envelope_document(event))
                diagnostics.record_unresolved_event(event, resolution)
            diagnostics.filtered += 1
            _checkpoint_and_ack(
                store,
                source,
                event,
                state,
                phase,
                acknowledge=not replaying,
            )
            if not replaying:
                diagnostics.acknowledged += 1
            state = store.load()
            continue
        state = _note_authoritative_event(store, state, checkpoint_seq)
        diagnostics.last_authoritative_event_seq = event_seq
        diagnostics.last_observed_progress_at = state.goals.last_progress_at
        if config.heartbeat is not None:
            state = _note_heartbeat_progress(store, state)
        direct_reviewer_verdict = event.event_type == "pr.review" and "verdict" in event.data
        if event.kind in {EventKind.PR_REVIEW, EventKind.COPILOT_REVIEW}:
            diagnostics.correlated += 1
            if _review_workflow_enabled(config) or direct_reviewer_verdict:
                state = _route_review_event(
                    plan, store, state, phase, event, checkpoint_seq, config, effects, effects_log
                )
            else:
                state = _record_review_event(store, state, phase, event, checkpoint_seq)
            if plan.sub_tls:
                state = _run_sub_tls(plan, state, config, source, effects, store, effects_log)
                phase = _phase_from_state(state)
            state = _apply_convergence(state, convergence, store, config, effects, effects_log)
            _ack_event(source, event, replaying, diagnostics)
            _release_replayed_event(store, event, replaying)
            if _is_terminal_phase(phase):
                break
            continue
        if event.kind is EventKind.CI_STATUS_CHANGED:
            diagnostics.correlated += 1
            state = _route_ci_event(
                store, state, phase, event, checkpoint_seq, config, effects, effects_log
            )
            if plan.sub_tls:
                state = _run_sub_tls(plan, state, config, source, effects, store, effects_log)
                phase = _phase_from_state(state)
            state = _apply_convergence(state, convergence, store, config, effects, effects_log)
            _ack_event(source, event, replaying, diagnostics)
            _release_replayed_event(store, event, replaying)
            if _is_terminal_phase(phase):
                break
            continue
        # Rust's direct agent.spawned records carry the canonical branch and
        # child identity; shadowed replay records use the normal decoder path.
        if event.kind is EventKind.AGENT_SPAWNED:
            correlation = correlate_dispatch_event(state, event)
            if correlation.classification != DISPATCH_CORRELATED:
                diagnostics.filtered += 1
                diagnostics.rejected += 1
                LOGGER.warning(
                    "Ignoring %s agent.spawned event run_id=%s event_seq=%s reason=%s",
                    correlation.classification,
                    run_id,
                    event_seq,
                    correlation.reason,
                )
                _record_dispatch_correlation_failure(
                    store,
                    state,
                    event,
                    correlation,
                    config,
                    effects,
                    effects_log,
                )
                _checkpoint_and_ack(store, source, event, state, phase, acknowledge=not replaying)
                if not replaying:
                    diagnostics.acknowledged += 1
                state = store.load()
                continue
            event_slice_id = _event_slice_id(event, state)
            if event_slice_id is None:
                raise TLLoopError("agent.spawned matched an intent but no dispatch slice was found")
            current = state.slices[event_slice_id]
            branch = event.data.get("branch")
            branch = branch if isinstance(branch, str) and branch else current.branch or ""
            agent_type = event.data.get("agent_type")
            agent_type = (
                agent_type
                if isinstance(agent_type, str) and agent_type
                else current.agent_type or "unknown"
            )
            fsm_event = ChildSpawned(ChildHandle(event_slice_id, branch, agent_type))
            try:
                next_phase = phase_transition(phase, fsm_event)
            except IllegalTransition as error:
                raise TLLoopError(str(error)) from error
            next_slices = _confirm_dispatch_event(
                state.slices,
                state.slices,
                event,
                event_slice_id,
                event_seq,
                state.controller_epoch,
            )
            next_slices[event_slice_id] = replace(
                next_slices[event_slice_id],
                branch=branch or next_slices[event_slice_id].branch,
            )
            previous_state = state
            state = store.checkpoint(next_phase, next_slices, state.budgets, checkpoint_seq)
            _emit_slice_status_changes(
                previous_state.slices,
                next_slices,
                config,
                effects,
                effects_log,
            )
            _emit_dispatch_confirmation(
                previous_state.slices,
                next_slices,
                event,
                event_slice_id,
                config,
                effects,
                effects_log,
            )
            state = _apply_convergence(state, convergence, store, config, effects, effects_log)
            _ack_event(source, event, replaying, diagnostics)
            _release_replayed_event(store, event, replaying)
            diagnostics.correlated += 1
            transitions.append(
                LoopTransition(
                    event_seq,
                    event.event_type,
                    _phase_tag(phase),
                    _phase_tag(next_phase),
                )
            )
            _emit_phase_change(run_id, phase, next_phase, config, effects, effects_log)
            phase = next_phase
            if _is_terminal_phase(phase):
                break
            continue
        try:
            fsm_event = decoder.decode(event)
        except Exception as error:
            raise TLLoopError(str(error)) from error
        event_slice_id = _event_slice_id(event, state)
        if (
            isinstance(fsm_event, (PRFiled, PRUpdated))
            and fsm_event.slice_id is None
            and event_slice_id is not None
        ):
            fsm_event = replace(fsm_event, slice_id=event_slice_id)
        if event.kind is EventKind.AGENT_TASK_BLOCKED:
            state = _record_task_blocked_recovery(
                event,
                state,
                store,
                phase,
                checkpoint_seq,
                effects,
            )
            phase = _phase_from_state(state)
            state = _apply_convergence(state, convergence, store, config, effects, effects_log)
            _ack_event(source, event, replaying, diagnostics)
            _release_replayed_event(store, event, replaying)
            diagnostics.correlated += 1
            continue
        if _duplicate_event(phase, fsm_event, state):
            diagnostics.filtered += 1
            _checkpoint_and_ack(store, source, event, state, phase, acknowledge=not replaying)
            if not replaying:
                diagnostics.acknowledged += 1
            state = store.load()
            continue
        if isinstance(phase, RecursiveTLRunning) and isinstance(fsm_event, AllChildrenDone):
            next_phase = _complete_legacy_direct_children(phase)
            if next_phase == phase and not phase.parallel_pending:
                next_phase = TLDone()
            if next_phase != phase:
                state = store.checkpoint(
                    next_phase,
                    state.slices,
                    state.budgets,
                    checkpoint_seq,
                )
                _ack_event(source, event, replaying, diagnostics)
                _release_replayed_event(store, event, replaying)
                diagnostics.correlated += 1
                transitions.append(
                    LoopTransition(
                        event_seq,
                        event.event_type,
                        _phase_tag(phase),
                        _phase_tag(next_phase),
                    )
                )
                _emit_phase_change(run_id, phase, next_phase, config, effects, effects_log)
                phase = next_phase
                continue
        try:
            next_phase = phase_transition(phase, fsm_event)
        except IllegalTransition as error:
            raise TLLoopError(str(error)) from error
        next_slices = _update_slices(
            state.slices,
            fsm_event,
            slice_id=event_slice_id,
            allow_spawn_confirmation=_dispatch_confirmation_matches(
                state.slices, event, controller_epoch=state.controller_epoch
            ),
        )
        if isinstance(fsm_event, ChildCompleted):
            completed_slice = next_slices.get(event_slice_id or "")
            next_slices = _apply_child_completion(
                next_slices,
                event_slice_id,
                event,
                persist_publication=(
                    config.parent_run_id is not None
                    or (
                        completed_slice is not None
                        and "/sub_tl/" in (completed_slice.manifest_node_id or "")
                    )
                ),
            )
        if _is_spawn_confirmation_event(event):
            next_slices = _confirm_dispatch_event(
                state.slices,
                next_slices,
                event,
                event_slice_id,
                event_seq,
                state.controller_epoch,
            )
        if isinstance(fsm_event, (PRFiled, PRUpdated)):
            next_slices = _bind_publication_evidence(
                next_slices,
                fsm_event,
                event,
                event_slice_id,
            )
        previous_state = state
        state = store.checkpoint(next_phase, next_slices, state.budgets, checkpoint_seq)
        _emit_slice_status_changes(
            previous_state.slices,
            next_slices,
            config,
            effects,
            effects_log,
        )
        _emit_dispatch_confirmation(
            previous_state.slices,
            next_slices,
            event,
            event_slice_id,
            config,
            effects,
            effects_log,
        )
        if (
            isinstance(phase, RecursiveTLRunning)
            and isinstance(fsm_event, ChildSpawned)
            and not transitions
            and isinstance(event.data.get("shadow_event"), Mapping)
            and event.data["shadow_event"].get("kind") == "child_spawned"
        ):
            _emit_phase_change(
                run_id,
                TLPlanning(),
                TLWaiting({}),
                config,
                effects,
                effects_log,
            )
        _ack_event(source, event, replaying, diagnostics)
        _release_replayed_event(store, event, replaying)
        diagnostics.correlated += 1
        before_tag = _phase_tag(phase)
        after_tag = _phase_tag(next_phase)
        transitions.append(LoopTransition(event_seq, event.event_type, before_tag, after_tag))
        _emit_phase_change(run_id, phase, next_phase, config, effects, effects_log)
        LOGGER.info(
            "[TL loop] transition run_id=%s event_seq=%d before=%s after=%s",
            run_id,
            event_seq,
            before_tag.value,
            after_tag.value,
        )
        phase = next_phase
        if config.policy is not None and config.max_parallel_slices is not None:
            state = _dispatch_children(plan, state, config, effects, effects_log, store)
        state = _apply_convergence(state, convergence, store, config, effects, effects_log)
        if _is_terminal_phase(phase):
            break
    else:
        if config.test_harness:
            raise LoopLimitExceeded(
                f"event limit {config.max_events} reached before TL reached a terminal phase"
            )
    if not _is_terminal_phase(phase):
        raise TLLoopError("TL controller exited without an authoritative terminal phase")
    if phase is TLFailed:
        reason = next(
            (current.dispatch_error for current in state.slices.values() if current.dispatch_error),
            "TL reached the failed terminal phase",
        )
        store.record_terminal_summary(
            {
                "reason": reason,
                "deadline_reason": None,
                "timeout_seconds": None,
                "diagnostics": diagnostics.snapshot(),
            }
        )
    return TLRunResult(
        state,
        tuple(effects_log),
        tuple(transitions),
        tuple(consumed),
        tuple(heartbeat_events),
        diagnostics.snapshot(),
        _journal_entries(effects_log),
    )


def _apply_convergence(
    state: RunState,
    tracker: ConvergenceTracker,
    store: RunStore,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    """Run bounded post-reduction convergence until the next effect or wait."""
    if not config.enable_reviewer_spawn:
        decision = derive_next_action(
            state,
            reviewer_max_rounds=tracker.reviewer_max_rounds,
            review_freshness_window_secs=tracker.review_freshness_window_secs,
            now=tracker.review_now,
        )
        if isinstance(decision, ExternalIntent) and decision.operation == "spawn_reviewer":
            tracker.last_decision = Quiescent("reviewer_spawn_disabled")
            return state
    for _ in range(MAX_CONVERGENCE_STEPS):
        try:
            result = tracker.reduce(state)
        except ConvergenceInvariantError as error:
            for event in error.events:
                _record_convergence_event(error.key, event, config, effects, effects_log)
            raise TLLoopError(str(error)) from error
        if isinstance(result.state, RunState) and result.state.state_version > state.state_version:
            state = store.set_state_version(result.state.state_version)
        for event in result.events:
            _record_convergence_event(
                _event_target(event.payload), event, config, effects, effects_log
            )
        if isinstance(result.decision, InternalTransition):
            if result.decision.reason == "review_rounds_exhausted":
                return _park_review_rounds_exhausted(
                    state,
                    store,
                    config,
                    effects,
                    effects_log,
                    tracker.reviewer_max_rounds,
                )
            prior = state
            state = _apply_internal_transition(state, result.decision, store)
            if result.decision.transition == "terminal" or state == prior:
                return state
            continue
        if isinstance(result.decision, ExternalIntent):
            return _execute_external_intent(
                state,
                result.decision,
                tracker,
                store,
                config,
                effects,
                effects_log,
            )
        return state
    raise TLLoopError("convergence did not reach a stable action or wait state")


def _drain_direct_scope_convergence(
    state: RunState,
    convergence: ConvergenceTracker,
    store: RunStore,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    """Advance a direct-leaf/worker scope's internal-only work to a real wait.

    _apply_convergence advances at most one post-merge boundary (or other
    internal) step per call, and its ConvergenceTracker deliberately rejects
    repeating the same action against an unchanged ``state_version`` -- most
    post-merge boundary steps do not bump it. Each step below therefore uses
    its own fresh tracker (the same bounded-drain idiom already used by
    tests exercising this pipeline directly), so a direct-leaf scope with no
    remaining child dispatch to observe can reach TLDone, or a genuine wait,
    within this call instead of only advancing on the next ledger event.
    The caller's shared ``convergence`` tracker is refreshed with the final
    decision so its own wait/Quiescent bookkeeping reflects drained state.

    A fresh tracker per step means the dedup guard that would normally catch
    a non-progressing action repeated against an unchanged state_version
    cannot fire here. This drain owns that check itself instead, comparing
    persisted content (every field but the ``version``/``revision``/
    ``state_version`` write counters, which a checkpoint -- or, for
    state_version, _apply_convergence's own InternalTransition handling --
    can bump even when nothing meaningful changed) before and after each
    step: a step whose content doesn't move is a non-progressing action,
    and raises immediately rather than letting the caller's "no event" poll
    silently re-attempt it forever. A step
    that *does* move content is real progress, so exhausting
    DIRECT_SCOPE_DRAIN_STEP_LIMIT is only a per-call fairness cap -- it
    returns normally, exactly as if the event source had been empty from
    the start, and the next "no event" poll continues draining. A scope
    that legitimately needs more action boundaries than one call's budget
    must never be mistaken for a stuck one.
    """
    for _ in range(DIRECT_SCOPE_DRAIN_STEP_LIMIT):
        decision = derive_next_action(
            state,
            reviewer_max_rounds=convergence.reviewer_max_rounds,
            review_freshness_window_secs=convergence.review_freshness_window_secs,
            now=convergence.review_now,
        )
        if isinstance(decision, Quiescent):
            convergence.last_decision = decision
            return state
        before = replace(state, version=0, revision=0, state_version=0)
        state = _apply_convergence(
            state,
            ConvergenceTracker(
                reviewer_max_rounds=convergence.reviewer_max_rounds,
                review_freshness_window_secs=convergence.review_freshness_window_secs,
                review_now=convergence.review_now,
            ),
            store,
            config,
            effects,
            effects_log,
        )
        if replace(state, version=0, revision=0, state_version=0) == before:
            raise TLLoopError(
                "direct-leaf/worker scope made no progress advancing "
                f"{decision!r}; refusing to retry a non-progressing action"
            )
    convergence.last_decision = derive_next_action(
        state,
        reviewer_max_rounds=convergence.reviewer_max_rounds,
        review_freshness_window_secs=convergence.review_freshness_window_secs,
        now=convergence.review_now,
    )
    return state


def _park_review_rounds_exhausted(
    state: RunState,
    store: RunStore,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    reviewer_max_rounds: int | None,
) -> RunState:
    """Durably park the first exhausted direct-review slice and open its gate."""
    effective_reviewer_max_rounds = (
        state.reviewer_max_rounds
        if state.reviewer_max_rounds_source is not None
        else reviewer_max_rounds
    )
    target = next(
        (
            current
            for _, current in sorted(state.slices.items())
            if current.verdict is Verdict.NO_GO
            and effective_reviewer_max_rounds is not None
            and current.review_rounds >= effective_reviewer_max_rounds
            and not _is_aggregate_slice(current)
            and current.status
            not in {
                SliceStatus.MERGED,
                SliceStatus.FAILED,
                SliceStatus.PARKED,
                SliceStatus.BLOCKED,
            }
        ),
        None,
    )
    if target is None:
        return state
    audit = {
        "review_rounds": target.review_rounds,
        "reviewer_max_rounds": effective_reviewer_max_rounds,
        "head_sha": _persisted_slice_head(target),
        "pr_number": target.pr_number,
    }
    if config.active:
        park(
            target,
            ParkCause.REVIEW_ROUNDS_EXHAUSTED,
            store=store,
            issue_creator=cast(EffectClient, effects),
            audit=audit,
        )
    else:
        parked = slice_transition(target, ReviewRoundsExhausted())
        parked = replace(
            parked,
            park_cause=ParkCause.REVIEW_ROUNDS_EXHAUSTED,
            park_audit=audit,
        )
        store.checkpoint(
            state.fsm,
            {**state.slices, target.id: parked},
            state.budgets,
            state.events.last_consumed_offset,
            current_order=state.current_order,
            ordered_stages=state.ordered_stages,
            integration=state.integration,
        )
    parked_state = store.load()
    _record_controller_event(
        target.id,
        "tl.review_rounds_exhausted",
        {
            "slice_id": target.id,
            "pr_number": target.pr_number,
            "head_sha": _persisted_slice_head(target),
            "review_rounds": target.review_rounds,
            "reviewer_max_rounds": effective_reviewer_max_rounds,
        },
        config,
        effects,
        effects_log,
    )
    return parked_state


def _apply_internal_transition(
    state: RunState,
    decision: InternalTransition,
    store: RunStore,
) -> RunState:
    """Persist lifecycle changes derived from durable evidence on restart."""
    if decision.transition == "terminal":
        return state
    candidates = tuple(
        current
        for _, current in sorted(state.slices.items())
        if current.status not in {SliceStatus.MERGED, SliceStatus.FAILED}
    )
    target: SliceState | None = None
    if decision.transition == "revalidate_review":
        if decision.target_id is None:
            return state
        target = state.slices.get(decision.target_id)
        if target is None:
            return state
        updated = slice_transition(target, RevalidateReview())
        if updated == target:
            return state
        return store.checkpoint(
            state.fsm,
            {**state.slices, target.id: updated},
            state.budgets,
            state.events.last_consumed_offset,
            current_order=state.current_order,
            ordered_stages=state.ordered_stages,
            integration=state.integration,
        )
    if decision.reason == "head_reset":
        target = next(
            (
                current
                for current in candidates
                if current.reviewed_head is not None
                and _persisted_slice_head(current) is not None
                and current.reviewed_head != _persisted_slice_head(current)
            ),
            None,
        )
        if target is None:
            return state
        updated = slice_transition(target, HeadChanged(None))
        updated = slice_transition(updated, SliceStatusChanged(SliceStatus.IN_REVIEW))
        updated = replace(
            updated,
            dispatch_last_boundary="restart_head_reset",
            dispatch_error=None,
        )
    elif decision.transition == "repairing":
        target = next((current for current in candidates if _slice_has_conflict(current)), None)
        if target is None:
            return state
        updated = slice_transition(target, RepairQueued())
        updated = replace(
            updated,
            dispatch_last_boundary="restart_repair_queued",
            dispatch_error=None,
        )
    else:
        return state
    return _checkpoint_slice_action(store, state, target.id, None, slice_state=updated)


def _persisted_slice_head(current: SliceState) -> str | None:
    """Read the publication/handoff head without importing policy internals."""
    if current.publication is not None:
        return current.publication.head_sha
    if current.handoff is not None:
        return current.handoff.head_sha
    return current.reviewed_head


def _slice_has_conflict(current: SliceState) -> bool:
    """Match the reducer's conflict predicate for transition targeting."""
    if current.dispatch_error is not None and "conflict" in current.dispatch_error.lower():
        return True
    reconciliation = current.reconciliation
    return isinstance(reconciliation, Mapping) and bool(reconciliation.get("conflicts"))


def _execute_external_intent(
    state: RunState,
    intent: ExternalIntent,
    tracker: ConvergenceTracker,
    store: RunStore,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    """Execute one direct-leaf intent after its durable convergence event."""
    if intent.operation in {"validate_integration", "merge_aggregate", "revalidate_base"}:
        candidate_id = next(
            (
                candidate_id
                for candidate_id, candidate in state.integration.candidates.items()
                if candidate_id == intent.target_id
                or candidate.integration_owner_id == intent.target_id
            ),
            None,
        )
        if candidate_id is None:
            return state
        return _integrate_one_candidate(
            SubTLTask(candidate_id, WorkPlan()),
            state,
            config,
            effects,
            store,
            effects_log,
        )
    if intent.target_id not in state.slices:
        return state
    if intent.operation == "merge":
        return _execute_direct_merge_intent(
            state, intent, tracker, store, config, effects, effects_log
        )
    if intent.operation == "post_merge_recovery":
        return _execute_post_merge_recovery_intent(
            state, intent, store, config, effects, effects_log
        )
    if intent.operation == "repair":
        return _execute_direct_repair_intent(
            state, intent, tracker, store, config, effects, effects_log
        )
    if intent.operation == "spawn_reviewer":
        return _execute_direct_reviewer_intent(
            state, intent, tracker, store, config, effects, effects_log
        )
    return state


def _execute_direct_reviewer_intent(
    state: RunState,
    intent: ExternalIntent,
    tracker: ConvergenceTracker,
    store: RunStore,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    """Persist and execute a reviewer spawn derived from a direct leaf."""
    if not config.enable_reviewer_spawn:
        return state
    current = state.slices[intent.target_id]
    head_sha = intent.arguments.get("head_sha")
    pr_number = intent.arguments.get("pr_number")
    if not isinstance(head_sha, str) or not isinstance(pr_number, int):
        return state
    contract = _review_contract_for_slice(current)
    requested_digest = intent.arguments.get("review_contract_digest")
    if isinstance(requested_digest, str) and requested_digest != contract.digest:
        raise TLLoopError(
            f"review contract changed for {current.id!r} head {head_sha!r}; "
            "reduction must produce a fresh reviewer action"
        )
    repository_identity = _review_repository_identity(current, state, config, store)
    review_arguments: dict[str, object] = {
        "repository_identity": repository_identity,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "review_contract_digest": contract.digest,
    }
    claimed = replace(
        current,
        reviewer_attempt={**current.reviewer_attempt, head_sha: 1},
        review_contract=contract.as_mapping(),
    )
    action_intent_id = stable_action_key(
        state.run_id, "spawn_reviewer", current.id, review_arguments
    )
    claimed = slice_transition(
        claimed,
        ReviewerDispatched(
            intent_id=action_intent_id,
            head_sha=head_sha,
            attempt=max(1, current.attempts),
            contract_digest=contract.digest,
        ),
    )
    action = claimed.action
    if action is None:
        raise TLLoopError(f"reviewer dispatch did not create an action for {current.id!r}")
    state = _checkpoint_slice_action(
        store,
        state,
        current.id,
        action,
        slice_state=claimed,
    )
    current = state.slices[current.id]
    _record_convergence_event(
        current.id,
        tracker.action_started(state, intent),
        config,
        effects,
        effects_log,
    )
    journal_intent = EffectIntent("spawn_reviewer", current.id, review_arguments, config.active)
    try:
        result = _invoke(
            "spawn_reviewer",
            current.id,
            review_arguments,
            config.active,
            cast(EffectClient, effects) if config.active else None,
            lambda client: client.spawn_reviewer(
                pr_number=pr_number,
                head_sha=head_sha,
                acceptance_criteria=contract.acceptance_criteria,
                force=False,
            ),
            effects_log,
        )
    except EffectFailed:
        refreshed = store.load()
        _checkpoint_slice_action(
            store,
            refreshed,
            current.id,
            replace(action, phase=ActionPhase.REJECTED),
        )
        raise
    except BaseException as error:
        refreshed = store.load()
        unknown = _checkpoint_slice_action(
            store,
            refreshed,
            current.id,
            replace(action, phase=ActionPhase.UNKNOWN),
        )
        _record_convergence_event(
            current.id,
            tracker.action_outcome(unknown, intent, outcome="unknown", error=str(error)),
            config,
            effects,
            effects_log,
        )
        if isinstance(effects_log, EffectJournal):
            key = effects_log.key_for(journal_intent)
            gate_name = _action_journal_gate_name(key)
            if not any(gate.name == gate_name for gate in unknown.gates):
                store.set_gate(gate_name, GateStatus.PENDING)
        raise
    reviewer_name = (
        result.result.get("reviewer_name")
        if result is not None and isinstance(result.result, Mapping)
        else None
    )
    refreshed = store.load()
    latest = refreshed.slices[current.id]
    updated = slice_transition(
        latest,
        ReviewerIdentityObserved(
            reviewer_name if isinstance(reviewer_name, str) else latest.reviewer_agent_id
        ),
    )
    updated = slice_transition(
        updated,
        ActionChanged(replace(action, phase=ActionPhase.CONFIRMED)),
    )
    return _checkpoint_slice_action(
        store, refreshed, current.id, updated.action, slice_state=updated
    )


def _review_contract_for_slice(current: SliceState) -> ReviewContract:
    if current.review_contract is not None:
        try:
            return ReviewContract.from_mapping(current.review_contract)
        except ValueError as error:
            raise TLLoopError(f"invalid persisted review contract for {current.id!r}") from error
    return compose_review_contract(
        current,
        {"verify": current.test_plan, "boundary": current.paths},
    )


def _review_repository_identity(
    current: SliceState,
    state: RunState,
    config: TLLoopConfig,
    store: RunStore,
) -> dict[str, object]:
    if state.repository_identity is not None:
        return {
            "owner": state.repository_identity.owner,
            "repo": state.repository_identity.repo,
            "base_branch": state.repository_identity.base_branch,
        }
    root = Path(config.project_root or store.root_dir.parent).resolve()
    base_branch = (
        current.publication.base_branch
        if current.publication is not None
        else current.base_ref or config.branch
    )
    return {"owner": "local", "repo": root.name or "project", "base_branch": base_branch}


def _execute_direct_repair_intent(
    state: RunState,
    intent: ExternalIntent,
    tracker: ConvergenceTracker,
    store: RunStore,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    """Route direct-leaf repair through the existing same-owner handoff path."""
    current = state.slices[intent.target_id]
    reasons = _repair_reasons(current)
    review = {"verdict": Verdict.NO_GO.value, "reasons": reasons}
    _record_convergence_event(
        current.id,
        tracker.action_started(state, intent),
        config,
        effects,
        effects_log,
    )
    return _route_repair(
        store,
        state,
        state.fsm,
        state.events.last_consumed_offset,
        current.id,
        review,
        config,
        effects,
        effects_log,
    )


def _execute_post_merge_recovery_intent(
    state: RunState,
    intent: ExternalIntent,
    store: RunStore,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    """Finish bookkeeping for a merge already adopted by the parent."""
    current = state.slices[intent.target_id]
    if current.status is not SliceStatus.MERGED or current.pr_number is None:
        return state
    journal_id = current.action.intent_id if current.action is not None else None
    if not journal_id and isinstance(current.reconciliation, Mapping):
        persisted_journal_id = current.reconciliation.get("merge_journal_id")
        if isinstance(persisted_journal_id, str) and persisted_journal_id:
            journal_id = persisted_journal_id
    if not journal_id:
        return _block_post_merge_recovery(
            store,
            state,
            current.id,
            "post-merge recovery has no durable merge journal identity",
        )
    result = _reconcile_merged_slice(
        state,
        current.id,
        current.pr_number,
        journal_id,
        config,
        effects,
        store,
        effects_log,
        boundary="post_merge_recovery",
    )
    return result


def _reconcile_merged_slice(
    state: RunState,
    slice_id: str,
    pr_number: int,
    merge_journal_id: str,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
    *,
    boundary: str,
    merge_evidence: Mapping[str, object] | None = None,
) -> RunState:
    """Adopt a confirmed merge and execute at most one recovery boundary."""
    current = state.slices[slice_id]
    checkpoint_needed = False
    if current.post_merge is None or current.post_merge.phase is PostMergePhase.NOT_STARTED:
        try:
            current = _adopt_post_merge_slice(
                current,
                state,
                pr_number,
                merge_journal_id,
                boundary,
                merge_evidence,
            )
        except ValueError as error:
            if state.repository_identity is None and "repository identity is unavailable" in str(
                error
            ):
                # Lazily heal exactly the #1062 gap: identity is resolved the
                # first time a merge adoption actually needs it and finds it
                # missing, not proactively on every continuation (which would
                # fire even for runs that never touch a merged PR). Retried
                # once, immediately, so the adoption below still completes
                # within this same call when the effect succeeds.
                state = _heal_missing_repository_identity(state, config, effects, effects_log, store)
                if state.repository_identity is None:
                    return state
                try:
                    current = _adopt_post_merge_slice(
                        current,
                        state,
                        pr_number,
                        merge_journal_id,
                        boundary,
                        merge_evidence,
                    )
                except ValueError as retry_error:
                    return _block_post_merge_recovery(
                        store,
                        state,
                        slice_id,
                        f"cannot adopt merged PR #{pr_number}: {retry_error}",
                    )
            else:
                return _block_post_merge_recovery(
                    store,
                    state,
                    slice_id,
                    f"cannot adopt merged PR #{pr_number}: {error}",
                )
        checkpoint_needed = True
    try:
        integration = _resolve_recovered_lane_integration(state, current, merge_evidence)
    except ValueError as error:
        return _block_post_merge_recovery(
            store,
            state,
            slice_id,
            f"cannot resolve recovered lane for merged PR #{pr_number}: {error}",
        )
    if integration != state.integration:
        checkpoint_needed = True
    if checkpoint_needed:
        state = _checkpoint_slice_action(
            store,
            state,
            slice_id,
            None,
            slice_state=current,
            integration=integration,
        )
    return _advance_post_merge_boundary(
        store.load(),
        slice_id,
        pr_number,
        config,
        effects,
        effects_log,
        store,
    )


def _heal_missing_repository_identity(
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    store: RunStore,
) -> RunState:
    """Resolve repository identity the first time a merge adoption needs it.

    Repository identity is static run configuration -- identical for every
    slice, never changing during a run -- so it is resolved once and cached
    on the checkpoint. Persists it on success. On failure, opens the named
    REPOSITORY_IDENTITY_GATE_NAME gate rather than raising or guessing an
    owner/repo (#1062); the caller re-checks state.repository_identity to
    decide whether to retry the adoption that triggered this healing.
    """
    resolved = _resolve_repository_identity(config, effects, effects_log)
    if resolved is None:
        return store.set_gate(REPOSITORY_IDENTITY_GATE_NAME)
    return store.set_repository_identity(resolved)


def _adopt_post_merge_slice(
    current: SliceState,
    state: RunState,
    pr_number: int,
    merge_journal_id: str,
    boundary: str,
    merge_evidence: Mapping[str, object] | None,
) -> SliceState:
    """Turn authoritative merge evidence into the first post-merge state."""
    if type(pr_number) is not int or pr_number <= 0:
        raise ValueError("merged PR number is unavailable in authoritative evidence")
    if not merge_journal_id:
        raise ValueError("merged PR journal identity is unavailable")
    if (
        current.post_merge is not None
        and current.post_merge.phase is not PostMergePhase.NOT_STARTED
    ):
        return replace(
            current,
            status=SliceStatus.MERGED,
            action=None,
            dispatch_last_boundary=boundary,
        )
    evidence = merge_evidence or {}
    head_sha = _required_merge_identity(
        evidence,
        ("head_sha", "expected_head_sha"),
        "merged PR head SHA",
    )
    expected_base_sha = _required_merge_identity(
        evidence,
        ("base_sha", "expected_base_sha"),
        "merged PR base SHA",
    )
    lane_epoch = _merge_adoption_lane_epoch(state, current, evidence)
    persisted_repository = _repository_identity(state)
    repository = (
        _required_merge_identity(evidence, ("repository",), "merged PR repository")
        if "repository" in evidence
        else persisted_repository
    )
    if repository != persisted_repository:
        raise ValueError("merged PR repository does not match persisted repository identity")
    persisted_parent_branch = _publication_parent_branch_from_slice(current)
    parent_branch = (
        _required_merge_identity(evidence, ("base_branch", "parent_branch"), "parent branch")
        if "base_branch" in evidence or "parent_branch" in evidence
        else persisted_parent_branch
    )
    if parent_branch != persisted_parent_branch:
        raise ValueError("merged PR parent branch does not match persisted publication identity")
    adopted = replace(current, status=SliceStatus.MERGED, action=None, post_merge=None)
    adopted = slice_transition(
        adopted,
        PostMergeEventObserved(
            MergeAdopted(
                child_id=current.id,
                pr_number=pr_number,
                head_sha=head_sha,
                journal_id=merge_journal_id,
                repository=repository,
                parent_branch=parent_branch,
                lane_epoch=lane_epoch,
            )
        ),
    )
    assert adopted.post_merge is not None
    reconciliation = dict(current.reconciliation or {})
    defaults: dict[str, object] = {
        "confirmed_stage": boundary,
        "authoritative_evidence": [],
        "missing_evidence": [],
        "conflicts": [],
        "next_action": "post_merge_recovery",
    }
    for key, default in defaults.items():
        value = reconciliation.get(key)
        if (key in {"confirmed_stage", "next_action"} and not isinstance(value, str)) or (
            key not in {"confirmed_stage", "next_action"} and not isinstance(value, list)
        ):
            reconciliation[key] = default
    reconciliation.update(
        {
            "merge_base_sha": expected_base_sha,
            "merge_head_sha": head_sha,
            "merge_journal_id": merge_journal_id,
        }
    )
    return replace(
        adopted,
        dispatch_last_boundary=boundary,
        reconciliation=reconciliation,
    )


def _required_merge_identity(
    evidence: Mapping[str, object], keys: tuple[str, ...], name: str
) -> str:
    for key in keys:
        value = evidence.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError(f"{name} is unavailable in authoritative merge evidence")


def _merge_adoption_lane_epoch(
    state: RunState,
    current: SliceState,
    evidence: Mapping[str, object],
) -> int:
    value = evidence.get("lane_epoch")
    if value is None:
        try:
            repository, parent_branch = _candidate_lane_key(state, current)
            lane = state.integration.lanes.get(f"{repository}:{parent_branch}")
            value = lane.lane_epoch if lane is not None else 1
        except ValueError:
            value = 1
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if type(value) is not int or value <= 0:
        raise ValueError("merged PR lane epoch is unavailable")
    return value


def _repository_identity(state: RunState) -> str:
    identity = state.repository_identity
    if identity is None or not identity.owner or not identity.repo:
        raise ValueError("repository identity is unavailable")
    return f"{identity.owner}/{identity.repo}"


def _publication_parent_branch_from_slice(current: SliceState) -> str:
    """Use persisted publication identity, then the immutable target branch."""
    if current.publication is not None and current.publication.base_branch:
        return current.publication.base_branch
    if current.base_ref:
        return current.base_ref
    raise ValueError("parent branch is unavailable in persisted slice identity")


def _merge_result_payload(result: ToolResult | None) -> Mapping[str, object] | None:
    if result is None or result.success is not True or not isinstance(result.result, Mapping):
        return None
    return result.result


def _post_merge_effect(
    operation: str,
    target: str,
    arguments: Mapping[str, object],
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    method: str,
    *,
    active: bool = True,
    call_arguments: Mapping[str, object] | None = None,
    retryable_failure: bool = True,
) -> ToolResult | None:
    callback = getattr(effects, method, None)
    if not callable(callback):
        raise TLLoopError(f"{operation} requires effect {method!r}; no safe fallback exists")
    dispatch_arguments = call_arguments or arguments
    return _invoke(
        operation,
        target,
        arguments,
        active,
        cast(EffectClient, effects),
        lambda client: cast(ToolResult, getattr(client, method)(**dispatch_arguments)),
        effects_log,
        raise_on_failure=False,
        retryable_failure=retryable_failure,
    )


def _checkpoint_post_merge_event(
    store: RunStore,
    state: RunState,
    slice_id: str,
    event: object,
    reconciliation_additions: Mapping[str, object] | None = None,
    lane_event: object | tuple[object, ...] | None = None,
) -> RunState:
    current = state.slices[slice_id]
    updated = slice_transition(current, PostMergeEventObserved(event))
    if reconciliation_additions:
        reconciliation = dict(updated.reconciliation or {})
        reconciliation.update(reconciliation_additions)
        updated = replace(updated, reconciliation=reconciliation)
    integration = state.integration
    if lane_event is not None:
        repository, parent_branch = _candidate_lane_key(state, current)
        key = f"{repository}:{parent_branch}"
        lane = integration.lanes.get(key)
        if lane is None:
            raise ValueError(f"lane {key!r} is not reserved for post-merge progress")
        lanes = dict(integration.lanes)
        lane_events = lane_event if isinstance(lane_event, tuple) else (lane_event,)
        for lane_transition_event in lane_events:
            lane = transition_lane(lane, lane_transition_event)
        lanes[key] = lane
        integration = replace(integration, lanes=lanes)
    result = _checkpoint_slice_action(
        store,
        state,
        slice_id,
        None,
        slice_state=updated,
        integration=integration,
    )
    return result


def _advance_post_merge_boundary(
    state: RunState,
    slice_id: str,
    pr_number: int,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    store: RunStore,
) -> RunState:
    """Run one authoritative effect or persist the next pending boundary."""
    current = state.slices[slice_id]
    post_merge = current.post_merge
    if post_merge is None or post_merge.phase is PostMergePhase.COMPLETE:
        return state
    evidence = dict(post_merge.evidence)
    try:
        if post_merge.phase is PostMergePhase.REMOTE_MERGE_ADOPTED:
            return _run_parent_sync(
                state, current, pr_number, evidence, config, effects, effects_log, store
            )
        if post_merge.phase is PostMergePhase.PARENT_BRANCH_SYNCED:
            issue_id = _required_issue_id(config.chainlink_issue_id)
            intent_id = _post_merge_key(state, "issue_close", current.id, pr_number)
            return _checkpoint_post_merge_event(
                store,
                state,
                slice_id,
                IssueClosePending(current.id, issue_id, intent_id),
            )
        if post_merge.phase is PostMergePhase.ISSUE_CLOSE_PENDING:
            return _run_issue_close(state, current, evidence, config, effects, effects_log, store)
        if post_merge.phase is PostMergePhase.ISSUE_CLOSE_CONFIRMED:
            intent_id = _post_merge_key(state, "changelog", current.id, pr_number)
            return _checkpoint_post_merge_event(
                store,
                state,
                slice_id,
                ChangelogPending(current.id, intent_id, 0),
            )
        if post_merge.phase is PostMergePhase.CHANGELOG_PENDING:
            if post_merge.evidence.get("rebuild_reason") and not post_merge.evidence.get(
                "rebuild_applied"
            ):
                generation = _required_int(
                    post_merge.evidence,
                    "changelog_generation",
                    "changelog generation",
                    allow_zero=True,
                )
                return _checkpoint_post_merge_event(
                    store,
                    state,
                    slice_id,
                    ChangelogPending(
                        current.id,
                        _post_merge_key(
                            state,
                            "changelog_rebuild",
                            current.id,
                            pr_number,
                            generation=generation,
                        ),
                        generation,
                    ),
                )
            return _run_changelog(state, current, evidence, config, effects, effects_log, store)
        if post_merge.phase is PostMergePhase.CHANGELOG_COMMITTED:
            generation = (
                _required_int(
                    post_merge.evidence,
                    "changelog_generation",
                    "changelog generation",
                    allow_zero=True,
                )
                if post_merge.evidence.get("rebuild_reason")
                else None
            )
            operation = "parent_push_rebuild" if generation is not None else "parent_push"
            journal_operation = (
                "parent_push_rebuild_journal" if generation is not None else "parent_push_journal"
            )
            intent_id = _post_merge_key(
                state, operation, current.id, pr_number, generation=generation
            )
            journal_id = _post_merge_key(
                state, journal_operation, current.id, pr_number, generation=generation
            )
            try:
                state, lane_ready = _ensure_bookkeeping_lane(
                    state,
                    current,
                    evidence,
                    store,
                    push_intent_id=intent_id,
                    push_journal_id=journal_id,
                )
            except (TypeError, ValueError) as error:
                return _block_post_merge_recovery(store, state, slice_id, str(error))
            if not lane_ready:
                return state
            current = state.slices[slice_id]
            evidence = dict(current.post_merge.evidence) if current.post_merge else evidence
            return _checkpoint_post_merge_event(
                store,
                state,
                slice_id,
                ParentPushPending(
                    current.id,
                    intent_id,
                    _required_current_base(current),
                    journal_id,
                ),
                lane_event=LaneBookkeepingStarted(
                    current.id,
                    _required_merge_identity(evidence, ("merge_journal_id",), "merge journal"),
                    intent_id,
                    journal_id,
                    _required_merge_identity(
                        evidence, ("changelog_commit_sha",), "changelog commit"
                    ),
                    _required_current_base(current),
                ),
            )
        if post_merge.phase is PostMergePhase.PARENT_PUSH_PENDING:
            return _run_parent_push(state, current, evidence, config, effects, effects_log, store)
    except (EffectFailed, TLLoopError, ToolUnavailableError, TypeError, ValueError) as error:
        return _block_post_merge_recovery(store, state, slice_id, str(error))
    return state


def _drain_post_merge_recovery(
    state: RunState,
    slice_id: str,
    pr_number: int,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    store: RunStore,
) -> RunState:
    """Advance checkpointed post-merge edges until an effect blocks or completes."""
    for _ in range(12):
        current = state.slices[slice_id]
        if current.post_merge is None or current.post_merge.phase is PostMergePhase.COMPLETE:
            return state
        before = (current.post_merge.phase, tuple(current.post_merge.evidence.items()))
        state = _advance_post_merge_boundary(
            state, slice_id, pr_number, config, effects, effects_log, store
        )
        current = state.slices[slice_id]
        if current.post_merge is None:
            return state
        after = (current.post_merge.phase, tuple(current.post_merge.evidence.items()))
        if before == after:
            return state
    return state


def _required_issue_id(issue_id: int | None) -> str:
    if type(issue_id) is not int or issue_id <= 0:
        raise ValueError("Chainlink issue ID is unavailable")
    return str(issue_id)


def _post_merge_key(
    state: RunState,
    operation: str,
    child_id: str,
    pr_number: int,
    *,
    generation: int | None = None,
) -> str:
    arguments: dict[str, object] = {"pr_number": pr_number}
    if generation is not None:
        arguments["generation"] = generation
    return stable_action_key(state.run_id, f"post_merge_{operation}", child_id, arguments)


def _run_parent_sync(
    state: RunState,
    current: SliceState,
    pr_number: int,
    evidence: Mapping[str, object],
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    store: RunStore,
) -> RunState:
    arguments, payload = _parent_sync_effect(
        state,
        current,
        pr_number,
        evidence,
        config,
        effects,
        effects_log,
        operation="post_merge_parent_sync",
        expected_base_sha=_required_current_base(current),
    )
    return _checkpoint_post_merge_event(
        store,
        state,
        current.id,
        ParentBranchSynced(current.id, arguments["parent_branch"], payload["parent_commit_sha"]),
        {
            "remote_head_sha": payload["remote_head_sha"],
            "ancestry_proof": payload["ancestry_proof"],
        },
    )


def _parent_sync_effect(
    state: RunState,
    current: SliceState,
    pr_number: int,
    evidence: Mapping[str, object],
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    *,
    operation: str,
    expected_base_sha: str,
) -> tuple[dict[str, object], Mapping[str, str]]:
    """Run and validate one authoritative parent-branch observation."""
    arguments = _parent_sync_arguments(
        current, pr_number, evidence, expected_base_sha, config.working_dir
    )
    result = _post_merge_effect(
        operation,
        current.id,
        arguments,
        effects,
        effects_log,
        "post_merge_parent_sync",
        active=config.active,
    )
    payload = _required_effect_result(
        result,
        (
            "child_id",
            "pr_number",
            "repository",
            "parent_branch",
            "merged_head_sha",
            "expected_base_sha",
            "lane_epoch",
            "parent_commit_sha",
            "remote_head_sha",
            "ancestry_proof",
        ),
    )
    _require_matching_text(
        payload,
        arguments,
        "child_id",
        "repository",
        "parent_branch",
        "merged_head_sha",
        "expected_base_sha",
    )
    if str(payload["pr_number"]) != str(arguments["pr_number"]):
        raise ValueError("parent synchronization receipt mismatch for pr_number")
    if str(payload["lane_epoch"]) != str(arguments["lane_epoch"]):
        raise ValueError("parent synchronization receipt mismatch for lane_epoch")
    if payload["parent_commit_sha"] != payload["remote_head_sha"]:
        raise ValueError("parent synchronization returned divergent local and remote heads")
    if payload["ancestry_proof"] != (
        f"ancestor:{arguments['merged_head_sha']}->{payload['parent_commit_sha']}"
    ):
        raise ValueError("parent synchronization returned unverifiable ancestry evidence")
    return arguments, payload


def _parent_sync_arguments(
    current: SliceState,
    pr_number: int,
    evidence: Mapping[str, object],
    expected_base_sha: str,
    working_dir: str | None,
) -> dict[str, object]:
    return {
        "child_id": current.id,
        "pr_number": pr_number,
        "repository": _required_merge_identity(evidence, ("repository",), "repository"),
        "parent_branch": _required_merge_identity(evidence, ("parent_branch",), "parent branch"),
        "merged_head_sha": _required_merge_identity(evidence, ("head_sha",), "merged head SHA"),
        "expected_base_sha": expected_base_sha,
        "lane_epoch": _required_int(evidence, "lane_epoch", "lane epoch"),
        "working_dir": working_dir,
    }


def _remote_reconcile_effect(
    current: SliceState,
    pr_number: int,
    evidence: Mapping[str, object],
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    *,
    expected_base_sha: str,
) -> tuple[dict[str, object], Mapping[str, str]]:
    """Rebase local bookkeeping onto a new parent base and validate its receipt."""
    arguments = _parent_sync_arguments(
        current, pr_number, evidence, expected_base_sha, config.working_dir
    )
    result = _post_merge_effect(
        "post_merge_remote_reconcile",
        current.id,
        arguments,
        effects,
        effects_log,
        "post_merge_remote_reconcile",
        active=config.active,
    )
    payload = _required_effect_result(
        result,
        (
            "child_id",
            "pr_number",
            "repository",
            "parent_branch",
            "merged_head_sha",
            "expected_base_sha",
            "lane_epoch",
            "parent_commit_sha",
            "rebuilt_commit_sha",
            "remote_head_sha",
            "new_base_sha",
            "remote_ancestry_proof",
            "ancestry_proof",
        ),
    )
    _require_matching_text(
        payload,
        arguments,
        "child_id",
        "repository",
        "parent_branch",
        "merged_head_sha",
        "expected_base_sha",
    )
    if str(payload["pr_number"]) != str(arguments["pr_number"]):
        raise ValueError("remote rebuild receipt mismatch for pr_number")
    if str(payload["lane_epoch"]) != str(arguments["lane_epoch"]):
        raise ValueError("remote rebuild receipt mismatch for lane_epoch")
    if payload["new_base_sha"] != payload["remote_head_sha"]:
        raise ValueError("remote rebuild receipt has divergent new base and remote head")
    if payload["parent_commit_sha"] != payload["rebuilt_commit_sha"]:
        raise ValueError("remote rebuild receipt has divergent rebuilt commit identities")
    if payload["remote_ancestry_proof"] != (
        f"ancestor:{payload['remote_head_sha']}->{payload['rebuilt_commit_sha']}"
    ):
        raise ValueError("remote rebuild receipt has unverifiable parent ancestry")
    if payload["ancestry_proof"] != (
        f"ancestor:{arguments['merged_head_sha']}->{payload['rebuilt_commit_sha']}"
    ):
        raise ValueError("remote rebuild receipt has unverifiable merge ancestry")
    return arguments, payload


def _run_issue_close(
    state: RunState,
    current: SliceState,
    evidence: Mapping[str, object],
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    store: RunStore,
) -> RunState:
    issue_id = _required_issue_id(config.chainlink_issue_id)
    intent_id = _required_merge_identity(evidence, ("issue_close_intent_id",), "issue-close intent")
    arguments = {"issue_id": int(issue_id), "force": True, "commit_changelog": False}
    result = _post_merge_effect(
        "post_merge_issue_close",
        current.id,
        {**arguments, "intent_id": intent_id},
        effects,
        effects_log,
        "chainlink_issue_close",
        active=config.active,
        call_arguments={
            "issue_id": int(issue_id),
            "force": True,
            "commit_changelog": False,
            "summary": f"Closed after successful merge of PR #{evidence['pr_number']}",
        },
    )
    payload = _required_effect_result(result, ("issue_id", "receipt_id"))
    if str(payload["issue_id"]) != issue_id:
        raise ValueError("issue-close receipt has a different issue ID")
    updated = _checkpoint_post_merge_event(
        store,
        state,
        current.id,
        IssueCloseConfirmed(current.id, issue_id, intent_id, payload["receipt_id"]),
    )
    return updated


def _run_changelog(
    state: RunState,
    current: SliceState,
    evidence: Mapping[str, object],
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    store: RunStore,
) -> RunState:
    issue_id = _required_issue_id(config.chainlink_issue_id)
    intent_id = _required_merge_identity(evidence, ("changelog_intent_id",), "changelog intent")
    generation = _required_int(
        evidence, "changelog_generation", "changelog generation", allow_zero=True
    )
    arguments = {
        "child_id": current.id,
        "issue_id": int(issue_id),
        "repository": _required_merge_identity(evidence, ("repository",), "repository"),
        "parent_branch": _required_merge_identity(evidence, ("parent_branch",), "parent branch"),
        "expected_base_sha": _required_current_base(current),
        "generation": generation,
        "intent_id": intent_id,
        "working_dir": config.working_dir,
    }
    result = _post_merge_effect(
        "post_merge_changelog",
        current.id,
        arguments,
        effects,
        effects_log,
        "post_merge_changelog",
        active=config.active,
    )
    payload = _required_effect_result(
        result,
        (
            "child_id",
            "issue_id",
            "repository",
            "parent_branch",
            "expected_base_sha",
            "generation",
            "intent_id",
            "commit_sha",
        ),
    )
    _require_matching_text(
        payload,
        arguments,
        "child_id",
        "repository",
        "parent_branch",
        "expected_base_sha",
        "intent_id",
    )
    if str(payload["issue_id"]) != str(arguments["issue_id"]):
        raise ValueError("changelog receipt mismatch for issue_id")
    if str(payload["generation"]) != str(arguments["generation"]):
        raise ValueError("changelog receipt mismatch for generation")
    updated = _checkpoint_post_merge_event(
        store,
        state,
        current.id,
        ChangelogCommitted(current.id, intent_id, payload["commit_sha"]),
    )
    return updated


def _run_parent_push(
    state: RunState,
    current: SliceState,
    evidence: Mapping[str, object],
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    store: RunStore,
) -> RunState:
    try:
        state, lane_ready = _ensure_bookkeeping_lane(state, current, evidence, store)
    except (TypeError, ValueError):
        return state
    if not lane_ready:
        return state
    current = state.slices[current.id]
    arguments = {
        "child_id": current.id,
        "repository": _required_merge_identity(evidence, ("repository",), "repository"),
        "parent_branch": _required_merge_identity(evidence, ("parent_branch",), "parent branch"),
        "lane_epoch": _required_int(evidence, "lane_epoch", "lane epoch"),
        "push_intent_id": _required_merge_identity(
            evidence, ("parent_push_intent_id",), "push intent"
        ),
        "push_journal_id": _required_merge_identity(evidence, ("push_journal_id",), "push journal"),
        "expected_base_sha": _required_merge_identity(
            evidence, ("expected_base_sha",), "push base SHA"
        ),
        "pushed_commit": _required_merge_identity(
            evidence, ("changelog_commit_sha",), "changelog commit"
        ),
        "working_dir": config.working_dir,
    }
    result = _post_merge_effect(
        "post_merge_push",
        current.id,
        arguments,
        effects,
        effects_log,
        "post_merge_push",
        active=config.active,
        retryable_failure=False,
    )
    if result is None or result.success is not True:
        return _recover_remote_advance(
            state,
            current,
            pr_number=int(evidence["pr_number"]),
            evidence=evidence,
            push_arguments=arguments,
            failed_result=result,
            config=config,
            effects=effects,
            effects_log=effects_log,
            store=store,
        )
    payload = _required_effect_result(
        result,
        (
            "repository",
            "parent_branch",
            "child_id",
            "lane_epoch",
            "push_intent_id",
            "push_journal_id",
            "push_receipt_id",
            "expected_base_sha",
            "pushed_commit",
            "observed_remote_head",
            "ancestry_proof",
        ),
    )
    _require_matching_text(payload, arguments, "repository", "parent_branch", "child_id")
    for key in (
        "lane_epoch",
        "push_intent_id",
        "push_journal_id",
        "expected_base_sha",
        "pushed_commit",
    ):
        if str(payload[key]) != str(arguments[key]):
            raise ValueError(f"parent-push receipt mismatch for {key}")
    if payload["observed_remote_head"] != payload["pushed_commit"]:
        raise ValueError("parent-push receipt observed a different remote head")
    if payload["ancestry_proof"] != (
        f"ancestor:{payload['pushed_commit']}->{payload['observed_remote_head']}"
    ):
        raise ValueError("parent-push receipt has unverifiable ancestry evidence")
    lane_epoch = payload["lane_epoch"]
    if isinstance(lane_epoch, str) and lane_epoch.isdigit():
        lane_epoch = int(lane_epoch)
    receipt = PushReceipt(
        repository=payload["repository"],
        parent_branch=payload["parent_branch"],
        child_id=payload["child_id"],
        lane_epoch=_positive_int(lane_epoch, "lane epoch"),
        push_intent_id=payload["push_intent_id"],
        push_journal_id=payload["push_journal_id"],
        push_receipt_id=payload["push_receipt_id"],
        expected_base_sha=payload["expected_base_sha"],
        pushed_commit=payload["pushed_commit"],
        observed_remote_head=payload["observed_remote_head"],
        ancestry_proof=payload["ancestry_proof"],
    )
    return _checkpoint_post_merge_event(
        store,
        state,
        current.id,
        PostMergeComplete(
            current.id,
            evidence["merge_journal_id"],
            arguments["push_intent_id"],
            arguments["pushed_commit"],
            receipt,
        ),
        lane_event=LaneReleased(current.id, receipt),
    )


def _recover_remote_advance(
    state: RunState,
    current: SliceState,
    pr_number: int,
    evidence: Mapping[str, object],
    push_arguments: Mapping[str, object],
    failed_result: ToolResult | None,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    store: RunStore,
) -> RunState:
    """Reconcile a stale parent push into a fresh bookkeeping generation."""
    failure = failed_result.error if failed_result is not None else None
    if not _is_remote_advance_failure(failure):
        raise ValueError(failure or "parent push returned no authoritative result")
    expected_base_sha = push_arguments.get("expected_base_sha")
    if not isinstance(expected_base_sha, str) or not expected_base_sha:
        raise ValueError("failed parent push has no expected base SHA")
    _, payload = _remote_reconcile_effect(
        current,
        pr_number,
        evidence,
        config,
        effects,
        effects_log,
        expected_base_sha=expected_base_sha,
    )
    observed_remote_head = payload["remote_head_sha"]
    if observed_remote_head == expected_base_sha:
        raise ValueError("parent push failed without authoritative remote advancement")
    generation = (
        _required_int(evidence, "changelog_generation", "changelog generation", allow_zero=True) + 1
    )
    failed_push_result = failure or "parent push failed"
    reason = f"parent push compare-and-swap rejected after remote advancement: {failed_push_result}"
    rebuilt = PostMergeRebuildRequested(
        current.id,
        generation,
        str(push_arguments["push_intent_id"]),
        str(push_arguments["push_journal_id"]),
        failed_push_result,
        observed_remote_head,
        observed_remote_head,
        payload["ancestry_proof"],
        reason,
    )
    rebuilt_slice = slice_transition(current, PostMergeEventObserved(rebuilt))
    rebuilt_intent = _post_merge_key(
        state,
        "changelog_rebuild",
        current.id,
        pr_number,
        generation=generation,
    )
    committed_slice = slice_transition(
        rebuilt_slice,
        PostMergeEventObserved(
            ChangelogCommitted(
                current.id,
                rebuilt_intent,
                payload["rebuilt_commit_sha"],
            )
        ),
    )
    repository, parent_branch = _candidate_lane_key(state, current)
    lane_key = f"{repository}:{parent_branch}"
    lane = state.integration.lanes.get(lane_key)
    prior_epoch = (
        lane.lane_epoch if lane is not None else _required_int(evidence, "lane_epoch", "lane epoch")
    )
    next_epoch = prior_epoch + 1
    if committed_slice.post_merge is None:
        raise ValueError("remote rebuild did not preserve post-merge state")
    rebuilt_evidence = dict(committed_slice.post_merge.evidence)
    rebuilt_evidence["lane_epoch"] = str(next_epoch)
    rebuilt_evidence["expected_base_sha"] = observed_remote_head
    committed_slice = replace(
        committed_slice,
        post_merge=replace(
            committed_slice.post_merge,
            evidence=rebuilt_evidence,
        ),
    )
    reconciliation = dict(committed_slice.reconciliation or {})
    reconciliation.update(
        {
            "remote_head_sha": observed_remote_head,
            "merge_base_sha": observed_remote_head,
        }
    )
    lane_for_rebuild = lane or LaneState(repository, parent_branch)
    lane_events: tuple[object, ...] = (
        (LaneRecoveryRequested(reason),) if lane is not None else ()
    ) + (
        LaneReserved(current.id, next_epoch, observed_remote_head),
        LaneIntegrationStarted(current.id, payload["rebuilt_commit_sha"]),
    )
    rebuilt_lane = lane_for_rebuild
    for lane_event in lane_events:
        rebuilt_lane = transition_lane(rebuilt_lane, lane_event)
    return _checkpoint_slice_action(
        store,
        state,
        current.id,
        None,
        slice_state=replace(committed_slice, reconciliation=reconciliation),
        integration=replace(
            state.integration,
            lanes={
                **state.integration.lanes,
                lane_key: rebuilt_lane,
            },
        ),
    )


def _is_remote_advance_failure(error: str | None) -> bool:
    """Recognize only server errors that identify a stale compare-and-swap base."""
    if not error:
        return False
    normalized = error.lower()
    return any(marker in normalized for marker in REMOTE_ADVANCE_FAILURE_MARKERS)


def _required_effect_result(
    result: ToolResult | None, fields: tuple[str, ...]
) -> Mapping[str, str]:
    payload = _merge_result_payload(result)
    if payload is None:
        raise ValueError("post-merge effect returned no authoritative success receipt")
    values: dict[str, str] = {}
    for field_name in fields:
        value = payload.get(field_name)
        if isinstance(value, str) and value:
            values[field_name] = value
        elif (
            field_name in {"lane_epoch", "pr_number", "issue_id", "generation"}
            and type(value) is int
        ):
            values[field_name] = str(value)
        else:
            raise ValueError(f"post-merge effect receipt is missing {field_name}")
    return values


def _require_matching_text(
    payload: Mapping[str, object], arguments: Mapping[str, object], *fields: str
) -> None:
    for field_name in fields:
        if payload.get(field_name) != arguments.get(field_name):
            raise ValueError(f"post-merge receipt mismatch for {field_name}")


def _required_current_base(current: SliceState) -> str:
    reconciliation = current.reconciliation
    value = None
    if isinstance(reconciliation, Mapping):
        for key in ("remote_head_sha", "merge_base_sha"):
            candidate = reconciliation.get(key)
            if isinstance(candidate, str) and candidate:
                value = candidate
                break
    if value is None:
        raise ValueError("merged PR base SHA is unavailable")
    return value


def _required_int(
    evidence: Mapping[str, object], key: str, name: str, *, allow_zero: bool = False
) -> int:
    value = evidence.get(key)
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} is unavailable")
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _block_post_merge_recovery(
    store: RunStore,
    state: RunState,
    slice_id: str,
    reason: str,
) -> RunState:
    bounded = reason[:500]
    blocked = replace(state.slices[slice_id], dispatch_error=bounded, action=None)
    blocked_state = _checkpoint_slice_action(
        store,
        state,
        slice_id,
        None,
        slice_state=blocked,
        integration=_abandon_owned_lane_integration(
            state, blocked, state.integration, "post_merge_gate", bounded
        ),
    )
    gate_name = f"tl-post-merge-{slice_id}"
    gate = next(
        (candidate for candidate in blocked_state.gates if candidate.name == gate_name), None
    )
    if gate is None:
        return store.set_gate(gate_name, GateStatus.PENDING)
    return blocked_state


def _execute_direct_merge_intent(
    state: RunState,
    intent: ExternalIntent,
    tracker: ConvergenceTracker,
    store: RunStore,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    """Compare, journal, merge, and re-read one direct PR before adoption."""
    current = state.slices[intent.target_id]
    if current.pr_number is None or not config.active:
        return state
    live = cast(EffectClient, effects)
    watcher = _invoke(
        "watcher_pr_state",
        current.id,
        {"pr_number": current.pr_number},
        True,
        live,
        lambda client: client.watcher_pr_state(pr_number=current.pr_number or 0),
        effects_log,
        raise_on_failure=False,
    )
    if (
        watcher is not None
        and watcher.success is True
        and (_watcher_result_observation(watcher) is not None)
        and _watcher_result_observation(watcher).merged is True
    ):
        observation = _watcher_result_observation(watcher)
        return _reconcile_merged_slice(
            store.load(),
            current.id,
            current.pr_number,
            current.action.intent_id if current.action is not None else "",
            config,
            effects,
            store,
            effects_log,
            boundary="direct_merge_adopted",
            merge_evidence=_watcher_merge_evidence(observation),
        )
    if (
        watcher is not None
        and watcher.success is True
        and isinstance(watcher.result, Mapping)
        and not _direct_compare_evidence_complete(watcher)
    ):
        reason = (
            "direct merge requires complete watcher evidence: "
            "base_sha, head_sha, patch_digest, merge_tree_sha, and ci_status"
        )
        previous_gate = next(
            (gate for gate in state.gates if gate.name == INTEGRITY_RECONCILIATION_GATE_NAME),
            None,
        )
        state = _clear_action_for_reduction(store, state, current.id)
        state = store.set_gate(INTEGRITY_RECONCILIATION_GATE_NAME, GateStatus.PENDING)
        if previous_gate is None or previous_gate.status is not GateStatus.PENDING:
            _record_controller_event(
                "controller",
                "tl.gate_opened",
                {
                    "gate_name": INTEGRITY_RECONCILIATION_GATE_NAME,
                    "run_id": state.run_id,
                    "reason": reason,
                },
                config,
                effects,
                effects_log,
            )
        return state
    freshness_window_secs = (
        load_freshness_window(config.review_policy_path)
        if config.review_policy_path is not None
        else None
    )
    if current.verdict is not None and verdict_is_stale(
        current,
        now=config.review_clock() if config.review_clock is not None else None,
        freshness_window_secs=freshness_window_secs,
    ):
        return _request_review_revalidation(
            state,
            current,
            store,
            reason="pre_merge_review_validation_expired",
        )
    evidence = _direct_merge_evidence(current, watcher, config)
    if evidence is None:
        return _clear_action_for_reduction(store, state, current.id)
    state, lane_ready = _ensure_candidate_lane(state, current, evidence["base_sha"], store)
    if not lane_ready:
        return state
    state, lane_ready = _start_candidate_lane(
        state, state.slices[current.id], evidence["head_sha"], store
    )
    if not lane_ready:
        return state
    current = state.slices[current.id]
    arguments = {
        "pr_number": current.pr_number,
        "expected_base_sha": evidence["base_sha"],
        "expected_head_sha": evidence["head_sha"],
        "expected_patch_digest": evidence["patch_digest"],
        "expected_merge_tree_sha": evidence["merge_tree_sha"],
    }
    if config.chainlink_issue_id is not None:
        arguments["chainlink_issue_id"] = config.chainlink_issue_id
    action = ActionState(
        ActionKind.MERGE,
        ActionPhase.IN_FLIGHT,
        intent_id=stable_action_key(state.run_id, "merge_pr", current.id, arguments),
        head_sha=cast(str, evidence["head_sha"]),
        attempt=max(1, current.attempts),
    )
    state = _checkpoint_slice_action(store, state, current.id, action)
    _record_convergence_event(
        current.id,
        tracker.action_started(state, intent),
        config,
        effects,
        effects_log,
    )
    try:
        merge_result = _invoke(
            "merge_pr",
            current.id,
            arguments,
            True,
            live,
            lambda client: client.merge_pr(
                pr_number=current.pr_number or 0,
                chainlink_issue_id=config.chainlink_issue_id,
                strategy=config.merge_strategy,
                working_dir=config.working_dir,
                expected_base_sha=cast(str, evidence["base_sha"]),
                expected_head_sha=cast(str, evidence["head_sha"]),
                expected_patch_digest=cast(str, evidence["patch_digest"]),
                expected_merge_tree_sha=cast(str, evidence["merge_tree_sha"]),
            ),
            effects_log,
            raise_on_failure=False,
        )
    except ToolUnavailableError:
        raise
    except (ConnectionError, OSError, RuntimeError, TimeoutError) as error:
        return _reconcile_unknown_merge(
            state,
            intent,
            action,
            arguments,
            error,
            tracker,
            store,
            config,
            effects,
            effects_log,
        )
    return _adopt_direct_merge_result(
        state,
        current.id,
        action,
        merge_result,
        store,
        config,
        effects,
        effects_log,
    )


def _resolve_nonmerged_merge(
    state: RunState,
    slice_id: str,
    store: RunStore,
    effects_log: list[EffectIntent],
    *,
    journal_key: str | None,
    diagnostic: str,
    terminal: bool = False,
) -> RunState:
    """Clear a disproved merge and release its durable integration lane."""
    refreshed = store.load()
    current = refreshed.slices[slice_id]
    cleared = slice_transition(current, ActionChanged(None))
    if terminal:
        cleared = slice_transition(cleared, SliceStatusChanged(SliceStatus.PARKED))
        cleared = replace(
            cleared,
            park_cause=ParkCause.HUMAN_DECISION_REQUIRED,
            dispatch_error=diagnostic[:500],
        )
    integration = _abandon_owned_lane_integration(
        refreshed,
        cleared,
        refreshed.integration,
        "merge_not_confirmed",
        diagnostic,
    )
    updated = _checkpoint_slice_action(
        store,
        refreshed,
        slice_id,
        None,
        slice_state=cleared,
        integration=integration,
    )
    if journal_key and isinstance(effects_log, EffectJournal):
        try:
            status = "rejected" if terminal else "compensated"
            effects_log.resolve_by_key(
                journal_key,
                status=status,
                result={
                    "success": False,
                    "result": {"merged": False, "reconciled": True},
                    "error": diagnostic,
                },
                error=diagnostic,
            )
        except ActionJournalError:
            LOGGER.info(
                "[TL loop] merge journal key=%s was already absent during non-merge recovery",
                journal_key,
            )
    return store.load() if updated != refreshed else updated


def _merge_recovery_gate_name(state: RunState, current: SliceState) -> str:
    """Return an occurrence-scoped gate for a journal-less merge attempt."""
    intent_id = (
        current.action.intent_id
        if current.action is not None and current.action.intent_id
        else "missing-intent"
    )
    attempt = (
        current.action.attempt
        if current.action is not None and current.action.attempt is not None
        else current.attempts
    )
    lane_epoch: int | None = None
    try:
        repository, parent_branch = _candidate_lane_key(state, current)
        lane = state.integration.lanes.get(f"{repository}:{parent_branch}")
        if lane is not None and lane.lane_epoch is not None:
            lane_epoch = lane.lane_epoch
    except ValueError:
        pass
    occurrence = {
        "attempt": attempt,
        "intent_id": intent_id,
        "lane_epoch": lane_epoch,
        "slice_id": current.id,
    }
    encoded = json.dumps(occurrence, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"{MERGE_RECOVERY_GATE_PREFIX}{digest}"


def _reconcile_merge_recovery_gate(
    state: RunState,
    current: SliceState,
    store: RunStore,
    effects_log: list[EffectIntent],
) -> tuple[RunState, bool]:
    """Resolve a journal-less unknown merge through an explicit operator gate."""
    gate_name = _merge_recovery_gate_name(state, current)
    gate = next((item for item in state.gates if item.name == gate_name), None)
    if gate is None:
        return store.set_gate(gate_name, GateStatus.PENDING), True
    if gate.status is GateStatus.PENDING:
        return state, True
    return (
        _resolve_nonmerged_merge(
            state,
            current.id,
            store,
            effects_log,
            journal_key=None,
            diagnostic=(
                "operator approved a fresh merge attempt"
                if gate.status is GateStatus.APPROVED
                else "operator abandoned journal-less unknown merge"
            ),
            terminal=gate.status is GateStatus.REJECTED,
        ),
        True,
    )


def _reconcile_unknown_merge(
    state: RunState,
    intent: ExternalIntent,
    action: ActionState,
    arguments: Mapping[str, object],
    error: BaseException,
    tracker: ConvergenceTracker,
    store: RunStore,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    """Probe Forgejo after a lost merge response before leaving an unknown intent."""
    current = state.slices[intent.target_id]
    watcher = _invoke(
        "watcher_pr_state",
        current.id,
        {"pr_number": current.pr_number or 0},
        config.active,
        cast(EffectClient, effects) if config.active else None,
        lambda client: client.watcher_pr_state(pr_number=current.pr_number or 0),
        effects_log,
        raise_on_failure=False,
    )
    observation = _watcher_result_observation(watcher)
    if (
        watcher is not None
        and watcher.success is True
        and observation is not None
        and observation.merged
    ):
        if isinstance(effects_log, EffectJournal):
            key = stable_action_key(state.run_id, "merge_pr", current.id, arguments)
            effects_log.resolve_by_key(
                key,
                status="confirmed",
                result={"success": True, "result": {"merged": True, "reconciled": True}},
            )
        refreshed = store.load()
        return _reconcile_merged_slice(
            refreshed,
            current.id,
            current.pr_number or 0,
            action.intent_id,
            config,
            effects,
            store,
            effects_log,
            boundary="direct_merge_reconciled",
            merge_evidence={
                **arguments,
                **_watcher_merge_evidence(observation),
            },
        )
    if (
        watcher is not None
        and watcher.success is True
        and observation is not None
        and observation.merged is False
        and observation.pr_number in {None, current.pr_number}
    ):
        diagnostic = "authoritative watcher state says the merge did not happen"
        key = stable_action_key(state.run_id, "merge_pr", current.id, arguments)
        updated = _resolve_nonmerged_merge(
            state,
            current.id,
            store,
            effects_log,
            journal_key=key,
            diagnostic=diagnostic,
        )
        _record_convergence_event(
            current.id,
            tracker.action_outcome(updated, intent, outcome="rejected", error=diagnostic),
            config,
            effects,
            effects_log,
        )
        return updated
    refreshed = store.load()
    unknown = replace(action, phase=ActionPhase.UNKNOWN)
    lane_integration = _recover_owned_lane_integration(
        refreshed,
        refreshed.slices[current.id],
        refreshed.integration,
        "direct merge outcome is unknown",
    )
    updated = _checkpoint_slice_action(
        store,
        refreshed,
        current.id,
        unknown,
        integration=lane_integration,
    )
    _record_convergence_event(
        current.id,
        tracker.action_outcome(updated, intent, outcome="unknown", error=str(error)),
        config,
        effects,
        effects_log,
    )
    return updated


def _direct_compare_evidence_complete(watcher: ToolResult) -> bool:
    observation = _watcher_result_observation(watcher)
    if observation is None:
        return False
    return all(
        isinstance(getattr(observation, key), str) and bool(getattr(observation, key))
        for key in ("base_sha", "head_sha", "patch_digest", "merge_tree_sha", "ci_status")
    )


def _watcher_merge_evidence(observation: object) -> dict[str, object]:
    """Copy only authoritative merge identity from a watcher observation."""
    if observation is None:
        return {}
    values: dict[str, object] = {}
    for name in ("head_sha", "base_sha", "base_branch"):
        value = getattr(observation, name, None)
        if value is not None:
            values[name] = value
    return values


def _direct_merge_evidence(
    current: SliceState,
    watcher: ToolResult | None,
    config: TLLoopConfig,
) -> dict[str, str] | None:
    """Validate the watcher snapshot used as the direct merge compare."""
    if watcher is None:
        return None
    observation = _watcher_result_observation(watcher)
    if observation is None:
        return None
    head_sha = observation.head_sha or ""
    evidence = {
        "base_sha": observation.base_sha,
        "head_sha": head_sha,
        "patch_digest": observation.patch_digest,
        "merge_tree_sha": observation.merge_tree_sha,
    }
    if any(not isinstance(value, str) or not value for value in evidence.values()):
        return None
    if observation.ci_status not in {"success", "neutral"}:
        return None
    try:
        freshness_window_secs = (
            load_freshness_window(config.review_policy_path)
            if config.review_policy_path is not None
            else None
        )
        verify_review(
            current,
            head_sha,
            now=config.review_clock() if config.review_clock is not None else None,
            freshness_window_secs=freshness_window_secs,
            current_patch_digest=evidence["patch_digest"],
        )
    except ReviewGateError:
        return None
    return cast(dict[str, str], evidence)


def _adopt_direct_merge_result(
    state: RunState,
    slice_id: str,
    action: ActionState,
    result: ToolResult | None,
    store: RunStore,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    """Adopt a merge only after a second authoritative merged snapshot."""
    current = state.slices[slice_id]
    if result is not None and result.success is False:
        cleared = replace(current, action=None)
        return _checkpoint_slice_action(
            store,
            state,
            slice_id,
            None,
            slice_state=cleared,
            integration=_abandon_owned_lane_integration(
                state, cleared, state.integration, "merge_failed", "direct merge failed"
            ),
        )
    watcher = _invoke(
        "watcher_pr_state",
        slice_id,
        {"pr_number": current.pr_number or 0},
        config.active,
        cast(EffectClient, effects) if config.active else None,
        lambda client: client.watcher_pr_state(pr_number=current.pr_number or 0),
        effects_log,
        raise_on_failure=False,
    )
    if (
        watcher is not None
        and watcher.success is True
        and (_watcher_result_observation(watcher) is not None)
        and _watcher_result_observation(watcher).merged is True
    ):
        observation = _watcher_result_observation(watcher)
        return _reconcile_merged_slice(
            store.load(),
            slice_id,
            current.pr_number,
            action.intent_id,
            config,
            effects,
            store,
            effects_log,
            boundary="direct_merge_confirmed",
            merge_evidence=_watcher_merge_evidence(observation),
        )
    return _checkpoint_slice_action(
        store,
        store.load(),
        slice_id,
        replace(action, phase=ActionPhase.CONFIRMED),
    )


def _checkpoint_slice_action(
    store: RunStore,
    state: RunState,
    slice_id: str,
    action: ActionState | None,
    *,
    slice_state: SliceState | None = None,
    integration: IntegrationRuntimeState | None = None,
) -> RunState:
    """Persist one direct action phase without changing unrelated run state."""
    current = slice_state or state.slices[slice_id]
    if slice_state is None:
        current = slice_transition(current, ActionChanged(action))
    checkpoint_phase = _recursive_phase_after_slice_update(state, slice_id, current)
    if checkpoint_phase is None:
        checkpoint_phase = state.fsm
        if (
            current.status is SliceStatus.MERGED
            and current.post_merge is not None
            and current.post_merge.phase is PostMergePhase.COMPLETE
        ):
            checkpoint_phase = _phase_after_slice_merge(state, slice_id, current)
    return store.checkpoint(
        checkpoint_phase,
        {**state.slices, slice_id: current},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=integration or state.integration,
    )


def _recursive_phase_after_slice_update(
    state: RunState,
    slice_id: str,
    current: SliceState,
) -> PhaseValue | None:
    """Project slice recovery into the canonical recursive scope FSM."""
    phase = state.recursive_fsm
    if not isinstance(phase, RecursiveTLRunning) or current.post_merge is None:
        return None
    if slice_id not in phase.post_merge:
        return None
    if current.post_merge.phase is not PostMergePhase.COMPLETE:
        post_merge = dict(phase.post_merge)
        post_merge[slice_id] = current.post_merge
        return replace(phase, post_merge=post_merge)
    # The terminal boundary is dispatched as a PostMergeComplete scope event
    # against the pre-update phase (not a pre-projected copy already showing
    # COMPLETE) so post_merge_transition's own not-yet-complete branch runs
    # and removes this child from parallel_pending/pending_by_order. Handing
    # it an already-COMPLETE phase makes it treat the call as a no-op repeat,
    # leaving the child parked in the FSM's pending set forever even once its
    # post-merge recovery has truly finished.
    evidence = current.post_merge.evidence
    try:
        receipt = PushReceipt(
            repository=evidence["repository"],
            parent_branch=evidence["parent_branch"],
            child_id=evidence["child_id"],
            lane_epoch=int(evidence["lane_epoch"]),
            push_intent_id=evidence["parent_push_intent_id"],
            push_journal_id=evidence["push_journal_id"],
            push_receipt_id=evidence["push_receipt_id"],
            expected_base_sha=evidence["expected_base_sha"],
            pushed_commit=evidence["pushed_commit"],
            observed_remote_head=evidence["observed_remote_head"],
            ancestry_proof=evidence["ancestry_proof"],
        )
        return scope_transition(
            phase,
            PostMergeComplete(
                child_id=slice_id,
                journal_id=evidence["merge_journal_id"],
                push_intent_id=evidence["parent_push_intent_id"],
                bookkeeping_commit=evidence["bookkeeping_commit"],
                receipt=receipt,
            ),
        )
    except (KeyError, TypeError, ValueError, IllegalTransition):
        post_merge = dict(phase.post_merge)
        post_merge[slice_id] = current.post_merge
        return replace(phase, post_merge=post_merge)


def _phase_after_slice_merge(
    state: RunState,
    slice_id: str,
    merged_slice: SliceState,
) -> PhaseValue:
    """Remove a merged slice from the waiting FSM in the same checkpoint."""
    phase = _phase_from_state(state)
    if isinstance(phase, (TLWaiting, TLMerging)):
        return phase_transition(
            phase,
            PRMerged(merged_slice.pr_number or 0, slice_id),
        )
    active = {
        current_id: ChildHandle(
            current_id,
            current.branch or "",
            current.agent_type or "unknown",
        )
        for current_id, current in state.slices.items()
        if current_id != slice_id
        and current.status in {SliceStatus.SPAWNED, SliceStatus.IN_REVIEW, SliceStatus.REPAIRING}
    }
    if active:
        return TLWaiting(active)
    if all(
        current.status is SliceStatus.MERGED
        and current.post_merge is not None
        and current.post_merge.phase is PostMergePhase.COMPLETE
        for current in state.slices.values()
    ):
        return TLAllMerged()
    return phase


def _clear_action_for_reduction(store: RunStore, state: RunState, slice_id: str) -> RunState:
    """Clear a stale direct intent so the next authoritative reduction can act."""
    return _checkpoint_slice_action(store, state, slice_id, None)


def _repair_reasons(current: SliceState) -> list[dict[str, object]]:
    """Convert persisted reviewer findings into the repair contract."""
    findings = current.review_findings.get(current.reviewed_head or "", ())
    return [
        {
            "severity": finding.get("severity", "blocking"),
            "file": finding.get("path", "review"),
            "line": 0,
            "claim": finding.get("rationale", "Reviewer requested changes"),
        }
        for finding in findings
    ] or [
        {
            "severity": "blocking",
            "file": "review",
            "line": 0,
            "claim": "Reviewer requested changes",
        }
    ]


def _record_convergence_event(
    target: str,
    event: object,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> None:
    """Emit a statically declared convergence event for contract checking."""
    event_type = getattr(event, "event_type", None)
    payload = getattr(event, "payload", None)
    if not isinstance(payload, Mapping):
        raise TLLoopError("convergence event payload must be an object")
    if event_type == "tl.action_queued":
        _record_controller_event(
            target,
            "tl.action_queued",
            {
                "run_id": payload.get("run_id"),
                "target_id": payload.get("target_id"),
                "state_version": payload.get("state_version"),
                "action": payload.get("action"),
                "action_key": payload.get("action_key"),
                "arguments": payload.get("arguments"),
            },
            config,
            effects,
            effects_log,
        )
    elif event_type == "tl.action_started":
        _record_controller_event(
            target,
            "tl.action_started",
            {
                "run_id": payload.get("run_id"),
                "target_id": payload.get("target_id"),
                "state_version": payload.get("state_version"),
                "action": payload.get("action"),
                "action_key": payload.get("action_key"),
            },
            config,
            effects,
            effects_log,
        )
    elif event_type == "tl.action_unknown":
        _record_controller_event(
            target,
            "tl.action_unknown",
            {
                "run_id": payload.get("run_id"),
                "target_id": payload.get("target_id"),
                "state_version": payload.get("state_version"),
                "action": payload.get("action"),
                "action_key": payload.get("action_key"),
                "outcome": payload.get("outcome"),
                "error": payload.get("error"),
            },
            config,
            effects,
            effects_log,
        )
    elif event_type == "tl.action_reconciled":
        _record_controller_event(
            target,
            "tl.action_reconciled",
            {
                "run_id": payload.get("run_id"),
                "target_id": payload.get("target_id"),
                "state_version": payload.get("state_version"),
                "action": payload.get("action"),
                "action_key": payload.get("action_key"),
                "outcome": payload.get("outcome"),
            },
            config,
            effects,
            effects_log,
        )
    elif event_type == "tl.wait_reason_changed":
        _record_controller_event(
            target,
            "tl.wait_reason_changed",
            {
                "run_id": payload.get("run_id"),
                "target_id": payload.get("target_id"),
                "state_version": payload.get("state_version"),
                "reason": payload.get("reason"),
                "previous_reason": payload.get("previous_reason"),
            },
            config,
            effects,
            effects_log,
        )
    elif event_type == "tl.transition_applied":
        _record_controller_event(
            target,
            "tl.transition_applied",
            {
                "run_id": payload.get("run_id"),
                "target_id": payload.get("target_id"),
                "state_version": payload.get("state_version"),
                "transition": payload.get("transition"),
                "reason": payload.get("reason"),
            },
            config,
            effects,
            effects_log,
        )
    elif event_type == "tl.transition_invariant_failed":
        _record_controller_event(
            target,
            "tl.transition_invariant_failed",
            {
                "run_id": payload.get("run_id"),
                "target_id": payload.get("target_id"),
                "state_version": payload.get("state_version"),
                "invariant": payload.get("invariant"),
                "action_key": payload.get("action_key"),
            },
            config,
            effects,
            effects_log,
        )
    else:
        raise TLLoopError(f"unknown convergence event {event_type!r}")


def _event_target(payload: Mapping[str, object]) -> str:
    target = payload.get("target_id", payload.get("run_id", "controller"))
    return target if isinstance(target, str) and target else "controller"


def _source_has_pending(source: EventQueue) -> bool:
    """Inspect only explicit in-memory source queues before honoring WAIT."""
    events = getattr(source, "events", None)
    if isinstance(events, (list, tuple, set, frozenset)):
        return bool(events)
    if events is not None and hasattr(events, "empty"):
        try:
            return not events.empty()
        except (AttributeError, TypeError):
            return True
    queued = getattr(source, "_events", None)
    if queued is not None:
        try:
            return not queued.empty()
        except (AttributeError, TypeError):
            return True
    nested = getattr(source, "queue", None)
    if nested is not None and nested is not source:
        return _source_has_pending(nested)
    return True


def _record_reader_findings(source: EventQueue, diagnostics: EventDiagnostics) -> None:
    """Promote reader-side filtering into durable controller diagnostics."""
    findings = getattr(source, "findings", ())
    if findings:
        diagnostics.record_reader_findings(tuple(findings))


def _record_dispatch_intent(
    slice_id: str,
    attempt: DispatchAttempt,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> None:
    _record_controller_event(
        slice_id,
        "tl.dispatch_intended",
        _dispatch_payload(slice_id, attempt, "dispatch_intended"),
        config,
        effects,
        effects_log,
    )


def _record_spawn_request(
    slice_id: str,
    attempt: DispatchAttempt,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> None:
    _record_controller_event(
        slice_id,
        "tl.spawn_requested",
        _dispatch_payload(slice_id, attempt, "spawn_requested"),
        config,
        effects,
        effects_log,
    )


def _record_dispatch_result(
    store: RunStore,
    state: RunState,
    slice_id: str,
    attempt: DispatchAttempt,
    result: ToolResult | None,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    if result is not None and result.success is False:
        return _record_dispatch_failure(
            store,
            state,
            slice_id,
            attempt,
            result.error or "spawn request rejected",
            config,
            effects,
            effects_log,
        )
    boundary = "spawn_request_accepted" if result is not None else "spawn_not_executed"
    current = state.slices[slice_id]
    updated = slice_transition(current, SliceStatusChanged(SliceStatus.DISPATCH_UNCONFIRMED))
    updated = replace(
        updated,
        park_cause=ParkCause.DISPATCH_UNCONFIRMED,
        dispatch_last_boundary=boundary,
        dispatch_agent_id=_spawn_agent_id(result),
        dispatch_invocation_id=_spawn_invocation_id(result),
        dispatch_error=None,
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, slice_id: updated},
        state.budgets,
        state.events.last_consumed_offset,
    )
    if result is not None:
        _record_controller_event(
            slice_id,
            "tl.spawn_request_accepted",
            _dispatch_payload(slice_id, attempt, boundary),
            config,
            effects,
            effects_log,
        )
    return state


def _record_dispatch_failure(
    store: RunStore,
    state: RunState,
    slice_id: str,
    attempt: DispatchAttempt,
    reason: str,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    bounded_reason = reason[:500]
    current = state.slices[slice_id]
    updated = slice_transition(current, SliceStatusChanged(SliceStatus.DISPATCH_FAILED))
    updated = replace(
        updated,
        park_cause=ParkCause.DISPATCH_FAILED,
        dispatch_last_boundary="spawn_request_failed",
        dispatch_error=bounded_reason,
    )
    before_phase = _phase_from_state(state)
    state = store.checkpoint(
        _failure_phase(state, f"dispatch failed for {slice_id!r}: {bounded_reason}"),
        {**state.slices, slice_id: updated},
        state.budgets,
        state.events.last_consumed_offset,
    )
    previous_gate = next(
        (gate for gate in state.gates if gate.name == DISPATCH_FAILURE_GATE_NAME),
        None,
    )
    state = store.set_gate(DISPATCH_FAILURE_GATE_NAME)
    if previous_gate is None or previous_gate.status is not GateStatus.PENDING:
        _record_controller_event(
            "controller",
            "tl.gate_opened",
            {"gate_name": DISPATCH_FAILURE_GATE_NAME, "run_id": state.run_id},
            config,
            effects,
            effects_log,
        )
    _record_controller_event(
        slice_id,
        "tl.spawn_request_failed",
        _dispatch_payload(slice_id, attempt, "spawn_request_failed", error=bounded_reason),
        config,
        effects,
        effects_log,
    )
    _emit_phase_change(
        state.run_id,
        before_phase,
        _phase_from_state(state),
        config,
        effects,
        effects_log,
    )
    return state


ACTION_JOURNAL_GATE_PREFIX = "tl-action-journal-"


def _action_journal_gate_name(key: str, attempt: int = 0) -> str:
    suffix = f"-{attempt}" if attempt else ""
    return f"{ACTION_JOURNAL_GATE_PREFIX}{key[:16]}{suffix}"


def _action_journal_compensation_attempt(entry: Mapping[str, object]) -> int:
    attempt = entry.get("compensation_attempt")
    return attempt if isinstance(attempt, int) and attempt >= 0 else 0


def _reconcile_action_journal(
    state: RunState,
    store: RunStore,
    effects_log: list[EffectIntent],
    *,
    effects: EffectClient | ReadOnlyEffectClient | None = None,
    project_root: str | Path | None = None,
    ledger_run_id: str | None = None,
) -> RunState:
    """Resolve action-journal entries stuck at intended/unknown so a restart
    does not crash permanently on the same blocked lifecycle effect.

    Whether the underlying side effect (spawn, PR file, merge, cleanup, ...)
    actually happened cannot be inferred generically here, so this never
    guesses. Each stuck entry instead opens (or reuses) a durable named gate:
    approving it records the entry as compensated, clearing the block so the
    next matching dispatch proceeds as a genuinely fresh attempt; rejecting
    it records a durable rejected outcome so callers observe a normal
    failure instead of a crash on the next retry.

    An approval only ever authorizes the one ambiguous occurrence the
    operator looked at. The gate name is scoped by a persisted per-key
    compensation_attempt counter that increments every time this key is
    resolved, so a *later* unknown/intended outcome for the same
    operation+target+arguments (e.g. the retried dispatch itself lands in
    another ambiguous state) opens a brand-new PENDING gate rather than
    silently matching the earlier APPROVED one. Without this, one approval
    would authorize the effect (spawn, merge, PR, or cleanup) to be retried
    indefinitely with no further human check, risking it running twice.

    This provides at-most-once dispatch until an operator reconciles an
    ambiguous outcome; it cannot autonomously prove exactly-once execution
    after a process crash between the external effect and its response.
    """
    if not isinstance(effects_log, EffectJournal):
        return state
    for entry in effects_log.pending_entries():
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            continue
        if effects is not None and _reconcile_pending_merge_entry(
            entry,
            state,
            store,
            effects_log,
            effects,
        ):
            state = store.load()
            continue
        if _controller_event_was_committed(
            entry,
            store,
            project_root=project_root,
            ledger_run_id=ledger_run_id,
        ):
            effects_log.resolve_by_key(
                key,
                status="confirmed",
                result={"success": True, "result": {"reconciled": True}},
            )
            continue
        attempt = _action_journal_compensation_attempt(entry)
        gate_name = _action_journal_gate_name(key, attempt)
        gate = next((candidate for candidate in state.gates if candidate.name == gate_name), None)
        if gate is None:
            state = store.set_gate(gate_name, GateStatus.PENDING)
            LOGGER.warning(
                "[TL loop] action journal entry key=%s operation=%s target=%s has "
                "unknown outcome; opened gate=%s for operator resolution",
                key,
                entry.get("operation"),
                entry.get("target"),
                gate_name,
            )
        elif gate.status is GateStatus.APPROVED:
            effects_log.resolve_by_key(key, status="compensated", compensation_attempt=attempt + 1)
        elif gate.status is GateStatus.REJECTED:
            effects_log.resolve_by_key(
                key,
                status="rejected",
                compensation_attempt=attempt + 1,
                result={
                    "success": False,
                    "error": f"operator rejected retry via gate {gate_name!r}",
                },
            )
    return state


def _reconcile_confirmed_repair_actions(
    state: RunState,
    store: RunStore,
    effects_log: list[EffectIntent],
) -> RunState:
    """Adopt a confirmed repair effect before retry derivation can run."""
    if not isinstance(effects_log, EffectJournal):
        return state
    for slice_id, current in state.slices.items():
        action = current.action
        if (
            action is None
            or action.kind is not ActionKind.REPAIR
            or action.phase not in {ActionPhase.INTENDED, ActionPhase.IN_FLIGHT}
        ):
            continue
        confirmed = _confirmed_repair_entry(effects_log, current)
        if confirmed is None:
            continue
        attempts = current.attempts
        repair_attempts = current.repair_attempts
        if attempts <= action.attempt:
            attempts = action.attempt + 1
            repair_attempts += 1
        updated = slice_transition(
            current,
            ActionChanged(replace(action, phase=ActionPhase.CONFIRMED)),
        )
        updated = replace(
            updated,
            attempts=attempts,
            repair_attempts=repair_attempts,
        )
        state = _checkpoint_slice_action(
            store,
            state,
            slice_id,
            updated.action,
            slice_state=updated,
        )
    return state


def _confirmed_repair_entry(
    effects_log: EffectJournal,
    current: SliceState,
) -> Mapping[str, object] | None:
    """Find successful resume evidence for the current PR, newest first."""
    if current.pr_number is None:
        return None
    entries = effects_log.confirmed_entries("resume_pr", current.id)
    if len(entries) < max(1, current.action.attempt if current.action else 1):
        return None
    for entry in reversed(entries):
        arguments = entry.get("arguments")
        if not isinstance(arguments, Mapping) or arguments.get("pr_number") != current.pr_number:
            continue
        try:
            result = effects_log.replay(entry)
        except ActionJournalError:
            continue
        if result.success is True:
            return entry
    return None


def _reconcile_pending_merge_entry(
    entry: Mapping[str, object],
    state: RunState,
    store: RunStore,
    effects_log: EffectJournal,
    effects: EffectClient | ReadOnlyEffectClient,
) -> bool:
    """Adopt a merge that completed before restart but lost its response."""
    if entry.get("operation") != "merge_pr":
        return False
    arguments = entry.get("arguments")
    if not isinstance(arguments, Mapping):
        return False
    pr_number = arguments.get("pr_number")
    target = entry.get("target")
    if type(pr_number) is not int or not isinstance(target, str) or target not in state.slices:
        return False
    key = entry.get("key")
    if not isinstance(key, str) or not key:
        return False
    try:
        watcher = effects.watcher_pr_state(pr_number=pr_number)
    except (ConnectionError, OSError, RuntimeError, TimeoutError):
        return False
    observation = _watcher_result_observation(watcher)
    if watcher.success is True and observation is not None and observation.merged is False:
        _resolve_nonmerged_merge(
            state,
            target,
            store,
            effects_log,
            journal_key=key,
            diagnostic="authoritative watcher state says the merge did not happen",
        )
        return True
    if watcher.success is not True or observation is None or observation.merged is not True:
        return False
    chainlink_issue_id = arguments.get("chainlink_issue_id")
    if chainlink_issue_id is not None and type(chainlink_issue_id) is not int:
        return False
    effects_log.resolve_by_key(
        key,
        status="confirmed",
        result={"success": True, "result": {"merged": True, "reconciled": True}},
    )
    _reconcile_merged_slice(
        state,
        target,
        pr_number,
        key,
        TLLoopConfig(active=True, chainlink_issue_id=chainlink_issue_id),
        effects,
        store,
        effects_log,
        boundary="restart_merge_reconciled",
        merge_evidence={
            **arguments,
            **_watcher_merge_evidence(_watcher_result_observation(watcher)),
        },
    )
    return True


def _controller_event_was_committed(
    entry: Mapping[str, object],
    store: RunStore,
    *,
    project_root: str | Path | None,
    ledger_run_id: str | None,
) -> bool:
    """Use the authoritative ledger to resolve an interrupted event emission."""
    if project_root is None or ledger_run_id is None:
        return False
    if entry.get("operation") != "emit_controller_event":
        return False
    arguments = entry.get("arguments")
    if not isinstance(arguments, Mapping):
        return False
    event_type = arguments.get("event_type")
    payload = arguments.get("payload")
    if not isinstance(event_type, str) or not isinstance(payload, Mapping):
        return False
    try:
        reader = LedgerReader(
            Path(project_root) / ".exo" / "ledger" / "segments",
            run_dir=store.run_dir,
            ledger_run_id=ledger_run_id,
        )
        return reader.has_record(event_type, payload)
    except (LedgerReadError, OSError, ValueError):
        return False


def _reconcile_dispatches(
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
) -> RunState:
    """Adopt persisted dispatches before any restart can issue a duplicate."""
    pending = [
        slice_state
        for slice_state in state.slices.values()
        if slice_state.status in DISPATCHING_STATUSES
    ]
    if not pending:
        return state
    agent_listing: ToolResult | None = None
    if config.active:
        live = cast(EffectClient, effects)
        agent_listing = _invoke(
            "list_agents",
            "controller",
            {},
            True,
            live,
            lambda client: client.list_agents(),
            effects_log,
            raise_on_failure=False,
        )
    for pending_slice in pending:
        current = state.slices.get(pending_slice.id, pending_slice)
        if current.dispatch_intent_id is None or current.dispatch_started_at is None:
            continue
        attempt = DispatchAttempt(
            current.dispatch_intent_id,
            current.dispatch_started_at,
            current.agent_type or "",
            attempt=max(1, current.attempts),
            controller_epoch=state.controller_epoch,
            dispatch_generation=current.dispatch_generation,
        )
        _record_controller_event(
            current.id,
            "tl.dispatch_reconciliation_started",
            _dispatch_payload(current.id, attempt, "reconciliation_started"),
            config,
            effects,
            effects_log,
        )
        owner_id = _agent_for_dispatch_intent(agent_listing, attempt.intent_id)
        if owner_id is not None:
            _record_controller_event(
                current.id,
                "tl.dispatch_reconciliation_completed",
                _dispatch_payload(current.id, attempt, "owner_found"),
                config,
                effects,
                effects_log,
            )
            confirmation = _record_controller_event(
                current.id,
                "tl.dispatch_confirmed",
                _dispatch_payload(current.id, attempt, "owner_adopted"),
                config,
                effects,
                effects_log,
            )
            authoritative_seq = _controller_event_run_seq(confirmation)
            if authoritative_seq is not None:
                adopted_slices = {
                    **state.slices,
                    current.id: replace(
                        slice_transition(current, SliceStatusChanged(SliceStatus.SPAWNED)),
                        park_cause=None,
                        dispatch_last_boundary="owner_adopted",
                        dispatch_error=None,
                        dispatch_agent_id=owner_id,
                        dispatch_authoritative_event_seq=authoritative_seq,
                    ),
                }
                state = store.checkpoint(
                    _dispatch_waiting_phase(adopted_slices),
                    adopted_slices,
                    state.budgets,
                    state.events.last_consumed_offset,
                )
                continue
        if current.status is SliceStatus.DISPATCHING:
            state = store.checkpoint(
                state.fsm,
                {
                    **state.slices,
                    current.id: replace(
                        slice_transition(
                            current, SliceStatusChanged(SliceStatus.DISPATCH_UNCONFIRMED)
                        ),
                        park_cause=ParkCause.DISPATCH_UNCONFIRMED,
                        dispatch_last_boundary="reconciliation_started",
                    ),
                },
                state.budgets,
                state.events.last_consumed_offset,
            )
        _record_controller_event(
            current.id,
            "tl.dispatch_reconciliation_completed",
            _dispatch_payload(current.id, attempt, "awaiting_authoritative_event"),
            config,
            effects,
            effects_log,
        )
    return state


def _reconcile_nonterminal_slices(
    plan: WorkPlan,
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
) -> RunState:
    """Rebuild safe derived fields before the cursor advances."""
    candidates = [
        slice_state
        for slice_state in state.slices.values()
        if slice_state.status
        in {
            SliceStatus.SPAWNED,
            SliceStatus.IN_REVIEW,
            SliceStatus.REPAIRING,
        }
    ]
    if not candidates:
        return state

    agent_listing: ToolResult | None = None
    if config.active:
        agent_listing = _invoke(
            "list_agents",
            "controller-reconciliation",
            {},
            True,
            cast(EffectClient, effects),
            lambda client: client.list_agents(),
            effects_log,
            raise_on_failure=False,
        )
    snapshots: dict[int, WatcherObservation | None] = {}
    updated = dict(state.slices)
    conflicts_found = False
    changed = False
    for current in candidates:
        watcher = None
        if current.pr_number is not None and config.ledger_run_id is not None:
            if current.pr_number not in snapshots:
                snapshots[current.pr_number] = _watcher_snapshot(
                    current.pr_number,
                    config,
                    effects,
                    effects_log,
                )
            watcher = snapshots[current.pr_number]
        elif config.ledger_run_id is not None:
            # pr_number was never persisted (e.g. a crash between pr.filed
            # being acknowledged and identity association). Recover it from
            # the durable publication registry keyed by slice_id.
            watcher = _watcher_snapshot_for_slice(
                current.id,
                config,
                effects,
                effects_log,
            )
            recovered_pr_number = watcher.get("pr_number") if watcher else None
            if isinstance(recovered_pr_number, int) and recovered_pr_number > 0:
                snapshots[recovered_pr_number] = watcher
        if (
            current.action is not None
            and current.action.kind is ActionKind.MERGE
            and current.action.phase is ActionPhase.UNKNOWN
        ):
            if watcher is not None and watcher.merged is True:
                merge_evidence = _watcher_merge_evidence(watcher)
                if (
                    current.action.intent_id
                    and isinstance(merge_evidence.get("head_sha"), str)
                    and isinstance(merge_evidence.get("base_sha"), str)
                    and watcher.pr_number in {None, current.pr_number}
                ):
                    state = _reconcile_merged_slice(
                        store.load(),
                        current.id,
                        watcher.pr_number or current.pr_number or 0,
                        current.action.intent_id,
                        config,
                        effects,
                        store,
                        effects_log,
                        boundary="legacy_unknown_merge_reconciled",
                        merge_evidence=merge_evidence,
                    )
                    updated = dict(state.slices)
                    changed = False
                    continue
            if (
                watcher is not None
                and watcher.merged is False
                and watcher.pr_number in {None, current.pr_number}
            ):
                state = _resolve_nonmerged_merge(
                    state,
                    current.id,
                    store,
                    effects_log,
                    journal_key=None,
                    diagnostic="authoritative watcher state says the merge did not happen",
                )
                updated = dict(state.slices)
                changed = False
                continue
            state, handled = _reconcile_merge_recovery_gate(
                state,
                current,
                store,
                effects_log,
            )
            if handled:
                updated = dict(state.slices)
                changed = False
                continue
        if watcher is not None and watcher.merged is True:
            merge_entry = _confirmed_merge_entry(
                effects_log,
                current,
                watcher.pr_number,
            )
            if merge_entry is not None:
                state = _adopt_authoritative_merged_snapshot(
                    state,
                    current.id,
                    watcher.pr_number or current.pr_number or 0,
                    merge_entry,
                    config,
                    effects,
                    store,
                    effects_log,
                    merge_evidence=_watcher_merge_evidence(watcher),
                )
                updated = dict(state.slices)
                changed = False
                continue
        owner_id = _agent_for_dispatch_intent(
            agent_listing,
            current.dispatch_intent_id or "",
        )
        result = reconcile_slice(
            current,
            authoritative_owner_id=owner_id,
            watcher=watcher,
        )
        reconciled = _apply_reconciliation_observations(
            current,
            result,
            watcher,
            owner_id,
        )
        if watcher is not None:
            replay_state = replace(
                state,
                slices={**state.slices, **updated, current.id: reconciled},
            )
            replayed = _replay_watcher_review_if_needed(
                plan,
                replay_state,
                reconciled,
                watcher,
                config,
                effects,
                store,
                effects_log,
            )
            if replayed != state:
                state = replayed
                reconciled = state.slices[current.id]
        if watcher is not None:
            reconciled = reconcile_merge_observation(reconciled, watcher)
        if current.handoff is None and watcher is not None:
            handoff_payload = _handoff_reconciliation_event_payload(
                current,
                reconciled,
                watcher,
                owner_id,
            )
            if handoff_payload is not None:
                _record_controller_event(
                    current.id,
                    "tl.handoff_reconciled",
                    handoff_payload,
                    config,
                    effects,
                    effects_log,
                )
        if result.next_action in {
            "park_closed_unmerged_pr",
            "park_unreachable_pr_head",
            "park_publication_ownership_unresolved",
        }:
            if isinstance(effects, ReadOnlyEffectClient):
                raise TLLoopError(
                    f"reconciliation for {current.id!r} requires an active effect client to park"
                )
            cause = {
                "park_closed_unmerged_pr": ParkCause.PR_CLOSED_UNMERGED,
                "park_unreachable_pr_head": ParkCause.PR_HEAD_UNREACHABLE,
                "park_publication_ownership_unresolved": (
                    ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED
                ),
            }[result.next_action]
            park_audit = {
                "reconciliation": result.as_state(),
                "pr_number": current.pr_number or (watcher.pr_number if watcher else None),
                "head_sha": _snapshot_text(watcher, "head_sha") if watcher else None,
                "branch": _snapshot_text(watcher, "head_branch") if watcher else current.branch,
                "observed_at": _now_timestamp(),
                "observation_error": _snapshot_text(watcher, "evidence_error") if watcher else None,
                "publication_ownership_error": (
                    (
                        watcher.publication_ownership_error
                        or _publication_ownership_status(watcher)[1]
                    )
                    if watcher
                    else None
                ),
            }
            if reconciled != current:
                state = store.checkpoint(
                    state.fsm,
                    {**state.slices, current.id: reconciled},
                    state.budgets,
                    state.events.last_consumed_offset,
                    current_order=state.current_order,
                    ordered_stages=state.ordered_stages,
                    integration=state.integration,
                )
            park(
                state.slices[current.id],
                cause,
                store=store,
                issue_creator=effects,
                ledger=state.budgets,
                audit=park_audit,
            )
            state = store.load()
            parked_slice = replace(state.slices[current.id], reconciliation=result.as_state())
            state = store.checkpoint(
                state.fsm,
                {**state.slices, current.id: parked_slice},
                state.budgets,
                state.events.last_consumed_offset,
                current_order=state.current_order,
                ordered_stages=state.ordered_stages,
                integration=state.integration,
            )
            updated = dict(state.slices)
            changed = False
            continue
        if reconciled != current:
            updated[current.id] = reconciled
            changed = True
        conflicts_found |= bool(result.conflicts)

    if changed:
        checkpoint_phase = _reconciliation_phase(state, updated)
        state = store.checkpoint(
            checkpoint_phase,
            updated,
            state.budgets,
            state.events.last_consumed_offset,
            current_order=state.current_order,
            ordered_stages=state.ordered_stages,
            integration=state.integration,
        )
    if conflicts_found:
        state = store.set_gate(INTEGRITY_RECONCILIATION_GATE_NAME, GateStatus.PENDING)
    return state


def _confirmed_merge_entry(
    effects_log: list[EffectIntent],
    current: SliceState,
    observed_pr_number: int | None,
) -> Mapping[str, object] | None:
    """Find durable merge evidence without reconstructing its arguments."""
    if not isinstance(effects_log, EffectJournal):
        return None
    for entry in effects_log.confirmed_entries("merge_pr", current.id):
        arguments = entry.get("arguments")
        if not isinstance(arguments, Mapping):
            continue
        journal_pr_number = arguments.get("pr_number")
        if type(journal_pr_number) is not int:
            continue
        if observed_pr_number is not None and journal_pr_number != observed_pr_number:
            continue
        if current.pr_number is not None and journal_pr_number != current.pr_number:
            continue
        result = entry.get("result")
        if isinstance(result, Mapping):
            nested = result.get("result")
            if isinstance(nested, Mapping) and nested.get("merged") is False:
                continue
        return entry
    return None


def _adopt_authoritative_merged_snapshot(
    state: RunState,
    slice_id: str,
    pr_number: int,
    merge_entry: Mapping[str, object],
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
    merge_evidence: Mapping[str, object] | None = None,
) -> RunState:
    """Adopt a journal-confirmed merge before review/ownership recovery."""
    arguments = merge_entry.get("arguments")
    issue_id = config.chainlink_issue_id
    if isinstance(arguments, Mapping) and type(arguments.get("chainlink_issue_id")) is int:
        issue_id = cast(int, arguments["chainlink_issue_id"])
    merge_key = merge_entry.get("key")
    merge_journal_id = merge_key if isinstance(merge_key, str) else ""
    authoritative_evidence = dict(arguments) if isinstance(arguments, Mapping) else {}
    if merge_evidence is not None:
        authoritative_evidence.update(merge_evidence)
    return _reconcile_merged_slice(
        state,
        slice_id,
        pr_number,
        merge_journal_id,
        replace(config, chainlink_issue_id=issue_id),
        effects,
        store,
        effects_log,
        boundary="authoritative_merge_adopted",
        merge_evidence=authoritative_evidence,
    )


def _reconciliation_phase(
    state: RunState,
    slices: Mapping[str, SliceState],
) -> PhaseValue:
    phase = _phase_from_state(state)
    if not isinstance(phase, TLWaiting):
        return phase
    handles = {
        slice_id: ChildHandle(
            slice_id,
            slice_state.branch or "",
            slice_state.agent_type or "unknown",
        )
        for slice_id, slice_state in slices.items()
        if slice_state.status
        in {
            SliceStatus.SPAWNED,
            SliceStatus.IN_REVIEW,
            SliceStatus.REPAIRING,
            SliceStatus.MERGED,
        }
        and (
            slice_state.status is not SliceStatus.MERGED
            or slice_state.post_merge is None
            or slice_state.post_merge.phase is not PostMergePhase.COMPLETE
        )
    }
    if handles:
        return TLWaiting(handles)
    return (
        TLAllMerged()
        if all(item.status is SliceStatus.MERGED for item in slices.values())
        else TLPlanning()
    )


def _replay_watcher_review_if_needed(
    plan: WorkPlan,
    state: RunState,
    current: SliceState,
    watcher: WatcherObservation | Mapping[str, object],
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
) -> RunState:
    """Revalidate one exact-head review snapshot without creating a new round."""
    observed_watcher = _as_watcher_observation(watcher)
    if observed_watcher is None:
        return state
    watcher = observed_watcher
    head_sha = watcher.head_sha
    review_head_sha = watcher.review_head_sha
    reviewer_agent_id = watcher.reviewer_agent_id
    ownership_verified, _ = watcher.ownership_status()
    freshness_window_secs = (
        load_freshness_window(config.review_policy_path)
        if config.review_policy_path is not None
        else None
    )
    stale_verdict = current.verdict is not None and verdict_is_stale(
        current,
        now=config.review_clock() if config.review_clock is not None else None,
        freshness_window_secs=freshness_window_secs,
    )
    if (
        current.status not in {SliceStatus.SPAWNED, SliceStatus.IN_REVIEW, SliceStatus.REPAIRING}
        or watcher.found is not True
        or not head_sha
        or not review_head_sha
        or review_head_sha != head_sha
        or current.reviewed_head not in {None, head_sha}
        or watcher.review_id is None
        or not reviewer_agent_id
        or watcher.reviewer_identity_error
        or not ownership_verified
        or current.handoff is None
        or current.handoff.head_sha != head_sha
        or current.dispatch_agent_id == reviewer_agent_id
        or (current.pr_number is not None and watcher.pr_number != current.pr_number)
        or watcher.review_verdict not in {"approved", "changes_requested"}
    ):
        if stale_verdict and not current.review_validation_required:
            return _persist_review_validation_failure(
                state,
                current,
                store,
                disposition=ReviewValidationDisposition.INVALIDATED,
                reason="authoritative_review_observation_incomplete",
            )
        return state
    publication = watcher.publication
    if publication is not None:
        if publication.slice_id not in {None, current.id}:
            return state
        if current.dispatch_agent_id is not None and publication.author_agent not in {
            None,
            current.dispatch_agent_id,
        }:
            return state
    kind = "approved" if watcher.review_verdict == "approved" else "review_received"
    review_rationale = (watcher.review_body or "").strip()
    if not review_rationale:
        review_rationale = (
            f"Forgejo review {watcher.review_id} requested changes on exact head {head_sha}"
        )
    findings = (
        []
        if kind == "approved"
        else [
            {
                "severity": "blocking",
                "path": "review",
                "rationale": review_rationale,
            }
        ]
    )
    observed_at = _now_timestamp()
    pr_number = watcher.pr_number or current.pr_number
    if pr_number is None:
        return _request_review_revalidation(
            state,
            current,
            store,
            reason="authoritative_review_pr_missing",
        )
    observation = ReviewValidationObservation(
        review_id=watcher.review_id,
        pr_number=pr_number,
        head_sha=head_sha,
        reviewer_agent_id=reviewer_agent_id,
        verdict=Verdict.GO if kind == "approved" else Verdict.NO_GO,
        observed_at=observed_at,
        submitted_at=(
            current.review_evidence.submitted_at
            if current.review_evidence is not None
            and current.review_evidence.review_id == watcher.review_id
            else watcher.review_submitted_at
        ),
    )
    disposition = review_validation_disposition(
        current.review_evidence,
        observation,
        now=config.review_clock() if config.review_clock is not None else None,
        freshness_window_secs=freshness_window_secs,
    )
    if disposition is ReviewValidationDisposition.OUT_OF_ORDER:
        LOGGER.info(
            "[TL loop] stale watcher review ignored target=%s review_id=%s current_review_id=%s",
            current.id,
            observation.review_id,
            current.review_evidence.review_id if current.review_evidence is not None else None,
        )
        return state
    if current.verdict is not None and disposition is ReviewValidationDisposition.ALREADY_FRESH:
        if current.review_validation_required:
            return _persist_review_validation(
                state,
                current,
                observation,
                store,
                submitted_at=observation.submitted_at,
            )
        LOGGER.info(
            "[TL loop] review validation ignored target=%s disposition=%s state_version=%s",
            current.id,
            disposition.value,
            state.state_version,
        )
        return state
    if (
        current.verdict is not None
        and disposition is ReviewValidationDisposition.REFRESHED
        and current.reviewed_head == head_sha
        and current.verdict == observation.verdict
    ):
        return _persist_review_validation(
            state,
            current,
            observation,
            store,
            submitted_at=observation.submitted_at,
        )
    if current.verdict is not None and disposition in {
        ReviewValidationDisposition.INVALIDATED,
        ReviewValidationDisposition.UNAUTHORIZED,
    }:
        return _persist_review_validation_failure(
            state,
            current,
            store,
            disposition=disposition,
            reason=f"review_validation_{disposition.value}",
        )
    event = project(
        {
            "event_type": "pr.review",
            "run_id": state.run_id,
            "agent_id": reviewer_agent_id,
            "slice_id": current.id,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "role": "reviewer",
            "lifecycle_state": "running",
            "observed_at": observed_at,
            "data": {
                "kind": kind,
                "slice_id": current.id,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "review_head_sha": review_head_sha,
                "review_id": watcher.review_id,
                "review_submitted_at": observation.submitted_at or observed_at,
                "submitted_at": observation.submitted_at or observed_at,
                "validated_at": observed_at,
                "reviewer_agent_id": reviewer_agent_id,
                "reviewer_account_authenticated": True,
                "reviewer_identity_unresolved": False,
                "verdict": "GO" if kind == "approved" else "NO-GO",
                "findings": findings,
                "review_state": watcher.review_verdict,
                "ci_status": watcher.ci_status,
                "source": "watcher_snapshot",
            },
        }
    )
    replayed = _route_review_event(
        plan,
        store,
        state,
        _phase_from_state(state),
        event,
        state.events.last_consumed_offset,
        config,
        effects,
        effects_log,
    )
    updated = replayed.slices[current.id]
    if updated.verdict is None:
        LOGGER.warning(
            "[TL loop] rejected exact-head watcher review replay target=%s review_id=%s reviewer=%s",
            current.id,
            watcher.review_id,
            reviewer_agent_id,
        )
        return state
    _record_controller_event(
        current.id,
        "tl.review_reconciled",
        {
            "slice_id": current.id,
            "pr_number": watcher.pr_number or current.pr_number,
            "head_sha": head_sha,
            "review_id": watcher.review_id,
            "verdict": watcher.review_verdict,
            "reviewer_agent_id": reviewer_agent_id,
            "source": "authoritative_watcher_snapshot",
        },
        config,
        effects,
        effects_log,
    )
    return replayed


def _persist_review_validation(
    state: RunState,
    current: SliceState,
    observation: ReviewValidationObservation,
    store: RunStore,
    *,
    submitted_at: str | None,
) -> RunState:
    """Persist one successful revalidation and its single version advance."""
    requested = slice_transition(current, RevalidateReview())
    validated = slice_transition(
        requested,
        ReviewValidated(observation, submitted_at=submitted_at),
    )
    if validated == current:
        return state
    refreshed = store.checkpoint(
        state.fsm,
        {**state.slices, current.id: validated},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        state_version=(
            state.state_version
            if (
                current.review_validation_required
                and current.review_evidence is not None
                and current.review_validation_disposition is None
            )
            else state.state_version + 1
        ),
    )
    LOGGER.info(
        "[TL loop] review validation refreshed target=%s disposition=refreshed state_version=%s->%s",
        current.id,
        state.state_version,
        refreshed.state_version,
    )
    return refreshed


def _persist_review_validation_failure(
    state: RunState,
    current: SliceState,
    store: RunStore,
    *,
    disposition: ReviewValidationDisposition,
    reason: str,
) -> RunState:
    """Persist a named validation failure and keep the verdict quiescent."""
    if not reason:
        raise ValueError("review validation failure reason must be non-empty")
    failed = slice_transition(
        current,
        ReviewValidationFailed(disposition=disposition, reason=reason),
    )
    if failed == current:
        return state
    failed_state = store.checkpoint(
        state.fsm,
        {**state.slices, current.id: failed},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        state_version=state.state_version + 1,
    )
    LOGGER.warning(
        "[TL loop] review validation failed target=%s disposition=%s reason=%s state_version=%s->%s",
        current.id,
        disposition.value,
        reason,
        state.state_version,
        failed_state.state_version,
    )
    return failed_state


def _request_review_revalidation(
    state: RunState,
    current: SliceState,
    store: RunStore,
    *,
    reason: str,
) -> RunState:
    """Durably quiesce a stale verdict until a fresh authoritative observation arrives."""
    if not reason:
        raise ValueError("review revalidation reason must be non-empty")
    requested = slice_transition(current, RevalidateReview())
    if requested == current:
        return state
    requested_state = store.checkpoint(
        state.fsm,
        {**state.slices, current.id: requested},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        state_version=state.state_version + 1,
    )
    LOGGER.info(
        "[TL loop] review validation requested target=%s reason=%s state_version=%s->%s",
        current.id,
        reason,
        state.state_version,
        requested_state.state_version,
    )
    return requested_state


def _apply_reconciliation_observations(
    current: SliceState,
    result: ReconciliationResult,
    watcher: WatcherObservation | Mapping[str, object] | None,
    owner_id: str | None,
) -> SliceState:
    watcher = _as_watcher_observation(watcher)
    updates: dict[str, object] = {"reconciliation": result.as_state()}
    transitioned = current
    if result.conflicts:
        return replace(current, **updates)

    if watcher is not None and watcher.found is True:
        if current.pr_number is None:
            recovered_pr_number = watcher.pr_number
            if isinstance(recovered_pr_number, int) and recovered_pr_number > 0:
                updates["pr_number"] = recovered_pr_number
        head_sha = watcher.head_sha
        ci_status = watcher.ci_status
        if head_sha and ci_status in CI_STATUS_VALUES:
            transitioned = slice_transition(
                transitioned,
                HeadEvidenceObserved(
                    head_sha=head_sha,
                    ci_status=ci_status,
                    bind_reviewed_head=transitioned.reviewed_head in {None, head_sha},
                ),
            )
        publication = _publication_from_watcher(current, watcher, head_sha, owner_id)
        if publication is not None:
            updates["publication"] = publication
            updates["pr_number"] = publication.pr_number
        published_pr_number = (
            publication.pr_number if publication is not None else current.pr_number
        )
        effective_agent_id = current.dispatch_agent_id or owner_id
        invocation_id = current.dispatch_invocation_id
        if invocation_id is None and publication is not None:
            invocation_id = publication.invocation_id
        if (
            watcher.publication_ownership_verified is True
            and head_sha
            and published_pr_number is not None
            and effective_agent_id
            and invocation_id
            and (
                current.publication is None
                or (
                    current.publication.pr_number == published_pr_number
                    and current.publication.head_sha == head_sha
                )
            )
        ):
            attempt = current.publication.attempt if current.publication else current.attempts
            updates["handoff"] = HandoffEvidence(
                pr_number=published_pr_number,
                head_sha=head_sha,
                attempt=attempt,
                invocation_id=invocation_id,
                agent_id=effective_agent_id,
                observed_at=_now_timestamp(),
            )
            if current.status is SliceStatus.SPAWNED:
                transitioned = slice_transition(
                    transitioned, SliceStatusChanged(SliceStatus.IN_REVIEW)
                )
        elif watcher.publication_ownership_verified is True:
            missing = [
                name
                for name, value in (
                    ("head_sha", head_sha),
                    ("pr_number", published_pr_number),
                    ("owner_agent_id", effective_agent_id),
                    ("invocation_id_provenance", invocation_id),
                )
                if not value
            ]
            LOGGER.warning(
                "[TL loop] skipping handoff backfill for %s: missing %s",
                current.id,
                ", ".join(missing) or "matching publication identity",
            )
        elif watcher.found is True:
            _, ownership_reason = _publication_ownership_status(watcher)
            LOGGER.warning(
                "[TL loop] skipping handoff backfill for %s: publication ownership is not verified (%s)",
                current.id,
                watcher.publication_ownership_error
                or ownership_reason
                or "host publication identity unavailable",
            )
        if watcher.merged is True:
            transitioned = slice_transition(transitioned, MergeCompleted(watcher.pr_number or 0))
    if owner_id is not None and current.dispatch_agent_id is None:
        updates["dispatch_agent_id"] = owner_id
    return replace(transitioned, **updates)


def _publication_from_watcher(
    current: SliceState,
    watcher: WatcherObservation | Mapping[str, object],
    head_sha: str | None,
    owner_id: str | None,
) -> PublicationBinding | None:
    """Recover a publication only from an ownership-verified watcher snapshot."""
    watcher = _as_watcher_observation(watcher)
    assert watcher is not None
    if watcher.publication_ownership_verified is not True or not head_sha:
        return None
    pr_number = watcher.pr_number
    head_branch = watcher.head_branch
    base_branch = watcher.base_branch
    if (
        type(pr_number) is not int
        or pr_number <= 0
        or not isinstance(head_branch, str)
        or not head_branch
        or not isinstance(base_branch, str)
        or not base_branch
    ):
        LOGGER.warning(
            "[TL loop] cannot recover publication evidence for target=%s: incomplete watcher identity",
            current.id,
        )
        return None
    existing = current.publication
    if existing is not None and (existing.pr_number != pr_number or existing.head_sha != head_sha):
        LOGGER.warning(
            "[TL loop] refusing watcher publication replacement for target=%s: head changed",
            current.id,
        )
        return None
    publication_record = _publication_evidence(watcher)
    record_slice_id = _publication_record_text(publication_record, "slice_id")
    if record_slice_id is not None and record_slice_id != current.id:
        LOGGER.warning(
            "[TL loop] refusing publication evidence for %s: slice identity %s disagrees",
            current.id,
            record_slice_id,
        )
        return None
    expected_owner = current.dispatch_agent_id or owner_id
    record_owner = _publication_record_text(publication_record, "author_agent")
    if record_owner is not None and expected_owner is not None and record_owner != expected_owner:
        LOGGER.warning(
            "[TL loop] refusing publication evidence for %s: owner identity %s disagrees",
            current.id,
            record_owner,
        )
        return None
    record_invocation_id = _publication_record_text(publication_record, "invocation_id")
    return PublicationBinding(
        pr_number=pr_number,
        head_sha=head_sha,
        head_branch=head_branch,
        base_branch=base_branch,
        attempt=existing.attempt if existing is not None else current.attempts,
        invocation_id=(
            current.dispatch_invocation_id
            or (existing.invocation_id if existing is not None else None)
            or record_invocation_id
        ),
    )


def _dispatch_waiting_phase(slices: Mapping[str, SliceState]) -> TLWaiting:
    handles = {
        slice_id: ChildHandle(
            slice_id,
            slice_state.branch or "",
            slice_state.agent_type or "unknown",
        )
        for slice_id, slice_state in slices.items()
        if slice_state.status is SliceStatus.SPAWNED
    }
    return TLWaiting(handles)


def _agent_for_dispatch_intent(result: ToolResult | None, intent_id: str) -> str | None:
    """Resolve the owning agent id for a dispatch intent.

    Deliberately does not require the agent to still be alive: intent_id is
    a per-dispatch UUID persisted at spawn time (agent_dir/dispatch_intent)
    and never reused, so a matching record safely identifies the owner
    regardless of current liveness. Coding agents are one-shot by design
    (see CLAUDE.md "One-shot coding invocations") -- a dev normally exits
    right after filing its PR, well before the controller reconciles
    ownership on restart. Requiring is_alive here would make runtime_owner
    permanently unrecoverable for the common case of a dev that already
    finished its one assignment.
    """
    if result is None or result.success is False or not isinstance(result.result, Mapping):
        return None
    agents = result.result.get("agents")
    if not isinstance(agents, list):
        return None
    for raw_agent in agents:
        if not isinstance(raw_agent, Mapping):
            continue
        if raw_agent.get("intent_id") != intent_id:
            continue
        for key in ("agent_id", "id"):
            agent_id = raw_agent.get(key)
            if isinstance(agent_id, str) and agent_id:
                return agent_id
    return None


def _controller_event_run_seq(result: ToolResult | None) -> int | None:
    if result is None or result.success is False or not isinstance(result.result, Mapping):
        return None
    value = result.result.get("run_seq")
    return value if type(value) is int and value > 0 else None


def _dispatch_payload(
    slice_id: str,
    attempt: DispatchAttempt,
    boundary: str,
    *,
    error: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "slice_id": slice_id,
        "intent_id": attempt.intent_id,
        "boundary": boundary,
        "started_at": attempt.started_at,
        "attempt": attempt.attempt,
    }
    if attempt.controller_epoch is not None:
        payload["controller_epoch"] = attempt.controller_epoch
        payload["dispatch_generation"] = attempt.dispatch_generation
    if error is not None:
        payload["error"] = error
    if attempt.harness:
        payload["harness"] = attempt.harness
    if attempt.agent_type:
        payload["agent_type"] = attempt.agent_type
    if attempt.model:
        payload["model"] = attempt.model
    return payload


def _spawn_route(
    attempt: DispatchAttempt, fallback_harness: str | None
) -> tuple[str | None, str | None]:
    """Return protocol fields while preserving the qualified audit identity."""
    if attempt.agent_type:
        return attempt.agent_type, attempt.model
    if not fallback_harness:
        return None, None
    route = parse_harness_identifier(fallback_harness)
    return route.agent_type, route.model


def _confirm_dispatch_event(
    previous_slices: Mapping[str, SliceState],
    updated_slices: Mapping[str, SliceState],
    event: EventEnvelope,
    slice_id: str | None,
    event_seq: int,
    controller_epoch: str | None = None,
) -> dict[str, SliceState]:
    if (
        not _is_spawn_confirmation_event(event)
        or slice_id is None
        or not _dispatch_confirmation_matches(
            previous_slices, event, controller_epoch=controller_epoch
        )
    ):
        return dict(updated_slices)
    current = updated_slices.get(slice_id)
    if current is None or current.dispatch_intent_id is None:
        return dict(updated_slices)
    data_agent = event.data.get("child_agent") or event.data.get("slug")
    agent_id = data_agent if isinstance(data_agent, str) and data_agent else None
    if not agent_id and isinstance(event.agent_id, str):
        agent_id = event.agent_id
    return {
        **updated_slices,
        slice_id: replace(
            slice_transition(current, SliceStatusChanged(SliceStatus.SPAWNED)),
            park_cause=None,
            dispatch_last_boundary="agent.spawned",
            dispatch_error=None,
            dispatch_agent_id=agent_id,
            dispatch_invocation_id=event.invocation_id,
            dispatch_authoritative_event_seq=event_seq,
        ),
    }


def _event_dispatch_intent_id(event: EventEnvelope) -> str | None:
    value = event.data.get("intent_id")
    if isinstance(value, str) and value:
        return value
    shadow_event = event.data.get("shadow_event")
    if isinstance(shadow_event, Mapping):
        value = shadow_event.get("intent_id")
        if isinstance(value, str) and value:
            return value
    return None


def _event_dispatch_epoch(event: EventEnvelope) -> str | None:
    value = event.data.get("controller_epoch")
    return value if isinstance(value, str) and value else None


def _event_dispatch_generation(event: EventEnvelope) -> int | None:
    value = event.data.get("dispatch_generation")
    return value if type(value) is int and value >= 0 else None


def _event_slice_hint(event: EventEnvelope) -> str | None:
    value = event.data.get("slice_id") or event.slice_id
    return value if isinstance(value, str) and value else None


def _record_dispatch_correlation_failure(
    store: RunStore,
    state: RunState,
    event: EventEnvelope,
    correlation: DispatchCorrelation,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> None:
    """Retain rejected observations and emit one deduplicated integrity fact."""
    run_seq = event.run_seq
    prior = store.quarantined_events()
    if run_seq is not None and any(item.get("run_seq") == run_seq for item in prior):
        return
    document = dict(envelope_document(event))
    document["correlation"] = correlation.classification
    document["correlation_reason"] = correlation.reason
    store.quarantine_event(document)
    payload = {
        "slice_id": correlation.slice_id,
        "intent_id": _event_dispatch_intent_id(event),
        "attempt": event.data.get("attempt"),
        "controller_epoch": _event_dispatch_epoch(event),
        "dispatch_generation": _event_dispatch_generation(event),
        "classification": correlation.classification,
        "reason": correlation.reason,
        "event_run_seq": event.run_seq,
    }
    _record_controller_event(
        correlation.slice_id or state.run_id,
        "tl.dispatch_event_rejected",
        payload,
        config,
        effects,
        effects_log,
    )


def correlate_dispatch_event(state: RunState, event: EventEnvelope) -> DispatchCorrelation:
    """Correlate a spawn observation using the current controller epoch.

    Events from an earlier controller epoch remain queryable for audit but
    can never confirm a current dispatch or charge a new owner. A matching
    epoch with a stale generation is an integrity conflict, not evidence of
    a new worker.
    """
    if not _is_spawn_confirmation_event(event):
        return DispatchCorrelation(DISPATCH_CORRELATED)
    intent_id = _event_dispatch_intent_id(event)
    matches = [
        slice_state
        for slice_state in state.slices.values()
        if intent_id is not None
        and slice_state.dispatch_intent_id == intent_id
        and slice_state.status in DISPATCHING_STATUSES
    ]
    candidate = matches[0] if len(matches) == 1 else None
    event_epoch = _event_dispatch_epoch(event)
    if candidate is not None:
        if event_epoch is not None and event_epoch != state.controller_epoch:
            return DispatchCorrelation(
                DISPATCH_HISTORICAL_AUDIT,
                candidate.id,
                "controller_epoch_mismatch",
            )
        event_generation = _event_dispatch_generation(event)
        if event_generation is not None and event_generation != candidate.dispatch_generation:
            return DispatchCorrelation(
                DISPATCH_INTEGRITY_CONFLICT,
                candidate.id,
                "dispatch_generation_mismatch",
            )
        return DispatchCorrelation(DISPATCH_CORRELATED, candidate.id)
    hint = _event_slice_hint(event)
    current = state.slices.get(hint) if hint is not None else None
    if current is not None and event_epoch is not None and event_epoch != state.controller_epoch:
        return DispatchCorrelation(
            DISPATCH_HISTORICAL_AUDIT,
            current.id,
            "controller_epoch_mismatch",
        )
    return DispatchCorrelation(DISPATCH_INTEGRITY_CONFLICT, hint, "intent_mismatch")


def _dispatch_confirmation_matches(
    slices: Mapping[str, SliceState],
    event: EventEnvelope,
    *,
    controller_epoch: str | None = None,
) -> bool:
    if not _is_spawn_confirmation_event(event):
        return True
    intent_id = _event_dispatch_intent_id(event)
    if intent_id is None:
        return False
    matches = [
        slice_state
        for slice_state in slices.values()
        if slice_state.dispatch_intent_id == intent_id
        and slice_state.status in DISPATCHING_STATUSES
    ]
    if len(matches) != 1:
        return False
    current = matches[0]
    if (
        controller_epoch is not None
        and _event_dispatch_epoch(event) is not None
        and _event_dispatch_epoch(event) != controller_epoch
    ):
        return False
    generation = _event_dispatch_generation(event)
    return generation is None or generation == current.dispatch_generation


def _emit_dispatch_confirmation(
    before: Mapping[str, SliceState],
    after: Mapping[str, SliceState],
    event: EventEnvelope,
    slice_id: str | None,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> None:
    if not _is_spawn_confirmation_event(event) or slice_id is None:
        return
    previous = before.get(slice_id)
    current = after.get(slice_id)
    if (
        previous is None
        or current is None
        or previous.dispatch_intent_id is None
        or current.dispatch_authoritative_event_seq is None
        or previous.dispatch_authoritative_event_seq == current.dispatch_authoritative_event_seq
    ):
        return
    attempt = DispatchAttempt(
        previous.dispatch_intent_id,
        previous.dispatch_started_at or time.time(),
        previous.agent_type or "",
        attempt=previous.attempts,
    )
    _record_controller_event(
        slice_id,
        "tl.dispatch_confirmed",
        _dispatch_payload(slice_id, attempt, "agent.spawned"),
        config,
        effects,
        effects_log,
    )


def _is_spawn_confirmation_event(event: EventEnvelope) -> bool:
    if event.kind is EventKind.AGENT_SPAWNED:
        return True
    shadow_event = event.data.get("shadow_event")
    return isinstance(shadow_event, Mapping) and shadow_event.get("kind") == "child_spawned"


def _spawn_agent_id(result: ToolResult | None) -> str | None:
    if result is None or not isinstance(result.result, Mapping):
        return None
    for key in ("agent_id", "id", "child_agent"):
        value = result.result.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _spawn_invocation_id(result: ToolResult | None) -> str | None:
    if result is None or not isinstance(result.result, Mapping):
        return None
    value = result.result.get("invocation_id")
    if isinstance(value, str) and value:
        return value
    nested = result.result.get("invocation")
    if isinstance(nested, Mapping):
        value = nested.get("invocation_id")
        if isinstance(value, str) and value:
            return value
    return None


def _dispatch_children(
    plan: WorkPlan,
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
    store: RunStore,
) -> RunState:
    live = cast(EffectClient, effects) if config.active else None
    before_slices = state.slices
    for worker in plan.workers:
        try:
            dispatchable = _can_dispatch(worker.name, state, config)
        except ScheduleDeadlock as error:
            _park_schedule_deadlock(error, state, config, live, store)
            raise TLLoopError(str(error)) from error
        if not dispatchable:
            continue
        if _already_dispatched(worker.name, state):
            continue
        attempt = _prepare_spawn(worker.name, state, config, effects, store, effects_log)
        state = store.load()
        _record_spawn_request(worker.name, attempt, config, effects, effects_log)
        runtime_name = config.dispatch_names.get(worker.name, worker.name)
        worker_args: dict[str, object] = {"name": runtime_name, "task": worker.task}
        agent_type, model = _spawn_route(attempt, worker.agent_type)
        _optional_argument(worker_args, "agent_type", agent_type)
        _optional_argument(worker_args, "model", model)
        try:
            result = _invoke(
                "spawn_worker",
                worker.name,
                worker_args,
                config.active,
                live,
                _worker_call(worker, agent_type, model, attempt.intent_id, runtime_name),
                effects_log,
                raise_on_failure=False,
            )
        except Exception as error:  # noqa: BLE001 - persist the boundary failure
            return _record_dispatch_failure(
                store, state, worker.name, attempt, str(error), config, effects, effects_log
            )
        state = _record_dispatch_result(
            store, state, worker.name, attempt, result, config, effects, effects_log
        )
        if state.fsm.phase is TLPhase.TLFailed:
            return state
    for leaf in plan.leaves:
        try:
            dispatchable = _can_dispatch(leaf.name, state, config)
        except ScheduleDeadlock as error:
            _park_schedule_deadlock(error, state, config, live, store)
            raise TLLoopError(str(error)) from error
        if not dispatchable:
            continue
        if _already_dispatched(leaf.name, state):
            continue
        attempt = _prepare_spawn(leaf.name, state, config, effects, store, effects_log)
        state = store.load()
        _record_spawn_request(leaf.name, attempt, config, effects, effects_log)
        runtime_name = config.dispatch_names.get(leaf.name, leaf.name)
        leaf_args: dict[str, object] = {"name": runtime_name, "task": leaf.task}
        _optional_argument(leaf_args, "intent_id", attempt.intent_id)
        agent_type, model = _spawn_route(attempt, leaf.agent_type)
        _optional_argument(leaf_args, "agent_type", agent_type)
        _optional_argument(leaf_args, "model", model)
        for name, value in (
            ("boundary", leaf.boundary),
            ("read_first", leaf.read_first),
            ("steps", leaf.steps),
            ("verify", leaf.verify),
        ):
            if value:
                leaf_args[name] = list(value)
        _optional_argument(leaf_args, "context", leaf.context)
        try:
            result = _invoke(
                "spawn_leaf",
                leaf.name,
                leaf_args,
                config.active,
                live,
                _leaf_call(leaf, agent_type, model, attempt.intent_id, runtime_name),
                effects_log,
                raise_on_failure=False,
            )
        except Exception as error:  # noqa: BLE001 - persist the boundary failure
            return _record_dispatch_failure(
                store, state, leaf.name, attempt, str(error), config, effects, effects_log
            )
        state = _record_dispatch_result(
            store, state, leaf.name, attempt, result, config, effects, effects_log
        )
        if state.fsm.phase is TLPhase.TLFailed:
            return state
    updated = store.load() if config.policy is not None else state
    _emit_slice_status_changes(
        before_slices,
        updated.slices,
        config,
        effects,
        effects_log,
    )
    return updated


def _child_recovery_projection(
    child_state: RunState | None,
    parent_run_id: str,
) -> tuple[SubTLLifecycle, ChildRecoverySummary] | None:
    """Project one child's authoritative recovery to its nearest parent."""
    if child_state is None:
        return None
    slices = getattr(child_state, "slices", {})
    if not isinstance(slices, Mapping):
        return None
    for slice_id, slice_state in slices.items():
        recovery = slice_state.recovery
        if recovery is None:
            continue
        cause = BlockCause(recovery.cause)
        lifecycle = (
            SubTLLifecycle.HUMAN_GATE
            if recovery.phase is RecoveryPhase.HUMAN_GATE
            else SubTLLifecycle.RECOVERING
        )
        owner_run_id = getattr(child_state, "run_id", parent_run_id)
        child_path = tuple(
            item
            for item in (parent_run_id, owner_run_id, slice_id)
            if isinstance(item, str) and item
        )
        return lifecycle, ChildRecoverySummary(
            owner_run_id=owner_run_id,
            child_path=child_path,
            slice_id=slice_id,
            cause=cause,
            recovery_round=recovery.recovery_round,
            next_probe_at=recovery.next_probe_at,
        )
    integration = getattr(child_state, "integration", None)
    nested_recoveries = getattr(integration, "sub_tl_recovery", {})
    if isinstance(nested_recoveries, Mapping):
        nested_states = getattr(integration, "sub_tl_states", {})
        for child_name, summary in nested_recoveries.items():
            if not isinstance(summary, ChildRecoverySummary):
                continue
            lifecycle = (
                nested_states.get(child_name, SubTLLifecycle.RECOVERING)
                if isinstance(nested_states, Mapping)
                else SubTLLifecycle.RECOVERING
            )
            if not isinstance(lifecycle, SubTLLifecycle):
                lifecycle = SubTLLifecycle.RECOVERING
            path = summary.child_path
            if not path or path[0] != parent_run_id:
                path = (parent_run_id, *path)
            return lifecycle, replace(summary, child_path=path)
    return None


def _run_sub_tls(
    plan: WorkPlan,
    state: RunState,
    config: TLLoopConfig,
    source: EventQueue,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
) -> RunState:
    """Run recursive children in deterministic, bounded order batches."""
    if not plan.sub_tls:
        return state
    tasks_by_name = {task.name: task for task in plan.sub_tls}
    current_order = state.current_order or 1
    for stage in plan.ordered_stages:
        if stage.order < current_order:
            continue
        if stage.order > current_order:
            break
        stage_tasks = tuple(tasks_by_name[name] for name in stage.sub_tls)
        state = store.load()
        stage_states = [state.slices.get(task.name) for task in stage_tasks]
        if any(current is None for current in stage_states):
            missing = next(
                task.name for task, current in zip(stage_tasks, stage_states) if current is None
            )
            raise TLLoopError(f"recursive slice {missing!r} is missing")
        for task, current in zip(stage_tasks, stage_states):
            if (
                current is not None
                and current.status is SliceStatus.MERGED
                and current.pr_number is not None
                and current.post_merge is not None
                and current.post_merge.phase is not PostMergePhase.COMPLETE
            ):
                state = _drain_post_merge_recovery(
                    state,
                    task.name,
                    current.pr_number,
                    config,
                    effects,
                    effects_log,
                    store,
                )
        state = store.load()
        stage_states = [state.slices.get(task.name) for task in stage_tasks]
        if any(
            current.status in {SliceStatus.FAILED, SliceStatus.PARKED}
            for current in stage_states
            if current is not None
        ):
            return _fail_recursive_parent(
                state, config, effects, store, effects_log, "recursive child is not recoverable"
            )
        was_stage_complete = _ordered_stage_complete(stage_tasks, state.slices)
        pending = tuple(
            task
            for task, current in zip(stage_tasks, stage_states)
            if current is not None
            and current.status not in {SliceStatus.IN_REVIEW, SliceStatus.REPAIRING}
            and not _ordered_child_complete(task, current)
        )
        if not pending:
            state = _integrate_stage_candidates(
                stage_tasks, state, config, effects, store, effects_log
            )
            _emit_stage_completion(
                stage,
                state,
                was_stage_complete,
                config,
                effects,
                effects_log,
            )
            if any(
                state.slices[task.name].status in {SliceStatus.IN_REVIEW, SliceStatus.REPAIRING}
                for task in stage_tasks
            ):
                break
            if not _ordered_stage_complete(stage_tasks, state.slices):
                break
            state, current_order = _advance_ordered_stage(
                stage.order, state, current_order, plan, store
            )
            continue
        if not any(
            current is not None and current.status is not SliceStatus.PENDING
            for current in stage_states
        ):
            _record_controller_event(
                "controller",
                "tl.stage_started",
                {
                    "order": stage.order,
                    "sub_tl_ids": list(stage.sub_tls),
                    "run_id": store.run_id,
                },
                config,
                effects,
                effects_log,
            )
        _validate_stage_event_routes(pending)
        if config.max_parallel_slices == 0:
            raise LoopLimitExceeded("max_parallel_slices must permit one recursive child")
        state, spawned = _prepare_sub_tl_stage(
            pending, state, config, source, effects, store, effects_log, stage.order
        )
        width = config.max_parallel_slices or len(spawned)
        stage_recovery = False
        for batch_start in range(0, len(spawned), width):
            batch = spawned[batch_start : batch_start + width]
            outcomes = _run_sub_tl_batch(batch, config, source, effects, store, state.budgets)
            state = _complete_sub_tl_batch(
                batch, outcomes, state, config, effects, store, effects_log
            )
            if any(phase is TLPhase.TLFailed for _, phase, _ in outcomes):
                return _fail_recursive_parent(
                    state, config, effects, store, effects_log, "recursive child failed"
                )
            if any(task.name in state.integration.sub_tl_recovery for task in stage_tasks):
                stage_recovery = True
        state = _integrate_stage_candidates(stage_tasks, state, config, effects, store, effects_log)
        _emit_stage_completion(
            stage,
            state,
            was_stage_complete,
            config,
            effects,
            effects_log,
        )
        if any(
            state.slices[task.name].status in {SliceStatus.IN_REVIEW, SliceStatus.REPAIRING}
            for task in stage_tasks
        ):
            break
        if not _ordered_stage_complete(stage_tasks, state.slices):
            break
        if stage_recovery:
            break
        state, current_order = _advance_ordered_stage(
            stage.order, state, current_order, plan, store
        )
    if plan.sub_tls and not plan.workers and not plan.leaves:
        awaiting_integration = tuple(
            task.name
            for task in plan.sub_tls
            if not _ordered_child_complete(task, state.slices[task.name])
        )
        if awaiting_integration:
            before_phase = _phase_from_state(state)
            handles = {
                task_id: ChildHandle(task_id, state.slices[task_id].branch or "", "sub-tl")
                for task_id in awaiting_integration
            }
            state = store.checkpoint(
                TLWaiting(handles),
                state.slices,
                state.budgets,
                state.events.last_consumed_offset,
                current_order=state.current_order,
                ordered_stages=state.ordered_stages,
                integration=state.integration,
            )
            _emit_phase_change(
                store.run_id,
                before_phase,
                _phase_from_state(state),
                config,
                effects,
                effects_log,
            )
            return state
        before_phase = _phase_from_state(state)
        if isinstance(state.recursive_fsm, RecursiveTLAllMerged):
            return state
        state = store.checkpoint(
            TLDone(), state.slices, state.budgets, state.events.last_consumed_offset
        )
        _emit_phase_change(
            store.run_id,
            before_phase,
            _phase_from_state(state),
            config,
            effects,
            effects_log,
        )
    return state


def _emit_stage_completion(
    stage: OrderedStage,
    state: RunState,
    was_complete: bool,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> None:
    """Publish one durable completion observation for an ordered stage."""
    if was_complete or any(
        not _ordered_slice_complete(state.slices[slice_id])
        for slice_id in stage.sub_tls
        if slice_id in state.slices
    ):
        return
    _record_controller_event(
        "controller",
        "tl.stage_completed",
        {
            "order": stage.order,
            "sub_tl_ids": list(stage.sub_tls),
        },
        config,
        effects,
        effects_log,
    )


def _advance_ordered_stage(
    order: int,
    state: RunState,
    current_order: int,
    plan: WorkPlan,
    store: RunStore,
) -> tuple[RunState, int]:
    """Persist the next manifest stage only after this stage is complete."""
    next_orders = [stage.order for stage in plan.ordered_stages if stage.order > order]
    if not next_orders:
        return state, current_order
    next_order = min(next_orders)
    if current_order == next_order:
        return state, current_order
    return (
        store.set_ordered_state(
            next_order,
            state.ordered_stages,
            state.integration,
        ),
        next_order,
    )


def _prepare_sub_tl_stage(
    tasks: Sequence[SubTLTask],
    state: RunState,
    config: TLLoopConfig,
    source: EventQueue,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
    order: int,
) -> tuple[RunState, tuple[SubTLTask, ...]]:
    """Persist every owner before launching a same-order batch."""
    del source
    updated_slices = dict(state.slices)
    prepared: list[SubTLTask] = []
    for task in tasks:
        current = state.slices[task.name]
        branch = derive_child_branch(config.branch, task.name)
        worktree = str(
            task.worktree
            or derive_child_worktree(
                _effective_worktree(config, store.root_dir, store.run_id), task.name
            )
        )
        if config.depth >= config.max_depth:
            before_phase = _phase_from_state(state)
            parked = replace(
                slice_transition(current, SliceStatusChanged(SliceStatus.PARKED)),
                park_cause=ParkCause.SCHEDULE_DEADLOCK,
            )
            failed_slices = {**state.slices, task.name: parked}
            state = store.checkpoint(
                TLFailed(f"depth ceiling reached for {task.name}"),
                failed_slices,
                state.budgets,
                state.events.last_consumed_offset,
                current_order=order,
                ordered_stages=state.ordered_stages,
                integration=state.integration,
            )
            _emit_slice_status_changes(
                {task.name: current},
                {task.name: parked},
                config,
                effects,
                effects_log,
            )
            _record_controller_event(
                task.name,
                "tl.slice_parked",
                {
                    "slice_id": task.name,
                    "park_cause": ParkCause.SCHEDULE_DEADLOCK.value,
                    "attempts": parked.attempts,
                },
                config,
                effects,
                effects_log,
            )
            _emit_phase_change(
                store.run_id,
                before_phase,
                _phase_from_state(state),
                config,
                effects,
                effects_log,
            )
            raise DepthLimitExceeded(f"depth ceiling {config.max_depth} reached for {task.name!r}")
        if current.status is SliceStatus.PENDING:
            internal_intent_id = hashlib.sha256(
                f"{store.run_id}:{task.name}:{current.attempts + 1}".encode()
            ).hexdigest()[:32]
            internal_attempt = DispatchAttempt(
                internal_intent_id,
                time.time() if config.active else 0.0,
                current.agent_type or "sub-tl",
                attempt=current.attempts + 1,
            )
            confirmation = _record_controller_event(
                task.name,
                "tl.dispatch_confirmed",
                _dispatch_payload(task.name, internal_attempt, "sub_tl_started"),
                config,
                effects,
                effects_log,
            )
            authoritative_seq = _controller_event_run_seq(confirmation)
            if authoritative_seq is None:
                authoritative_seq = state.events.last_consumed_offset
            updated_slices[task.name] = replace(
                slice_transition(current, SliceStatusChanged(SliceStatus.SPAWNED)),
                base_ref=config.branch,
                branch=branch,
                worktree=worktree,
                dispatch_intent_id=internal_intent_id,
                dispatch_started_at=internal_attempt.started_at,
                dispatch_last_boundary="sub_tl_started",
                dispatch_agent_id=task.name,
                dispatch_authoritative_event_seq=authoritative_seq,
                dispatch_generation=internal_attempt.dispatch_generation,
            )
        prepared.append(task)
    states = dict(state.integration.sub_tl_states)
    for task in prepared:
        if not isinstance(states.get(task.name), SubTLLifecycle):
            states[task.name] = IntegrationLifecycle.RUNNING
    integration = replace(state.integration, sub_tl_states=states)
    previous_slices = state.slices
    state = store.checkpoint(
        _phase_from_state(state),
        updated_slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=order,
        ordered_stages=state.ordered_stages,
        integration=integration,
    )
    _emit_slice_status_changes(previous_slices, state.slices, config, effects, effects_log)
    return state, tuple(prepared)


def _run_sub_tl_batch(
    tasks: Sequence[SubTLTask],
    config: TLLoopConfig,
    source: EventQueue,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    budgets: BudgetLedger,
) -> tuple[tuple[SubTLTask, TLPhase | None, RunState | None], ...]:
    """Run one bounded batch and collect outcomes in plan order."""
    if (
        config.active
        and isinstance(effects, EffectClient)
        and isinstance(effects.transport, TransportClient)
    ):
        return _run_live_sub_tl_batch(tasks, config, source, effects, store, budgets)

    def run_one(task: SubTLTask) -> tuple[SubTLTask, TLPhase | None, RunState | None]:
        branch = derive_child_branch(config.branch, task.name)
        worktree = str(
            task.worktree
            or derive_child_worktree(
                _effective_worktree(config, store.root_dir, store.run_id), task.name
            )
        )
        child_store = RunStore(task.name, store.run_dir)
        if child_store.path.exists():
            child_state = child_store.load()
            child_phase = _child_completion_phase(child_state)
            if child_phase in {
                TLPhase.TLAllMerged,
                TLPhase.TLDone,
                TLPhase.TLPRFiled,
                TLPhase.TLFailed,
            }:
                return task, child_phase, child_state
            if _child_recovery_projection(child_state, store.run_id) is not None:
                return task, child_phase, child_state
        child_config = _child_config(config, task, source, effects, store, branch, worktree)
        try:
            child_result = tl_run({"run_id": task.name, "plan": task.plan}, child_config, budgets)
        except Exception as error:  # noqa: BLE001 - batch completion persists a durable failure
            child_store.record_exit_reason(str(error))
            return task, TLPhase.TLFailed, None
        return task, _child_completion_phase(child_result.final_state), child_result.final_state

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [executor.submit(run_one, task) for task in tasks]
        return tuple(future.result() for future in futures)


def _child_completion_phase(state: RunState) -> TLPhase:
    """Project an empty canonical child for the legacy parent adapter."""
    recursive_fsm = getattr(state, "recursive_fsm", None)
    manifest = getattr(state, "plan_manifest", None)
    if (
        isinstance(recursive_fsm, RecursiveTLAllMerged)
        and manifest is not None
        and not manifest.nodes
    ):
        return TLPhase.TLDone
    return state.fsm.phase


def _run_live_sub_tl_batch(
    tasks: Sequence[SubTLTask],
    config: TLLoopConfig,
    source: EventQueue,
    effects: EffectClient,
    store: RunStore,
    budgets: BudgetLedger,
) -> tuple[tuple[SubTLTask, TLPhase | None, RunState | None], ...]:
    """Run live children as independently owned controller processes."""
    if "fork" not in multiprocessing.get_all_start_methods():
        raise TLLoopError("live ordered sub-TLs require fork-capable controller isolation")
    context = multiprocessing.get_context("fork")
    existing: dict[str, tuple[SubTLTask, TLPhase | None, RunState | None]] = {}
    runnable: list[SubTLTask] = []
    for task in tasks:
        child_store = RunStore(task.name, store.run_dir)
        if child_store.path.exists():
            child_state = child_store.load()
            child_phase = _child_completion_phase(child_state)
            if (
                child_phase
                in {
                    TLPhase.TLAllMerged,
                    TLPhase.TLDone,
                    TLPhase.TLPRFiled,
                    TLPhase.TLFailed,
                }
                or _child_recovery_projection(child_state, store.run_id) is not None
            ):
                existing[task.name] = (task, child_phase, child_state)
                continue
        runnable.append(task)
    processes = [
        (
            task,
            context.Process(
                target=_run_live_sub_tl,
                args=(task, config, source, effects, store, budgets),
                name=f"tl-sub-{task.name}",
            ),
        )
        for task in runnable
    ]
    for _, process in processes:
        process.start()
    outcomes_by_name: dict[str, tuple[SubTLTask, TLPhase | None, RunState | None]] = {
        task.name: existing[task.name] for task in tasks if task.name in existing
    }
    for task, process in processes:
        child_store = RunStore(task.name, store.run_dir)
        child_state = _supervise_live_sub_tl(process, child_store, config)
        phase = (
            _child_completion_phase(child_state) if child_state is not None else TLPhase.TLFailed
        )
        outcomes_by_name[task.name] = (task, phase, child_state)
    return tuple(outcomes_by_name[task.name] for task in tasks)


def _supervise_live_sub_tl(
    process: multiprocessing.Process,
    child_store: RunStore,
    config: TLLoopConfig,
) -> RunState | None:
    """Supervise a child until it resolves or explicit cancellation is requested."""
    poll_interval = max(config.poll_interval, 0.05)
    cancelled = False
    while process.is_alive():
        if config.cancel_event is not None and config.cancel_event.is_set():
            cancelled = True
            process.terminate()
            process.join()
            child_store.record_exit_reason("sub-TL controller cancelled explicitly")
            break
        try:
            child_state = child_store.load()
        except (OSError, ValueError):
            child_state = None
        if _child_recovery_projection(child_state, getattr(child_store, "run_id", "")) is not None:
            return child_state
        process.join(timeout=poll_interval)
    exitcode = getattr(process, "exitcode", None)
    if not cancelled and exitcode is not None:
        try:
            child_state = child_store.load()
        except (OSError, ValueError):
            child_state = None
        if (
            child_state is not None
            and _child_recovery_projection(child_state, getattr(child_store, "run_id", ""))
            is not None
        ):
            return child_state
        empty_scope = (
            child_state is not None
            and isinstance(getattr(child_state, "recursive_fsm", None), RecursiveTLAllMerged)
            and getattr(child_state, "plan_manifest", None) is not None
            and not child_state.plan_manifest.nodes
        )
        if child_state is None or (
            child_state.fsm.phase
            not in {
                TLPhase.TLAllMerged,
                TLPhase.TLDone,
                TLPhase.TLPRFiled,
                TLPhase.TLFailed,
            }
            and not empty_scope
        ):
            child_store.record_exit_reason(
                f"sub-TL controller exited before authoritative resolution with code {exitcode}"
            )
            return None
    try:
        return child_store.load()
    except (OSError, ValueError):
        return None


def _run_live_sub_tl(
    task: SubTLTask,
    config: TLLoopConfig,
    source: EventQueue,
    effects: EffectClient,
    store: RunStore,
    budgets: BudgetLedger,
) -> None:
    """Own one live child controller process and leave its checkpoint durable."""
    branch = derive_child_branch(config.branch, task.name)
    worktree = str(
        task.worktree
        or derive_child_worktree(
            _effective_worktree(config, store.root_dir, store.run_id), task.name
        )
    )
    child_config = _child_config(config, task, source, effects, store, branch, worktree)
    try:
        tl_run({"run_id": task.name, "plan": task.plan}, child_config, budgets)
    except Exception as error:  # noqa: BLE001 - parent reconciles the durable marker
        RunStore(task.name, store.run_dir).record_exit_reason(str(error))


def _complete_sub_tl_batch(
    tasks: Sequence[SubTLTask],
    outcomes: Sequence[tuple[SubTLTask, TLPhase | None, RunState | None]],
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
) -> RunState:
    """Apply a batch atomically after all same-order children finish."""
    del tasks
    updated_slices = dict(state.slices)
    sub_tl_states = dict(state.integration.sub_tl_states)
    sub_tl_recovery = dict(state.integration.sub_tl_recovery)
    candidate_records = dict(state.integration.candidates)
    canonical_phase = state.recursive_fsm
    for task, phase, child_state in outcomes:
        child_handoff_ready = isinstance(
            getattr(child_state, "recursive_fsm", None), RecursiveTLAllMerged
        )
        if (
            phase not in {TLPhase.TLDone, TLPhase.TLPRFiled, TLPhase.TLFailed}
            and not child_handoff_ready
        ):
            current = updated_slices[task.name]
            updated_slices[task.name] = slice_transition(
                current, SliceStatusChanged(SliceStatus.SPAWNED)
            )
            projection = _child_recovery_projection(child_state, state.run_id)
            if projection is not None:
                lifecycle, summary = projection
                sub_tl_states[task.name] = lifecycle
                sub_tl_recovery[task.name] = summary
            continue
        status = SliceStatus.FAILED if phase is TLPhase.TLFailed else SliceStatus.MERGED
        previous_lifecycle = sub_tl_states.get(task.name, IntegrationLifecycle.RUNNING)
        if isinstance(previous_lifecycle, SubTLLifecycle):
            previous_lifecycle = IntegrationLifecycle.RUNNING
        sub_tl_recovery.pop(task.name, None)
        lifecycle = _transition_sub_tl_lifecycle(
            previous_lifecycle,
            (
                IntegrationTransition.CHILDREN_MERGED
                if status is SliceStatus.MERGED
                else IntegrationTransition.FAILED
            ),
        )
        current = updated_slices[task.name]
        candidate_runtime = _candidate_runtime(state.integration, task.name)
        if status is SliceStatus.MERGED:
            candidate = _ensure_aggregate_candidate(
                task, child_state, config, effects, store, effects_log
            )
            if candidate is not None:
                owner_id = f"{store.run_id}:{task.name}:integration"
                owner_branch = (
                    child_state.owner_branch
                    if child_state is not None and child_state.owner_branch
                    else derive_child_branch(config.branch, task.name)
                )
                owner_worktree = (
                    child_state.owner_worktree
                    if child_state is not None and child_state.owner_worktree
                    else str(
                        derive_child_worktree(
                            _effective_worktree(config, store.root_dir, store.run_id),
                            task.name,
                        )
                    )
                )
                current = slice_transition(current, SliceStatusChanged(SliceStatus.IN_REVIEW))
                current = slice_transition(
                    current,
                    HeadEvidenceObserved(
                        head_sha=candidate.head_sha,
                        bind_reviewed_head=True,
                    ),
                )
                current = replace(
                    current,
                    pr_number=candidate.pr_number,
                    dispatch_agent_id=owner_id,
                    dispatch_last_boundary="aggregate_pr_open",
                )
                status = SliceStatus.IN_REVIEW
                lifecycle = _transition_sub_tl_lifecycle(
                    lifecycle, IntegrationTransition.AGGREGATE_PR_OPENED
                )
                candidate_runtime = replace(
                    candidate_runtime,
                    lifecycle=lifecycle,
                    aggregate_pr_number=candidate.pr_number,
                    aggregate_head_sha=candidate.head_sha,
                    aggregate_patch_digest=candidate.patch_digest,
                    aggregate_original_base_sha=candidate.original_base_sha,
                    integration_owner_id=owner_id,
                    integration_owner_run_id=task.name,
                    integration_owner_branch=owner_branch,
                    integration_owner_worktree=owner_worktree,
                    head_sha=candidate.head_sha,
                    patch_digest=candidate.patch_digest,
                )
        updated_slices[task.name] = slice_transition(current, SliceStatusChanged(status))
        sub_tl_states[task.name] = lifecycle
        candidate_records[task.name] = IntegrationCandidateState(
            lifecycle=candidate_runtime.lifecycle,
            aggregate_pr_number=candidate_runtime.aggregate_pr_number,
            aggregate_head_sha=candidate_runtime.aggregate_head_sha,
            aggregate_patch_digest=candidate_runtime.aggregate_patch_digest,
            aggregate_original_base_sha=candidate_runtime.aggregate_original_base_sha,
            integration_owner_id=candidate_runtime.integration_owner_id,
            integration_owner_run_id=candidate_runtime.integration_owner_run_id,
            integration_owner_branch=candidate_runtime.integration_owner_branch,
            integration_owner_worktree=candidate_runtime.integration_owner_worktree,
            head_sha=candidate_runtime.head_sha,
            patch_digest=candidate_runtime.patch_digest,
            validated_base_sha=candidate_runtime.validated_base_sha,
            merge_tree_sha=candidate_runtime.merge_tree_sha,
            integration_evidence_at=candidate_runtime.integration_evidence_at,
            ci_status=candidate_runtime.ci_status,
            merge_attempts=candidate_runtime.merge_attempts,
            base_revalidation_count=candidate_runtime.base_revalidation_count,
            stage_verification=candidate_runtime.stage_verification,
        )
    integration = replace(
        state.integration,
        sub_tl_states=sub_tl_states,
        sub_tl_recovery=sub_tl_recovery,
        candidates=candidate_records,
    )
    previous_slices = state.slices
    checkpoint_phase = (
        canonical_phase
        if isinstance(
            canonical_phase,
            (RecursiveTLRunning, RecursiveTLAllMerged, RecursiveTLFinalizing),
        )
        else _phase_from_state(state)
    )
    state = store.checkpoint(
        checkpoint_phase,
        updated_slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=integration,
    )
    _emit_slice_status_changes(previous_slices, state.slices, config, effects, effects_log)
    return state


def _ensure_aggregate_candidate(
    task: SubTLTask,
    child_state: RunState | None,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
) -> AggregateCandidate | None:
    """Publish one resumable aggregate PR, reusing the child checkpoint on restart."""
    if (
        child_state is None
        or not isinstance(child_state, RunState)
        or not task.integration.aggregate_pr_required
    ):
        return None
    if not _child_has_aggregate_output(child_state):
        return None
    child_integration = child_state.integration
    own_candidate = child_integration.candidates.get(task.name)
    owner_id = (
        own_candidate.integration_owner_id if own_candidate is not None else None
    ) or f"{store.run_id}:{task.name}:integration"
    branch = child_state.owner_branch or derive_child_branch(config.branch, task.name)
    owner_worktree = child_state.owner_worktree or str(
        derive_child_worktree(_effective_worktree(config, store.root_dir, store.run_id), task.name)
    )
    fallback_head = _child_head_sha(child_state, branch)
    fallback_patch = _child_patch_digest(child_state)
    fallback_base = (
        own_candidate.aggregate_original_base_sha if own_candidate is not None else None
    ) or config.branch
    if own_candidate is not None and own_candidate.aggregate_pr_number is not None:
        candidate = AggregateCandidate(
            task.name,
            own_candidate.aggregate_pr_number,
            own_candidate.aggregate_head_sha or fallback_head,
            own_candidate.aggregate_patch_digest or fallback_patch,
            fallback_base,
        )
    elif (
        own_candidate is None
        and child_integration.aggregate_pr_number is not None
        and child_integration.aggregate_head_sha
        and child_integration.integration_owner_run_id == task.name
    ):
        candidate = AggregateCandidate(
            task.name,
            child_integration.aggregate_pr_number,
            child_integration.aggregate_head_sha,
            child_integration.aggregate_patch_digest or fallback_patch,
            child_integration.aggregate_original_base_sha or fallback_base,
        )
    else:
        body = (
            f"Aggregate sub-TL `{task.name}` into `{config.branch}`.\n"
            f"Owner: `{owner_id}`\n"
            f"Head: `{fallback_head}`\n"
            f"Patch: `{fallback_patch}`"
        )
        owner_effects = _owner_effect_client(effects, task.agent_id or task.name)
        result = _invoke(
            "file_pr",
            task.name,
            {"title": f"Aggregate {task.name} into {config.branch}", "base_branch": config.branch},
            config.active,
            cast(EffectClient | None, owner_effects),
            lambda client: client.file_pr(
                title=f"Aggregate {task.name} into {config.branch}",
                body=body,
                base_branch=config.branch,
            ),
            effects_log,
        )
        result_data = (
            result.result if result is not None and isinstance(result.result, Mapping) else {}
        )
        pr_number = _positive_result_int(result_data, "pr_number")
        if pr_number is None:
            raise TLLoopError(
                f"aggregate publication for {task.name!r} returned no authoritative PR number"
            )
        head_sha = _result_text(result_data, "head_sha")
        patch_digest = _result_text(result_data, "patch_digest")
        base_sha = _result_text(result_data, "base_sha")
        if not head_sha or not patch_digest or not base_sha:
            raise TLLoopError(
                f"aggregate publication for {task.name!r} returned incomplete evidence"
            )
        candidate = AggregateCandidate(
            task.name,
            pr_number,
            head_sha,
            patch_digest,
            base_sha,
        )
    updated_integration = replace(
        child_integration,
        lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
        aggregate_pr_number=candidate.pr_number,
        aggregate_head_sha=candidate.head_sha,
        aggregate_patch_digest=candidate.patch_digest,
        aggregate_original_base_sha=candidate.original_base_sha,
        integration_owner_id=owner_id,
        integration_owner_run_id=task.name,
        integration_owner_branch=branch,
        integration_owner_worktree=owner_worktree,
        head_sha=candidate.head_sha,
        patch_digest=candidate.patch_digest,
    )
    child_store = RunStore(task.name, store.run_dir)
    updated_child_state = child_store.set_ordered_state(
        child_state.current_order,
        child_state.ordered_stages,
        updated_integration,
    )
    _persist_non_root_handoff(
        updated_child_state,
        child_store,
        updated_integration,
        candidate,
        parent_branch=config.branch,
    )
    if child_integration.aggregate_pr_number is None:
        _record_controller_event(
            task.name,
            "tl.aggregate_pr_opened",
            {
                "sub_tl_id": task.name,
                "pr_number": candidate.pr_number,
                "head_sha": candidate.head_sha,
                "patch_digest": candidate.patch_digest,
                "original_base_sha": candidate.original_base_sha,
                "integration_owner_id": owner_id,
            },
            config,
            effects,
            effects_log,
        )
    return candidate


def _integrate_stage_candidates(
    tasks: Sequence[SubTLTask],
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
) -> RunState:
    """Serialize ready aggregate merges through the parent controller only."""
    if not config.active or _integration_gate_pending(state):
        return state
    for task in sorted(tasks, key=lambda item: item.name):
        current = state.slices[task.name]
        if _ordered_child_complete(task, current):
            continue
        if (
            current.status is not SliceStatus.IN_REVIEW
            or current.pr_number is None
            or current.reviewed_head is None
            or current.verdict not in {Verdict.GO, Verdict.GO_WITH_NITS}
            or current.ci_state.get(current.reviewed_head) not in {"success", "neutral"}
        ):
            return state
        state = _integrate_one_candidate(task, state, config, effects, store, effects_log)
        if state.slices[task.name].status is not SliceStatus.MERGED:
            if _candidate_lane_blocked_by_other(state, state.slices[task.name]):
                continue
            break
    return state


def _candidate_lane_key(state: RunState, current: SliceState) -> tuple[str, str]:
    """Resolve the durable lane identity for one aggregate candidate."""
    return _repository_identity(state), _publication_parent_branch_from_slice(current)


def _candidate_lane_blocked_by_other(state: RunState, current: SliceState) -> bool:
    """Report whether another child currently owns this integration lane."""
    try:
        repository, parent_branch = _candidate_lane_key(state, current)
    except ValueError:
        return False
    lane = state.integration.lanes.get(f"{repository}:{parent_branch}")
    return lane is not None and lane.phase is not LanePhase.IDLE and lane.child_id != current.id


def _abandon_owned_lane_integration(
    state: RunState,
    current: SliceState,
    integration: IntegrationRuntimeState,
    cause: str,
    diagnostic: str,
) -> IntegrationRuntimeState:
    """Abandon an owned lane when a deterministic failure opens a durable gate."""
    try:
        repository, parent_branch = _candidate_lane_key(state, current)
    except ValueError:
        return integration
    key = f"{repository}:{parent_branch}"
    lane = integration.lanes.get(key)
    if lane is None or lane.child_id != current.id:
        return integration
    if lane.phase is LanePhase.IDLE:
        return integration
    abandoned = transition_lane(lane, LaneAbandoned(cause, diagnostic[:500]))
    return replace(integration, lanes={**integration.lanes, key: abandoned})


def _recover_owned_lane_integration(
    state: RunState,
    current: SliceState,
    integration: IntegrationRuntimeState,
    diagnostic: str,
) -> IntegrationRuntimeState:
    """Keep an uncertain effect lane owned until an operator resolves it."""
    try:
        repository, parent_branch = _candidate_lane_key(state, current)
    except ValueError:
        return integration
    key = f"{repository}:{parent_branch}"
    lane = integration.lanes.get(key)
    if lane is None or lane.child_id != current.id:
        return integration
    if lane.phase in {LanePhase.IDLE, LanePhase.RECOVERY, LanePhase.PARKED}:
        return integration
    recovered = transition_lane(lane, LaneRecoveryRequested(diagnostic[:500]))
    return replace(integration, lanes={**integration.lanes, key: recovered})


def _resolve_recovered_lane_integration(
    state: RunState,
    current: SliceState,
    merge_evidence: Mapping[str, object] | None = None,
) -> IntegrationRuntimeState:
    """Resolve a lost merge response once the watcher proves the merge."""
    try:
        repository, parent_branch = _candidate_lane_key(state, current)
    except ValueError:
        return state.integration
    key = f"{repository}:{parent_branch}"
    lane = state.integration.lanes.get(key)
    if lane is None or lane.child_id != current.id or lane.phase is not LanePhase.RECOVERY:
        return state.integration
    if current.post_merge is None:
        raise ValueError("merged slice has no post-merge evidence")
    evidence = merge_evidence or current.post_merge.evidence
    head_sha = _required_merge_identity(evidence, ("head_sha",), "recovered merge head SHA")
    resolved = transition_lane(lane, LaneRecoveryResolved(current.id, head_sha))
    return replace(state.integration, lanes={**state.integration.lanes, key: resolved})


def _reconcile_legacy_parked_lanes(
    state: RunState,
    store: RunStore,
    effects_log: list[EffectIntent] | None = None,
) -> RunState:
    """Move legacy parked resources into explicit, recoverable ownership."""
    for lane in tuple(state.integration.lanes.values()):
        if lane.phase is not LanePhase.PARKED:
            continue
        current = state.slices.get(lane.child_id or "")
        if not _legacy_parked_lane_needs_recovery(current, effects_log):
            state = store.transition_lane(
                lane.repository,
                lane.parent_branch,
                LaneAbandoned(
                    "legacy_parked_failure",
                    "legacy parked lane has no ambiguous merge effect to recover",
                ),
            )
            continue
        state = store.transition_lane(
            lane.repository,
            lane.parent_branch,
            LaneRecoveryRequested("legacy parked lane requires authoritative reconciliation"),
        )
    return state


def _legacy_parked_lane_needs_recovery(
    current: SliceState | None,
    effects_log: list[EffectIntent] | None,
) -> bool:
    """Keep only ambiguous legacy merge attempts behind recovery."""
    if (
        current is not None
        and current.action is not None
        and current.action.kind is ActionKind.MERGE
        and current.action.phase
        in {
            ActionPhase.INTENDED,
            ActionPhase.IN_FLIGHT,
            ActionPhase.UNKNOWN,
        }
    ):
        return True
    if not isinstance(effects_log, EffectJournal) or current is None:
        return False
    return any(
        entry.get("operation") == "merge_pr" and entry.get("target") == current.id
        for entry in effects_log.pending_entries()
    )


def _ensure_candidate_lane(
    state: RunState,
    current: SliceState,
    expected_base_sha: str,
    store: RunStore,
) -> tuple[RunState, bool]:
    """Atomically reserve a parent lane, preserving cross-controller races."""
    try:
        repository, parent_branch = _candidate_lane_key(state, current)
    except ValueError:
        return state, False
    key = f"{repository}:{parent_branch}"
    lane = state.integration.lanes.get(key)
    if lane is not None:
        if lane.phase is LanePhase.PARKED:
            if lane.child_id is None:
                try:
                    return (
                        store.transition_lane(
                            repository,
                            parent_branch,
                            LaneAbandoned(
                                "legacy_parked_without_owner",
                                "parked lane had no durable owner",
                            ),
                        ),
                        False,
                    )
                except ValueError:
                    return store.load(), False
            try:
                return (
                    store.transition_lane(
                        repository,
                        parent_branch,
                        LaneRecoveryRequested(
                            "legacy parked lane requires authoritative reconciliation"
                        ),
                    ),
                    False,
                )
            except ValueError:
                return store.load(), False
        if lane.phase in {
            LanePhase.RESERVED,
            LanePhase.INTEGRATING,
            LanePhase.BOOKKEEPING,
            LanePhase.RECOVERY,
        }:
            return (state, lane.child_id == current.id)
        next_epoch = lane.last_lane_epoch + 1
    else:
        next_epoch = 1
    try:
        return (
            store.transition_lane(
                repository,
                parent_branch,
                LaneReserved(current.id, next_epoch, expected_base_sha),
            ),
            True,
        )
    except ValueError:
        return store.load(), False


def _start_candidate_lane(
    state: RunState,
    current: SliceState,
    head_sha: str,
    store: RunStore,
) -> tuple[RunState, bool]:
    """Durably move a reservation into compare-bound integration."""
    try:
        repository, parent_branch = _candidate_lane_key(state, current)
    except ValueError:
        return state, False
    lane = state.integration.lanes.get(f"{repository}:{parent_branch}")
    if lane is None or lane.child_id != current.id:
        return state, False
    if lane.phase is LanePhase.INTEGRATING:
        return state, lane.head_sha == head_sha
    if lane.phase is not LanePhase.RESERVED:
        return state, False
    try:
        return (
            store.transition_lane(
                repository,
                parent_branch,
                LaneIntegrationStarted(current.id, head_sha),
            ),
            True,
        )
    except ValueError:
        return store.load(), False


def _ensure_bookkeeping_lane(
    state: RunState,
    current: SliceState,
    evidence: Mapping[str, object],
    store: RunStore,
    *,
    push_intent_id: str | None = None,
    push_journal_id: str | None = None,
) -> tuple[RunState, bool]:
    """Backfill a lane for a pre-lane checkpoint before pushing bookkeeping."""
    try:
        expected_base_sha = _required_current_base(current)
    except ValueError:
        expected_base_sha = _required_merge_identity(
            evidence, ("expected_base_sha",), "push base SHA"
        )
    state, ready = _ensure_candidate_lane(state, current, expected_base_sha, store)
    if not ready:
        return state, False
    repository, parent_branch = _candidate_lane_key(state, current)
    lane = state.integration.lanes[f"{repository}:{parent_branch}"]
    if lane.phase is LanePhase.RECOVERY:
        head_sha = _required_merge_identity(evidence, ("head_sha",), "merged head SHA")
        state = store.transition_lane(
            repository,
            parent_branch,
            LaneRecoveryResolved(current.id, head_sha),
        )
        lane = state.integration.lanes[f"{repository}:{parent_branch}"]
    lane_epoch = _required_int(evidence, "lane_epoch", "lane epoch")
    if lane.lane_epoch != lane_epoch:
        return state, False
    if lane.phase is LanePhase.RESERVED:
        state, ready = _start_candidate_lane(
            state,
            current,
            _required_merge_identity(evidence, ("head_sha",), "merged head SHA"),
            store,
        )
        if not ready:
            return state, False
        lane = state.integration.lanes[f"{repository}:{parent_branch}"]
    if lane.phase is LanePhase.INTEGRATING:
        event = LaneBookkeepingStarted(
            current.id,
            _required_merge_identity(evidence, ("merge_journal_id",), "merge journal"),
            push_intent_id
            or _required_merge_identity(evidence, ("parent_push_intent_id",), "push intent"),
            push_journal_id
            or _required_merge_identity(evidence, ("push_journal_id",), "push journal"),
            _required_merge_identity(evidence, ("changelog_commit_sha",), "changelog commit"),
            expected_base_sha,
        )
        state = store.transition_lane(repository, parent_branch, event)
    return state, state.integration.lanes[
        f"{repository}:{parent_branch}"
    ].phase is LanePhase.BOOKKEEPING


def _merge_result_is_authoritative(result: ToolResult | None) -> bool:
    """Accept a merge response as terminal only with an explicit merged flag."""
    return (
        result is not None
        and result.success is True
        and isinstance(result.result, Mapping)
        and result.result.get("merged") is True
    )


def _merge_failure_classification(result: ToolResult | None) -> str | None:
    if result is None or result.success is not False:
        return None
    values = [result.error or ""]
    if isinstance(result.result, Mapping):
        for key in ("classification", "reason", "message", "error"):
            value = result.result.get(key)
            if isinstance(value, str):
                values.append(value)
    text = " ".join(values).lower()
    if "conflict" in text:
        return "conflict"
    if any(
        marker in text
        for marker in (
            "stale",
            "compare-and-swap",
            "compare_and_swap",
            "cas",
            "base changed",
            "head changed",
        )
    ):
        return "base_changed"
    return "failure"


def _merge_failure_reason(result: ToolResult | None) -> str:
    if result is None:
        return "merge result was not returned"
    if result.error:
        return result.error[:500]
    if isinstance(result.result, Mapping):
        for key in ("reason", "message", "error"):
            value = result.result.get(key)
            if isinstance(value, str) and value:
                return value[:500]
    return "merge_pr returned failure"


def _watcher_evidence_text(result: ToolResult, key: str) -> str | None:
    """Read one non-empty compare field from a watcher response."""
    observation = _watcher_result_observation(result)
    if observation is None:
        return None
    value = getattr(observation, key, None)
    return value if isinstance(value, str) and value else None


def _watcher_result_observation(result: ToolResult | None) -> WatcherObservation | None:
    if result is None or result.success is not True or not isinstance(result.result, Mapping):
        return None
    return WatcherObservation.from_response(result.result)


def _integration_gate_pending(state: RunState) -> bool:
    return any(
        gate.name in {INTEGRATION_REVALIDATION_GATE_NAME, INTEGRATION_CONFLICT_GATE_NAME}
        and gate.status is GateStatus.PENDING
        for gate in state.gates
    )


def _open_integration_gate(
    task: SubTLTask,
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
    *,
    gate_name: str,
    lifecycle: IntegrationLifecycle,
    reason: str,
) -> RunState:
    current = state.slices[task.name]
    sub_states = dict(state.integration.sub_tl_states)
    sub_states[task.name] = lifecycle
    candidate_runtime = _candidate_runtime(state.integration, task.name)
    updated = slice_transition(current, SliceStatusChanged(SliceStatus.IN_REVIEW))
    updated = slice_transition(updated, ActionChanged(None))
    updated = replace(
        updated,
        dispatch_last_boundary="integration_gate",
        dispatch_error=reason[:500],
    )
    previous = next((gate for gate in state.gates if gate.name == gate_name), None)
    candidate_runtime = replace(
        candidate_runtime,
        lifecycle=lifecycle,
        validated_base_sha=None,
        merge_tree_sha=None,
        integration_evidence_at=None,
        ci_status="unknown",
        stage_verification="failed",
        head_sha=(
            None
            if lifecycle is IntegrationLifecycle.INTEGRATION_CONFLICT
            else candidate_runtime.head_sha
        ),
        patch_digest=(
            None
            if lifecycle is IntegrationLifecycle.INTEGRATION_CONFLICT
            else candidate_runtime.patch_digest
        ),
    )
    integration = _persist_candidate_runtime(
        replace(state.integration, sub_tl_states=sub_states), task.name, candidate_runtime
    )
    integration = _abandon_owned_lane_integration(
        state, updated, integration, "integration_gate", reason
    )
    state = store.checkpoint(
        _phase_from_state(state),
        {**state.slices, task.name: updated},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=integration,
    )
    state = store.set_gate(gate_name, GateStatus.PENDING)
    if previous is None or previous.status is not GateStatus.PENDING:
        _record_controller_event(
            "controller",
            "tl.gate_opened",
            {"gate_name": gate_name, "run_id": store.run_id, "reason": reason[:500]},
            config,
            effects,
            effects_log,
        )
    return state


def _handle_external_base_change(
    task: SubTLTask,
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
    *,
    base_sha: str,
    head_sha: str,
    patch_digest: str,
    reason: str,
) -> RunState:
    _record_controller_event(
        task.name,
        "tl.integration_base_invalidated",
        {
            "slice_id": task.name,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "reason": reason[:500],
        },
        config,
        effects,
        effects_log,
    )
    candidate_runtime = _candidate_runtime(state.integration, task.name)
    if candidate_runtime.base_revalidation_count >= config.max_base_revalidations:
        return _open_integration_gate(
            task,
            state,
            config,
            effects,
            store,
            effects_log,
            gate_name=INTEGRATION_REVALIDATION_GATE_NAME,
            lifecycle=IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
            reason=reason,
        )
    invalidated = invalidate_integration_evidence(
        candidate_runtime,
        base_sha="",
        head_sha=head_sha,
        patch_digest=patch_digest,
    )
    current = state.slices[task.name]
    return _checkpoint_integration_retry(
        state,
        task.name,
        invalidated,
        config,
        store,
        slice_update=replace(
            slice_transition(current, ActionChanged(None)),
            dispatch_last_boundary="base_revalidation",
            dispatch_error=reason[:500],
        ),
    )


def _handle_integration_revalidation(
    task: SubTLTask,
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
    *,
    base_sha: str,
    head_sha: str,
    patch_digest: str,
    reason: str,
) -> RunState:
    """Clear non-review integration evidence without misclassifying a base change."""
    _record_controller_event(
        task.name,
        "tl.integration_evidence_invalidated",
        {
            "slice_id": task.name,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "reason": reason[:500],
        },
        config,
        effects,
        effects_log,
    )
    candidate_runtime = _candidate_runtime(state.integration, task.name)
    invalidated = replace(
        candidate_runtime,
        lifecycle=IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
        validated_base_sha=None,
        merge_tree_sha=None,
        ci_status="unknown",
        stage_verification="pending",
        integration_evidence_at=None,
    )
    current = state.slices[task.name]
    return _checkpoint_integration_retry(
        state,
        task.name,
        invalidated,
        config,
        store,
        slice_update=replace(
            slice_transition(current, ActionChanged(None)),
            dispatch_last_boundary="integration_revalidation",
            dispatch_error=reason[:500],
        ),
    )


def _handle_integration_conflict(
    task: SubTLTask,
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
    *,
    base_sha: str,
    head_sha: str,
    reason: str,
) -> RunState:
    current = state.slices[task.name]
    if (
        current.repair_attempts >= config.max_integration_repairs
        or config.review_model_choice is None
    ):
        return _open_integration_gate(
            task,
            state,
            config,
            effects,
            store,
            effects_log,
            gate_name=INTEGRATION_CONFLICT_GATE_NAME,
            lifecycle=IntegrationLifecycle.INTEGRATION_CONFLICT,
            reason=reason,
        )
    candidate_runtime = _candidate_runtime(state.integration, task.name)
    conflict = replace(
        candidate_runtime,
        lifecycle=IntegrationLifecycle.INTEGRATION_CONFLICT,
        head_sha=head_sha,
        patch_digest=current.review_patch_digests.get(head_sha),
        validated_base_sha=base_sha,
        merge_tree_sha=None,
        ci_status="failure",
        integration_evidence_at=None,
        stage_verification="failed",
        merge_attempts=candidate_runtime.merge_attempts + 1,
    )
    conflicted = slice_transition(current, RepairQueued())
    conflicted = replace(
        conflicted,
        dispatch_last_boundary="integration_conflict",
        dispatch_error=reason[:500],
    )
    state = store.checkpoint(
        _phase_from_state(state),
        {**state.slices, task.name: conflicted},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=_persist_candidate_runtime(state.integration, task.name, conflict),
    )
    _record_controller_event(
        task.name,
        "tl.integration_conflict",
        {
            "slice_id": task.name,
            "pr_number": current.pr_number,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "reason": reason[:500],
            "repair_attempt": current.repair_attempts + 1,
        },
        config,
        effects,
        effects_log,
    )
    _record_controller_event(
        task.name,
        "tl.integration_repair_requested",
        {
            "slice_id": task.name,
            "pr_number": current.pr_number,
            "owner_id": current.dispatch_agent_id,
            "reason": reason[:500],
            "repair_attempt": current.repair_attempts + 1,
        },
        config,
        effects,
        effects_log,
    )
    repairing = replace(
        conflict,
        lifecycle=IntegrationLifecycle.REPAIRING_AGGREGATE,
        head_sha=None,
        patch_digest=None,
        validated_base_sha=None,
    )
    state = store.checkpoint(
        _phase_from_state(state),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=repairing,
    )
    return _route_repair(
        store,
        state,
        _phase_from_state(state),
        state.events.last_consumed_offset,
        task.name,
        {
            "verdict": Verdict.NO_GO.value,
            "reasons": [
                {
                    "severity": "blocking",
                    "file": "integration",
                    "line": 0,
                    "claim": reason[:500],
                }
            ],
        },
        config,
        effects,
        effects_log,
    )


def _integrate_one_candidate(
    task: SubTLTask,
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
) -> RunState:
    """Validate one aggregate candidate, recheck its base, then merge it."""
    current = state.slices[task.name]
    candidate_runtime = _candidate_runtime(state.integration, task.name)
    first: Mapping[str, object] | None = None
    if candidate_runtime.lifecycle is IntegrationLifecycle.MERGING:
        first = _watcher_snapshot(current.pr_number, config, effects, effects_log)
        if first is None:
            return state
        if _snapshot_bool(first, "merged"):
            _record_controller_event(
                task.name,
                "tl.merge_reconciled",
                {
                    "slice_id": task.name,
                    "pr_number": current.pr_number,
                    "head_sha": candidate_runtime.head_sha,
                    "reconciliation": "authoritative_merge_reconciled",
                },
                config,
                effects,
                effects_log,
            )
            return _checkpoint_aggregate_merged(
                task, state, store, candidate_runtime, config, effects, effects_log
            )
        # A successful merge response is not terminal. Keep the action in
        # flight and wait for an authoritative merged observation instead of
        # issuing a second request while the remote operation settles.
        return state
    first = first or _watcher_snapshot(current.pr_number, config, effects, effects_log)
    if first is None:
        return state
    if _snapshot_bool(first, "merged"):
        merged_head = _snapshot_text(first, "head_sha") or candidate_runtime.head_sha
        merged_patch = (
            _snapshot_text(first, "patch_digest")
            or candidate_runtime.patch_digest
            or current.review_patch_digests.get(merged_head or "")
        )
        merged_base = _snapshot_text(first, "base_sha") or candidate_runtime.validated_base_sha
        reconciled = replace(
            candidate_runtime,
            lifecycle=IntegrationLifecycle.MERGED,
            head_sha=merged_head,
            patch_digest=merged_patch,
            validated_base_sha=merged_base,
            merge_tree_sha=(
                _snapshot_text(first, "merge_tree_sha") or candidate_runtime.merge_tree_sha
            ),
            ci_status=_snapshot_text(first, "ci_status") or candidate_runtime.ci_status,
            stage_verification=candidate_runtime.stage_verification or "passed",
        )
        _record_controller_event(
            task.name,
            "tl.merge_reconciled",
            {
                "slice_id": task.name,
                "pr_number": current.pr_number,
                "head_sha": merged_head,
                "reconciliation": "unexpected_external_merge",
            },
            config,
            effects,
            effects_log,
        )
        return _checkpoint_aggregate_merged(
            task, state, store, reconciled, config, effects, effects_log
        )
    head_sha = _snapshot_text(first, "head_sha")
    base_sha = _snapshot_text(first, "base_sha")
    patch_digest = _snapshot_text(first, "patch_digest") or current.review_patch_digests.get(
        current.reviewed_head or ""
    )
    merge_tree_sha = _snapshot_text(first, "merge_tree_sha")
    ci_status = _snapshot_text(first, "ci_status")
    if not all((head_sha, base_sha, patch_digest, merge_tree_sha, ci_status)):
        return state
    try:
        verify_review(current, head_sha, current_patch_digest=patch_digest)
    except ReviewGateError:
        return state
    integration = replace(
        candidate_runtime,
        lifecycle=IntegrationLifecycle.INTEGRATION_VALIDATED,
        head_sha=head_sha,
        patch_digest=patch_digest,
        validated_base_sha=base_sha,
        merge_tree_sha=merge_tree_sha,
        ci_status=ci_status,
        stage_verification="passed",
        integration_evidence_at=_now_timestamp(),
    )
    state = store.checkpoint(
        _phase_from_state(state),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=_persist_candidate_runtime(state.integration, task.name, integration),
    )
    _record_controller_event(
        task.name,
        "tl.integration_validated",
        {
            "slice_id": task.name,
            "pr_number": current.pr_number,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "merge_tree_sha": merge_tree_sha,
            "ci_status": ci_status,
        },
        config,
        effects,
        effects_log,
    )
    second = _watcher_snapshot(current.pr_number, config, effects, effects_log)
    if second is None:
        return state
    if _snapshot_text(second, "base_sha") != base_sha:
        return _handle_external_base_change(
            task,
            state,
            config,
            effects,
            store,
            effects_log,
            base_sha=base_sha,
            head_sha=head_sha,
            patch_digest=patch_digest,
            reason=(
                f"aggregate base changed from {base_sha!r} to "
                f"{_snapshot_text(second, 'base_sha')!r} before merge"
            ),
        )
    live_head_sha = _snapshot_text(second, "head_sha")
    live_patch_digest = _snapshot_text(second, "patch_digest")
    live_merge_tree_sha = _snapshot_text(second, "merge_tree_sha")
    live_ci_status = _snapshot_text(second, "ci_status")
    if not all((live_head_sha, live_patch_digest, live_merge_tree_sha, live_ci_status)):
        return state
    try:
        verify_integration(
            integration,
            base_sha=base_sha,
            head_sha=live_head_sha,
            patch_digest=live_patch_digest,
            merge_tree_sha=live_merge_tree_sha,
            ci_status=live_ci_status,
        )
    except IntegrationEvidenceMismatch as error:
        if error.field in {"head_sha", "patch_digest"}:
            return _handle_integration_conflict(
                task,
                state,
                config,
                effects,
                store,
                effects_log,
                base_sha=base_sha,
                head_sha=live_head_sha,
                reason=str(error),
            )
        return _handle_integration_revalidation(
            task,
            state,
            config,
            effects,
            store,
            effects_log,
            base_sha=base_sha,
            head_sha=live_head_sha,
            patch_digest=live_patch_digest,
            reason=str(error),
        )
    _record_controller_event(
        task.name,
        "tl.integration_revalidated",
        {
            "slice_id": task.name,
            "pr_number": current.pr_number,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "merge_tree_sha": merge_tree_sha,
            "ci_status": ci_status,
        },
        config,
        effects,
        effects_log,
    )
    lane_base_sha = base_sha
    state, lane_ready = _ensure_candidate_lane(state, current, lane_base_sha, store)
    if not lane_ready:
        return state
    state, lane_ready = _start_candidate_lane(state, current, live_head_sha, store)
    if not lane_ready:
        return state
    merge_arguments = {
        "pr_number": current.pr_number,
        "strategy": config.merge_strategy or task.integration.merge_strategy,
        "working_dir": config.working_dir,
        "base_sha": base_sha,
    }
    merge_intent_id = stable_action_key(state.run_id, "merge_pr", task.name, merge_arguments)
    merge_action = ActionState(
        ActionKind.MERGE,
        ActionPhase.IN_FLIGHT,
        intent_id=merge_intent_id,
        head_sha=head_sha,
        attempt=max(1, current.attempts),
    )
    merge_slice = slice_transition(current, ActionChanged(merge_action))
    state = store.checkpoint(
        _phase_from_state(state),
        {**state.slices, task.name: merge_slice},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=_persist_candidate_runtime(
            state.integration,
            task.name,
            replace(integration, lifecycle=IntegrationLifecycle.MERGING),
        ),
    )
    _emit_merge_decision(
        task.name,
        current.pr_number,
        "merge",
        head_sha,
        config,
        effects,
        effects_log,
    )
    merge_result = _invoke(
        "merge_pr",
        task.name,
        merge_arguments,
        config.active,
        cast(EffectClient, effects),
        lambda client: client.merge_pr(
            pr_number=current.pr_number or 0,
            strategy=config.merge_strategy or task.integration.merge_strategy,
            working_dir=config.working_dir,
            expected_base_sha=base_sha,
            expected_head_sha=head_sha,
            expected_patch_digest=patch_digest,
            expected_merge_tree_sha=merge_tree_sha,
        ),
        effects_log,
        raise_on_failure=False,
    )
    failure = _merge_failure_classification(merge_result)
    if _merge_result_is_authoritative(merge_result):
        return _checkpoint_aggregate_merged(
            task, state, store, integration, config, effects, effects_log
        )
    if failure == "conflict":
        return _handle_integration_conflict(
            task,
            state,
            config,
            effects,
            store,
            effects_log,
            base_sha=base_sha,
            head_sha=head_sha,
            reason=_merge_failure_reason(merge_result),
        )
    if failure == "base_changed":
        return _handle_external_base_change(
            task,
            state,
            config,
            effects,
            store,
            effects_log,
            base_sha=base_sha,
            head_sha=head_sha,
            patch_digest=patch_digest,
            reason=_merge_failure_reason(merge_result),
        )
    if failure is not None:
        raise EffectFailed(f"merge_pr for {task.name!r}: {_merge_failure_reason(merge_result)}")
    return state


def _checkpoint_aggregate_merged(
    task: SubTLTask,
    state: RunState,
    store: RunStore,
    integration: IntegrationRuntimeState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    """Persist one merge result, including a restart reconciliation result."""
    current = state.slices[task.name]
    previous_slices = state.slices
    updated_slices = dict(state.slices)
    aggregate_pr_number = current.pr_number
    if type(aggregate_pr_number) is not int or aggregate_pr_number <= 0:
        return _block_post_merge_recovery(
            store,
            state,
            task.name,
            f"aggregate merge for {task.name!r} has no authoritative PR number",
        )
    if current.action is None or not current.action.intent_id:
        return _block_post_merge_recovery(
            store,
            state,
            task.name,
            f"aggregate merge for {task.name!r} has no durable merge intent",
        )
    aggregate_merge_key = current.action.intent_id
    aggregate_head = integration.aggregate_head_sha or integration.head_sha
    aggregate_base = integration.validated_base_sha or integration.aggregate_original_base_sha
    if not aggregate_head or not aggregate_base:
        return _block_post_merge_recovery(
            store,
            state,
            task.name,
            f"aggregate merge for {task.name!r} has incomplete merge evidence",
        )
    state, lane_ready = _ensure_candidate_lane(state, current, aggregate_base, store)
    if not lane_ready:
        return state
    state, lane_ready = _start_candidate_lane(state, current, aggregate_head, store)
    if not lane_ready:
        return state
    current = state.slices[task.name]
    try:
        merge_evidence = {
            "head_sha": aggregate_head,
            "base_sha": aggregate_base,
            "repository": _repository_identity(state),
            "parent_branch": _publication_parent_branch_from_slice(current),
            "lane_epoch": state.integration.lanes[
                f"{_repository_identity(state)}:{_publication_parent_branch_from_slice(current)}"
            ].lane_epoch,
        }
    except ValueError as error:
        return _block_post_merge_recovery(
            store,
            state,
            task.name,
            f"cannot adopt aggregate merge for {task.name!r}: {error}",
        )
    updated_slices[task.name] = _adopt_post_merge_slice(
        current,
        state,
        aggregate_pr_number,
        aggregate_merge_key,
        "aggregate_merged",
        merge_evidence,
    )
    sub_states = dict(state.integration.sub_tl_states)
    sub_states[task.name] = IntegrationLifecycle.MERGED
    candidate_records = dict(state.integration.candidates)
    for sibling in state.ordered_stages:
        if task.name not in sibling.sub_tls:
            continue
        for sibling_id in sibling.sub_tls:
            if (
                sibling_id != task.name
                and updated_slices[sibling_id].status is SliceStatus.IN_REVIEW
            ):
                sub_states[sibling_id] = IntegrationLifecycle.NEEDS_BASE_REVALIDATION
                sibling_runtime = _candidate_runtime(state.integration, sibling_id)
                candidate_records[sibling_id] = IntegrationCandidateState(
                    lifecycle=IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
                    aggregate_pr_number=sibling_runtime.aggregate_pr_number,
                    aggregate_head_sha=sibling_runtime.aggregate_head_sha,
                    aggregate_patch_digest=sibling_runtime.aggregate_patch_digest,
                    aggregate_original_base_sha=sibling_runtime.aggregate_original_base_sha,
                    integration_owner_id=sibling_runtime.integration_owner_id,
                    integration_owner_run_id=sibling_runtime.integration_owner_run_id,
                    integration_owner_branch=sibling_runtime.integration_owner_branch,
                    integration_owner_worktree=sibling_runtime.integration_owner_worktree,
                    head_sha=sibling_runtime.head_sha,
                    patch_digest=sibling_runtime.patch_digest,
                    validated_base_sha=None,
                    merge_tree_sha=None,
                    integration_evidence_at=None,
                    ci_status="unknown",
                    merge_attempts=sibling_runtime.merge_attempts,
                    base_revalidation_count=sibling_runtime.base_revalidation_count,
                    stage_verification="pending",
                )
    merged_integration = replace(
        integration,
        lifecycle=IntegrationLifecycle.MERGED,
        aggregate_pr_number=integration.aggregate_pr_number or current.pr_number,
        aggregate_head_sha=integration.aggregate_head_sha
        or integration.head_sha
        or current.reviewed_head,
        aggregate_patch_digest=(
            integration.aggregate_patch_digest
            or integration.patch_digest
            or current.review_patch_digests.get(current.reviewed_head or "")
        ),
        integration_owner_id=(
            integration.integration_owner_id
            or current.dispatch_agent_id
            or f"{store.run_id}:{task.name}:integration"
        ),
        integration_owner_run_id=integration.integration_owner_run_id or task.name,
        integration_owner_branch=(
            integration.integration_owner_branch
            or current.branch
            or derive_child_branch(current.base_ref or "main", task.name)
        ),
        integration_owner_worktree=(
            integration.integration_owner_worktree
            or current.worktree
            or str(store.root_dir / task.name)
        ),
        merge_attempts=integration.merge_attempts + 1,
        integration_evidence_at=integration.integration_evidence_at or _now_timestamp(),
        stage_verification="passed",
    )
    persisted_merged = _persist_candidate_runtime(state.integration, task.name, merged_integration)
    remaining = {
        sibling_id: ChildHandle(
            sibling_id,
            sibling_slice.branch or "",
            "sub-tl",
        )
        for sibling_id, sibling_slice in updated_slices.items()
        if sibling_slice.status
        in {
            SliceStatus.SPAWNED,
            SliceStatus.IN_REVIEW,
            SliceStatus.REPAIRING,
        }
        or (
            sibling_slice.status is SliceStatus.MERGED
            and (
                sibling_slice.post_merge is None
                or sibling_slice.post_merge.phase is not PostMergePhase.COMPLETE
            )
        )
    }
    checkpoint_phase: PhaseValue = TLWaiting(remaining) if remaining else TLPlanning()
    checkpointed = store.checkpoint(
        checkpoint_phase,
        updated_slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=replace(
            persisted_merged,
            sub_tl_states=sub_states,
            candidates=candidate_records,
        ),
    )
    _emit_slice_status_changes(previous_slices, checkpointed.slices, config, effects, effects_log)
    return _drain_post_merge_recovery(
        checkpointed,
        task.name,
        aggregate_pr_number,
        config,
        effects,
        effects_log,
        store,
    )


def _checkpoint_integration_retry(
    state: RunState,
    slice_id: str,
    integration: IntegrationRuntimeState,
    config: TLLoopConfig,
    store: RunStore,
    *,
    slice_update: SliceState | None = None,
) -> RunState:
    """Persist a base retry without changing the candidate head or sibling slices."""
    del config
    sub_states = dict(state.integration.sub_tl_states)
    sub_states[slice_id] = IntegrationLifecycle.NEEDS_BASE_REVALIDATION
    persisted = _persist_candidate_runtime(state.integration, slice_id, integration)
    slices = dict(state.slices)
    if slice_update is not None:
        slices[slice_id] = slice_update
    return store.checkpoint(
        _phase_from_state(state),
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=replace(persisted, sub_tl_states=sub_states),
    )


def _resolve_repository_identity(
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RepositoryIdentity | None:
    """Resolve repository identity once from the pinned git remote.

    A single read-only effect call through the same EffectJournal executor as
    every other effect -- never the watcher, since identity is static run
    configuration (owner, repo, base branch), not a per-PR observation.
    Returns None on any failure so the caller opens a durable gate instead of
    raising or guessing an identity (#1062).
    """
    result = _invoke(
        "repository_identity",
        "repository_identity",
        {},
        config.active,
        cast(EffectClient, effects),
        lambda client: client.repository_identity(),
        effects_log,
        raise_on_failure=False,
    )
    if result is None or result.success is not True or not isinstance(result.result, Mapping):
        return None
    payload = result.result
    owner = payload.get("owner")
    repo = payload.get("repo")
    base_branch = payload.get("base_branch")
    if not isinstance(owner, str) or not owner:
        return None
    if not isinstance(repo, str) or not repo:
        return None
    if not isinstance(base_branch, str) or not base_branch:
        return None
    forge_host = payload.get("forge_host")
    remote_url = payload.get("remote_url")
    try:
        identity = RepositoryIdentity(
            owner=owner,
            repo=repo,
            base_branch=base_branch,
            forge_host=forge_host if isinstance(forge_host, str) and forge_host else None,
            remote_url=remote_url if isinstance(remote_url, str) and remote_url else None,
        )
    except ValueError:
        return None
    LOGGER.info(
        "[TL loop] Resolved repository identity owner=%s repo=%s base_branch=%s",
        owner,
        repo,
        base_branch,
    )
    return identity


def _watcher_snapshot(
    pr_number: int,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> WatcherObservation | None:
    """Read one live PR/base snapshot through the parent-owned watcher."""
    result = _invoke(
        "watcher_pr_state",
        str(pr_number),
        {"pr_number": pr_number},
        config.active,
        cast(EffectClient, effects),
        lambda client: client.watcher_pr_state(pr_number=pr_number),
        effects_log,
    )
    if result is None or result.success is not True or not isinstance(result.result, Mapping):
        return None
    return WatcherObservation.from_response(result.result)


def _watcher_snapshot_for_slice(
    slice_id: str,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> WatcherObservation | None:
    """Resolve a live PR identity, then read its Forgejo facts."""
    result = _invoke(
        "resolve_live_pr_for_slice",
        f"slice:{slice_id}",
        {"slice_id": slice_id},
        config.active,
        cast(EffectClient, effects),
        lambda client: client.resolve_live_pr_for_slice(slice_id=slice_id),
        effects_log,
        raise_on_failure=False,
    )
    if result is None or result.success is not True or not isinstance(result.result, Mapping):
        return None
    resolution = result.result.get("resolution")
    if resolution == "never_published":
        return None
    if resolution != "live":
        return WatcherObservation.from_response(
            {
                "found": False,
                "publication_ownership_verified": False,
                "publication_ownership_error": result.result.get("error")
                or f"slice publication resolution is {resolution!r}",
            }
        )
    pr_number = result.result.get("pr_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        return None
    snapshot = _watcher_snapshot(pr_number, config, effects, effects_log)
    publication = result.result.get("publication")
    if snapshot is not None and isinstance(publication, Mapping):
        return snapshot.with_publication(publication)
    return snapshot


def _as_watcher_observation(
    snapshot: WatcherObservation | Mapping[str, object] | None,
) -> WatcherObservation | None:
    if snapshot is None or isinstance(snapshot, WatcherObservation):
        return snapshot
    return WatcherObservation.from_response(snapshot)


def _snapshot_text(snapshot: WatcherObservation | Mapping[str, object], key: str) -> str | None:
    observation = _as_watcher_observation(snapshot)
    assert observation is not None
    value = getattr(observation, key, None)
    return value if isinstance(value, str) and value else None


def _publication_evidence(
    watcher: WatcherObservation | Mapping[str, object],
) -> object | None:
    observation = _as_watcher_observation(watcher)
    return observation.publication if observation is not None else None


def _publication_record_text(
    watcher: WatcherObservation | Mapping[str, object] | object | None,
    key: str,
) -> str | None:
    if watcher is None:
        return None
    record = watcher if hasattr(watcher, key) else _publication_evidence(watcher)
    if record is None:
        return None
    value = getattr(record, key, None)
    return value if isinstance(value, str) and value else None


def _handoff_reconciliation_event_payload(
    current: SliceState,
    reconciled: SliceState,
    watcher: WatcherObservation | Mapping[str, object],
    owner_id: str | None,
) -> dict[str, object] | None:
    watcher = _as_watcher_observation(watcher)
    assert watcher is not None
    if watcher.found is not True:
        return None
    if watcher.publication_ownership_verified is not True:
        _, ownership_reason = _publication_ownership_status(watcher)
        return {
            "slice_id": current.id,
            "pr_number": watcher.pr_number or 0,
            "head_sha": _snapshot_text(watcher, "head_sha") or "",
            "invocation_id": "",
            "outcome": "skipped",
            "reason": watcher.publication_ownership_error
            or ownership_reason
            or "publication_ownership_unverified",
            "source": "host_publication",
        }
    publication = reconciled.publication or current.publication
    head_sha = _snapshot_text(watcher, "head_sha") or (
        publication.head_sha if publication is not None else ""
    )
    pr_number = (
        reconciled.pr_number
        or (publication.pr_number if publication is not None else None)
        or watcher.pr_number
    )
    handoff = reconciled.handoff
    if handoff is not None:
        return {
            "slice_id": current.id,
            "pr_number": handoff.pr_number,
            "head_sha": handoff.head_sha,
            "invocation_id": handoff.invocation_id,
            "outcome": "backfilled",
            "reason": "",
            "source": "host_publication",
        }
    record = _publication_evidence(watcher)
    expected_owner = current.dispatch_agent_id or owner_id
    record_slice = _publication_record_text(record, "slice_id")
    record_owner = _publication_record_text(record, "author_agent")
    if record_slice is not None and record_slice != current.id:
        reason = f"slice_id_mismatch:{record_slice}"
    elif record_owner is not None and expected_owner is not None and record_owner != expected_owner:
        reason = f"owner_agent_mismatch:{record_owner}"
    else:
        missing = [
            name
            for name, value in (
                ("head_sha", head_sha),
                ("pr_number", pr_number),
                ("owner_agent_id", expected_owner),
                ("invocation_id_provenance", _publication_record_text(record, "invocation_id")),
            )
            if not value
        ]
        reason = "missing " + ", ".join(missing) if missing else "publication_identity"
    return {
        "slice_id": current.id,
        "pr_number": pr_number or 0,
        "head_sha": head_sha,
        "invocation_id": "",
        "outcome": "skipped",
        "reason": reason,
        "source": "host_publication",
    }


def _snapshot_bool(snapshot: Mapping[str, object], key: str) -> bool:
    value = snapshot.get(key)
    return value is True or value == "true"


def _now_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _review_event_submitted_at(event: EventEnvelope) -> str:
    """Prefer the immutable Forgejo submission timestamp over observation time."""
    for key in ("review_submitted_at", "submitted_at"):
        value = event.data.get(key)
        if isinstance(value, str) and value:
            return value
    return event.observed_at


def _child_has_aggregate_output(state: RunState) -> bool:
    """Require a child result with a reviewable head before opening an aggregate PR."""
    if state.integration.aggregate_pr_number is not None:
        return True
    return any(
        slice_state.pr_number is not None or slice_state.reviewed_head is not None
        for slice_state in state.slices.values()
    )


def _child_head_sha(state: RunState, branch: str) -> str:
    for slice_state in state.slices.values():
        if slice_state.reviewed_head:
            return slice_state.reviewed_head
    return hashlib.sha256(f"{branch}:{state.revision}".encode()).hexdigest()


def _child_patch_digest(state: RunState) -> str:
    values = sorted(
        f"{slice_state.id}:{slice_state.pr_number or ''}:{slice_state.reviewed_head or ''}"
        for slice_state in state.slices.values()
    )
    return hashlib.sha256("|".join(values).encode()).hexdigest()


def _persist_non_root_handoff(
    state: RunState,
    store: RunStore,
    integration: IntegrationRuntimeState,
    candidate: AggregateCandidate,
    *,
    parent_branch: str,
) -> None:
    """Durably finish a non-root scope after its parent publishes its candidate."""
    phase = state.recursive_fsm
    manifest = state.plan_manifest
    if manifest is None or manifest.role != "non_root":
        return
    if isinstance(phase, RecursiveTLPRFiled):
        return
    if not isinstance(phase, (RecursiveTLAllMerged, RecursiveTLFinalizing)):
        return
    owner_id = integration.integration_owner_id
    if not owner_id:
        raise TLLoopError("non-root aggregate handoff is missing its durable owner identity")
    evidence = {
        "aggregate_pr": str(candidate.pr_number),
        "head_sha": candidate.head_sha,
        "base_sha": candidate.original_base_sha or parent_branch,
        "parent_branch": parent_branch,
        "handoff": owner_id,
    }
    if isinstance(phase, RecursiveTLAllMerged):
        finalizing = scope_transition(phase, ScopeFinalizationRequested(ScopeRole.NON_ROOT))
        finalizing = replace(finalizing, evidence=evidence)
        state = _checkpoint_scope_phase(finalizing, state, store)
    else:
        finalizing = phase
        if not finalizing.evidence:
            finalizing = replace(finalizing, evidence=evidence)
            state = _checkpoint_scope_phase(finalizing, state, store)
    filed = scope_transition(
        finalizing,
        ScopeFinalizationComplete(ScopeRole.NON_ROOT, finalizing.evidence),
    )
    _checkpoint_scope_phase(filed, state, store)


def _positive_result_int(value: Mapping[str, object], key: str) -> int | None:
    candidate = value.get(key)
    return candidate if type(candidate) is int and candidate > 0 else None


def _result_text(value: Mapping[str, object], key: str) -> str | None:
    candidate = value.get(key)
    return candidate if isinstance(candidate, str) and candidate else None


def _fail_recursive_parent(
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
    reason: str,
) -> RunState:
    """Stop stage advancement while retaining completed sibling results."""
    before_phase = _phase_from_state(state)
    state = store.checkpoint(
        _failure_phase(state, reason),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )
    _emit_phase_change(
        store.run_id,
        before_phase,
        _phase_from_state(state),
        config,
        effects,
        effects_log,
    )
    return state


def _validate_stage_event_routes(tasks: Sequence[SubTLTask]) -> None:
    """Require independent cursors for concurrent children that consume events."""
    consuming = [task for task in tasks if _plan_consumes_events(task.plan)]
    if len(consuming) < 2:
        return
    if any(task.source is None for task in consuming):
        raise TLLoopError("same-order sub-TLs that consume events require isolated sources")
    if len({id(task.source) for task in consuming}) != len(consuming):
        raise TLLoopError("same-order sub-TLs cannot share an event source")


def _plan_consumes_events(plan: WorkPlan | Mapping[str, object]) -> bool:
    """Return whether a child can consume a projected event stream."""
    if isinstance(plan, WorkPlan):
        return bool(plan.workers or plan.leaves or plan.sub_tls)
    return bool(plan.get("workers") or plan.get("leaves") or plan.get("sub_tls"))


def _child_config(
    config: TLLoopConfig,
    task: SubTLTask,
    source: EventQueue,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    branch: str,
    worktree: str,
) -> TLLoopConfig:
    child_effects = _owner_effect_client(effects, task.agent_id or task.name)
    return replace(
        config,
        source=task.source or source,
        effects=task.effects or child_effects,
        root_dir=store.run_dir,
        run_id=task.name,
        branch=branch,
        worktree=worktree,
        parent_branch=config.branch,
        parent_run_id=store.run_id,
        parent_agent_id=config.agent_id or store.run_id,
        agent_id=task.agent_id or task.name,
        working_dir=worktree,
        depth=config.depth + 1,
        dispatch_names={},
        keep_alive_on_waiting=False,
    )


def _owner_effect_client(
    effects: EffectClient | ReadOnlyEffectClient, owner_name: str
) -> EffectClient | ReadOnlyEffectClient:
    """Bind live effect calls to the persistent child controller identity."""
    if not isinstance(effects, EffectClient):
        return effects
    return EffectClient(effects.transport, role=effects.role, name=owner_name)


def _park_schedule_deadlock(
    error: ScheduleDeadlock,
    state: RunState,
    config: TLLoopConfig,
    live: EffectClient | None,
    store: RunStore,
) -> None:
    if not config.active:
        return
    if live is None:
        raise TLLoopError("active loop has no effect client for escalation")
    blocked_id = error.blocked_slices[0]
    slice_state = state.slices.get(blocked_id)
    if slice_state is None:
        raise TLLoopError(f"deadlock references missing slice {blocked_id!r}")
    park(
        slice_state,
        ParkCause.SCHEDULE_DEADLOCK,
        store=store,
        issue_creator=live,
        ledger=state.budgets,
    )


def _can_dispatch(name: str, state: RunState, config: TLLoopConfig) -> bool:
    if config.policy is None or config.max_parallel_slices is None:
        return True
    return name in {
        slice_state.id for slice_state in ready(state.slices, config.max_parallel_slices)
    }


def _already_dispatched(name: str, state: RunState) -> bool:
    current = state.slices.get(name)
    return current is not None and current.status is not SliceStatus.PENDING


def _prepare_spawn(
    name: str,
    state: RunState,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    effects_log: list[EffectIntent],
) -> DispatchAttempt:
    intent = _new_dispatch_attempt(state, name, config)
    if config.policy is None:
        current = state.slices.get(name)
        if current is None:
            raise TLLoopError(f"dispatch slice {name!r} is missing from run state")
        updated = slice_transition(current, SliceStatusChanged(SliceStatus.DISPATCHING))
        updated = replace(
            updated,
            attempts=current.attempts + 1,
            dispatch_intent_id=intent.intent_id,
            dispatch_started_at=intent.started_at,
            dispatch_last_boundary="dispatch_intended",
            dispatch_error=None,
            dispatch_agent_id=None,
            dispatch_invocation_id=None,
            dispatch_authoritative_event_seq=None,
            park_cause=None,
        )
        state = store.checkpoint(
            state.fsm,
            {**state.slices, name: updated},
            state.budgets,
            state.events.last_consumed_offset,
        )
        _record_dispatch_intent(name, intent, config, effects, effects_log)
        return intent
    slice_state = state.slices.get(name)
    if slice_state is None:
        raise TLLoopError(f"selector slice {name!r} is missing from run state")
    capabilities = config.capabilities or load_capability()
    choice = select_agent_type(
        slice_state,
        config.role,
        state.budgets,
        config.policy,
        capabilities,
        config.learned_policy,
    )
    if choice is None:
        failure = selection_failure(
            slice_state, config.role, state.budgets, config.policy, capabilities
        )
        cause = {
            "over_budget": ParkCause.BUDGET_EXHAUSTED,
            "no_capable_harness": ParkCause.NO_CAPABLE_HARNESS,
        }.get(failure.value)
        if cause is None:
            raise TLLoopError(f"cannot select harness for {name!r}: {failure.value}")
        if config.active:
            live = cast(EffectClient, effects)
            park(
                slice_state,
                cause,
                store=store,
                issue_creator=live,
                ledger=state.budgets,
            )
        raise TLLoopError(f"cannot select harness for {name!r}: {failure.value}; slice parked")
    route = parse_harness_identifier(choice.harness)
    if config.catalog is not None:
        if config.requested_model:
            model_id = select_model(choice.harness, config.catalog, config.requested_model).model_id
        else:
            model_id = select_model_for_difficulty(
                choice.harness,
                config.catalog,
                choice.difficulty,
                escalated=choice.reason == "escalated_after_no_go",
            ).model_id
    else:
        model_id = route.model

    intent = DispatchAttempt(
        intent.intent_id,
        intent.started_at,
        choice.harness,
        route.agent_type,
        model_id,
        intent.attempt,
    )

    def record_spawn(document: dict[str, object]) -> dict[str, object]:
        slices = document.get("slices")
        if not isinstance(slices, dict):
            raise TLLoopError("run state slices are not an object")
        raw_slice = slices.get(name)
        if not isinstance(raw_slice, dict):
            raise TLLoopError(f"selector slice {name!r} is not an object")
        dispatched = slice_transition(slice_state, SliceStatusChanged(SliceStatus.DISPATCHING))
        raw_slice.update(
            {
                "status": dispatched.status.value,
                "agent_type": route.agent_type,
                "model": model_id,
                "attempts": slice_state.attempts + 1,
                "dispatch_intent_id": intent.intent_id,
                "dispatch_started_at": intent.started_at,
                "dispatch_last_boundary": "dispatch_intended",
                "dispatch_error": None,
                "dispatch_agent_id": None,
                "dispatch_invocation_id": None,
                "dispatch_authoritative_event_seq": None,
                "park_cause": None,
            }
        )
        return document

    apply_spawn_and_charge(store.run_dir, choice, slice_state, record_spawn)
    LOGGER.info(
        "[TL loop] selection target=%s harness=%s model=%s estimate=%d",
        name,
        choice.harness,
        model_id or "unresolved",
        choice.estimated_cost,
    )
    _record_dispatch_intent(name, intent, config, effects, effects_log)
    return intent


def _new_dispatch_attempt(state: RunState, name: str, config: TLLoopConfig) -> DispatchAttempt:
    current = state.slices.get(name)
    attempt = (current.attempts if current is not None else 0) + 1
    # Keep the public intent stable for legacy event readers. The controller
    # epoch and generation are part of the persisted dispatch payload and
    # journal key, so a recreated controller cannot adopt the old observation.
    identity = f"{state.run_id}:{name}:{attempt}"
    intent_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    started_at = time.time() if config.active else 0.0
    return DispatchAttempt(
        intent_id,
        started_at,
        "",
        attempt=attempt,
        controller_epoch=state.controller_epoch,
        dispatch_generation=attempt if state.controller_epoch is not None else 0,
    )


def _controller_epoch(root_dir: Path, run_id: str) -> str:
    """Read the init-owned epoch marker, with a deterministic first-run value."""
    marker = Path(root_dir) / f"{run_id}.controller-epoch"
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        value = ""
    if value:
        return value
    value = hashlib.sha256(f"controller:{run_id}".encode()).hexdigest()[:32]
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(value + "\n", encoding="utf-8")
    except OSError:
        LOGGER.warning("unable to persist controller epoch marker path=%s", marker)
    return value


def _worker_call(
    task: WorkerTask,
    selected_agent_type: str | None,
    selected_model: str | None,
    intent_id: str | None,
    runtime_name: str | None = None,
) -> Callable[[EffectClient], ToolResult]:
    def invoke(client: EffectClient) -> ToolResult:
        return client.spawn_worker(
            name=runtime_name or task.name,
            task=task.task,
            intent_id=intent_id,
            agent_type=selected_agent_type or task.agent_type,
            model=selected_model,
        )

    return invoke


def _leaf_call(
    task: LeafTask,
    selected_agent_type: str | None,
    selected_model: str | None,
    intent_id: str | None,
    runtime_name: str | None = None,
) -> Callable[[EffectClient], ToolResult]:
    def invoke(client: EffectClient) -> ToolResult:
        return client.spawn_leaf(
            name=runtime_name or task.name,
            task=task.task,
            intent_id=intent_id,
            agent_type=selected_agent_type or task.agent_type,
            model=selected_model,
            boundary=task.boundary,
            context=task.context,
            read_first=task.read_first,
            steps=task.steps,
            verify=task.verify,
        )

    return invoke


def _emit_merge_decision(
    slice_id: str,
    pr_number: int,
    decision: str,
    head_sha: str | None,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> None:
    _record_controller_event(
        "controller",
        "tl.merge_decided",
        {
            "slice_id": slice_id,
            "pr_number": pr_number,
            "decision": decision,
            "head_sha_hash": _hash_head_sha(head_sha),
        },
        config,
        effects,
        effects_log,
    )


def _hash_head_sha(head_sha: str | None) -> str:
    if head_sha is None:
        return "missing"
    return hashlib.sha256(head_sha.encode("utf-8")).hexdigest()


def _discard_review(slices: Mapping[str, SliceState], slice_id: str) -> dict[str, SliceState]:
    current = slices.get(slice_id)
    if current is None:
        return dict(slices)
    discarded = slice_transition(current, ReviewDiscarded())
    return {
        **slices,
        slice_id: discarded,
    }


def _record_review_event(
    store: RunStore,
    state: RunState,
    phase: PhaseValue,
    event: EventEnvelope,
    event_seq: int,
) -> RunState:
    slice_id = _review_slice_id(event, state)
    if slice_id is None:
        raise TLLoopError(f"{event.event_type!r} has no slice identity")
    current = state.slices.get(slice_id)
    if current is None:
        raise TLLoopError(f"review event references unknown slice {slice_id!r}")
    findings = _event_findings(event)
    if event.head_sha is None:
        if findings is not None:
            raise TLLoopError(f"{event.event_type!r} findings have no head SHA")
        return store.checkpoint(phase, state.slices, state.budgets, event_seq)
    review_findings = _review_findings(current, event.head_sha, findings)
    patch_digest = _event_patch_digest(event)
    patch_digests = dict(current.review_patch_digests)
    if patch_digest is not None:
        patch_digests[event.head_sha] = patch_digest
    stall_classification = _event_stall_classification(event)
    if current.reviewed_head is not None and current.reviewed_head != event.head_sha:
        updated = dict(state.slices)
        updated[slice_id] = replace(
            current,
            review_findings=review_findings,
            review_patch_digests=patch_digests,
        )
        return store.checkpoint(phase, updated, state.budgets, event_seq)
    verdict = _review_verdict(event)
    if verdict in {Verdict.GO, Verdict.GO_WITH_NITS} and not _reviewer_identity_authorized(
        event,
        current,
        strict=event.event_type == "pr.review" and "verdict" in event.data,
    ):
        LOGGER.warning(
            "[TL loop] ignoring unauthorized approval evidence target=%s agent=%s",
            slice_id,
            event.agent_id,
        )
        verdict = None
    if verdict is None:
        updated = dict(state.slices)
        updated[slice_id] = replace(
            slice_transition(
                current,
                StallClassificationObserved(stall_classification or current.stall_classification),
            ),
            pr_number=event.pr_number or current.pr_number,
            review_findings=review_findings,
            review_patch_digests=patch_digests,
        )
        return store.checkpoint(phase, updated, state.budgets, event_seq)
    updated = dict(state.slices)
    transitioned = slice_transition(
        current,
        ReviewVerdictObserved(
            head_sha=event.head_sha,
            verdict=verdict,
            reviewer_agent_id=current.reviewer_agent_id,
            review_id=event.data.get("review_id")
            if type(event.data.get("review_id")) is int
            else None,
            findings=tuple(findings or ()),
            source="ledger",
            pr_number=event.pr_number,
            reviewer_account_authenticated=event.data.get("reviewer_account_authenticated") is True,
            reviewer_identity_unresolved=event.data.get("reviewer_identity_unresolved")
            is not False,
            self_approval=(
                event.agent_id is not None and event.agent_id == current.dispatch_agent_id
            ),
            stall_classification=stall_classification or current.stall_classification,
            requires_authenticated_evidence=False,
            increment_round=not (
                current.reviewed_head == event.head_sha and current.verdict is not None
            ),
            next_status=(
                SliceStatus.IN_REVIEW
                if current.status is SliceStatus.REPAIRING and _is_aggregate_slice(current)
                else None
            ),
            submitted_at=_review_event_submitted_at(event),
            validated_at=(
                event.data.get("validated_at")
                if isinstance(event.data.get("validated_at"), str)
                else event.observed_at
            ),
            observed_at=event.observed_at,
            dismissed=event.data.get("dismissed") is True,
            forgejo_stale=event.data.get("forgejo_stale") is True,
        ),
    )
    updated[slice_id] = replace(
        transitioned,
        pr_number=event.pr_number or current.pr_number,
        verdict_at=event.observed_at,
        review_findings=review_findings,
        review_patch_digests=patch_digests,
    )
    return store.checkpoint(phase, updated, state.budgets, event_seq)


def _review_workflow_enabled(config: TLLoopConfig) -> bool:
    return config.active and config.review_model_choice is not None


def _reviewer_max_rounds(path: str | Path | None) -> int | None:
    """Load the configured review ceiling for controller convergence."""
    if path is None:
        return None
    return load_reviewer_max_rounds(path)


def _review_policy_for_state(
    state: RunState,
    path: str | Path | None,
    store: RunStore,
) -> ReviewPolicySnapshot:
    """Resolve review policy once, then reuse the persisted snapshot on restart."""
    if state.reviewer_max_rounds_source is not None:
        return ReviewPolicySnapshot(
            state.reviewer_max_rounds,
            state.reviewer_max_rounds_source,
        )
    resolved = load_reviewer_policy_snapshot(path)
    store.set_review_policy(resolved.reviewer_max_rounds, resolved.source)
    return resolved


def _reviewer_identity_authorized(
    event: EventEnvelope,
    current: SliceState,
    *,
    strict: bool = False,
) -> bool:
    """Accept authorized reviewer/human evidence, never a worker self-approval."""
    if strict:
        if current.pr_number is not None and event.pr_number != current.pr_number:
            return False
        if event.head_sha is None:
            return False
        if current.reviewed_head is not None and current.reviewed_head != event.head_sha:
            return False
        if current.handoff is not None and current.handoff.head_sha != event.head_sha:
            return False
        if (
            current.handoff is not None
            and current.pr_number is not None
            and current.handoff.pr_number != current.pr_number
        ):
            return False
        if not _review_evidence_matches_exact_head(event, event.head_sha):
            return False
        reviewer_agent_id = event.data.get("reviewer_agent_id")
        if current.reviewer_agent_id is not None:
            return reviewer_agent_id == current.reviewer_agent_id
        if current.reviewer_attempt.get(event.head_sha, 0) <= 0:
            return False
        return reviewer_agent_id != current.dispatch_agent_id
    actor_role = event.role or event.data.get("actor_role") or event.data.get("actor_type")
    if isinstance(actor_role, str):
        normalized = actor_role.strip().lower()
        if normalized in {"worker", "dev", "developer", "agent"}:
            return False
        if normalized in {"reviewer", "human", "operator"}:
            return True
    actor_id = event.agent_id
    owner_id = current.dispatch_agent_id
    if owner_id is not None and actor_id == owner_id:
        return False
    return actor_id is not None


def _review_evidence_matches_exact_head(event: EventEnvelope, head_sha: str) -> bool:
    """Validate watcher proof for one exact-head reviewer decision.

    ``reviewer_agent_id`` is the durable invocation assigned to the slice; it
    is not a Forgejo login.  The Rust watcher only emits
    ``reviewer_account_authenticated`` after the review author matched the
    configured shared reviewer token and the exact-head invocation lookup
    succeeded.  Keep that proof separate from the assignment binding.
    """
    if event.data.get("review_head_sha") != head_sha:
        return False
    review_id = event.data.get("review_id")
    if type(review_id) is not int or review_id <= 0:
        return False
    if "reviewer_agent_id" in event.data:
        reviewer_agent_id = event.data.get("reviewer_agent_id")
        if not isinstance(reviewer_agent_id, str) or not reviewer_agent_id:
            return False
    else:
        return False
    if event.data.get("reviewer_account_authenticated") is not True:
        return False
    if "reviewer_identity_error" in event.data and event.data.get("reviewer_identity_error"):
        return False
    return event.data.get("reviewer_identity_unresolved") is False


def _review_slice_id(event: EventEnvelope, state: RunState) -> str | None:
    return resolve_event_slice(event, state).slice_id


def _is_aggregate_slice(slice_state: SliceState) -> bool:
    """Identify a persisted sub-TL aggregate owner without trusting event text."""
    return slice_state.dispatch_agent_id is not None and (
        slice_state.dispatch_last_boundary
        in {
            "aggregate_pr_open",
            "integration_conflict",
            "integration_gate",
        }
        or slice_state.dispatch_agent_id.endswith(":integration")
    )


def _event_findings(event: EventEnvelope) -> list[dict[str, str]] | None:
    if "findings" not in event.data:
        return None
    raw_findings = event.data["findings"]
    if not isinstance(raw_findings, list):
        raise TLLoopError(f"{event.event_type!r} findings must be an array")
    findings: list[dict[str, str]] = []
    for index, raw_finding in enumerate(raw_findings):
        if not isinstance(raw_finding, Mapping):
            raise TLLoopError(f"{event.event_type!r} findings[{index}] must be an object")
        finding: dict[str, str] = {}
        for key in ("severity", "path", "rationale"):
            value = raw_finding.get(key)
            if not isinstance(value, str) or not value.strip():
                raise TLLoopError(f"{event.event_type!r} findings[{index}].{key} must be non-empty")
            finding[key] = value
        findings.append(finding)
    return findings


def _event_patch_digest(event: EventEnvelope) -> str | None:
    """Persist only a patch hash when the event carries patch material."""
    explicit = event.data.get("patch_digest")
    if isinstance(explicit, str) and explicit:
        return explicit
    patch = event.data.get("diff", event.data.get("patch"))
    if patch is None:
        return None
    return hashlib.sha256(repr(patch).encode("utf-8")).hexdigest()


def _review_findings(
    current: SliceState,
    head_sha: str,
    findings: list[dict[str, str]] | None,
) -> Mapping[str, tuple[Mapping[str, str], ...]]:
    updated = dict(current.review_findings)
    if findings is not None:
        updated[head_sha] = tuple(findings)
    return updated


def _event_stall_classification(event: EventEnvelope) -> str | None:
    classification = event.stall_classification
    return classification.value if classification is not None else None


def _route_review_event(
    plan: WorkPlan,
    store: RunStore,
    state: RunState,
    phase: PhaseValue,
    event: EventEnvelope,
    event_seq: int,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    slice_id = _review_slice_id(event, state)
    if slice_id is None:
        raise TLLoopError(f"{event.event_type!r} has no slice identity")
    current = state.slices.get(slice_id)
    if current is None:
        raise TLLoopError(f"review event references unknown slice {slice_id!r}")
    head_sha = event.head_sha
    findings = _event_findings(event)
    if head_sha is None:
        raise TLLoopError(f"{event.event_type!r} findings have no head SHA")
    incoming_verdict = _review_verdict(event)
    if (
        incoming_verdict is not None
        and current.reviewed_head == head_sha
        and current.verdict is not None
    ):
        incoming_review_id = event.data.get("review_id")
        existing_review_id = (
            current.review_evidence.review_id if current.review_evidence is not None else None
        )
        if existing_review_id is not None and not (
            type(incoming_review_id) is int and incoming_review_id > existing_review_id
        ):
            LOGGER.info(
                "[TL loop] ignoring repeated reviewer verdict target=%s head=%s existing=%s incoming=%s",
                slice_id,
                head_sha,
                current.verdict.value,
                incoming_verdict.value,
            )
            return state
    review_findings = _review_findings(current, head_sha, findings)
    patch_digest = _event_patch_digest(event)
    patch_digests = dict(current.review_patch_digests)
    if patch_digest is not None:
        patch_digests[head_sha] = patch_digest
    stall_classification = _event_stall_classification(event)
    if findings is None:
        LOGGER.warning(
            "[TL loop] ignoring review without binding findings target=%s head=%s",
            slice_id,
            head_sha,
        )
        updated = dict(state.slices)
        updated[slice_id] = replace(
            slice_transition(
                current,
                StallClassificationObserved(stall_classification or current.stall_classification),
            ),
            review_findings=review_findings,
            review_patch_digests=patch_digests,
        )
        return store.checkpoint(phase, updated, state.budgets, event_seq)
    if (
        current.reviewed_head is not None
        and current.reviewed_head != head_sha
        and not _is_aggregate_slice(current)
    ):
        LOGGER.warning(
            "[TL loop] ignoring stale review target=%s reviewed=%s event=%s",
            slice_id,
            current.reviewed_head,
            head_sha,
        )
        updated = dict(state.slices)
        updated[slice_id] = replace(
            current,
            review_findings=review_findings,
            review_patch_digests=patch_digests,
        )
        return store.checkpoint(phase, updated, state.budgets, event_seq)
    leaf = next((candidate for candidate in plan.leaves if candidate.name == slice_id), None)
    if leaf is None and not _is_aggregate_slice(current):
        raise TLLoopError(f"review event references non-leaf slice {slice_id!r}")
    criteria = compose_acceptance_criteria(current, leaf or {})
    direct_reviewer_event = event.event_type == "pr.review" and "verdict" in event.data
    reviewer_agent_id = current.reviewer_agent_id
    if direct_reviewer_event:
        if not _review_evidence_matches_exact_head(event, head_sha):
            LOGGER.warning(
                "[TL loop] ignoring incomplete exact-head reviewer evidence target=%s head=%s",
                slice_id,
                head_sha,
            )
            return state
        if incoming_verdict is None or not _reviewer_identity_authorized(
            event, current, strict=True
        ):
            LOGGER.warning(
                "[TL loop] ignoring unauthorized reviewer verdict target=%s agent=%s head=%s",
                slice_id,
                event.agent_id,
                head_sha,
            )
            return state
        decision_verdict = incoming_verdict
        decision_head = head_sha
        decision_reasons = tuple(
            {
                "severity": finding["severity"],
                "file": finding["path"],
                "line": 0,
                "claim": finding["rationale"],
            }
            for finding in findings
        )
        if reviewer_agent_id is None:
            reviewer_agent_id = event.data.get("reviewer_agent_id")
    else:
        result = adjudicate_review(
            _review_diff(event),
            findings,
            list(criteria),
            head_sha,
            model_choice=config.review_model_choice,
            policy_path=config.review_policy_path or Path(".exo/review-policy.toml"),
        )
        decision_verdict = result.verdict
        decision_head = result.reviewed_head
        decision_reasons = result.reasons
        if decision_verdict in {
            Verdict.GO,
            Verdict.GO_WITH_NITS,
        } and not _reviewer_identity_authorized(event, current):
            LOGGER.warning(
                "[TL loop] ignoring unauthorized approval evidence target=%s agent=%s",
                slice_id,
                event.agent_id,
            )
            updated = dict(state.slices)
            updated[slice_id] = replace(
                slice_transition(
                    current,
                    StallClassificationObserved(
                        stall_classification or current.stall_classification
                    ),
                ),
                pr_number=event.pr_number or current.pr_number,
                review_findings=review_findings,
                review_patch_digests=patch_digests,
            )
            return store.checkpoint(phase, updated, state.budgets, event_seq)
    review_findings = _persist_adjudication_nits(review_findings, head_sha, decision_reasons)
    updated = dict(state.slices)
    authorized_exact_verdict = (
        decision_verdict in {Verdict.GO, Verdict.GO_WITH_NITS}
        and _reviewer_identity_authorized(
            event,
            current,
            strict=direct_reviewer_event,
        )
        and decision_head == head_sha
    )
    next_stall_classification = (
        None
        if authorized_exact_verdict and current.stall_classification == "reviewer_not_responding"
        else stall_classification or current.stall_classification
    )
    transitioned = slice_transition(
        current,
        ReviewVerdictObserved(
            head_sha=decision_head,
            verdict=decision_verdict,
            reviewer_agent_id=reviewer_agent_id,
            review_id=event.data.get("review_id")
            if type(event.data.get("review_id")) is int
            else None,
            findings=tuple(findings),
            source=(
                "watcher_snapshot" if event.data.get("source") == "watcher_snapshot" else "ledger"
            ),
            pr_number=event.pr_number,
            reviewer_account_authenticated=event.data.get("reviewer_account_authenticated") is True,
            reviewer_identity_unresolved=event.data.get("reviewer_identity_unresolved")
            is not False,
            self_approval=(
                reviewer_agent_id is not None and reviewer_agent_id == current.dispatch_agent_id
            ),
            stall_classification=next_stall_classification,
            requires_authenticated_evidence=direct_reviewer_event,
            submitted_at=_review_event_submitted_at(event),
            validated_at=(
                event.data.get("validated_at")
                if isinstance(event.data.get("validated_at"), str)
                else event.observed_at
            ),
            observed_at=event.observed_at,
            dismissed=event.data.get("dismissed") is True,
            forgejo_stale=event.data.get("forgejo_stale") is True,
        ),
    )
    updated[slice_id] = replace(
        transitioned,
        pr_number=event.pr_number or current.pr_number,
        verdict_at=event.observed_at,
        review_findings=review_findings,
        review_patch_digests=patch_digests,
    )
    state = store.checkpoint(phase, updated, state.budgets, event_seq)
    if _is_aggregate_slice(updated[slice_id]):
        state = _record_aggregate_review_lifecycle(
            store, state, phase, event_seq, slice_id, decision_verdict
        )
    if decision_verdict is Verdict.NO_GO:
        max_rounds = (
            state.reviewer_max_rounds
            if state.reviewer_max_rounds_source is not None
            else _reviewer_max_rounds(config.review_policy_path)
        )
        current_rounds = state.slices[slice_id].review_rounds
        if (
            max_rounds is not None
            and current_rounds >= max_rounds
            and not _is_aggregate_slice(state.slices[slice_id])
        ):
            return state
        return _route_repair(
            store,
            state,
            phase,
            event_seq,
            slice_id,
            {"verdict": decision_verdict.value, "reasons": list(decision_reasons)},
            config,
            effects,
            effects_log,
        )
    return state


def _record_aggregate_review_lifecycle(
    store: RunStore,
    state: RunState,
    phase: PhaseValue,
    event_seq: int,
    slice_id: str,
    verdict: Verdict,
) -> RunState:
    """Apply hierarchical review outcomes through the ordered transition table."""
    candidate = _candidate_runtime(state.integration, slice_id)
    lifecycle = candidate.lifecycle
    if verdict is Verdict.NO_GO:
        if lifecycle is not IntegrationLifecycle.REPAIRING_AGGREGATE:
            lifecycle = transition_integration(
                IntegrationState(lifecycle=lifecycle),
                IntegrationTransition.REPAIR_STARTED,
            ).lifecycle
    elif verdict in {Verdict.GO, Verdict.GO_WITH_NITS}:
        if lifecycle is IntegrationLifecycle.REPAIRING_AGGREGATE:
            lifecycle = transition_integration(
                IntegrationState(lifecycle=lifecycle),
                IntegrationTransition.REPAIR_COMPLETED,
            ).lifecycle
        if lifecycle is IntegrationLifecycle.AGGREGATE_PR_OPEN:
            lifecycle = transition_integration(
                IntegrationState(lifecycle=lifecycle),
                IntegrationTransition.CODE_REVIEW_ACCEPTED,
            ).lifecycle
        if lifecycle is IntegrationLifecycle.CODE_REVIEWED:
            lifecycle = transition_integration(
                IntegrationState(lifecycle=lifecycle),
                IntegrationTransition.CODE_REVIEW_ACCEPTED,
            ).lifecycle
    else:
        raise IntegrationTransitionError(f"unsupported aggregate review verdict {verdict!r}")
    updated = _persist_candidate_runtime(
        state.integration,
        slice_id,
        replace(candidate, lifecycle=lifecycle),
    )
    return store.checkpoint(
        phase,
        state.slices,
        state.budgets,
        event_seq,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=updated,
    )


def _persist_adjudication_nits(
    review_findings: Mapping[str, tuple[Mapping[str, str], ...]],
    head_sha: str,
    reasons: Sequence[Mapping[str, object]],
) -> Mapping[str, tuple[Mapping[str, str], ...]]:
    """Store model-identified nits in the durable per-head review evidence."""
    nits = tuple(
        {
            "severity": "nit",
            "path": f"{reason['file']}:{reason['line']}",
            "rationale": str(reason["claim"]),
        }
        for reason in reasons
        if reason.get("severity") == "nit"
    )
    if not nits:
        return review_findings
    existing = list(review_findings.get(head_sha, ()))
    existing.extend(nit for nit in nits if nit not in existing)
    return {**review_findings, head_sha: tuple(existing)}


def _review_diff(event: EventEnvelope) -> Mapping[str, object] | str:
    candidate = event.data.get("diff", event.data.get("patch"))
    if isinstance(candidate, (Mapping, str)):
        return candidate
    if candidate is not None:
        raise TLLoopError(f"{event.event_type!r} diff must be an object or string")
    return event.data


def _route_ci_event(
    store: RunStore,
    state: RunState,
    phase: PhaseValue,
    event: EventEnvelope,
    event_seq: int,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    slice_id = _review_slice_id(event, state)
    if slice_id is None:
        raise TLLoopError(f"{event.event_type!r} has no slice identity")
    current = state.slices.get(slice_id)
    if current is None:
        raise TLLoopError(f"CI event references unknown slice {slice_id!r}")
    head_sha = event.head_sha
    if head_sha is None:
        raise TLLoopError(f"{event.event_type!r} has no head SHA")
    status = _ci_status(event)
    updated = dict(state.slices)
    transitioned = slice_transition(
        current,
        CIStatusObserved(
            head_sha=head_sha,
            status=status,
            observed_at=event.observed_at,
        ),
    )
    updated[slice_id] = replace(
        transitioned,
        pr_number=event.pr_number or current.pr_number,
    )
    state = store.checkpoint(phase, updated, state.budgets, event_seq)
    should_repair = (
        _review_workflow_enabled(config)
        and status == "failure"
        and current.reviewed_head == head_sha
        and current.status is not SliceStatus.REPAIRING
        and current.ci_state.get(head_sha) != "failure"
    )
    if not should_repair:
        return state
    reason = {
        "severity": "blocking",
        "file": _ci_reason_file(event),
        "line": 0,
        "claim": _ci_reason(event),
    }
    return _route_repair(
        store,
        state,
        phase,
        event_seq,
        slice_id,
        {"verdict": Verdict.NO_GO.value, "reasons": [reason]},
        config,
        effects,
        effects_log,
    )


def _ci_status(event: EventEnvelope) -> str:
    value = event.ci_status
    aliases = {"passed": "success", "error": "failure", "cancelled": "failure"}
    status = aliases.get(value, value)
    if status not in CI_STATUS_VALUES:
        raise TLLoopError(f"{event.event_type!r} has unsupported CI status {value!r}")
    return status


def _ci_reason(event: EventEnvelope) -> str:
    value = event.data.get("message", event.notification)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "CI reported a failure for the reviewed PR head"


def _ci_reason_file(event: EventEnvelope) -> str:
    value = event.data.get("path")
    return value.strip() if isinstance(value, str) and value.strip() else "CI"


def _route_repair(
    store: RunStore,
    state: RunState,
    phase: PhaseValue,
    event_seq: int,
    slice_id: str,
    review: object,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> RunState:
    current = state.slices.get(slice_id)
    if current is None or current.pr_number is None:
        raise TLLoopError(f"repair event references slice without PR {slice_id!r}")
    repair_arguments = {
        "pr_number": current.pr_number,
        "head_sha": current.reviewed_head,
    }
    repair_action = ActionState(
        ActionKind.REPAIR,
        ActionPhase.IN_FLIGHT,
        intent_id=stable_action_key(state.run_id, "resume_pr", slice_id, repair_arguments),
        head_sha=current.reviewed_head,
        attempt=max(1, current.attempts),
    )
    repairing = slice_transition(current, RepairQueued())
    repairing = slice_transition(repairing, ActionChanged(repair_action))
    state = store.checkpoint(
        phase,
        {**state.slices, slice_id: repairing},
        state.budgets,
        event_seq,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )
    current = state.slices[slice_id]
    live = cast(EffectClient, effects)
    repair_model = _repair_model(current, config)
    pr = {
        "pr_number": current.pr_number,
        "paths": list(current.paths),
        "slice_id": slice_id,
        "attempts": current.attempts,
    }
    effects_log.append(
        EffectIntent(
            "watcher_pr_state",
            slice_id,
            {"pr_number": current.pr_number},
            True,
        )
    )
    try:

        def dispatch_resume(arguments: JsonObject) -> object:
            return _invoke(
                "resume_pr",
                slice_id,
                arguments,
                True,
                live,
                lambda client: client.resume_pr(**arguments),
                effects_log,
                raise_on_failure=False,
            )

        compose_repair(
            pr,
            Verdict.NO_GO,
            review,
            client=live,
            model_choice=config.review_model_choice,
            store=store,
            slice_id=slice_id,
            model=repair_model,
            dispatch=dispatch_resume,
        )
    except (RepairError, ValueError) as error:
        parked = slice_transition(
            slice_transition(current, SliceStatusChanged(SliceStatus.PARKED)),
            ActionChanged(None),
        )
        parked = slice_transition(parked, StallClassificationObserved("review_stuck"))
        parked = replace(
            parked,
            park_cause=ParkCause.REVIEW_STUCK,
            dispatch_last_boundary="repair_exhausted",
            dispatch_error=str(error)[:500],
        )
        remaining = {
            waiting_id: ChildHandle(
                waiting_id,
                state.slices[waiting_id].branch or "",
                state.slices[waiting_id].agent_type or "unknown",
            )
            for waiting_id in state.fsm.waiting
            if waiting_id != slice_id
            and state.slices[waiting_id].status
            in {SliceStatus.SPAWNED, SliceStatus.IN_REVIEW, SliceStatus.REPAIRING}
        }
        repair_phase: PhaseValue = TLWaiting(remaining) if remaining else TLPlanning()
        state = store.checkpoint(
            repair_phase,
            {**state.slices, slice_id: parked},
            state.budgets,
            event_seq,
        )
        _record_controller_event(
            slice_id,
            "tl.slice_parked",
            {
                "slice_id": slice_id,
                "park_cause": ParkCause.REVIEW_STUCK.value,
                "reason": str(error)[:500],
            },
            config,
            effects,
            effects_log,
        )
        return state
    refreshed = store.load()
    resumed = slice_transition(
        refreshed.slices[slice_id],
        ActionChanged(replace(repair_action, phase=ActionPhase.CONFIRMED)),
    )
    return store.checkpoint(
        phase,
        {**refreshed.slices, slice_id: resumed},
        refreshed.budgets,
        event_seq,
        current_order=refreshed.current_order,
        ordered_stages=refreshed.ordered_stages,
        integration=refreshed.integration,
    )


def _repair_model(current: SliceState, config: TLLoopConfig) -> str | None:
    if config.requested_model:
        if config.catalog is not None and current.agent_type:
            return select_model(current.agent_type, config.catalog, config.requested_model).model_id
        return config.requested_model
    if config.catalog is None or config.policy is None:
        return current.model
    role_policy = config.policy.roles[config.role]
    if current.attempts < role_policy.escalate_after_attempts:
        return current.model
    if not current.agent_type:
        return current.model
    return select_model_for_difficulty(
        current.agent_type,
        config.catalog,
        Difficulty.HARD,
        escalated=True,
    ).model_id


def _repair_arguments(
    pr_number: int, handoff: RepairHandoff, model: str | None
) -> dict[str, object]:
    root_cause = handoff.root_cause
    proposed_solution = handoff.proposed_solution
    arguments: dict[str, object] = {
        "pr_number": pr_number,
        "task": proposed_solution,
        "context": f"ROOT CAUSE: {root_cause}\nPROPOSED SOLUTION: {proposed_solution}",
        "read_first": list(handoff.read_first),
        "steps": list(handoff.steps),
        "verify": list(handoff.verify),
        "boundary": list(handoff.boundary),
        "done_criteria": list(handoff.done_criteria),
    }
    if model is not None:
        arguments["model"] = model
    return arguments


def _review_verdict(event: EventEnvelope) -> Verdict | None:
    explicit = event.data.get("verdict")
    if isinstance(explicit, str):
        normalized = explicit.strip().upper().replace("_", "-")
        if normalized in {"GO", "APPROVED"}:
            return Verdict.GO
        if normalized in {"GO-WITH-NITS", "APPROVED-WITH-NITS"}:
            return Verdict.GO_WITH_NITS
        if normalized in {"NO-GO", "CHANGES-REQUESTED", "REQUESTED-CHANGES"}:
            return Verdict.NO_GO
    if event.review_kind in {"merge_ready", "approved"}:
        return Verdict.GO
    if event.review_state in {"approved", "approve"}:
        return Verdict.GO
    if event.review_state in {"changes_requested", "request_changes"}:
        return Verdict.NO_GO
    return None


def _emit_phase_change(
    run_id: str,
    before: PhaseValue,
    after: PhaseValue,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> None:
    before_tag = _phase_tag(before)
    after_tag = _phase_tag(after)
    if before_tag is after_tag:
        return
    _record_controller_event(
        "controller",
        "tl.phase_changed",
        {
            "from_phase": before_tag.value,
            "to_phase": after_tag.value,
            "run_id": run_id,
        },
        config,
        effects,
        effects_log,
    )


def _emit_slice_status_changes(
    before: Mapping[str, SliceState],
    after: Mapping[str, SliceState],
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> None:
    for slice_id in sorted(after):
        current = after[slice_id]
        previous = before.get(slice_id)
        from_status = previous.status if previous is not None else SliceStatus.PENDING
        if from_status is current.status:
            continue
        _record_controller_event(
            slice_id,
            "tl.slice_status_changed",
            {
                "slice_id": slice_id,
                "from_status": from_status.value,
                "to_status": current.status.value,
            },
            config,
            effects,
            effects_log,
        )


def _record_controller_event(
    target: str,
    event_type: str,
    payload: Mapping[str, object],
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> ToolResult | None:
    LOGGER.info(
        "[TL loop] effect=emit_controller_event target=%s event_type=%s active=%s",
        target,
        event_type,
        config.active,
    )
    if isinstance(effects_log, EffectJournal):
        arguments = {"event_type": event_type, "payload": dict(payload)}
        try:
            result = _invoke(
                "emit_controller_event",
                target,
                arguments,
                config.active,
                cast(EffectClient, effects),
                lambda client: client.emit_controller_event(
                    event_type=event_type,
                    payload=cast(JsonObject, dict(payload)),
                ),
                effects_log,
                raise_on_failure=False,
            )
        except DurableWriteError:
            raise
        except Exception as error:  # noqa: BLE001 - observability is fail-open
            LOGGER.warning("controller event %s failed: %s", event_type, error)
            return None
        if result is not None and result.success is False:
            LOGGER.warning(
                "controller event %s failed: %s",
                event_type,
                result.error or "effect returned failure",
            )
        return result
    intent = EffectIntent("emit_controller_event", target, payload, config.active)
    effects_log.append(intent)
    if not config.active:
        return None
    live = cast(EffectClient, effects)
    try:
        result = live.emit_controller_event(
            event_type=event_type,
            payload=cast(JsonObject, dict(payload)),
        )
    except Exception as error:  # noqa: BLE001 - observability is fail-open
        LOGGER.warning("controller event %s failed: %s", event_type, error)
        return None
    if result.success is False:
        LOGGER.warning(
            "controller event %s failed: %s",
            event_type,
            result.error or "effect returned failure",
        )
    return result


def _journal_entries(
    effects_log: list[EffectIntent],
) -> tuple[Mapping[str, object], ...]:
    """Expose journal evidence without allowing callers to mutate it."""
    if not isinstance(effects_log, EffectJournal):
        return ()
    return effects_log.snapshot()


def _invoke(
    operation: str,
    target: str,
    arguments: Mapping[str, object],
    active: bool,
    client: EffectClient | None,
    call: Callable[[EffectClient], ToolResult],
    effects_log: list[EffectIntent],
    *,
    raise_on_failure: bool = True,
    retryable_failure: bool = False,
) -> ToolResult | None:
    intent = EffectIntent(operation, target, arguments, active)
    journal = effects_log if isinstance(effects_log, EffectJournal) else None
    try:
        probe = journal.probe(intent) if journal and operation in MUTATING_OPERATIONS else None
        prior = probe.entry if probe is not None else None
    except DurableWriteError as error:
        raise error.with_context(operation=operation, target=target) from error
    if prior is not None:
        status = prior.get("status")
        if status in {"confirmed", "rejected"}:
            if probe is None or probe.result is None:
                raise TLLoopError(f"journal probe has no result for {operation} {target!r}")
            result = probe.result
            if result.error_kind == "tool_unavailable":
                raise ToolUnavailableError(operation, result, target=target)
            if result.success is False and raise_on_failure:
                detail = result.error or f"{operation} returned failure"
                raise EffectFailed(f"{operation} for {target!r}: {detail}")
            return result
        if status in {"intended", "unknown"}:
            key = prior.get("key", journal.key_for(intent))
            gate_name = _action_journal_gate_name(key, _action_journal_compensation_attempt(prior))
            raise TLLoopError(
                f"lifecycle effect {operation} for {target!r} has unknown outcome "
                f"and requires reconciliation before retry (key={key}); restart to "
                f"open gate={gate_name}, then answer it with `tl_loop gate --name "
                f"{gate_name} --approve|--reject`"
            )
        if status != "compensated":
            key = prior.get("key", journal.key_for(intent))
            raise TLLoopError(
                f"lifecycle effect {operation} for {target!r} has unrecognized "
                f"action journal status {status!r} (key={key})"
            )
        # An operator-approved _reconcile_action_journal() pass cleared this
        # entry for a fresh attempt; fall through and dispatch as new.
    try:
        effects_log.append(intent)
    except DurableWriteError as error:
        raise error.with_context(operation=operation, target=target) from error
    LOGGER.info("[TL loop] effect=%s target=%s active=%s", operation, target, active)
    if not active:
        if journal is not None:
            try:
                journal.mark_not_dispatched(intent)
            except DurableWriteError as error:
                raise error.with_context(operation=operation, target=target) from error
        return None
    if client is None:
        raise TLLoopError("active loop has no effect client")
    try:
        result = call(client)
    except BaseException as error:
        if journal is not None:
            try:
                journal.mark_unknown(intent, error)
            except DurableWriteError as journal_error:
                raise journal_error.with_context(operation=operation, target=target) from error
        raise
    if journal is not None:
        try:
            if retryable_failure and result.success is not True:
                journal.mark_unknown(
                    intent,
                    RuntimeError(result.error or f"{operation} returned a retryable failure"),
                )
            else:
                journal.mark_result(intent, result)
        except DurableWriteError as error:
            raise error.with_context(operation=operation, target=target) from error
    if result.error_kind == "tool_unavailable":
        raise ToolUnavailableError(operation, result, target=target)
    if result.success is False and raise_on_failure:
        detail = result.error or f"{operation} returned failure"
        raise EffectFailed(f"{operation} for {target!r}: {detail}")
    return result


def _next_event(
    source: EventQueue,
    config: TLLoopConfig,
) -> EventEnvelope | None:
    if config.cancel_event is not None and config.cancel_event.is_set():
        raise LoopCancelled("TL controller cancellation requested")
    timeout = config.poll_interval or 0.01
    try:
        return source.get(timeout=timeout)
    except queue_module.Empty:
        if config.poll_interval == 0:
            time.sleep(0.01)
        return None


def _record_task_blocked_recovery(
    event: EventEnvelope,
    state: RunState,
    store: RunStore,
    phase: PhaseValue,
    event_seq: int,
    effects: EffectClient,
) -> RunState:
    """Record one externally blocked slice in nonterminal DIAGNOSING state."""
    blocked = event.task_blocked
    if blocked is None:
        raise TLLoopError("agent.task_blocked has no typed blocked payload")
    slice_id = blocked.slice_id or event.slice_id or event.agent_id
    if not isinstance(slice_id, str) or slice_id not in state.slices:
        raise TLLoopError(f"agent.task_blocked names unknown slice {slice_id!r}")
    cause_by_wire = {
        "base_ci_unstable": ParkCause.BASE_CI_UNSTABLE,
        "external_dependency": ParkCause.EXTERNAL_DEPENDENCY,
        "scope_boundary": ParkCause.SCOPE_BOUNDARY,
        "human_decision_required": ParkCause.HUMAN_DECISION_REQUIRED,
        "tooling_unavailable": ParkCause.TOOL_UNAVAILABLE,
    }
    cause = cause_by_wire.get(blocked.cause.value)
    if cause is None:
        raise TLLoopError(f"agent.task_blocked has unsupported cause {blocked.cause.value!r}")
    audit = dict(event.data)
    audit.update(
        {
            "attempt": blocked.attempt,
            "recovery_action": blocked.recovery_action,
            "needs_human": blocked.needs_human,
            "scope_attribution": blocked.scope_attribution,
            "retryable": blocked.retryable,
            "declared_difficulty": blocked.declared_difficulty.value,
            "matched_difficulty_rule": blocked.matched_difficulty_rule,
        }
    )
    current = state.slices[slice_id]
    audit.setdefault("invocation_id", event.invocation_id or current.dispatch_invocation_id)
    audit.setdefault("generation", event.generation)
    if current.recovery is not None:
        if current.recovery.cause != cause.value:
            raise TLLoopError(
                f"slice {slice_id!r} already recovers cause {current.recovery.cause!r}"
            )
        suspended = suspend_dependents(state.slices, slice_id, current.recovery.recovery_round)
        return store.checkpoint(phase, suspended, state.budgets, event_seq)
    generation = audit.get("generation")
    recovery = begin_recovery(
        cause=cause.value,
        owner_run_id=state.run_id,
        slice_attempt=blocked.attempt or current.attempts,
        owner_agent_id=current.dispatch_agent_id,
        invocation_generation=generation if type(generation) is int and generation >= 0 else 0,
        plan_revision=state.revision,
        evidence=audit,
    )
    policy = policy_for_cause(blocked.cause.value)
    if policy.immediate_human_gate:
        recovery = transition_recovery(
            recovery,
            RecoveryPhase.HUMAN_GATE,
            next_action="open_human_gate",
            evidence=audit,
        )
    updated = {**state.slices, slice_id: replace(current, recovery=recovery)}
    updated = suspend_dependents(updated, slice_id, recovery.recovery_round)
    checkpointed = store.checkpoint(phase, updated, state.budgets, event_seq)
    emit_controller_event(
        effects,
        "agent.recovery.started",
        {
            "slice_id": slice_id,
            "invocation_id": recovery.evidence.get("invocation_id", current.dispatch_invocation_id),
            "generation": recovery.invocation_generation,
            "cause": recovery.cause,
            "slice_attempt": recovery.slice_attempt,
            "invocation_generation": recovery.invocation_generation,
            "recovery_round": recovery.recovery_round,
            "authorization_source": "policy",
            "recursive_depth": checkpointed.depth,
            "parallel_impact": _recovery_parallel_impact(checkpointed, slice_id),
            "policy_decision": "wait",
        },
    )
    return checkpointed


def _recovery_parallel_impact(state: RunState, slice_id: str) -> str:
    """Bound sibling scheduling impact without exporting sibling identities."""
    siblings = [sibling.status for name, sibling in state.slices.items() if name != slice_id]
    if any(status in {SliceStatus.READY, SliceStatus.PENDING} for status in siblings):
        return "sibling_progress"
    if any(
        status not in {SliceStatus.MERGED, SliceStatus.FAILED, SliceStatus.PARKED}
        for status in siblings
    ):
        return "sibling_wait"
    return "none"


def _checkpoint_and_ack(
    store: RunStore,
    source: EventQueue,
    event: EventEnvelope,
    state: RunState,
    phase: PhaseValue,
    *,
    acknowledge: bool = True,
) -> None:
    if event.run_seq is None:
        raise TLLoopError(f"{event.event_type!r} has no run_seq")
    offset = max(state.events.last_consumed_offset, event.run_seq)
    store.checkpoint(phase, state.slices, state.budgets, offset)
    if acknowledge:
        source.acknowledge(event)


def _ack_event(
    source: EventQueue,
    event: EventEnvelope,
    replaying: bool,
    diagnostics: EventDiagnostics,
) -> None:
    """Acknowledge source events once; replayed rows were already acknowledged."""
    if not replaying:
        source.acknowledge(event)
        diagnostics.acknowledged += 1


def _release_replayed_event(store: RunStore, event: EventEnvelope, replaying: bool) -> None:
    if replaying and event.run_seq is not None:
        store.release_quarantined_event(event.run_seq)


def _is_reconcilable_identity_event(event: EventEnvelope) -> bool:
    return event.kind in {
        EventKind.PR_FILED,
        EventKind.PR_UPDATED,
        EventKind.PR_REVIEW,
        EventKind.COPILOT_REVIEW,
        EventKind.CI_STATUS_CHANGED,
    }


def _replayable_event_index(
    events: Sequence[EventEnvelope],
    state: RunState,
    expected: set[str],
) -> int | None:
    for index, event in enumerate(events):
        if resolve_event_slice(event, state, allowed_ids=expected).resolved:
            return index
    return None


def _event_belongs_to_plan(
    event: EventEnvelope, expected: set[str], state: RunState | None = None
) -> bool:
    if "shadow_event" in event.data:
        value = event.data["shadow_event"]
        if isinstance(value, Mapping):
            slug = value.get("slug")
            return slug is None or slug in expected
    if event.kind in {EventKind.PR_REVIEW, EventKind.COPILOT_REVIEW, EventKind.CI_STATUS_CHANGED}:
        if state is None:
            return False
        return resolve_event_slice(event, state, allowed_ids=expected).resolved
    if event.kind in {EventKind.PR_FILED, EventKind.PR_UPDATED}:
        if state is None:
            return False
        return resolve_event_slice(event, state, allowed_ids=expected).resolved
    if event.agent_id in expected:
        return True
    intent_id = event.data.get("intent_id")
    if (
        isinstance(intent_id, str)
        and state is not None
        and any(
            slice_state.dispatch_intent_id == intent_id for slice_state in state.slices.values()
        )
    ):
        return True
    for key in ("slug", "child_agent", "slice_id"):
        value = event.data.get(key)
        if isinstance(value, str) and value in expected:
            return True
    return False


def _event_slice_id(event: EventEnvelope, state: RunState) -> str | None:
    resolved = resolve_event_slice(event, state)
    if resolved.resolved:
        return resolved.slice_id
    shadow_event = event.data.get("shadow_event")
    if isinstance(shadow_event, Mapping):
        shadow_slug = shadow_event.get("slug")
        if isinstance(shadow_slug, str) and shadow_slug in state.slices:
            return shadow_slug
    return None


def _pr_event_target(
    slices: Mapping[str, SliceState], event: TLEvent, slice_id: str | None
) -> str | None:
    if not isinstance(event, (PRFiled, PRUpdated)):
        return None
    target_id = event.slice_id or slice_id
    if target_id is not None:
        return target_id
    matches = [
        candidate_id
        for candidate_id, candidate in slices.items()
        if candidate.pr_number == event.pr_number
    ]
    return matches[0] if len(matches) == 1 else None


def _bind_publication_evidence(
    slices: Mapping[str, SliceState],
    event: PRFiled | PRUpdated,
    envelope: EventEnvelope,
    slice_id: str | None,
) -> dict[str, SliceState]:
    """Bind host-verified publication metadata to its persisted owner."""
    target_id = _pr_event_target(slices, event, slice_id)
    current = slices.get(target_id) if target_id is not None else None
    if target_id is None or current is None:
        LOGGER.warning(
            "[TL loop] ignoring publication event without an unambiguous owner pr=%s",
            event.pr_number,
        )
        return dict(slices)
    if current.dispatch_agent_id is None or envelope.agent_id != current.dispatch_agent_id:
        LOGGER.warning(
            "[TL loop] ignoring publication event from untrusted owner target=%s agent=%s",
            target_id,
            envelope.agent_id,
        )
        return dict(slices)
    head_branch = envelope.data.get("head_branch")
    base_branch = envelope.data.get("base_branch")
    if (
        not isinstance(head_branch, str)
        or not head_branch
        or not isinstance(base_branch, str)
        or not base_branch
    ):
        LOGGER.warning(
            "[TL loop] ignoring publication event with incomplete verified identity target=%s",
            target_id,
        )
        return dict(slices)
    existing = current.publication
    if existing is not None and (
        existing.pr_number != event.pr_number or existing.head_sha != event.head_sha
    ):
        LOGGER.warning(
            "[TL loop] refusing publication replacement for target=%s: existing head differs",
            target_id,
        )
        return dict(slices)
    invocation_id = envelope.invocation_id or current.dispatch_invocation_id
    publication = PublicationBinding(
        pr_number=event.pr_number,
        head_sha=event.head_sha,
        head_branch=head_branch,
        base_branch=base_branch,
        attempt=existing.attempt if existing is not None else current.attempts,
        invocation_id=invocation_id,
    )
    updated = current
    if existing != publication:
        updated = replace(updated, publication=publication)
    if invocation_id is not None and current.dispatch_agent_id:
        handoff = HandoffEvidence(
            pr_number=event.pr_number,
            head_sha=event.head_sha,
            attempt=publication.attempt,
            invocation_id=invocation_id,
            agent_id=current.dispatch_agent_id,
            observed_at=envelope.observed_at,
        )
        if updated.handoff != handoff:
            updated = slice_transition(updated, SliceStatusChanged(SliceStatus.IN_REVIEW))
            updated = replace(updated, handoff=handoff)
    if updated == current:
        return dict(slices)
    return {**slices, target_id: updated}


def _complete_legacy_direct_children(phase: RecursiveTLRunning) -> PhaseValue:
    """Translate the legacy aggregate signal into typed direct completions."""
    next_phase: PhaseValue = phase
    for record in phase.parallel_pending:
        if record.kind is ChildKind.WORKER:
            next_phase = scope_transition(
                next_phase,
                ScopeWorkerCompleted(record.child_id, "legacy-all-children-done"),
            )
        elif record.kind is ChildKind.LEAF:
            next_phase = scope_transition(
                next_phase,
                ScopeLeafCompleted(record.child_id, "legacy-all-children-done"),
            )
    return next_phase


def _apply_child_completion(
    slices: Mapping[str, SliceState],
    slice_id: str | None,
    event: EventEnvelope,
    *,
    persist_publication: bool,
) -> dict[str, SliceState]:
    """Persist the child result carried by a completion notification."""
    if slice_id is None or slice_id not in slices:
        return dict(slices)
    current = slices[slice_id]
    if not persist_publication:
        return dict(slices)
    pr_number = event.data.get("pr_number")
    head_sha = event.data.get("head_sha")
    if type(pr_number) is int and pr_number > 0 and isinstance(head_sha, str) and head_sha:
        updated = replace(
            slice_transition(current, SliceStatusChanged(SliceStatus.IN_REVIEW)),
            pr_number=pr_number,
            reviewed_head=head_sha,
        )
        return {**slices, slice_id: updated}
    return dict(slices)


def _duplicate_event(phase: PhaseValue, event: TLEvent, state: RunState) -> bool:
    if isinstance(
        phase,
        (
            RecursiveTLAllMerged,
            RecursiveTLDone,
            RecursiveTLPRFiled,
            RecursiveTLFailed,
            RecursiveTLParked,
        ),
    ):
        return isinstance(event, AllChildrenDone)
    if isinstance(phase, RecursiveTLRunning):
        active = set(active_child_ids(phase))
        if isinstance(event, ChildSpawned):
            current = state.slices.get(event.handle.slug)
            if current is not None and current.status in DISPATCHING_STATUSES:
                return False
            return event.handle.slug in active
        if isinstance(event, (ChildCompleted, ChildFailed, PRMerged)):
            return event.slug not in active
        if isinstance(event, AllChildrenDone):
            return isinstance(
                phase,
                (
                    RecursiveTLAllMerged,
                    RecursiveTLDone,
                    RecursiveTLPRFiled,
                    RecursiveTLFailed,
                    RecursiveTLParked,
                ),
            )
        return False
    if isinstance(event, ChildSpawned):
        active = phase.children if isinstance(phase, (TLWaiting, TLMerging)) else {}
        return event.handle.slug in active
    if isinstance(event, (ChildCompleted, ChildFailed, PRMerged)):
        active = phase.children if isinstance(phase, (TLWaiting, TLMerging)) else {}
        return event.slug not in active
    if isinstance(event, AllChildrenDone):
        return isinstance(phase, (TLDone, TLFailed))
    return False


def _root_inputs(
    root_spec: WorkPlan | Mapping[str, object], config: TLLoopConfig
) -> tuple[WorkPlan | None, str, EventQueue, EffectClient | ReadOnlyEffectClient]:
    if isinstance(root_spec, WorkPlan):
        raw: Mapping[str, object] = {}
        plan = root_spec
    elif isinstance(root_spec, Mapping):
        raw = root_spec
        plan_value = raw.get("plan")
        if plan_value is None and any(key in raw for key in ("workers", "leaves", "sub_tls")):
            plan_value = {key: raw[key] for key in ("workers", "leaves", "sub_tls") if key in raw}
        if isinstance(plan_value, WorkPlan):
            plan = plan_value
        elif isinstance(plan_value, Mapping):
            plan = WorkPlan.from_mapping(plan_value)
        else:
            plan = None
    else:
        raise TypeError("root_spec must be a WorkPlan or object")
    run_id = raw.get("run_id", config.run_id)
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("root_spec.run_id must be a non-empty string")
    source = config.source or raw.get("source")
    effects = config.effects or raw.get("effects")
    if not hasattr(source, "get") or not hasattr(source, "acknowledge"):
        raise TypeError("tl_run requires an event source in cfg or root_spec")
    if not isinstance(effects, (EffectClient, ReadOnlyEffectClient)):
        raise TypeError("tl_run requires an effect client in cfg or root_spec")
    return plan, run_id, cast(EventQueue, source), effects


def derive_child_branch(parent_branch: str, name: str) -> str:
    _require_text(parent_branch, "parent branch")
    _require_text(name, "child name")
    return f"{parent_branch}.{name}"


def derive_child_worktree(parent_worktree: str | Path, name: str) -> Path:
    _require_text(str(parent_worktree), "parent worktree")
    _require_text(name, "child name")
    return Path(parent_worktree) / name


def _effective_worktree(config: TLLoopConfig, root_dir: Path, run_id: str) -> str:
    value = config.worktree or (root_dir / run_id)
    return str(Path(value).expanduser().resolve())


def _manifest_plan(plan: WorkPlan) -> Mapping[str, object]:
    """Convert the typed recursive plan into its canonical declaration shape."""
    workers = [
        {
            "name": task.name,
            "task": task.task,
            "agent_type": task.agent_type,
            **(
                {"task_timeout_seconds": task.task_timeout_seconds}
                if task.task_timeout_declared
                else {}
            ),
        }
        for task in plan.workers
    ]
    leaves = [
        {
            "name": task.name,
            "task": task.task,
            "agent_type": task.agent_type,
            "boundary": list(task.boundary),
            "context": task.context,
            "read_first": list(task.read_first),
            "steps": list(task.steps),
            "verify": list(task.verify),
            "done_criteria": list(task.done_criteria),
            **(
                {"task_timeout_seconds": task.task_timeout_seconds}
                if task.task_timeout_declared
                else {}
            ),
        }
        for task in plan.leaves
    ]
    sub_tls = []
    for task in plan.sub_tls:
        child_plan = (
            task.plan if isinstance(task.plan, WorkPlan) else WorkPlan.from_mapping(task.plan)
        )
        sub_tls.append(
            {
                "name": task.name,
                "order": task.order,
                "task": f"sub-TL {task.name}",
                "agent_type": task.agent_type,
                "worktree": str(task.worktree) if task.worktree is not None else None,
                "integration": _manifest_integration(task.integration),
                "plan": _manifest_plan(child_plan),
                "order_explicit": task.order_explicit,
                **(
                    {"task_timeout_seconds": task.task_timeout_seconds}
                    if task.task_timeout_declared
                    else {}
                ),
            }
        )
    return {"workers": workers, "leaves": leaves, "sub_tls": sub_tls}


def _manifest_for_plan(
    plan: WorkPlan,
    run_id: str,
    config: TLLoopConfig,
) -> PlanManifest:
    """Build a candidate declaration only when a run explicitly supplies one."""
    return build_plan_manifest(
        _manifest_plan(plan),
        scope_id=run_id,
        parent_scope_id=config.parent_run_id,
        role="non_root" if config.parent_run_id is not None or config.depth > 0 else "root",
        owned_branch=config.branch,
        parent_integration_target=config.parent_branch,
        manifest_revision=config.plan_revision,
    )


def _manifest_is_legacy(manifest: PlanManifest) -> bool:
    return manifest.role == "root" and manifest.owned_branch == "legacy"


def _work_plan_from_manifest(manifest: PlanManifest) -> WorkPlan:
    """Reconstruct executable declarations from the persisted recursive authority."""
    workers: list[WorkerTask] = []
    leaves: list[LeafTask] = []
    sub_tls: list[SubTLTask] = []
    for node in manifest.nodes:
        declaration = node.declaration
        if node.kind == "worker":
            workers.append(
                WorkerTask(
                    node.name,
                    node.task,
                    node.agent_type,
                    _manifest_timeout(declaration),
                    "task_timeout_seconds" in declaration,
                )
            )
        elif node.kind == "leaf":
            leaves.append(
                LeafTask(
                    name=node.name,
                    task=node.task,
                    agent_type=node.agent_type,
                    boundary=node.boundary,
                    context=_manifest_optional_text(declaration, "context"),
                    read_first=_manifest_texts(declaration, "read_first"),
                    steps=_manifest_texts(declaration, "steps"),
                    verify=_manifest_texts(declaration, "verify"),
                    done_criteria=_manifest_texts(declaration, "done_criteria"),
                    task_timeout_seconds=_manifest_timeout(declaration),
                    task_timeout_declared="task_timeout_seconds" in declaration,
                )
            )
        elif node.kind == "sub_tl":
            child = manifest.child_manifests.get(node.node_id)
            if child is None or node.order is None:
                raise TLLoopError(
                    f"manifest node {node.node_id!r} has no reconstructable child scope"
                )
            sub_tls.append(
                SubTLTask(
                    name=node.name,
                    plan=_work_plan_from_manifest(child),
                    agent_type=node.agent_type,
                    worktree=node.worktree,
                    order=node.order,
                    integration=_integration_contract(node.integration_contract),
                    order_explicit=bool(declaration.get("order_explicit", True)),
                    task_timeout_seconds=_manifest_timeout(declaration),
                    task_timeout_declared="task_timeout_seconds" in declaration,
                )
            )
        else:
            raise TLLoopError(
                f"manifest node {node.node_id!r} has legacy kind {node.kind!r} and cannot be resumed"
            )
    return normalize_work_plan(WorkPlan(tuple(workers), tuple(leaves), tuple(sub_tls)))


def _manifest_timeout(declaration: Mapping[str, object]) -> float | None:
    value = declaration.get("task_timeout_seconds")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TLLoopError("manifest task_timeout_seconds must be numeric or null")
    return float(value)


def _manifest_optional_text(declaration: Mapping[str, object], name: str) -> str | None:
    value = declaration.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TLLoopError(f"manifest declaration {name} must be a string or null")
    return value


def _manifest_texts(declaration: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = declaration.get(name, ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TLLoopError(f"manifest declaration {name} must be an array")
    if any(not isinstance(item, str) for item in value):
        raise TLLoopError(f"manifest declaration {name} must contain strings")
    return tuple(value)


def _manifest_integration(contract: IntegrationContract) -> Mapping[str, object]:
    return {
        "aggregate_pr_required": contract.aggregate_pr_required,
        "base_revalidation_required": contract.base_revalidation_required,
        "leaf_review_owner": contract.leaf_review_owner.value,
        "aggregate_review_owner": contract.aggregate_review_owner.value,
        "aggregate_repair_owner": contract.aggregate_repair_owner.value,
        "merge_strategy": contract.merge_strategy,
    }


def _bind_initial_slices(
    slices: Mapping[str, Mapping[str, object]],
    manifest: PlanManifest,
) -> Mapping[str, Mapping[str, object]]:
    """Attach exact manifest identity to every direct initial slice."""
    by_name = {node.name: node for node in manifest.nodes}
    bound: dict[str, Mapping[str, object]] = {}
    for slice_id, value in slices.items():
        node = by_name.get(slice_id)
        if node is None:
            raise TLLoopError(f"slice {slice_id!r} is not declared by the plan manifest")
        record = copy.deepcopy(dict(value))
        record["manifest_node_id"] = node.node_id
        record["manifest_revision"] = manifest.manifest_revision
        bound[slice_id] = record
    return bound


def _initial_slices(
    plan: WorkPlan,
    config: TLLoopConfig | None = None,
    root_dir: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, dict[str, object]]:
    selected = config or TLLoopConfig()
    state_root = Path(root_dir or selected.root_dir)
    nested = selected.parent_branch is not None
    current_run = run_id or selected.run_id
    owner_worktree = _effective_worktree(selected, state_root, current_run)
    result: dict[str, dict[str, object]] = {}
    for worker in plan.workers:
        result[worker.name] = _initial_slice_record(
            worker.name,
            (f"tl-loop/{worker.name}",),
            ("controller",),
            worker.agent_type,
            derive_child_branch(selected.branch, worker.name) if nested else None,
            str(derive_child_worktree(owner_worktree, worker.name)) if nested else None,
            selected.parent_branch if nested else None,
            config=selected,
            task_timeout_seconds=worker.task_timeout_seconds,
            task_timeout_declared=worker.task_timeout_declared,
        )
    for leaf in plan.leaves:
        paths = leaf.boundary or (f"tl-loop/{leaf.name}",)
        test_plan = leaf.verify or leaf.steps or ("controller",)
        review_contract = _materialize_leaf_review_contract(leaf, paths, test_plan)
        result[leaf.name] = _initial_slice_record(
            leaf.name,
            paths,
            test_plan,
            leaf.agent_type,
            derive_child_branch(selected.branch, leaf.name) if nested else None,
            str(derive_child_worktree(owner_worktree, leaf.name)) if nested else None,
            selected.parent_branch if nested else None,
            config=selected,
            task_timeout_seconds=leaf.task_timeout_seconds,
            task_timeout_declared=leaf.task_timeout_declared,
            review_contract=review_contract.as_mapping(),
        )
    for task in plan.sub_tls:
        result[task.name] = _initial_slice_record(
            task.name,
            (f"tl-loop/{task.name}",),
            ("controller",),
            task.agent_type,
            derive_child_branch(selected.branch, task.name),
            str(task.worktree or derive_child_worktree(owner_worktree, task.name)),
            selected.branch,
            config=selected,
            task_timeout_seconds=task.task_timeout_seconds,
            task_timeout_declared=task.task_timeout_declared,
        )
    return result


def _all_expected_terminal(state: RunState, expected: set[str]) -> bool:
    terminal = {
        SliceStatus.MERGED,
        SliceStatus.FAILED,
        SliceStatus.PARKED,
        SliceStatus.BLOCKED,
    }
    return bool(expected) and all(
        state.slices.get(slice_id) is not None and state.slices[slice_id].status in terminal
        for slice_id in expected
    )


def _note_heartbeat_progress(store: RunStore, state: RunState) -> RunState:
    return store.set_goals(replace(state.goals, last_progress_at=time.time()))


def _note_authoritative_event(store: RunStore, state: RunState, event_seq: int) -> RunState:
    now = time.time()
    return store.set_goals(
        replace(
            state.goals,
            last_authoritative_event_seq=event_seq,
            last_progress_at=now,
        )
    )


def _encode_goals(goals: GoalState) -> dict[str, object]:
    encoded = {
        "objective": goals.objective,
        "deadline": goals.deadline,
        "completion_predicate": goals.completion_predicate,
        "last_heartbeat_at": goals.last_heartbeat_at,
        "last_progress_at": goals.last_progress_at,
    }
    if goals.controller_started_at is not None:
        encoded["controller_started_at"] = goals.controller_started_at
    if goals.last_authoritative_event_seq is not None:
        encoded["last_authoritative_event_seq"] = goals.last_authoritative_event_seq
    return encoded


def _materialize_leaf_review_contract(
    leaf: LeafTask,
    paths: Sequence[str],
    test_plan: Sequence[str],
) -> ReviewContract:
    values = (
        ("Run-state test plan", test_plan),
        ("Plan verification", leaf.verify),
        ("Owned paths", paths),
        ("Plan boundary", leaf.boundary),
        ("DONE CRITERIA", leaf.done_criteria),
    )
    criteria = [f"{label}: {value}" for label, items in values for value in items]
    return ReviewContract.from_criteria(criteria)


def _initial_slice_record(
    name: str,
    paths: Sequence[str],
    test_plan: Sequence[str],
    agent_type: str | None,
    branch: str | None = None,
    worktree: str | None = None,
    base_ref: str | None = None,
    *,
    config: TLLoopConfig | None = None,
    task_timeout_seconds: float | None = None,
    task_timeout_declared: bool = False,
    review_contract: Mapping[str, object] | None = None,
) -> dict[str, object]:
    resolved_timeout, timeout_source = _resolve_task_timeout(
        config or TLLoopConfig(), task_timeout_seconds, task_timeout_declared
    )
    record: dict[str, object] = {
        "id": name,
        "status": SliceStatus.PENDING.value,
        "paths": list(paths),
        "depends_on": [],
        "base_ref": base_ref,
        "test_plan": list(test_plan),
        "agent_type": agent_type,
        "model": None,
        "branch": branch,
        "worktree": worktree,
        "pr_number": None,
        "review_findings": {},
        "ci_state": {},
        "reviewer_attempt": {},
        "repair_attempts": 0,
        "reviewed_head": None,
        "attempts": 0,
        "verdict": None,
        "task_timeout_seconds": resolved_timeout,
        "task_timeout_source": timeout_source,
    }
    if review_contract is not None:
        record["review_contract"] = copy.deepcopy(dict(review_contract))
    return record


def _resolve_task_timeout(
    config: TLLoopConfig,
    task_timeout_seconds: float | None,
    task_timeout_declared: bool,
) -> tuple[float | None, str]:
    if task_timeout_declared:
        _validate_task_timeout(task_timeout_seconds, "task_timeout_seconds")
        return task_timeout_seconds, "slice"
    policy = config.policy
    role_policy = policy.roles.get(config.role) if policy is not None else None
    if role_policy is not None and role_policy.task_timeout_configured:
        return role_policy.task_timeout_seconds, "role"
    return config.task_timeout_seconds, config.task_timeout_source


def _validate_task_timeout(value: float | None, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be null or a non-negative number")
    if value < 0 or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be null or a non-negative number")


def _budget_root(
    budgets: BudgetLedger | Mapping[str, object],
) -> dict[str, object]:
    if isinstance(budgets, BudgetLedger):
        ledger: dict[str, object] = {
            "tokens": budgets.tokens,
            "wall_seconds": budgets.wall_seconds,
        }
        for key, counter in (
            ("role_spent", budgets.role_spent),
            ("harness_spent", budgets.harness_spent),
            ("role_reserved", budgets.role_reserved),
            ("harness_reserved", budgets.harness_reserved),
        ):
            if counter:
                ledger[key] = dict(counter)
        return {"ledger": ledger}
    if not isinstance(budgets, Mapping):
        raise TypeError("budgets must be a BudgetLedger or object")
    raw = copy.deepcopy(dict(budgets))
    if "ledger" in raw:
        return cast(dict[str, object], raw)
    return {"ledger": raw}


def _validate_mode(config: TLLoopConfig, effects: EffectClient | ReadOnlyEffectClient) -> None:
    if config.active and not isinstance(effects, EffectClient):
        raise TypeError("active TL loops require EffectClient")
    if not config.active and not isinstance(effects, ReadOnlyEffectClient):
        raise TypeError("shadow TL loops require ReadOnlyEffectClient")


def _workers(value: object) -> tuple[WorkerTask, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("work plan workers must be an array")
    return tuple(_worker(item) for item in value)


def _leaves(value: object) -> tuple[LeafTask, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("work plan leaves must be an array")
    return tuple(_leaf(item) for item in value)


def _sub_tls(value: object, *, path: str = "plan.sub_tls") -> tuple[SubTLTask, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("work plan sub_tls must be an array")
    return tuple(_sub_tl(item, path=f"{path}[{index}]") for index, item in enumerate(value))


def _sub_tl(value: object, *, path: str = "plan.sub_tls[0]") -> SubTLTask:
    if isinstance(value, SubTLTask):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("sub-TL task must be an object")
    allowed = {
        "name",
        "plan",
        "workers",
        "leaves",
        "sub_tls",
        "source",
        "effects",
        "agent_type",
        "worktree",
        "agent_id",
        "order",
        "integration",
        "task_timeout_seconds",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"sub-TL contains unknown keys: {', '.join(unknown)}")
    plan_value = value.get("plan")
    if plan_value is None:
        plan_value = {key: value[key] for key in ("workers", "leaves", "sub_tls") if key in value}
    plan = (
        plan_value
        if isinstance(plan_value, WorkPlan)
        else WorkPlan.from_mapping(cast(Mapping[str, object], plan_value), path=f"{path}.plan")
    )
    integration = _integration_contract(value.get("integration"))
    return SubTLTask(
        _required_text(value, "name", "sub-TL"),
        plan,
        cast(EventQueue | None, value.get("source")),
        cast(EffectClient | ReadOnlyEffectClient | None, value.get("effects")),
        _optional_string(value, "agent_type", "sub-TL"),
        cast(str | Path | None, value.get("worktree")),
        _optional_string(value, "agent_id", "sub-TL"),
        _positive_order(value.get("order", 1), f"{path}.order"),
        integration,
        "order" in value,
        _optional_timeout(value.get("task_timeout_seconds"), f"{path}.task_timeout_seconds"),
        "task_timeout_seconds" in value,
    )


def _positive_order(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _integration_contract(value: object) -> IntegrationContract:
    if value is None:
        return IntegrationContract()
    if not isinstance(value, Mapping):
        raise TypeError("sub-TL integration must be an object")
    allowed = {
        "aggregate_pr_required",
        "base_revalidation_required",
        "leaf_review_owner",
        "aggregate_review_owner",
        "aggregate_repair_owner",
        "merge_strategy",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"sub-TL integration contains unknown keys: {', '.join(unknown)}")

    def owner(name: str) -> ReviewOwner:
        raw = value.get(
            name,
            ReviewOwner.LEAF.value if name == "leaf_review_owner" else ReviewOwner.AGGREGATE.value,
        )
        try:
            return ReviewOwner(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be 'leaf' or 'aggregate'") from error

    return IntegrationContract(
        aggregate_pr_required=value.get("aggregate_pr_required", True),
        base_revalidation_required=value.get("base_revalidation_required", True),
        leaf_review_owner=owner("leaf_review_owner"),
        aggregate_review_owner=owner("aggregate_review_owner"),
        aggregate_repair_owner=owner("aggregate_repair_owner"),
        merge_strategy=value.get("merge_strategy", "merge"),
    )


def normalize_work_plan(plan: WorkPlan, *, path: str = "plan") -> WorkPlan:
    """Normalize explicit sibling orders and validate stage ownership.

    A plan with no explicit orders is intentionally returned in source order.
    Once ordered mode is selected, all direct sub-TLs must declare an order
    and values must be contiguous from one. Direct workers and leaves remain
    an unordered parallel block that runs before the ordered sub-TL stages.
    The nested plans have already gone through this function with their own
    path, so recursive order scopes remain independent.
    """
    tasks = plan.sub_tls
    if not tasks or not any(task.order_explicit for task in tasks):
        return plan
    if not all(task.order_explicit for task in tasks):
        missing = next(index for index, task in enumerate(tasks) if not task.order_explicit)
        raise ValueError(f"{path}.sub_tls[{missing}].order is required when ordered mode is used")
    orders = sorted({task.order for task in tasks})
    expected = list(range(1, len(orders) + 1))
    if orders != expected:
        raise ValueError(f"{path}.sub_tls order values must be contiguous from 1; got {orders}")
    for order in orders:
        peers = [task for task in tasks if task.order == order]
        _validate_concurrent_ownership(peers, path, order)
    return WorkPlan(
        workers=plan.workers,
        leaves=plan.leaves,
        sub_tls=tuple(sorted(tasks, key=lambda task: (task.order, task.name))),
    )


def _validate_concurrent_ownership(tasks: Sequence[SubTLTask], path: str, order: int) -> None:
    owned: list[tuple[str, str]] = []
    for task in tasks:
        for owned_path in _sub_tl_owned_paths(task):
            for other_name, other_path in owned:
                if _patterns_overlap(owned_path, other_path):
                    raise ValueError(
                        f"{path} order {order} ownership overlaps between "
                        f"{task.name!r} path {owned_path!r} and "
                        f"{other_name!r} path {other_path!r}"
                    )
            owned.append((task.name, owned_path))


def _sub_tl_owned_paths(task: SubTLTask) -> tuple[str, ...]:
    paths: list[str] = []
    for leaf in task.plan.leaves if isinstance(task.plan, WorkPlan) else ():
        paths.extend(leaf.boundary or (f"tl-loop/{task.name}/{leaf.name}",))
    for child in task.plan.sub_tls if isinstance(task.plan, WorkPlan) else ():
        paths.extend(_sub_tl_owned_paths(child))
    return tuple(paths or (f"tl-loop/{task.name}",))


def _patterns_overlap(left: str, right: str) -> bool:
    return left == right or fnmatchcase(left, right) or fnmatchcase(right, left)


def _worker(value: object) -> WorkerTask:
    if isinstance(value, WorkerTask):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("worker task must be an object")
    return WorkerTask(
        name=_required_text(value, "name", "worker"),
        task=_required_text(value, "task", "worker"),
        agent_type=_optional_string(value, "agent_type", "worker"),
        task_timeout_seconds=_optional_timeout(
            value.get("task_timeout_seconds"), "worker.task_timeout_seconds"
        ),
        task_timeout_declared="task_timeout_seconds" in value,
    )


def _leaf(value: object) -> LeafTask:
    if isinstance(value, LeafTask):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("leaf task must be an object")
    return LeafTask(
        name=_required_text(value, "name", "leaf"),
        task=_required_text(value, "task", "leaf"),
        agent_type=_optional_string(value, "agent_type", "leaf"),
        boundary=_string_tuple(value.get("boundary", ()), "leaf boundary"),
        context=_optional_string(value, "context", "leaf"),
        read_first=_string_tuple(value.get("read_first", ()), "leaf read_first"),
        steps=_string_tuple(value.get("steps", ()), "leaf steps"),
        verify=_string_tuple(value.get("verify", ()), "leaf verify"),
        done_criteria=_string_tuple(value.get("done_criteria", ()), "leaf done_criteria"),
        task_timeout_seconds=_optional_timeout(
            value.get("task_timeout_seconds"), "leaf.task_timeout_seconds"
        ),
        task_timeout_declared="task_timeout_seconds" in value,
    )


def _required_text(value: Mapping[str, object], key: str, kind: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise ValueError(f"{kind}.{key} must be a non-empty string")
    return candidate


def _optional_string(value: Mapping[str, object], key: str, kind: str) -> str | None:
    candidate = value.get(key)
    if candidate is None:
        return None
    if not isinstance(candidate, str) or not candidate:
        raise ValueError(f"{kind}.{key} must be null or a non-empty string")
    return candidate


def _optional_timeout(value: object, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be null or a non-negative number")
    parsed = float(value)
    if parsed < 0 or not math.isfinite(parsed):
        raise ValueError(f"{label} must be null or a non-negative number")
    return None if parsed == 0 else parsed


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{label} must be an array")
    if any(not isinstance(item, str) or not item for item in value):
        raise TypeError(f"{label} entries must be non-empty strings")
    return tuple(value)


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")


def _optional_text(value: str | None, label: str) -> None:
    if value is not None:
        _require_text(value, label)


def _text_tuple(value: Sequence[str], label: str) -> None:
    _string_tuple(value, label)


def _optional_argument(arguments: dict[str, object], key: str, value: object) -> None:
    if value is not None:
        arguments[key] = value


__all__ = [
    "DepthLimitExceeded",
    "EffectFailed",
    "EffectIntent",
    "EventQueue",
    "LeafTask",
    "LoopCancelled",
    "LoopLimitExceeded",
    "SubTLTask",
    "TLLoopConfig",
    "TLLoopError",
    "TLRunResult",
    "WorkPlan",
    "WorkerTask",
    "derive_child_branch",
    "derive_child_worktree",
    "run_tl_loop",
    "tl_run",
]
