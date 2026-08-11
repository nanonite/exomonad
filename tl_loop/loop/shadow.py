"""Read-only shadow execution of the TL FSM."""

from __future__ import annotations

import copy
import queue as queue_module
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol

from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.events.envelope import EventEnvelope, EventKind
from tl_loop.fsm.event import (
    AllChildrenDone,
    ChildCompleted,
    ChildFailed,
    ChildSpawned,
    OwnPRFiled,
    PRMerged,
    TLEvent,
)
from tl_loop.fsm.phase import (
    ChildHandle,
    PhaseValue,
    TLAllMerged,
    TLDispatching,
    TLFailed,
    TLPhase,
    TLPlanning,
    TLPRFiled,
    TLDone,
    TLMerging,
    TLWaiting,
)
from tl_loop.fsm.transition import IllegalTransition, transition
from tl_loop.state.schema import RunState, SliceState, SliceStatus
from tl_loop.state.store import DEFAULT_ROOT, RunStore, create

DEFAULT_SHADOW_ROOT = DEFAULT_ROOT / "shadow"


class ShadowLoopError(RuntimeError):
    """A shadow event could not be decoded or checkpointed safely."""


class EventSource(Protocol):
    """Queue capability consumed by the shadow loop."""

    def get(self, timeout: float | None = None) -> EventEnvelope:
        """Return the next projected ledger event."""

    def acknowledge(self, event: EventEnvelope) -> int:
        """Persist consumption of one event sequence."""


class ActionRecorder(Protocol):
    """Persistence capability for intended actions."""

    def record(self, action: IntendedAction) -> None:
        """Persist one action before the source is acknowledged."""


class ShadowJudgments(Protocol):
    """The three judgment seams kept deterministic until the LLM milestone."""

    def choose_dispatch(self, event: TLEvent, before: PhaseValue, after: PhaseValue) -> Judgment:
        """Choose the intended dispatch decision."""

    def choose_repair(self, event: TLEvent, before: PhaseValue, after: PhaseValue) -> Judgment:
        """Choose the intended repair decision."""

    def choose_merge(self, event: TLEvent, before: PhaseValue, after: PhaseValue) -> Judgment:
        """Choose the intended merge decision."""


@dataclass(frozen=True)
class Judgment:
    """A deterministic, non-executing judgment result."""

    kind: str
    rationale: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


class DeterministicJudgments:
    """Default M3 stub; it records intent without contacting an LLM."""

    def choose_dispatch(self, event: TLEvent, before: PhaseValue, after: PhaseValue) -> Judgment:
        del event, before, after
        return Judgment("dispatch", "deterministic shadow dispatch; no effect executed", {})

    def choose_repair(self, event: TLEvent, before: PhaseValue, after: PhaseValue) -> Judgment:
        del event, before, after
        return Judgment("repair", "deterministic shadow repair; no effect executed", {})

    def choose_merge(self, event: TLEvent, before: PhaseValue, after: PhaseValue) -> Judgment:
        del event, before, after
        return Judgment("merge", "deterministic shadow merge; no effect executed", {})


@dataclass(frozen=True)
class IntendedAction:
    """What the active loop would do for one event, without doing it."""

    kind: str
    target: str
    arguments: Mapping[str, object]
    rationale: str
    event_seq: int
    phase_before: TLPhase
    phase_after: TLPhase

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(copy.deepcopy(dict(self.arguments))))


@dataclass(frozen=True)
class ShadowRunResult:
    """Actions observed and the final durable checkpoint."""

    actions: tuple[IntendedAction, ...]
    final_state: RunState


