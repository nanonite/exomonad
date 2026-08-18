"""Bounded active and shadow execution for the programmatic TL."""

from __future__ import annotations

import copy
import hashlib
import logging
import multiprocessing
import queue as queue_module
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.client.transport import JsonObject, TransportClient
from tl_loop.events.envelope import EnvelopeError, EventEnvelope, EventKind, project
from tl_loop.events.identity import (
    IdentityResolution,
    envelope_document,
    resolve_event_slice,
)
from tl_loop.events.reader import FindingKind, LedgerFinding
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
from tl_loop.fsm.phase import (
    ChildHandle,
    PhaseValue,
    TLDone,
    TLFailed,
    TLMerging,
    TLPhase,
    TLPlanning,
    TLWaiting,
)
from tl_loop.fsm.transition import IllegalTransition, transition
from tl_loop.loop.escalate import park
from tl_loop.loop.heartbeat import HeartbeatConfig, SyntheticHeartbeatEvent, heartbeat_once
from tl_loop.loop.review import (
    IntegrationEvidenceMismatch,
    ReviewGateError,
    compose_acceptance_criteria,
    invalidate_integration_evidence,
    load_freshness_window,
    verify_integration,
    verify_review,
    watcher_head,
    watcher_patch_digest,
)
from tl_loop.loop.schedule import ScheduleDeadlock, ready
from tl_loop.ordered import (
    AggregateCandidate,
    IntegrationContract,
    IntegrationLifecycle,
    IntegrationState,
    IntegrationTransition,
    IntegrationTransitionError,
    OrderedStage,
    ReviewOwner,
    transition_integration,
)
from tl_loop.rlm.adjudicate import adjudicate_review
from tl_loop.rlm.repair import RepairError, RepairHandoff, compose_repair
from tl_loop.select.agent_type import parse_harness_identifier, select_agent_type, selection_failure
from tl_loop.select.capability import CapabilityMap, load_capability
from tl_loop.select.learned_policy import LearnedPolicy
from tl_loop.select.ledger import apply_spawn_and_charge
from tl_loop.select.model import ModelCatalog, select_model
from tl_loop.select.policy import HarnessPolicy, load_policy
from tl_loop.state.schema import (
    CI_STATUS_VALUES,
    BudgetLedger,
    GateStatus,
    GoalState,
    IntegrationCandidateState,
    IntegrationRuntimeState,
    OrderedStageState,
    ParkCause,
    RunState,
    SliceState,
    SliceStatus,
    Verdict,
)
from tl_loop.state.store import DEFAULT_ROOT, RunStore, create

from .shadow import TLEventDecoder, _phase_from_state, _phase_tag, _update_slices

LOGGER = logging.getLogger(__name__)
TIMEOUT_GATE_NAME = "tl-timeout"
DISPATCH_TIMEOUT_GATE_NAME = "tl-dispatch-timeout"
DISPATCH_FAILURE_GATE_NAME = "tl-dispatch-failed"
INTEGRATION_REVALIDATION_GATE_NAME = "tl-integration-revalidation"
INTEGRATION_CONFLICT_GATE_NAME = "tl-integration-conflict"
DISPATCHING_STATUSES = frozenset({SliceStatus.DISPATCHING, SliceStatus.DISPATCH_UNCONFIRMED})


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
    stage_ids = {
        task_id
        for stage in plan.ordered_stages
        if state.current_order is None or stage.order == state.current_order
        for task_id in stage.sub_tls
    }
    statuses = [state.slices[task_id].status for task_id in stage_ids if task_id in state.slices]
    pending = {SliceStatus.SPAWNED, SliceStatus.IN_REVIEW, SliceStatus.REPAIRING}
    return (
        bool(statuses)
        and any(status in pending for status in statuses)
        and all(status in pending | {SliceStatus.MERGED} for status in statuses)
    )


class TLLoopError(RuntimeError):
    """The TL loop cannot continue without operator intervention."""


class LoopLimitExceeded(TLLoopError):
    """The loop reached its event ceiling before reaching a terminal state."""


class LoopTimeout(TLLoopError):
    """The loop received no event for its configured idle window."""

    def __init__(self, message: str, *, reason: str = "idle", timeout_seconds: float | None = None):
        super().__init__(message)
        self.deadline_reason = reason
        self.timeout_seconds = timeout_seconds


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


@dataclass(frozen=True)
class LoopDeadline:
    """Effective deadline and the budget that selected it."""

    at: float
    reason: str
    timeout_seconds: float


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
                self.filtered += 1

    def record_unresolved_event(
        self, event: EventEnvelope, resolution: IdentityResolution
    ) -> None:
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

    def __post_init__(self) -> None:
        _require_text(self.name, "worker name")
        _require_text(self.task, "worker task")
        _optional_text(self.agent_type, "worker agent_type")


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

    def __post_init__(self) -> None:
        _require_text(self.name, "leaf name")
        _require_text(self.task, "leaf task")
        _optional_text(self.agent_type, "leaf agent_type")
        _optional_text(self.context, "leaf context")
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
        return tuple(OrderedStage(order, tuple(grouped[order])) for order in sorted(grouped))


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
    idle_timeout: float = 30.0
    keep_alive_on_waiting: bool = False
    dispatch_timeout: float = 5.0
    controller_stall_timeout: float = 300.0
    max_base_revalidations: int = 3
    max_integration_repairs: int = 3
    heartbeat: HeartbeatConfig | None = None
    goals: GoalState | None = None
    chainlink_issue_id: int | None = None
    merge_strategy: str | None = None
    working_dir: str | None = None
    source: EventQueue | None = None
    effects: EffectClient | ReadOnlyEffectClient | None = None
    root_dir: str | Path = DEFAULT_ROOT
    run_id: str = "tl-run"
    ledger_run_id: str | None = None
    policy: HarnessPolicy | None = None
    learned_policy: LearnedPolicy | None = None
    capabilities: CapabilityMap | None = None
    catalog: ModelCatalog | None = None
    requested_model: str | None = None
    role: str = "worker"
    review_policy_path: str | Path | None = None
    enable_reviewer_spawn: bool = False
    review_model_choice: object | None = None
    branch: str = "main"
    worktree: str | Path | None = None
    agent_id: str | None = None
    parent_branch: str | None = None
    parent_run_id: str | None = None
    parent_agent_id: str | None = None
    depth: int = 0
    max_depth: int = 3

    def __post_init__(self) -> None:
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
        if self.poll_interval < 0:
            raise ValueError("poll_interval must be non-negative")
        if self.idle_timeout <= 0:
            raise ValueError("idle_timeout must be positive")
        if type(self.keep_alive_on_waiting) is not bool:
            raise ValueError("keep_alive_on_waiting must be a boolean")
        if self.dispatch_timeout <= 0:
            raise ValueError("dispatch_timeout must be positive")
        if self.controller_stall_timeout <= 0:
            raise ValueError("controller_stall_timeout must be positive")
        if type(self.enable_reviewer_spawn) is not bool:
            raise ValueError("enable_reviewer_spawn must be a boolean")
        if self.chainlink_issue_id is not None and self.chainlink_issue_id <= 0:
            raise ValueError("chainlink_issue_id must be positive")
        _optional_text(self.merge_strategy, "merge_strategy")
        _optional_text(self.working_dir, "working_dir")
        _require_text(self.run_id, "run_id")
        _optional_text(self.ledger_run_id, "ledger_run_id")
        _require_text(self.role, "role")
        for name in ("depth", "max_depth"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        _require_text(self.branch, "branch")
        _optional_text(self.agent_id, "agent_id")
        _optional_text(self.parent_branch, "parent_branch")
        _optional_text(self.parent_run_id, "parent_run_id")
        _optional_text(self.parent_agent_id, "parent_agent_id")
        if self.worktree is not None:
            _require_text(str(self.worktree), "worktree")
        _optional_text(self.requested_model, "requested_model")


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
        initial_slices=_initial_slices(plan, selected),
    )


