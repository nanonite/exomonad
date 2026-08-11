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
    PRMerged,
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
from tl_loop.select.agent_type import select_agent_type, selection_failure
from tl_loop.select.capability import CapabilityMap, load_capability
from tl_loop.select.ledger import apply_spawn_and_charge
from tl_loop.select.model import ModelCatalog, select_model
from tl_loop.select.policy import HarnessPolicy, load_policy
from tl_loop.state.schema import BudgetLedger, RunState, SliceStatus
from tl_loop.state.store import DEFAULT_ROOT, RunStore, create

from .shadow import TLEventDecoder, _phase_from_state, _phase_tag, _update_slices

LOGGER = logging.getLogger(__name__)


class TLLoopError(RuntimeError):
    """The TL loop cannot continue without operator intervention."""


class LoopLimitExceeded(TLLoopError):
    """The loop reached its event ceiling before reaching a terminal state."""


class LoopTimeout(TLLoopError):
    """The loop received no event for its configured idle window."""


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
        ):
            _text_tuple(values, f"leaf {field_name}")


@dataclass(frozen=True)
class WorkPlan:
    """Direct children the TL may dispatch for one bounded run."""

    workers: tuple[WorkerTask, ...] = ()
    leaves: tuple[LeafTask, ...] = ()

    def __post_init__(self) -> None:
        names = [task.name for task in self.workers] + [task.name for task in self.leaves]
        if len(names) != len(set(names)):
            raise ValueError("worker and leaf names must be unique")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> WorkPlan:
        """Parse the small, closed plan shape used by the TL entry point."""
        unknown = sorted(set(value) - {"workers", "leaves"})
        if unknown:
            raise ValueError(f"work plan contains unknown keys: {', '.join(unknown)}")
        return cls(
            workers=_workers(value.get("workers", ())),
            leaves=_leaves(value.get("leaves", ())),
        )