class TLEventDecoder:
    """Decode canonical event payloads into the pure FSM event vocabulary."""

    def decode(self, event: EventEnvelope) -> TLEvent:
        explicit = event.data.get("shadow_event")
        if explicit is not None:
            return self._decode_explicit(explicit)
        if event.kind is EventKind.AGENT_NOTIFY_PARENT:
            return self._decode_parent_notification(event)
        if event.kind is EventKind.AGENT_COMPLETED:
            return self._decode_completion(event)
        if event.kind is EventKind.AGENT_STUCK:
            return ChildFailed(_agent(event), _string(event.data, "reason", "agent.stuck"))
        if event.kind is EventKind.PR_MERGED:
            return PRMerged(_positive_int(event.data, "pr_number", event.event_type), _agent(event))
        if event.kind is EventKind.PR_FILED:
            return OwnPRFiled(
                _positive_int(event.data, "pr_number", event.event_type),
                _string(event.data, "url", event.event_type),
                _string(event.data, "branch", event.event_type),
            )
        raise ShadowLoopError(f"no shadow FSM mapping for {event.event_type!r}")

    def _decode_explicit(self, value: object) -> TLEvent:
        if not isinstance(value, Mapping):
            raise ShadowLoopError("shadow_event must be an object")
        kind = value.get("kind")
        if kind == "child_spawned":
            return ChildSpawned(
                ChildHandle(
                    _required_value(value, "slug"),
                    _required_value(value, "branch"),
                    _required_value(value, "agent_type"),
                )
            )
        if kind == "child_completed":
            return ChildCompleted(_required_value(value, "slug"))
        if kind == "child_failed":
            return ChildFailed(_required_value(value, "slug"), _required_value(value, "reason"))
        if kind == "pr_merged":
            pr_number = value.get("pr_number")
            if type(pr_number) is not int or pr_number <= 0:
                raise ShadowLoopError("shadow_event.pr_number must be a positive integer")
            return PRMerged(pr_number, _required_value(value, "slug"))
        if kind == "all_children_done":
            return AllChildrenDone()
        if kind == "own_pr_filed":
            pr_number = value.get("pr_number")
            if type(pr_number) is not int or pr_number <= 0:
                raise ShadowLoopError("shadow_event.pr_number must be a positive integer")
            return OwnPRFiled(
                pr_number,
                _required_value(value, "url"),
                _required_value(value, "branch"),
            )
        raise ShadowLoopError(f"unknown shadow FSM event kind: {kind!r}")

    def _decode_parent_notification(self, event: EventEnvelope) -> TLEvent:
        status = _string(event.data, "status", event.event_type)
        if status in {"success", "completed"}:
            return ChildCompleted(_agent(event))
        return ChildFailed(_agent(event), _string(event.data, "message", event.event_type))

    def _decode_completion(self, event: EventEnvelope) -> TLEvent:
        status = _string(event.data, "status", event.event_type)
        if status in {"success", "completed"}:
            return ChildCompleted(_agent(event))
        return ChildFailed(_agent(event), _string(event.data, "message", event.event_type))


class ShadowLoop:
    """Consume projected events, checkpoint every step, and emit intent only."""

    def __init__(
        self,
        source: EventSource,
        store: RunStore,
        *,
        readonly_client: ReadOnlyEffectClient,
        judgments: ShadowJudgments | None = None,
        decoder: TLEventDecoder | None = None,
        recorder: ActionRecorder | None = None,
    ) -> None:
        self.source = source
        self.store = store
        self.judgments = judgments or DeterministicJudgments()
        self.decoder = decoder or TLEventDecoder()
        self.readonly_client = readonly_client
        self.recorder = recorder

    @classmethod
    def for_run(
        cls,
        source: EventSource,
        run_id: str,
        *,
        readonly_client: ReadOnlyEffectClient,
        root_dir: str | Path = DEFAULT_SHADOW_ROOT,
        judgments: ShadowJudgments | None = None,
        decoder: TLEventDecoder | None = None,
        recorder: ActionRecorder | None = None,
    ) -> ShadowLoop:
        """Open a shadow checkpoint below ``.exo/tl-loop/shadow``."""
        store = RunStore(run_id, Path(root_dir))
        if not store.path.exists():
            create(run_id, {}, root_dir=store.root_dir)
        if recorder is None:
            from tl_loop.shadow.recorder import IntendedActionRecorder

            recorder = IntendedActionRecorder(run_id, root_dir=store.root_dir)
        return cls(
            source,
            store,
            readonly_client=readonly_client,
            judgments=judgments,
            decoder=decoder,
            recorder=recorder,
        )

    def run(self, *, timeout: float | None = None, max_events: int | None = None) -> ShadowRunResult:
        """Run until the source is empty, terminal, or ``max_events`` is reached."""
        if max_events is not None and max_events < 0:
            raise ValueError("max_events must be non-negative")
        state = self.store.load()
        phase = _phase_from_state(state)
        actions: list[IntendedAction] = []
        while max_events is None or len(actions) < max_events:
            try:
                event = self.source.get(timeout=timeout)
            except queue_module.Empty:
                break
            fsm_event = self.decoder.decode(event)
            try:
                next_phase = transition(phase, fsm_event)
            except IllegalTransition as error:
                raise ShadowLoopError(str(error)) from error
            event_seq = event.run_seq
            if event_seq is None:
                raise ShadowLoopError(f"event {event.event_type!r} has no run_seq")
            judgment = _judgment(self.judgments, fsm_event, phase, next_phase)
            slices = _update_slices(state.slices, fsm_event)
            action = IntendedAction(
                judgment.kind,
                _target(event, fsm_event),
                {
                    **dict(event.data),
                    **dict(judgment.arguments),
                    "fsm_event": type(fsm_event).__name__,
                },
                judgment.rationale,
                event_seq,
                _phase_tag(phase),
                _phase_tag(next_phase),
            )
            state = self.store.checkpoint(next_phase, slices, state.budgets, event_seq)
            if self.recorder is not None:
                self.recorder.record(action)
            self.source.acknowledge(event)
            phase = next_phase
            actions.append(action)
            if isinstance(phase, (TLDone, TLFailed)):
                break
        return ShadowRunResult(tuple(actions), state)