def run_tl_loop(
    run_id: str,
    plan: WorkPlan | Mapping[str, object],
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
    work_plan = plan if isinstance(plan, WorkPlan) else WorkPlan.from_mapping(plan)
    _validate_mode(selected, effects)
    if len(work_plan.workers) > selected.max_workers:
        raise LoopLimitExceeded("work plan exceeds max_workers")
    if len(work_plan.leaves) > selected.max_leaves:
        raise LoopLimitExceeded("work plan exceeds max_leaves")
    initial_slices = initial_slices or _initial_slices(work_plan, selected, root_dir, run_id)

    store = RunStore(run_id, Path(root_dir))
    if not store.path.exists():
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
        if budgets is not None:
            root_state["budgets"] = _budget_root(budgets)
        if selected.ledger_run_id is not None:
            root_state["ledger_run_id"] = selected.ledger_run_id
        create(run_id, root_state, root_dir=store.root_dir)
    state = store.load()
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
    state = _initialize_ordered_runtime(work_plan, state, store)
    effects_log: list[EffectIntent] = []
    state = _reconcile_dispatches(state, selected, effects, store, effects_log)
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
    leaf_names = {task.name for task in plan.leaves}
    merged: set[str] = set()
    transitions: list[LoopTransition] = []
    consumed: list[int] = []
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
    if not expected and not plan.sub_tls:
        before_phase = phase
        state = store.checkpoint(
            TLDone(), state.slices, state.budgets, state.events.last_consumed_offset
        )
        _emit_phase_change(
            run_id,
            before_phase,
            _phase_from_state(state),
            config,
            effects,
            effects_log,
        )
        return TLRunResult(
            state,
            tuple(effects_log),
            tuple(transitions),
            tuple(consumed),
            diagnostics=diagnostics.snapshot(),
        )

    deadline = _next_loop_deadline(state, config)

    while not config.test_harness or len(consumed) < config.max_events:
        if isinstance(phase, (TLDone, TLFailed)):
            break
        if config.cancel_event is not None and config.cancel_event.is_set():
            raise LoopCancelled(f"TL controller {run_id!r} was cancelled")
        _record_reader_findings(source, diagnostics)
        replaying = False
        replay_index = _replayable_event_index(quarantined, state, expected)
        if replay_index is not None:
            event = quarantined.pop(replay_index)
            replaying = True
        else:
            try:
                event = _next_event(source, config, deadline)
            except LoopTimeout as error:
                _record_reader_findings(source, diagnostics)
                if _has_pending_dispatch(state):
                    return _park_dispatch_timeout(
                        run_id,
                        store,
                        state,
                        effects,
                        config,
                        effects_log,
                        transitions,
                        consumed,
                        error,
                        diagnostics,
                    )
                deadline = _next_loop_deadline(state, config)
                continue
        _record_reader_findings(source, diagnostics)
        if event is None:
            if config.heartbeat is not None:
                heartbeat = heartbeat_once(
                    state,
                    store,
                    effects,
                    config.heartbeat,
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
                    if heartbeat.parked_slice_ids and _all_expected_terminal(state, expected):
                        before_phase = phase
                        state = store.checkpoint(
                            TLFailed("heartbeat parked the remaining active slices"),
                            state.slices,
                            state.budgets,
                            state.events.last_consumed_offset,
                        )
                        phase = _phase_from_state(state)
                        _emit_phase_change(
                            run_id, before_phase, phase, config, effects, effects_log
                        )
            if _sub_tls_waiting_for_integration(plan, state):
                if not config.keep_alive_on_waiting:
                    return TLRunResult(
                        state,
                        tuple(effects_log),
                        tuple(transitions),
                        tuple(consumed),
                        tuple(heartbeat_events),
                        diagnostics.snapshot(),
                    )
                deadline = _next_loop_deadline(state, config)
            continue
        event_seq = event.run_seq
        if event_seq is None:
            raise TLLoopError(f"{event.event_type!r} has no run_seq")
        if not replaying:
            consumed.append(event_seq)
            diagnostics.received += 1
        diagnostics.last_event_seq = event_seq
        checkpoint_seq = max(event_seq, state.events.last_consumed_offset)
        deadline = _next_loop_deadline(state, config)
        ledger_run_id = config.ledger_run_id or run_id
        if event.run_id not in {None, ledger_run_id}:
            diagnostics.filtered += 1
            _checkpoint_and_ack(
                store, source, event, state, phase, acknowledge=not replaying
            )
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
        if event.kind in {EventKind.PR_REVIEW, EventKind.COPILOT_REVIEW}:
            diagnostics.correlated += 1
            if _review_workflow_enabled(config):
                state = _route_review_event(
                    plan, store, state, phase, event, checkpoint_seq, config, effects, effects_log
                )
            else:
                state = _record_review_event(store, state, phase, event, checkpoint_seq)
            if plan.sub_tls:
                state = _run_sub_tls(plan, state, config, source, effects, store, effects_log)
                phase = _phase_from_state(state)
                deadline = _next_loop_deadline(state, config)
            _ack_event(source, event, replaying, diagnostics)
            _release_replayed_event(store, event, replaying)
            if isinstance(phase, (TLDone, TLFailed)):
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
                deadline = _next_loop_deadline(state, config)
            _ack_event(source, event, replaying, diagnostics)
            _release_replayed_event(store, event, replaying)
            if isinstance(phase, (TLDone, TLFailed)):
                break
            continue
        # Rust's direct agent.spawned records carry the canonical branch and
        # child identity; shadowed replay records use the normal decoder path.
        if event.kind is EventKind.AGENT_SPAWNED:
            if not _dispatch_confirmation_matches(state.slices, event):
                diagnostics.filtered += 1
                diagnostics.rejected += 1
                LOGGER.warning(
                    "Ignoring uncorrelated agent.spawned event run_id=%s event_seq=%s",
                    run_id,
                    event_seq,
                )
                _checkpoint_and_ack(
                    store, source, event, state, phase, acknowledge=not replaying
                )
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
                next_phase = transition(phase, fsm_event)
            except IllegalTransition as error:
                raise TLLoopError(str(error)) from error
            next_slices = _confirm_dispatch_event(
                state.slices, state.slices, event, event_slice_id, event_seq
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
            if isinstance(phase, (TLDone, TLFailed)):
                break
            continue
        try:
            fsm_event = decoder.decode(event)
        except Exception as error:
            raise TLLoopError(str(error)) from error
        if _duplicate_event(phase, fsm_event, state):
            diagnostics.filtered += 1
            _checkpoint_and_ack(
                store, source, event, state, phase, acknowledge=not replaying
            )
            if not replaying:
                diagnostics.acknowledged += 1
            state = store.load()
            continue
        if isinstance(fsm_event, ChildCompleted):
            merge_allowed = _merge_completed_leaf(
                event,
                fsm_event,
                leaf_names,
                merged,
                effects,
                config,
                effects_log,
                state,
            )
            if not merge_allowed:
                next_slices = _discard_review(state.slices, fsm_event.slug)
                state = store.checkpoint(phase, next_slices, state.budgets, checkpoint_seq)
                _ack_event(source, event, replaying, diagnostics)
                _release_replayed_event(store, event, replaying)
                continue
        try:
            next_phase = transition(phase, fsm_event)
        except IllegalTransition as error:
            raise TLLoopError(str(error)) from error
        event_slice_id = _event_slice_id(event, state)
        next_slices = _update_slices(
            state.slices,
            fsm_event,
            slice_id=event_slice_id,
            allow_spawn_confirmation=_dispatch_confirmation_matches(state.slices, event),
        )
        if (
            isinstance(fsm_event, ChildCompleted)
            and event_slice_id is not None
            and event.pr_number is not None
            and event.head_sha is not None
            and event_slice_id in next_slices
        ):
            next_slices[event_slice_id] = replace(
                next_slices[event_slice_id],
                pr_number=event.pr_number,
                reviewed_head=event.head_sha,
            )
        if _is_spawn_confirmation_event(event):
            next_slices = _confirm_dispatch_event(
                state.slices, next_slices, event, event_slice_id, event_seq
            )
        head_changed = _pr_head_changed(state.slices, fsm_event, event_slice_id)
        if head_changed and config.enable_reviewer_spawn:
            next_slices = _claim_reviewer_attempt(
                next_slices, fsm_event, _event_slice_id(event, state)
            )
        previous_state = state
        state = store.checkpoint(next_phase, next_slices, state.budgets, checkpoint_seq)
        deadline = _next_loop_deadline(state, config)
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
        if head_changed and config.enable_reviewer_spawn:
            _spawn_reviewer_for_head(plan, state, fsm_event, event, config, effects, effects_log)
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
        if isinstance(phase, (TLDone, TLFailed)):
            break
    else:
        if config.test_harness:
            raise LoopLimitExceeded(
                f"event limit {config.max_events} reached before TL reached a terminal phase"
            )
    if not isinstance(phase, (TLDone, TLFailed)):
        raise LoopTimeout(f"TL did not reach a terminal phase within {config.idle_timeout:g}s")
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
    )


def _park_timeout(
    run_id: str,
    store: RunStore,
    state: RunState,
    effects: EffectClient | ReadOnlyEffectClient,
    config: TLLoopConfig,
    effects_log: list[EffectIntent],
    transitions: list[LoopTransition],
    consumed: list[int],
    reason: str | LoopTimeout,
    diagnostics: EventDiagnostics,
) -> TLRunResult:
    """Persist a named timeout gate before returning a terminal failed run."""
    before_phase = _phase_from_state(state)
    previous_gate = next(
        (gate for gate in state.gates if gate.name == TIMEOUT_GATE_NAME),
        None,
    )
    state = store.set_gate(TIMEOUT_GATE_NAME, GateStatus.PENDING)
    if previous_gate is None or previous_gate.status is not GateStatus.PENDING:
        _record_controller_event(
            "controller",
            "tl.gate_opened",
            {"gate_name": TIMEOUT_GATE_NAME, "run_id": run_id},
            config,
            effects,
            effects_log,
        )
    timeout_reason = _diagnostic_timeout_reason(reason, diagnostics)
    message = f"timeout parked at gate {TIMEOUT_GATE_NAME!r}: {timeout_reason}"
    state = store.checkpoint(
        TLFailed(message),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
    )
    _emit_phase_change(
        run_id,
        before_phase,
        _phase_from_state(state),
        config,
        effects,
        effects_log,
    )
    LOGGER.warning("[TL loop] %s", message)
    store.record_terminal_summary(
        {
            "reason": message,
            "deadline_reason": getattr(reason, "deadline_reason", "idle"),
            "timeout_seconds": getattr(reason, "timeout_seconds", None),
            "diagnostics": diagnostics.snapshot(),
        }
    )
    return TLRunResult(
        state,
        tuple(effects_log),
        tuple(transitions),
        tuple(consumed),
        diagnostics=diagnostics.snapshot(),
    )


def _has_pending_dispatch(state: RunState) -> bool:
    return any(slice_state.status in DISPATCHING_STATUSES for slice_state in state.slices.values())


def _diagnostic_timeout_reason(reason: str | LoopTimeout, diagnostics: EventDiagnostics) -> str:
    if diagnostics.received == 0:
        return str(reason)
    return (
        f"{reason}; consumed {diagnostics.received} event(s), filtered "
        f"{diagnostics.filtered}, rejected {diagnostics.rejected} without a correlated event"
    )


def _record_reader_findings(source: EventQueue, diagnostics: EventDiagnostics) -> None:
    """Promote reader-side filtering into durable controller diagnostics."""
    findings = getattr(source, "findings", ())
    if findings:
        diagnostics.record_reader_findings(tuple(findings))


def _next_loop_deadline(state: RunState, config: TLLoopConfig) -> LoopDeadline | None:
    """Return no lifecycle deadline; evidence and cancellation drive progress."""
    del state, config
    return None


def _next_waiting_deadline(config: TLLoopConfig) -> LoopDeadline:
    """Use the liveness budget while review or CI is legally outstanding."""
    timeout = (
        config.heartbeat.stall_threshold_seconds
        if config.heartbeat is not None
        else config.controller_stall_timeout
    )
    reason = "stall" if config.heartbeat is not None else "waiting"
    return LoopDeadline(time.monotonic() + timeout, reason, timeout)


def _park_dispatch_timeout(
    run_id: str,
    store: RunStore,
    state: RunState,
    effects: EffectClient | ReadOnlyEffectClient,
    config: TLLoopConfig,
    effects_log: list[EffectIntent],
    transitions: list[LoopTransition],
    consumed: list[int],
    reason: str | LoopTimeout,
    diagnostics: EventDiagnostics,
) -> TLRunResult:
    """Persist a distinct dispatch timeout when spawn proof never arrives."""
    before_phase = _phase_from_state(state)
    if diagnostics.received:
        observed = (
            f"consumed {diagnostics.received} event(s), filtered {diagnostics.filtered}, "
            f"rejected {diagnostics.rejected} without an authoritative match"
        )
    else:
        observed = "no event was received"
    bounded_reason = (
        f"no authoritative agent.spawned event within {config.dispatch_timeout:g}s; "
        f"{observed}; {reason}"
    )[:500]
    pending = [
        current for current in state.slices.values() if current.status in DISPATCHING_STATUSES
    ]
    updated_slices = {
        current.id: replace(
            current,
            status=SliceStatus.DISPATCH_UNCONFIRMED,
            park_cause=ParkCause.DISPATCH_TIMEOUT,
            dispatch_last_boundary="dispatch_timeout",
            dispatch_error=bounded_reason,
        )
        for current in pending
    }
    state = store.checkpoint(
        TLFailed(f"dispatch timeout: {bounded_reason}"),
        {**state.slices, **updated_slices},
        state.budgets,
        state.events.last_consumed_offset,
    )
    previous_gate = next(
        (gate for gate in state.gates if gate.name == DISPATCH_TIMEOUT_GATE_NAME),
        None,
    )
    state = store.set_gate(DISPATCH_TIMEOUT_GATE_NAME, GateStatus.PENDING)
    if previous_gate is None or previous_gate.status is not GateStatus.PENDING:
        _record_controller_event(
            "controller",
            "tl.gate_opened",
            {"gate_name": DISPATCH_TIMEOUT_GATE_NAME, "run_id": run_id},
            config,
            effects,
            effects_log,
        )
    for current in pending:
        if current.dispatch_intent_id is None or current.dispatch_started_at is None:
            continue
        attempt = DispatchAttempt(
            current.dispatch_intent_id,
            current.dispatch_started_at,
            current.agent_type or "",
        )
        _record_controller_event(
            current.id,
            "tl.dispatch_reconciliation_completed",
            _dispatch_payload(
                current.id,
                attempt,
                "dispatch_timeout",
                error=bounded_reason,
            ),
            config,
            effects,
            effects_log,
        )
    _emit_phase_change(
        run_id,
        before_phase,
        _phase_from_state(state),
        config,
        effects,
        effects_log,
    )
    LOGGER.warning("[TL loop] dispatch timeout: %s", bounded_reason)
    store.record_terminal_summary(
        {
            "reason": f"dispatch timeout: {bounded_reason}",
            "deadline_reason": "dispatch",
            "timeout_seconds": config.dispatch_timeout,
            "diagnostics": diagnostics.snapshot(),
        }
    )
    return TLRunResult(
        state,
        tuple(effects_log),
        tuple(transitions),
        tuple(consumed),
        diagnostics=diagnostics.snapshot(),
    )


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
    updated = replace(
        current,
        status=SliceStatus.DISPATCH_UNCONFIRMED,
        park_cause=ParkCause.DISPATCH_UNCONFIRMED,
        dispatch_last_boundary=boundary,
        dispatch_agent_id=_spawn_agent_id(result),
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
    updated = replace(
        current,
        status=SliceStatus.DISPATCH_FAILED,
        park_cause=ParkCause.DISPATCH_FAILED,
        dispatch_last_boundary="spawn_request_failed",
        dispatch_error=bounded_reason,
    )
    before_phase = _phase_from_state(state)
    state = store.checkpoint(
        TLFailed(f"dispatch failed for {slice_id!r}: {bounded_reason}"),
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
                        current,
                        status=SliceStatus.SPAWNED,
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
                        current,
                        status=SliceStatus.DISPATCH_UNCONFIRMED,
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
    if result is None or result.success is False or not isinstance(result.result, Mapping):
        return None
    agents = result.result.get("agents")
    if not isinstance(agents, list):
        return None
    for raw_agent in agents:
        if not isinstance(raw_agent, Mapping):
            continue
        if raw_agent.get("intent_id") != intent_id or raw_agent.get("is_alive") is not True:
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
    }
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
) -> dict[str, SliceState]:
    if (
        not _is_spawn_confirmation_event(event)
        or slice_id is None
        or not _dispatch_confirmation_matches(previous_slices, event)
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
            current,
            status=SliceStatus.SPAWNED,
            park_cause=None,
            dispatch_last_boundary="agent.spawned",
            dispatch_error=None,
            dispatch_agent_id=agent_id,
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


def _dispatch_confirmation_matches(slices: Mapping[str, SliceState], event: EventEnvelope) -> bool:
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
    return len(matches) == 1


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
        worker_args: dict[str, object] = {"name": worker.name, "task": worker.task}
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
                _worker_call(worker, agent_type, model, attempt.intent_id),
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
        leaf_args: dict[str, object] = {"name": leaf.name, "task": leaf.task}
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
                _leaf_call(leaf, agent_type, model, attempt.intent_id),
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
    for stage in plan.ordered_stages:
        stage_tasks = tuple(tasks_by_name[name] for name in stage.sub_tls)
        state = store.load()
        stage_states = [state.slices.get(task.name) for task in stage_tasks]
        if any(current is None for current in stage_states):
            missing = next(
                task.name for task, current in zip(stage_tasks, stage_states) if current is None
            )
            raise TLLoopError(f"recursive slice {missing!r} is missing")
        if any(
            current.status in {SliceStatus.FAILED, SliceStatus.PARKED}
            for current in stage_states
            if current is not None
        ):
            return _fail_recursive_parent(
                state, config, effects, store, effects_log, "recursive child is not recoverable"
            )
        was_stage_complete = all(
            current is not None and current.status is SliceStatus.MERGED for current in stage_states
        )
        pending = tuple(
            task
            for task, current in zip(stage_tasks, stage_states)
            if current is not None
            and current.status
            not in {SliceStatus.MERGED, SliceStatus.IN_REVIEW, SliceStatus.REPAIRING}
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
    if plan.sub_tls and not plan.workers and not plan.leaves:
        awaiting_integration = tuple(
            task.name
            for task in plan.sub_tls
            if state.slices[task.name].status
            in {SliceStatus.SPAWNED, SliceStatus.IN_REVIEW, SliceStatus.REPAIRING}
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
        state.slices[slice_id].status is not SliceStatus.MERGED
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
                current, status=SliceStatus.PARKED, park_cause=ParkCause.SCHEDULE_DEADLOCK
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
                current,
                status=SliceStatus.SPAWNED,
                base_ref=config.branch,
                branch=branch,
                worktree=worktree,
                dispatch_intent_id=internal_intent_id,
                dispatch_started_at=internal_attempt.started_at,
                dispatch_last_boundary="sub_tl_started",
                dispatch_agent_id=task.name,
                dispatch_authoritative_event_seq=authoritative_seq,
            )
        prepared.append(task)
    states = dict(state.integration.sub_tl_states)
    for task in prepared:
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
            child_phase = child_state.fsm.phase
            if child_phase in {TLPhase.TLDone, TLPhase.TLFailed}:
                return task, child_phase, child_state
        child_config = _child_config(config, task, source, effects, store, branch, worktree)
        try:
            child_result = tl_run({"run_id": task.name, "plan": task.plan}, child_config, budgets)
        except Exception as error:  # noqa: BLE001 - batch completion persists a durable failure
            child_store.record_exit_reason(str(error))
            return task, TLPhase.TLFailed, None
        return task, child_result.final_state.fsm.phase, child_result.final_state

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [executor.submit(run_one, task) for task in tasks]
        return tuple(future.result() for future in futures)


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
    processes = [
        (
            task,
            context.Process(
                target=_run_live_sub_tl,
                args=(task, config, source, effects, store, budgets),
                name=f"tl-sub-{task.name}",
            ),
        )
        for task in tasks
    ]
    for _, process in processes:
        process.start()
    outcomes: list[tuple[SubTLTask, TLPhase | None, RunState | None]] = []
    for task, process in processes:
        child_store = RunStore(task.name, store.run_dir)
        child_state = _supervise_live_sub_tl(process, child_store, config)
        phase = child_state.fsm.phase if child_state is not None else TLPhase.TLFailed
        outcomes.append((task, phase, child_state))
    return tuple(outcomes)


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
        process.join(timeout=poll_interval)
    exitcode = getattr(process, "exitcode", None)
    if not cancelled and exitcode not in (None, 0):
        try:
            child_state = child_store.load()
        except (OSError, ValueError):
            child_state = None
        if child_state is None or child_state.fsm.phase not in {TLPhase.TLDone, TLPhase.TLFailed}:
            child_store.record_exit_reason(
                f"sub-TL controller exited unexpectedly with code {exitcode}"
            )
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
    candidate_records = dict(state.integration.candidates)
    for task, phase, child_state in outcomes:
        if phase not in {TLPhase.TLDone, TLPhase.TLFailed}:
            current = updated_slices[task.name]
            updated_slices[task.name] = replace(current, status=SliceStatus.SPAWNED)
            continue
        status = SliceStatus.MERGED if phase is TLPhase.TLDone else SliceStatus.FAILED
        previous_lifecycle = sub_tl_states.get(task.name, IntegrationLifecycle.RUNNING)
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
                current = replace(
                    current,
                    status=SliceStatus.IN_REVIEW,
                    pr_number=candidate.pr_number,
                    reviewed_head=candidate.head_sha,
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
        updated_slices[task.name] = replace(current, status=status)
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
        candidates=candidate_records,
    )
    previous_slices = state.slices
    state = store.checkpoint(
        _phase_from_state(state),
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
    else:
        body = (
            f"Aggregate sub-TL `{task.name}` into `{config.branch}`.\n"
            f"Owner: `{owner_id}`\n"
            f"Head: `{fallback_head}`\n"
            f"Patch: `{fallback_patch}`"
        )
        owner_effects = _owner_effect_client(effects, task.name)
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
            pr_number = (
                max(
                    (slice_state.pr_number or 0 for slice_state in child_state.slices.values()),
                    default=0,
                )
                + 1
            )
        candidate = AggregateCandidate(
            task.name,
            pr_number,
            _result_text(result_data, "head_sha") or fallback_head,
            _result_text(result_data, "patch_digest") or fallback_patch,
            _result_text(result_data, "base_sha") or fallback_base,
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
    child_store.set_ordered_state(
        child_state.current_order,
        child_state.ordered_stages,
        updated_integration,
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
        if (
            current.status is not SliceStatus.IN_REVIEW
            or current.pr_number is None
            or current.reviewed_head is None
            or current.verdict not in {Verdict.GO, Verdict.GO_WITH_NITS}
            or current.ci_state.get(current.reviewed_head) not in {"success", "neutral"}
        ):
            continue
        state = _integrate_one_candidate(task, state, config, effects, store, effects_log)
        if state.slices[task.name].status is not SliceStatus.MERGED:
            break
    return state


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
    updated = replace(
        current,
        status=SliceStatus.IN_REVIEW,
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
            current,
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
            current,
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
    conflicted = replace(
        current,
        status=SliceStatus.REPAIRING,
        verdict=Verdict.NO_GO,
        ci_state={},
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
                },
                config,
                effects,
                effects_log,
            )
            return _checkpoint_aggregate_merged(task, state, store, candidate_runtime)
    first = first or _watcher_snapshot(current.pr_number, config, effects, effects_log)
    if first is None:
        return state
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
    state = store.checkpoint(
        _phase_from_state(state),
        state.slices,
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
    arguments = {
        "pr_number": current.pr_number,
        "strategy": config.merge_strategy or task.integration.merge_strategy,
        "working_dir": config.working_dir,
        "base_sha": base_sha,
    }
    merge_result = _invoke(
        "merge_pr",
        task.name,
        arguments,
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
    return _checkpoint_aggregate_merged(task, state, store, integration)


def _checkpoint_aggregate_merged(
    task: SubTLTask,
    state: RunState,
    store: RunStore,
    integration: IntegrationRuntimeState,
) -> RunState:
    """Persist one merge result, including a restart reconciliation result."""
    current = state.slices[task.name]
    updated_slices = dict(state.slices)
    updated_slices[task.name] = replace(
        current,
        status=SliceStatus.MERGED,
        dispatch_last_boundary="aggregate_merged",
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
        if sibling_slice.status is SliceStatus.IN_REVIEW
    }
    checkpoint_phase: PhaseValue = TLWaiting(remaining) if remaining else TLPlanning()
    return store.checkpoint(
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


def _watcher_snapshot(
    pr_number: int,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> Mapping[str, object] | None:
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
    return result.result


def _snapshot_text(snapshot: Mapping[str, object], key: str) -> str | None:
    value = snapshot.get(key)
    return value if isinstance(value, str) and value else None


def _snapshot_bool(snapshot: Mapping[str, object], key: str) -> bool:
    value = snapshot.get(key)
    return value is True or value == "true"


def _now_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
        TLFailed(reason),
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
        depth=config.depth + 1,
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
        updated = replace(
            current,
            status=SliceStatus.DISPATCHING,
            attempts=current.attempts + 1,
            dispatch_intent_id=intent.intent_id,
            dispatch_started_at=intent.started_at,
            dispatch_last_boundary="dispatch_intended",
            dispatch_error=None,
            dispatch_agent_id=None,
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
        model_id = select_model(choice.harness, config.catalog, config.requested_model).model_id
    else:
        model_id = route.model

    intent = DispatchAttempt(
        intent.intent_id,
        intent.started_at,
        choice.harness,
        route.agent_type,
        model_id,
    )

    def record_spawn(document: dict[str, object]) -> dict[str, object]:
        slices = document.get("slices")
        if not isinstance(slices, dict):
            raise TLLoopError("run state slices are not an object")
        raw_slice = slices.get(name)
        if not isinstance(raw_slice, dict):
            raise TLLoopError(f"selector slice {name!r} is not an object")
        raw_slice["status"] = SliceStatus.DISPATCHING.value
        raw_slice["agent_type"] = route.agent_type
        raw_slice["model"] = model_id
        raw_slice["attempts"] = slice_state.attempts + 1
        raw_slice["dispatch_intent_id"] = intent.intent_id
        raw_slice["dispatch_started_at"] = intent.started_at
        raw_slice["dispatch_last_boundary"] = "dispatch_intended"
        raw_slice["dispatch_error"] = None
        raw_slice["dispatch_agent_id"] = None
        raw_slice["dispatch_authoritative_event_seq"] = None
        raw_slice["park_cause"] = None
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
    identity = f"{state.run_id}:{name}:{attempt}"
    intent_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    started_at = time.time() if config.active else 0.0
    return DispatchAttempt(intent_id, started_at, "")


def _worker_call(
    task: WorkerTask,
    selected_agent_type: str | None,
    selected_model: str | None,
    intent_id: str | None,
) -> Callable[[EffectClient], ToolResult]:
    def invoke(client: EffectClient) -> ToolResult:
        return client.spawn_worker(
            name=task.name,
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
) -> Callable[[EffectClient], ToolResult]:
    def invoke(client: EffectClient) -> ToolResult:
        return client.spawn_leaf(
            name=task.name,
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


def _merge_completed_leaf(
    event: EventEnvelope,
    completion: ChildCompleted,
    leaf_names: set[str],
    merged: set[str],
    effects: EffectClient | ReadOnlyEffectClient,
    config: TLLoopConfig,
    effects_log: list[EffectIntent],
    state: RunState,
) -> bool:
    pr_number = event.pr_number
    if completion.slug not in leaf_names or pr_number is None or completion.slug in merged:
        return True
    current = state.slices.get(completion.slug)
    head_sha = event.head_sha or (current.reviewed_head if current is not None else None)
    live = cast(EffectClient, effects) if config.active else None
    if (
        config.active
        and live is not None
        and current is not None
        and (current.verdict is not None or current.reviewed_head is not None)
    ):
        watcher_arguments = {"pr_number": pr_number}
        effects_log.append(
            EffectIntent("watcher_pr_state", completion.slug, watcher_arguments, True)
        )
        LOGGER.info(
            "[TL loop] effect=watcher_pr_state target=%s active=true",
            completion.slug,
        )
        watcher_result = live.watcher_pr_state(pr_number=pr_number)
        if watcher_result.success is False:
            raise EffectFailed(watcher_result.error or "watcher_pr_state returned failure")
        try:
            freshness_window_secs = (
                load_freshness_window(config.review_policy_path)
                if config.review_policy_path is not None
                else None
            )
            current_head = watcher_head(watcher_result)
            current_patch_digest = watcher_patch_digest(watcher_result)
            verify_review(
                current,
                current_head,
                freshness_window_secs=freshness_window_secs,
                current_patch_digest=current_patch_digest,
            )
            head_sha = current_head
        except ReviewGateError as error:
            LOGGER.warning(
                "[TL loop] refusing merge target=%s reason=%s",
                completion.slug,
                error,
            )
            _emit_merge_decision(
                completion.slug,
                pr_number,
                "blocked",
                head_sha,
                config,
                effects,
                effects_log,
            )
            return False
    _emit_merge_decision(
        completion.slug,
        pr_number,
        "merge",
        head_sha,
        config,
        effects,
        effects_log,
    )
    arguments: dict[str, object] = {"pr_number": pr_number}
    _optional_argument(arguments, "chainlink_issue_id", config.chainlink_issue_id)
    _optional_argument(arguments, "strategy", config.merge_strategy)
    _optional_argument(arguments, "working_dir", config.working_dir)
    _invoke(
        "merge_pr",
        completion.slug,
        arguments,
        config.active,
        live,
        lambda client: client.merge_pr(
            pr_number=pr_number,
            chainlink_issue_id=config.chainlink_issue_id,
            strategy=config.merge_strategy,
            working_dir=config.working_dir,
        ),
        effects_log,
    )
    merged.add(completion.slug)
    return True


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
    return {
        **slices,
        slice_id: replace(
            current,
            status=SliceStatus.IN_REVIEW,
            verdict=None,
            reviewed_head=None,
            verdict_at=None,
            stall_classification=None,
        ),
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
    if verdict is None:
        updated = dict(state.slices)
        updated[slice_id] = replace(
            current,
            pr_number=event.pr_number or current.pr_number,
            review_findings=review_findings,
            review_patch_digests=patch_digests,
            stall_classification=stall_classification or current.stall_classification,
        )
        return store.checkpoint(phase, updated, state.budgets, event_seq)
    updated = dict(state.slices)
    updated[slice_id] = replace(
        current,
        status=(
            SliceStatus.IN_REVIEW
            if current.status is SliceStatus.REPAIRING and _is_aggregate_slice(current)
            else current.status
        ),
        pr_number=event.pr_number or current.pr_number,
        reviewed_head=event.head_sha,
        verdict=verdict,
        verdict_at=event.observed_at,
        review_findings=review_findings,
        review_patch_digests=patch_digests,
        stall_classification=stall_classification or current.stall_classification,
    )
    return store.checkpoint(phase, updated, state.budgets, event_seq)


def _review_workflow_enabled(config: TLLoopConfig) -> bool:
    return config.active and config.review_model_choice is not None


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
            current,
            review_findings=review_findings,
            review_patch_digests=patch_digests,
            stall_classification=stall_classification or current.stall_classification,
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
    result = adjudicate_review(
        _review_diff(event),
        findings,
        list(criteria),
        head_sha,
        model_choice=config.review_model_choice,
        policy_path=config.review_policy_path or Path(".exo/review-policy.toml"),
    )
    review_findings = _persist_adjudication_nits(review_findings, head_sha, result.reasons)
    updated = dict(state.slices)
    updated[slice_id] = replace(
        current,
        pr_number=event.pr_number or current.pr_number,
        reviewed_head=result.reviewed_head,
        verdict=result.verdict,
        verdict_at=event.observed_at,
        review_findings=review_findings,
        review_patch_digests=patch_digests,
        ci_state={} if current.reviewed_head != result.reviewed_head else current.ci_state,
        stall_classification=stall_classification or current.stall_classification,
    )
    state = store.checkpoint(phase, updated, state.budgets, event_seq)
    if _is_aggregate_slice(updated[slice_id]):
        state = _record_aggregate_review_lifecycle(
            store, state, phase, event_seq, slice_id, result.verdict
        )
    if result.verdict is Verdict.NO_GO:
        return _route_repair(
            store,
            state,
            phase,
            event_seq,
            slice_id,
            result,
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
    ci_state = dict(current.ci_state)
    ci_state[head_sha] = status
    updated = dict(state.slices)
    updated[slice_id] = replace(
        current,
        pr_number=event.pr_number or current.pr_number,
        ci_state=ci_state,
        verdict=Verdict.NO_GO if status == "failure" else current.verdict,
        verdict_at=event.observed_at if status == "failure" else current.verdict_at,
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
    live = cast(EffectClient, effects)
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
        handoff = compose_repair(
            pr,
            Verdict.NO_GO,
            review,
            client=live,
            model_choice=config.review_model_choice,
            store=store,
            slice_id=slice_id,
        )
    except (RepairError, ValueError) as error:
        parked = replace(
            current,
            status=SliceStatus.PARKED,
            park_cause=ParkCause.REVIEW_STUCK,
            stall_classification="review_stuck",
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
    effects_log.append(
        EffectIntent(
            "resume_pr",
            slice_id,
            _repair_arguments(current.pr_number, handoff),
            True,
        )
    )
    refreshed = store.load()
    return store.checkpoint(phase, refreshed.slices, refreshed.budgets, event_seq)


def _repair_arguments(pr_number: int, handoff: RepairHandoff) -> dict[str, object]:
    root_cause = handoff.root_cause
    proposed_solution = handoff.proposed_solution
    return {
        "pr_number": pr_number,
        "task": proposed_solution,
        "context": f"ROOT CAUSE: {root_cause}\nPROPOSED SOLUTION: {proposed_solution}",
        "read_first": list(handoff.read_first),
        "steps": list(handoff.steps),
        "verify": list(handoff.verify),
        "boundary": list(handoff.boundary),
        "done_criteria": list(handoff.done_criteria),
    }


def _review_verdict(event: EventEnvelope) -> Verdict | None:
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
    effects_log.append(EffectIntent("emit_controller_event", target, payload, config.active))
    LOGGER.info(
        "[TL loop] effect=emit_controller_event target=%s event_type=%s active=%s",
        target,
        event_type,
        config.active,
    )
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
) -> ToolResult | None:
    effects_log.append(EffectIntent(operation, target, arguments, active))
    LOGGER.info("[TL loop] effect=%s target=%s active=%s", operation, target, active)
    if not active:
        return None
    if client is None:
        raise TLLoopError("active loop has no effect client")
    result = call(client)
    if result.success is False and raise_on_failure:
        detail = result.error or f"{operation} returned failure"
        raise EffectFailed(f"{operation} for {target!r}: {detail}")
    return result


def _next_event(
    source: EventQueue,
    config: TLLoopConfig,
    deadline: LoopDeadline | None,
) -> EventEnvelope | None:
    if config.cancel_event is not None and config.cancel_event.is_set():
        raise LoopCancelled("TL controller cancellation requested")
    if deadline is None:
        remaining = config.poll_interval or 0.01
    else:
        remaining = deadline.at - time.monotonic()
    if remaining <= 0 and deadline is not None:
        raise LoopTimeout(
            f"TL did not receive an event within {deadline.timeout_seconds:g}s "
            f"(deadline={deadline.reason})",
            reason=deadline.reason,
            timeout_seconds=deadline.timeout_seconds,
        )
    timeout = min(config.poll_interval or 0.01, remaining)
    try:
        return source.get(timeout=timeout)
    except queue_module.Empty:
        if config.poll_interval == 0:
            time.sleep(min(0.01, remaining))
        return None


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


def _pr_head_changed(
    slices: Mapping[str, SliceState], event: TLEvent, slice_id: str | None
) -> bool:
    if not isinstance(event, (PRFiled, PRUpdated)):
        return False
    target_id = _pr_event_target(slices, event, slice_id)
    current = slices.get(target_id) if target_id is not None else None
    return current is not None and current.reviewed_head != event.head_sha


def _claim_reviewer_attempt(
    slices: Mapping[str, SliceState], event: TLEvent, slice_id: str | None
) -> dict[str, SliceState]:
    if not isinstance(event, (PRFiled, PRUpdated)):
        return dict(slices)
    target_id = _pr_event_target(slices, event, slice_id)
    current = slices.get(target_id) if target_id is not None else None
    if target_id is None or current is None:
        return dict(slices)
    attempts = dict(current.reviewer_attempt)
    if attempts.get(event.head_sha, 0) > 0:
        return dict(slices)
    attempts[event.head_sha] = 1
    return {**slices, target_id: replace(current, reviewer_attempt=attempts)}


def _spawn_reviewer_for_head(
    plan: WorkPlan,
    state: RunState,
    event: TLEvent,
    envelope: EventEnvelope,
    config: TLLoopConfig,
    effects: EffectClient | ReadOnlyEffectClient,
    effects_log: list[EffectIntent],
) -> None:
    if not isinstance(event, (PRFiled, PRUpdated)):
        return
    target_id = _pr_event_target(state.slices, event, envelope.slice_id)
    current = state.slices.get(target_id) if target_id is not None else None
    leaf = next((candidate for candidate in plan.leaves if candidate.name == target_id), None)
    if target_id is None or current is None or leaf is None:
        return
    criteria = compose_acceptance_criteria(current, leaf)
    arguments: dict[str, object] = {
        "pr_number": event.pr_number,
        "head_sha": event.head_sha,
        "acceptance_criteria": list(criteria),
        "force": False,
    }
    live = cast(EffectClient, effects) if config.active else None
    _invoke(
        "spawn_reviewer",
        target_id,
        arguments,
        config.active,
        live,
        lambda client: client.spawn_reviewer(
            pr_number=event.pr_number,
            head_sha=event.head_sha,
            acceptance_criteria=criteria,
            force=False,
        ),
        effects_log,
    )


def _duplicate_event(phase: PhaseValue, event: TLEvent, state: RunState) -> bool:
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
) -> tuple[WorkPlan, str, EventQueue, EffectClient | ReadOnlyEffectClient]:
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
            raise TypeError("root_spec must contain a work plan")
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
        )
    for leaf in plan.leaves:
        paths = leaf.boundary or (f"tl-loop/{leaf.name}",)
        test_plan = leaf.verify or leaf.steps or ("controller",)
        result[leaf.name] = _initial_slice_record(
            leaf.name,
            paths,
            test_plan,
            leaf.agent_type,
            derive_child_branch(selected.branch, leaf.name) if nested else None,
            str(derive_child_worktree(owner_worktree, leaf.name)) if nested else None,
            selected.parent_branch if nested else None,
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


def _initial_slice_record(
    name: str,
    paths: Sequence[str],
    test_plan: Sequence[str],
    agent_type: str | None,
    branch: str | None = None,
    worktree: str | None = None,
    base_ref: str | None = None,
) -> dict[str, object]:
    return {
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
    }


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
    Once ordered mode is selected, all direct sub-TLs must declare an order,
    values must be contiguous from one, and top-level leaves are forbidden.
    The nested plans have already gone through this function with their own
    path, so recursive order scopes remain independent.
    """
    tasks = plan.sub_tls
    if not tasks or not any(task.order_explicit for task in tasks):
        return plan
    if not all(task.order_explicit for task in tasks):
        missing = next(index for index, task in enumerate(tasks) if not task.order_explicit)
        raise ValueError(f"{path}.sub_tls[{missing}].order is required when ordered mode is used")
    if plan.leaves:
        raise ValueError(f"{path}.leaves must be empty when ordered sub-TL stages are used")
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
        sub_tls=tuple(sorted(tasks, key=lambda task: task.order)),
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
        _required_text(value, "name", "worker"),
        _required_text(value, "task", "worker"),
        _optional_string(value, "agent_type", "worker"),
    )


def _leaf(value: object) -> LeafTask:
    if isinstance(value, LeafTask):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("leaf task must be an object")
    return LeafTask(
        _required_text(value, "name", "leaf"),
        _required_text(value, "task", "leaf"),
        _optional_string(value, "agent_type", "leaf"),
        _string_tuple(value.get("boundary", ()), "leaf boundary"),
        _optional_string(value, "context", "leaf"),
        _string_tuple(value.get("read_first", ()), "leaf read_first"),
        _string_tuple(value.get("steps", ()), "leaf steps"),
        _string_tuple(value.get("verify", ()), "leaf verify"),
        _string_tuple(value.get("done_criteria", ()), "leaf done_criteria"),
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
    "TIMEOUT_GATE_NAME",
    "DepthLimitExceeded",
    "EffectFailed",
    "EffectIntent",
    "EventQueue",
    "LeafTask",
    "LoopCancelled",
    "LoopLimitExceeded",
    "LoopTimeout",
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
