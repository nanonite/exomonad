"""Bounded active and shadow execution for the programmatic TL."""

from __future__ import annotations

import copy
import logging
import queue as queue_module
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.events.envelope import EventEnvelope, EventKind
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
    PhaseValue,
    TLDone,
    TLFailed,
    TLMerging,
    TLPhase,
    TLWaiting,
)
from tl_loop.fsm.transition import IllegalTransition, transition
from tl_loop.loop.escalate import park
from tl_loop.loop.heartbeat import HeartbeatConfig, SyntheticHeartbeatEvent, heartbeat_once
from tl_loop.loop.observability import emit_controller_event
from tl_loop.loop.review import (
    ReviewGateError,
    compose_acceptance_criteria,
    load_freshness_window,
    verify_review,
    watcher_head,
)
from tl_loop.loop.schedule import ScheduleDeadlock, ready
from tl_loop.rlm.adjudicate import adjudicate_review
from tl_loop.rlm.repair import RepairHandoff, compose_repair
from tl_loop.select.agent_type import select_agent_type, selection_failure
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


class TLLoopError(RuntimeError):
    """The TL loop cannot continue without operator intervention."""


class LoopLimitExceeded(TLLoopError):
    """The loop reached its event ceiling before reaching a terminal state."""


class LoopTimeout(TLLoopError):
    """The loop received no event for its configured idle window."""


class DepthLimitExceeded(TLLoopError):
    """A recursive child reached the configured depth ceiling."""