@dataclass(frozen=True)
class TLLoopConfig:
    """Safety ceilings and effect mode for one TL invocation."""

    active: bool = True
    max_workers: int = 8
    max_leaves: int = 8
    max_events: int = 256
    poll_interval: float = 0.1
    idle_timeout: float = 30.0
    chainlink_issue_id: int | None = None
    merge_force: bool | None = None
    merge_strategy: str | None = None
    working_dir: str | None = None
    source: EventQueue | None = None
    effects: EffectClient | ReadOnlyEffectClient | None = None
    root_dir: str | Path = DEFAULT_ROOT
    run_id: str = "tl-run"
    policy: HarnessPolicy | None = None
    capabilities: CapabilityMap | None = None
    catalog: ModelCatalog | None = None
    requested_model: str | None = None
    role: str = "worker"

    def __post_init__(self) -> None:
        for name in ("max_workers", "max_leaves", "max_events"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_events == 0:
            raise ValueError("max_events must be positive")
        if self.poll_interval < 0:
            raise ValueError("poll_interval must be non-negative")
        if self.idle_timeout <= 0:
            raise ValueError("idle_timeout must be positive")
        if self.chainlink_issue_id is not None and self.chainlink_issue_id <= 0:
            raise ValueError("chainlink_issue_id must be positive")
        _optional_text(self.merge_strategy, "merge_strategy")
        _optional_text(self.working_dir, "working_dir")
        _require_text(self.run_id, "run_id")
        _require_text(self.role, "role")
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
        initial_slices=_initial_slices(plan),
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

    store = RunStore(run_id, Path(root_dir))
    if not store.path.exists():
        root_state: dict[str, object] = {}
        if initial_slices is not None:
            root_state["slices"] = copy.deepcopy(dict(initial_slices))
        if budgets is not None:
            root_state["budgets"] = _budget_root(budgets)
        create(run_id, root_state, root_dir=store.root_dir)
    state = store.load()
    effects_log: list[EffectIntent] = []
    state = _dispatch_children(work_plan, state, selected, effects, effects_log, store)
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
    deadline = time.monotonic() + config.idle_timeout

    while len(consumed) < config.max_events:
        if isinstance(phase, (TLDone, TLFailed)):
            break
        event = _next_event(source, config, deadline)
        if event is None:
            deadline = time.monotonic() + config.idle_timeout
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
        try:
            fsm_event = decoder.decode(event)
        except Exception as error:
            raise TLLoopError(str(error)) from error
        if _duplicate_event(phase, fsm_event, state):
            _checkpoint_and_ack(store, source, event, state, phase)
            state = store.load()
            continue
        try:
            next_phase = transition(phase, fsm_event)
        except IllegalTransition as error:
            raise TLLoopError(str(error)) from error
        if isinstance(fsm_event, ChildCompleted):
            _merge_completed_leaf(
                event,
                fsm_event,
                leaf_names,
                merged,
                effects,
                config,
                effects_log,
            )
        next_slices = _update_slices(state.slices, fsm_event)
        state = store.checkpoint(next_phase, next_slices, state.budgets, event_seq)
        source.acknowledge(event)
        before_tag = _phase_tag(phase)
        after_tag = _phase_tag(next_phase)
        transitions.append(LoopTransition(event_seq, event.event_type, before_tag, after_tag))
        LOGGER.info(
            "[TL loop] transition run_id=%s event_seq=%d before=%s after=%s",
            run_id,
            event_seq,
            before_tag.value,
            after_tag.value,
        )
        phase = next_phase
        if isinstance(phase, (TLDone, TLFailed)):
            break
    else:
        raise LoopLimitExceeded(
            f"event limit {config.max_events} reached before TL reached a terminal phase"
        )
    if not isinstance(phase, (TLDone, TLFailed)):
        raise LoopTimeout(f"TL did not reach a terminal phase within {config.idle_timeout:g}s")
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
    for worker in plan.workers:
        if _already_dispatched(worker.name, state):
            continue
        selected_harness = _prepare_spawn(worker.name, state, config, store)
        if selected_harness is not None:
            state = store.load()
        worker_args: dict[str, object] = {"name": worker.name, "task": worker.task}
        _optional_argument(
            worker_args, "agent_type", selected_harness or worker.agent_type
        )
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
        if _already_dispatched(leaf.name, state):
            continue
        selected_harness = _prepare_spawn(leaf.name, state, config, store)
        if selected_harness is not None:
            state = store.load()
        leaf_args: dict[str, object] = {"name": leaf.name, "task": leaf.task}
        _optional_argument(
            leaf_args, "agent_type", selected_harness or leaf.agent_type
        )
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
    return store.load() if config.policy is not None else state


def _already_dispatched(name: str, state: RunState) -> bool:
    current = state.slices.get(name)
    return current is not None and current.status is not SliceStatus.PENDING


def _prepare_spawn(
    name: str,
    state: RunState,
    config: TLLoopConfig,
    store: RunStore,
) -> str | None:
    if config.policy is None:
        return None
    slice_state = state.slices.get(name)
    if slice_state is None:
        raise TLLoopError(f"selector slice {name!r} is missing from run state")
    capabilities = config.capabilities or load_capability()
    choice = select_agent_type(
        slice_state, config.role, state.budgets, config.policy, capabilities
    )
    if choice is None:
        failure = selection_failure(
            slice_state, config.role, state.budgets, config.policy, capabilities
        )
        raise TLLoopError(f"cannot select harness for {name!r}: {failure.value}")
    model_id: str | None = None
    if config.catalog is not None:
        model_id = select_model(
            choice.harness, config.catalog, config.requested_model
        ).model_id

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
) -> None:
    pr_number = event.pr_number
    if completion.slug not in leaf_names or pr_number is None or completion.slug in merged:
        return
    arguments: dict[str, object] = {"pr_number": pr_number}
    _optional_argument(arguments, "chainlink_issue_id", config.chainlink_issue_id)
    _optional_argument(arguments, "force", config.merge_force)
    _optional_argument(arguments, "strategy", config.merge_strategy)
    _optional_argument(arguments, "working_dir", config.working_dir)
    live = cast(EffectClient, effects) if config.active else None
    _invoke(
        "merge_pr",
        completion.slug,
        arguments,
        config.active,
        live,
        lambda client: client.merge_pr(
            pr_number=pr_number,
            chainlink_issue_id=config.chainlink_issue_id,
            force=config.merge_force,
            strategy=config.merge_strategy,
            working_dir=config.working_dir,
        ),
        effects_log,
    )
    merged.add(completion.slug)


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
    if event.agent_id in expected:
        return True
    for key in ("slug", "child_agent", "slice_id"):
        value = event.data.get(key)
        if isinstance(value, str) and value in expected:
            return True
    return False


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
        if plan_value is None and ("workers" in raw or "leaves" in raw):
            plan_value = {key: raw[key] for key in ("workers", "leaves") if key in raw}
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


def _initial_slices(plan: WorkPlan) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for worker in plan.workers:
        result[worker.name] = _initial_slice_record(
            worker.name, (f"tl-loop/{worker.name}",), ("controller",), worker.agent_type
        )
    for leaf in plan.leaves:
        paths = leaf.boundary or (f"tl-loop/{leaf.name}",)
        test_plan = leaf.verify or leaf.steps or ("controller",)
        result[leaf.name] = _initial_slice_record(
            leaf.name, paths, test_plan, leaf.agent_type
        )
    return result


def _initial_slice_record(
    name: str, paths: Sequence[str], test_plan: Sequence[str], agent_type: str | None
) -> dict[str, object]:
    return {
        "id": name,
        "status": SliceStatus.PENDING.value,
        "paths": list(paths),
        "depends_on": [],
        "base_ref": None,
        "test_plan": list(test_plan),
        "agent_type": agent_type,
        "model": None,
        "branch": None,
        "worktree": None,
        "pr_number": None,
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
    "EffectFailed",
    "EffectIntent",
    "EventQueue",
    "LeafTask",
    "LoopLimitExceeded",
    "LoopTimeout",
    "TLLoopConfig",
    "TLLoopError",
    "TLRunResult",
    "WorkPlan",
    "WorkerTask",
    "run_tl_loop",
    "tl_run",
]