def _judgment(
    judgments: ShadowJudgments,
    event: TLEvent,
    before: PhaseValue,
    after: PhaseValue,
) -> Judgment:
    if isinstance(event, ChildFailed):
        return judgments.choose_repair(event, before, after)
    if isinstance(event, PRMerged):
        return judgments.choose_merge(event, before, after)
    return judgments.choose_dispatch(event, before, after)


def _update_slices(slices: Mapping[str, SliceState], event: TLEvent) -> dict[str, SliceState]:
    updated = dict(slices)
    if isinstance(event, ChildSpawned):
        handle = event.handle
        current = updated.get(handle.slug)
        if current is None:
            updated[handle.slug] = SliceState(
                id=handle.slug,
                status=SliceStatus.SPAWNED,
                paths=("unknown",),
                depends_on=(),
                base_ref=None,
                test_plan=(),
                agent_type=handle.agent_type,
                model=None,
                branch=handle.branch,
                worktree=None,
                pr_number=None,
                reviewed_head=None,
                attempts=1,
                verdict=None,
            )
        else:
            updated[handle.slug] = replace(
                current,
                status=SliceStatus.SPAWNED,
                agent_type=handle.agent_type,
                branch=handle.branch,
            )
    elif isinstance(event, (ChildCompleted, PRMerged)):
        current = updated.get(event.slug)
        if current is not None:
            updated[event.slug] = replace(current, status=SliceStatus.MERGED)
    elif isinstance(event, ChildFailed):
        current = updated.get(event.slug)
        if current is not None:
            updated[event.slug] = replace(current, status=SliceStatus.FAILED)
    return updated


def _phase_from_state(state: RunState) -> PhaseValue:
    phase = state.fsm.phase
    handles = {
        slice_id: ChildHandle(
            slice_id,
            slice_state.branch or "",
            slice_state.agent_type or "unknown",
        )
        for slice_id, slice_state in state.slices.items()
        if slice_id in state.fsm.waiting
    }
    if phase is TLPhase.TLWaiting:
        return TLWaiting(handles)
    if phase is TLPhase.TLMerging:
        return TLMerging(0, handles)
    constructors: dict[TLPhase, PhaseValue] = {
        TLPhase.TLPlanning: TLPlanning(),
        TLPhase.TLDispatching: TLDispatching(),
        TLPhase.TLAllMerged: TLAllMerged(),
        TLPhase.TLPRFiled: TLPRFiled(0, ""),
        TLPhase.TLDone: TLDone(),
        TLPhase.TLFailed: TLFailed("resumed shadow failure"),
    }
    return constructors[phase]


def _phase_tag(phase: PhaseValue) -> TLPhase:
    if isinstance(phase, TLPlanning):
        return TLPhase.TLPlanning
    if isinstance(phase, TLDispatching):
        return TLPhase.TLDispatching
    if isinstance(phase, TLWaiting):
        return TLPhase.TLWaiting
    if isinstance(phase, TLMerging):
        return TLPhase.TLMerging
    if isinstance(phase, TLAllMerged):
        return TLPhase.TLAllMerged
    if isinstance(phase, TLPRFiled):
        return TLPhase.TLPRFiled
    if isinstance(phase, TLDone):
        return TLPhase.TLDone
    return TLPhase.TLFailed


def _target(event: EventEnvelope, fsm_event: TLEvent) -> str:
    for key in ("target", "slice_id", "recipient"):
        value = event.data.get(key)
        if isinstance(value, str) and value:
            return value
    if event.agent_id:
        return event.agent_id
    if isinstance(fsm_event, ChildSpawned):
        return fsm_event.handle.slug
    if isinstance(fsm_event, (ChildCompleted, ChildFailed, PRMerged)):
        return fsm_event.slug
    return "controller"


def _agent(event: EventEnvelope) -> str:
    if not event.agent_id:
        raise ShadowLoopError(f"{event.event_type!r} requires agent_id")
    return event.agent_id


def _required_value(value: Mapping[str, object], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise ShadowLoopError(f"shadow_event.{key} must be a non-empty string")
    return candidate


def _string(value: Mapping[str, object], key: str, event_type: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise ShadowLoopError(f"{event_type!r}: {key} must be a non-empty string")
    return candidate


def _positive_int(value: Mapping[str, object], key: str, event_type: str) -> int:
    candidate = value.get(key)
    if type(candidate) is not int or candidate <= 0:
        raise ShadowLoopError(f"{event_type!r}: {key} must be a positive integer")
    return candidate


__all__ = [
    "ActionRecorder",
    "DeterministicJudgments",
    "DEFAULT_SHADOW_ROOT",
    "IntendedAction",
    "Judgment",
    "ShadowLoop",
    "ShadowLoopError",
    "ShadowRunResult",
    "TLEventDecoder",
]
