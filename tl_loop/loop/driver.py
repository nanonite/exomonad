"""Bounded active and shadow execution for the programmatic TL."""

from __future__ import annotations

import logging
import queue as queue_module
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
from tl_loop.state.schema import RunState
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


def run_tl_loop(
    run_id: str,
    plan: WorkPlan | Mapping[str, object],
    source: EventQueue,
    effects: EffectClient | ReadOnlyEffectClient,
    *,
    config: TLLoopConfig | None = None,
    root_dir: str | Path = DEFAULT_ROOT,
    decoder: TLEventDecoder | None = None,
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
        create(run_id, {}, root_dir=store.root_dir)
    state = store.load()
    effects_log: list[EffectIntent] = []
    _dispatch_children(work_plan, state, selected, effects, effects_log)
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
) -> None:
    live = cast(EffectClient, effects) if config.active else None
    for worker in plan.workers:
        if worker.name in state.slices:
            continue
        worker_args: dict[str, object] = {"name": worker.name, "task": worker.task}
        _optional_argument(worker_args, "agent_type", worker.agent_type)
        _invoke(
            "spawn_worker",
            worker.name,
            worker_args,
            config.active,
            live,
            _worker_call(worker),
            effects_log,
        )
    for leaf in plan.leaves:
        if leaf.name in state.slices:
            continue
        leaf_args: dict[str, object] = {"name": leaf.name, "task": leaf.task}
        _optional_argument(leaf_args, "agent_type", leaf.agent_type)
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
            _leaf_call(leaf),
            effects_log,
        )


def _worker_call(task: WorkerTask) -> Callable[[EffectClient], ToolResult]:
    def invoke(client: EffectClient) -> ToolResult:
        return client.spawn_worker(name=task.name, task=task.task, agent_type=task.agent_type)

    return invoke


def _leaf_call(task: LeafTask) -> Callable[[EffectClient], ToolResult]:
    def invoke(client: EffectClient) -> ToolResult:
        return client.spawn_leaf(
            name=task.name,
            task=task.task,
            agent_type=task.agent_type,
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
        return event.handle.slug in state.slices
    if isinstance(event, (ChildCompleted, ChildFailed, PRMerged)):
        active = phase.children if isinstance(phase, (TLWaiting, TLMerging)) else {}
        return event.slug not in active
    if isinstance(event, AllChildrenDone):
        return isinstance(phase, (TLDone, TLFailed))
    return False


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
]