class EffectFailed(TLLoopError):
    """An active effect returned an explicit failure."""


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
    def from_mapping(cls, value: Mapping[str, object]) -> WorkPlan:
        """Parse the small, closed plan shape used by the TL entry point."""
        unknown = sorted(set(value) - {"workers", "leaves", "sub_tls"})
        if unknown:
            raise ValueError(f"work plan contains unknown keys: {', '.join(unknown)}")
        return cls(
            workers=_workers(value.get("workers", ())),
            leaves=_leaves(value.get("leaves", ())),
            sub_tls=_sub_tls(value.get("sub_tls", ())),
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

    def __post_init__(self) -> None:
        _require_text(self.name, "sub-TL name")
        if not isinstance(self.plan, (WorkPlan, Mapping)):
            raise TypeError("sub-TL plan must be a WorkPlan or object")
        _optional_text(self.agent_type, "sub-TL agent_type")
        _optional_text(self.agent_id, "sub-TL agent_id")
        if self.worktree is not None:
            _require_text(str(self.worktree), "sub-TL worktree")


@dataclass(frozen=True)
class TLLoopConfig:
    """Safety ceilings and effect mode for one TL invocation."""

    active: bool = True
    max_workers: int = 8
    max_leaves: int = 8
    max_parallel_slices: int | None = None
    max_events: int = 256
    poll_interval: float = 0.1
    idle_timeout: float = 30.0
    heartbeat: HeartbeatConfig | None = None
    goals: GoalState | None = None
    chainlink_issue_id: int | None = None
    merge_strategy: str | None = None
    working_dir: str | None = None
    source: EventQueue | None = None
    effects: EffectClient | ReadOnlyEffectClient | None = None
    root_dir: str | Path = DEFAULT_ROOT
    run_id: str = "tl-run"
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
        for name in ("max_workers", "max_leaves", "max_events"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_parallel_slices is not None and (
            type(self.max_parallel_slices) is not int or self.max_parallel_slices < 0
        ):
            raise ValueError("max_parallel_slices must be null or non-negative")
        if self.max_events == 0:
            raise ValueError("max_events must be positive")
        if self.poll_interval < 0:
            raise ValueError("poll_interval must be non-negative")
        if self.idle_timeout <= 0:
            raise ValueError("idle_timeout must be positive")
        if type(self.enable_reviewer_spawn) is not bool:
            raise ValueError("enable_reviewer_spawn must be a boolean")
        if self.chainlink_issue_id is not None and self.chainlink_issue_id <= 0:
            raise ValueError("chainlink_issue_id must be positive")
        _optional_text(self.merge_strategy, "merge_strategy")
        _optional_text(self.working_dir, "working_dir")
        _require_text(self.run_id, "run_id")
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
        create(run_id, root_state, root_dir=store.root_dir)
    state = store.load()
    effects_log: list[EffectIntent] = []
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
    expected = {task.name for task in plan.workers} | {task.name for task in plan.leaves}
    leaf_names = {task.name for task in plan.leaves}
    merged: set[str] = set()
    transitions: list[LoopTransition] = []
    consumed: list[int] = []
    heartbeat_events: list[SyntheticHeartbeatEvent] = []
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
        return TLRunResult(state, tuple(effects_log), tuple(transitions), tuple(consumed))

    deadline = time.monotonic() + config.idle_timeout

    while len(consumed) < config.max_events:
        if isinstance(phase, (TLDone, TLFailed)):
            break
        try:
            event = _next_event(source, config, deadline)
        except LoopTimeout as error:
            return _park_timeout(
                run_id,
                store,
                state,
                effects,
                config,
                effects_log,
                transitions,
                consumed,
                str(error),
            )
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
                    phase = _phase_from_state(state)
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
            continue
        event_seq = event.run_seq
        if event_seq is None:
            raise TLLoopError(f"{event.event_type!r} has no run_seq")
        consumed.append(event_seq)
        deadline = time.monotonic() + config.idle_timeout
        if event.run_id not in {None, run_id}:
            _checkpoint_and_ack(store, source, event, state, phase)
            state = store.load()
            continue
        if not _event_belongs_to_plan(event, expected):
            _checkpoint_and_ack(store, source, event, state, phase)
            state = store.load()
            continue
        if config.heartbeat is not None:
            state = _note_heartbeat_progress(store, state)
        if event.kind in {EventKind.PR_REVIEW, EventKind.COPILOT_REVIEW}:
            if _review_workflow_enabled(config):
                state = _route_review_event(
                    plan, store, state, phase, event, event_seq, config, effects, effects_log
                )
            else:
                state = _record_review_event(store, state, phase, event, event_seq)
            source.acknowledge(event)
            continue
        if event.kind is EventKind.CI_STATUS_CHANGED:
            state = _route_ci_event(
                store, state, phase, event, event_seq, config, effects, effects_log
            )
            source.acknowledge(event)
            continue
        try:
            fsm_event = decoder.decode(event)
        except Exception as error:
            raise TLLoopError(str(error)) from error
        if _duplicate_event(phase, fsm_event, state):
            _checkpoint_and_ack(store, source, event, state, phase)
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
                state = store.checkpoint(phase, next_slices, state.budgets, event_seq)
                source.acknowledge(event)
                continue
        try:
            next_phase = transition(phase, fsm_event)
        except IllegalTransition as error:
            raise TLLoopError(str(error)) from error
        next_slices = _update_slices(
            state.slices, fsm_event, slice_id=_event_slice_id(event, state)
        )
        head_changed = _pr_head_changed(state.slices, fsm_event, _event_slice_id(event, state))
        if head_changed and config.enable_reviewer_spawn:
            next_slices = _claim_reviewer_attempt(
                next_slices, fsm_event, _event_slice_id(event, state)
            )
        previous_state = state
        state = store.checkpoint(next_phase, next_slices, state.budgets, event_seq)
        _emit_slice_status_changes(
            previous_state.slices,
            next_slices,
            config,
            effects,
            effects_log,
        )
        if head_changed and config.enable_reviewer_spawn:
            _spawn_reviewer_for_head(plan, state, fsm_event, event, config, effects, effects_log)
        source.acknowledge(event)
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
        raise LoopLimitExceeded(
            f"event limit {config.max_events} reached before TL reached a terminal phase"
        )
    if not isinstance(phase, (TLDone, TLFailed)):
        raise LoopTimeout(f"TL did not reach a terminal phase within {config.idle_timeout:g}s")
    return TLRunResult(
        state, tuple(effects_log), tuple(transitions), tuple(consumed), tuple(heartbeat_events)
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
    reason: str,
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
    message = f"timeout parked at gate {TIMEOUT_GATE_NAME!r}: {reason}"
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
    return TLRunResult(state, tuple(effects_log), tuple(transitions), tuple(consumed))


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
        selected_harness = _prepare_spawn(worker.name, state, config, live, store)
        if selected_harness is not None:
            state = store.load()
        worker_args: dict[str, object] = {"name": worker.name, "task": worker.task}
        _optional_argument(worker_args, "agent_type", selected_harness or worker.agent_type)
        _invoke(
            "spawn_worker",
            worker.name,
            worker_args,
            config.active,
            live,
            _worker_call(worker, selected_harness),
            effects_log,
        )
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
        selected_harness = _prepare_spawn(leaf.name, state, config, live, store)
        if selected_harness is not None:
            state = store.load()
        leaf_args: dict[str, object] = {"name": leaf.name, "task": leaf.task}
        _optional_argument(leaf_args, "agent_type", selected_harness or leaf.agent_type)
        for name, value in (
            ("boundary", leaf.boundary),
            ("read_first", leaf.read_first),
            ("steps", leaf.steps),
            ("verify", leaf.verify),
        ):
            if value:
                leaf_args[name] = list(value)
        _optional_argument(leaf_args, "context", leaf.context)
        _invoke(
            "spawn_leaf",
            leaf.name,
            leaf_args,
            config.active,
            live,
            _leaf_call(leaf, selected_harness),
            effects_log,
        )
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
    """Run direct recursive children and retain only terminal child state."""
    for task in plan.sub_tls:
        current = state.slices.get(task.name)
        if current is None:
            raise TLLoopError(f"recursive slice {task.name!r} is missing")
        if current.status is not SliceStatus.PENDING:
            continue
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
            state = store.checkpoint(
                TLFailed(f"depth ceiling reached for {task.name}"),
                {**state.slices, task.name: parked},
                state.budgets,
                state.events.last_consumed_offset,
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
        spawned = replace(
            current,
            status=SliceStatus.SPAWNED,
            base_ref=config.branch,
            branch=branch,
            worktree=worktree,
        )
        previous_slices = state.slices
        state = store.checkpoint(
            _phase_from_state(state),
            {**state.slices, task.name: spawned},
            state.budgets,
            state.events.last_consumed_offset,
        )
        _emit_slice_status_changes(
            previous_slices,
            state.slices,
            config,
            effects,
            effects_log,
        )
        child_config = _child_config(config, task, source, effects, store, branch, worktree)
        child_result = tl_run({"run_id": task.name, "plan": task.plan}, child_config, state.budgets)
        status = (
            SliceStatus.MERGED
            if child_result.final_state.fsm.phase is TLPhase.TLDone
            else SliceStatus.FAILED
        )
        completed = replace(spawned, status=status)
        previous_slices = state.slices
        state = store.checkpoint(
            _phase_from_state(state),
            {**state.slices, task.name: completed},
            state.budgets,
            state.events.last_consumed_offset,
        )
        _emit_slice_status_changes(
            previous_slices,
            state.slices,
            config,
            effects,
            effects_log,
        )
        if status is SliceStatus.FAILED:
            before_phase = _phase_from_state(state)
            state = store.checkpoint(
                TLFailed(f"recursive child {task.name} failed"),
                state.slices,
                state.budgets,
                state.events.last_consumed_offset,
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
    if plan.sub_tls and not plan.workers and not plan.leaves:
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


def _child_config(
    config: TLLoopConfig,
    task: SubTLTask,
    source: EventQueue,
    effects: EffectClient | ReadOnlyEffectClient,
    store: RunStore,
    branch: str,
    worktree: str,
) -> TLLoopConfig:
    return replace(
        config,
        source=task.source or source,
        effects=task.effects or effects,
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
    live: EffectClient | None,
    store: RunStore,
) -> str | None:
    if config.policy is None:
        return None
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
            if live is None:
                raise TLLoopError("active loop has no effect client for escalation")
            park(
                slice_state,
                cause,
                store=store,
                issue_creator=live,
                ledger=state.budgets,
            )
        raise TLLoopError(f"cannot select harness for {name!r}: {failure.value}; slice parked")
    model_id: str | None = None
    if config.catalog is not None:
        model_id = select_model(choice.harness, config.catalog, config.requested_model).model_id

    def record_spawn(document: dict[str, object]) -> dict[str, object]:
        slices = document.get("slices")
        if not isinstance(slices, dict):
            raise TLLoopError("run state slices are not an object")
        raw_slice = slices.get(name)
        if not isinstance(raw_slice, dict):
            raise TLLoopError(f"selector slice {name!r} is not an object")
        raw_slice["status"] = SliceStatus.SPAWNED.value
        raw_slice["agent_type"] = choice.harness
        raw_slice["model"] = model_id
        raw_slice["attempts"] = slice_state.attempts + 1
        return document

    apply_spawn_and_charge(store.run_dir, choice, slice_state, record_spawn)
    LOGGER.info(
        "[TL loop] selection target=%s harness=%s model=%s estimate=%d",
        name,
        choice.harness,
        model_id or "unresolved",
        choice.estimated_cost,
    )
    return choice.harness


def _worker_call(
    task: WorkerTask, selected_harness: str | None
) -> Callable[[EffectClient], ToolResult]:
    def invoke(client: EffectClient) -> ToolResult:
        return client.spawn_worker(
            name=task.name,
            task=task.task,
            agent_type=selected_harness or task.agent_type,
        )

    return invoke


def _leaf_call(
    task: LeafTask, selected_harness: str | None
) -> Callable[[EffectClient], ToolResult]:
    def invoke(client: EffectClient) -> ToolResult:
        return client.spawn_leaf(
            name=task.name,
            task=task.task,
            agent_type=selected_harness or task.agent_type,
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
            verify_review(
                current,
                watcher_head(watcher_result),
                freshness_window_secs=freshness_window_secs,
            )
        except ReviewGateError as error:
            LOGGER.warning(
                "[TL loop] refusing merge target=%s reason=%s",
                completion.slug,
                error,
            )
            return False
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
    stall_classification = _event_stall_classification(event)
    if current.reviewed_head is not None and current.reviewed_head != event.head_sha:
        updated = dict(state.slices)
        updated[slice_id] = replace(current, review_findings=review_findings)
        return store.checkpoint(phase, updated, state.budgets, event_seq)
    verdict = _review_verdict(event)
    if verdict is None:
        updated = dict(state.slices)
        updated[slice_id] = replace(
            current,
            pr_number=event.pr_number or current.pr_number,
            review_findings=review_findings,
            stall_classification=stall_classification or current.stall_classification,
        )
        return store.checkpoint(phase, updated, state.budgets, event_seq)
    updated = dict(state.slices)
    updated[slice_id] = replace(
        current,
        pr_number=event.pr_number or current.pr_number,
        reviewed_head=event.head_sha,
        verdict=verdict,
        verdict_at=event.observed_at,
        review_findings=review_findings,
        stall_classification=stall_classification or current.stall_classification,
    )
    return store.checkpoint(phase, updated, state.budgets, event_seq)


def _review_workflow_enabled(config: TLLoopConfig) -> bool:
    return config.active and config.review_model_choice is not None


def _review_slice_id(event: EventEnvelope, state: RunState) -> str | None:
    direct = event.slice_id or event.agent_id
    if direct in state.slices:
        return direct
    if event.pr_number is None:
        return direct
    matches = [
        slice_id
        for slice_id, current in state.slices.items()
        if current.pr_number == event.pr_number
    ]
    return matches[0] if len(matches) == 1 else direct


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
            stall_classification=stall_classification or current.stall_classification,
        )
        return store.checkpoint(phase, updated, state.budgets, event_seq)
    if current.reviewed_head is not None and current.reviewed_head != head_sha:
        LOGGER.warning(
            "[TL loop] ignoring stale review target=%s reviewed=%s event=%s",
            slice_id,
            current.reviewed_head,
            head_sha,
        )
        updated = dict(state.slices)
        updated[slice_id] = replace(current, review_findings=review_findings)
        return store.checkpoint(phase, updated, state.budgets, event_seq)
    leaf = next((candidate for candidate in plan.leaves if candidate.name == slice_id), None)
    if leaf is None:
        raise TLLoopError(f"review event references non-leaf slice {slice_id!r}")
    criteria = compose_acceptance_criteria(current, leaf)
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
        stall_classification=stall_classification or current.stall_classification,
    )
    state = store.checkpoint(phase, updated, state.budgets, event_seq)
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
    handoff = compose_repair(
        pr,
        Verdict.NO_GO,
        review,
        client=live,
        model_choice=config.review_model_choice,
        store=store,
        slice_id=slice_id,
    )
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
) -> None:
    effects_log.append(EffectIntent("emit_controller_event", target, payload, config.active))
    LOGGER.info(
        "[TL loop] effect=emit_controller_event target=%s event_type=%s active=%s",
        target,
        event_type,
        config.active,
    )
    if not config.active:
        return
    live = cast(EffectClient, effects)
    emit_controller_event(live, event_type, payload)


def _invoke(
    operation: str,
    target: str,
    arguments: Mapping[str, object],
    active: bool,
    client: EffectClient | None,
    call: Callable[[EffectClient], ToolResult],
    effects_log: list[EffectIntent],
) -> None:
    effects_log.append(EffectIntent(operation, target, arguments, active))
    LOGGER.info("[TL loop] effect=%s target=%s active=%s", operation, target, active)
    if not active:
        return
    if client is None:
        raise TLLoopError("active loop has no effect client")
    result = call(client)
    if result.success is False:
        detail = result.error or f"{operation} returned failure"
        raise EffectFailed(f"{operation} for {target!r}: {detail}")


def _next_event(
    source: EventQueue,
    config: TLLoopConfig,
    deadline: float,
) -> EventEnvelope | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LoopTimeout(f"TL did not receive an event within {config.idle_timeout:g}s")
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
) -> None:
    if event.run_seq is None:
        raise TLLoopError(f"{event.event_type!r} has no run_seq")
    offset = max(state.events.last_consumed_offset, event.run_seq)
    store.checkpoint(phase, state.slices, state.budgets, offset)
    source.acknowledge(event)


def _event_belongs_to_plan(event: EventEnvelope, expected: set[str]) -> bool:
    if "shadow_event" in event.data:
        value = event.data["shadow_event"]
        if isinstance(value, Mapping):
            slug = value.get("slug")
            return slug is None or slug in expected
    if event.kind is EventKind.PR_FILED:
        return True
    if event.kind is EventKind.PR_UPDATED:
        return True
    if event.agent_id in expected:
        return True
    for key in ("slug", "child_agent", "slice_id"):
        value = event.data.get(key)
        if isinstance(value, str) and value in expected:
            return True
    return False


def _event_slice_id(event: EventEnvelope, state: RunState) -> str | None:
    if event.slice_id is not None:
        return event.slice_id
    if event.kind in {EventKind.PR_FILED, EventKind.PR_UPDATED} and event.agent_id in state.slices:
        return event.agent_id
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
    attempts[event.head_sha] = attempts.get(event.head_sha, 0) + 1
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


def _encode_goals(goals: GoalState) -> dict[str, object]:
    return {
        "objective": goals.objective,
        "deadline": goals.deadline,
        "completion_predicate": goals.completion_predicate,
        "last_heartbeat_at": goals.last_heartbeat_at,
        "last_progress_at": goals.last_progress_at,
    }


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


def _sub_tls(value: object) -> tuple[SubTLTask, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("work plan sub_tls must be an array")
    return tuple(_sub_tl(item) for item in value)


def _sub_tl(value: object) -> SubTLTask:
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
        else WorkPlan.from_mapping(cast(Mapping[str, object], plan_value))
    )
    return SubTLTask(
        _required_text(value, "name", "sub-TL"),
        plan,
        cast(EventQueue | None, value.get("source")),
        cast(EffectClient | ReadOnlyEffectClient | None, value.get("effects")),
        _optional_string(value, "agent_type", "sub-TL"),
        cast(str | Path | None, value.get("worktree")),
        _optional_string(value, "agent_id", "sub-TL"),
    )


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
