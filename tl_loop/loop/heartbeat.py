"""Configured idle-wave heartbeat and liveness reconciliation."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeAlias

from tl_loop.client.effects import EffectClient, ToolResult, ToolUnavailableError
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.events.reader import LedgerReader, LedgerReadError
from tl_loop.fsm.recovery import begin_recovery
from tl_loop.loop.escalate import park
from tl_loop.loop.journal import EffectJournal
from tl_loop.loop.observability import emit_controller_event
from tl_loop.state.schema import (
    GoalState,
    ParkCause,
    RunState,
    SliceState,
    SliceStatus,
)
from tl_loop.state.serialization import dumps as dumps_json
from tl_loop.state.store import RunStore

JsonMapping: TypeAlias = Mapping[str, object]
LiveEffects: TypeAlias = EffectClient | ReadOnlyEffectClient
LOGGER = logging.getLogger(__name__)


class HeartbeatError(RuntimeError):
    """A heartbeat observation cannot be reconciled without guessing."""


class HeartbeatDeadlineExceeded(HeartbeatError):
    """The configured goal deadline elapsed before completion."""


@dataclass(frozen=True)
class HeartbeatConfig:
    """Explicit idle interval and no-progress threshold in seconds."""

    interval_seconds: float = 30.0
    stall_threshold_seconds: float = 300.0
    task_timeout_seconds: float | None = 3600.0

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("heartbeat interval_seconds must be positive")
        if self.stall_threshold_seconds <= 0:
            raise ValueError("heartbeat stall_threshold_seconds must be positive")
        if self.task_timeout_seconds is not None and self.task_timeout_seconds < 0:
            raise ValueError("heartbeat task_timeout_seconds must be null or non-negative")


@dataclass(frozen=True)
class SyntheticHeartbeatEvent:
    """A deterministic local event produced by one reconciled observation."""

    event_id: str
    kind: str
    slice_id: str
    source: str
    payload: JsonMapping


@dataclass(frozen=True)
class HeartbeatResult:
    """One idempotent heartbeat attempt and its durable result."""

    fired: bool
    progress: bool
    state: RunState
    events: tuple[SyntheticHeartbeatEvent, ...]
    parked_slice_ids: tuple[str, ...]


@dataclass(frozen=True)
class PollWorkersSnapshot:
    """Worker rows plus the runtime identities the server could not report."""

    rows: dict[str, JsonMapping]
    missing_agents: frozenset[str]


def heartbeat_due(goals: GoalState, now: float, interval_seconds: float) -> bool:
    """Return whether the configured interval has elapsed since the last fire."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    return goals.last_heartbeat_at is None or now - goals.last_heartbeat_at >= interval_seconds


def heartbeat_once(
    state: RunState,
    store: RunStore,
    effects: LiveEffects,
    config: HeartbeatConfig,
    *,
    now: float | None = None,
    project_root: Path | str,
) -> HeartbeatResult:
    """Poll liveness and reconcile one idle wave through the durable store."""
    current_time = time.time() if now is None else now
    if not heartbeat_due(state.goals, current_time, config.interval_seconds):
        return HeartbeatResult(False, False, state, (), ())
    deadline_elapsed = state.goals.deadline and current_time >= state.goals.deadline
    deadline_event = (
        _event(
            "goal.deadline_elapsed",
            "heartbeat",
            "controller",
            {"deadline": state.goals.deadline, "observed_at": current_time},
        )
        if deadline_elapsed
        else None
    )

    active = _active_slices(state)
    if not active:
        return HeartbeatResult(
            False,
            False,
            state,
            (deadline_event,) if deadline_event is not None else (),
            (),
        )

    worker_snapshot = _poll_workers(effects, tuple(active))
    worker_rows = worker_snapshot.rows
    events: list[SyntheticHeartbeatEvent] = []
    if deadline_event is not None:
        events.append(deadline_event)
    parked: list[str] = []
    progress = False

    for slice_state in active:
        row = worker_rows.get(slice_state.id)
        if row is None:
            runtime_agent_id = slice_state.dispatch_agent_id or slice_state.id
            terminal, evidence = _missing_worker_evidence(
                Path(project_root),
                slice_state,
                poll_workers_missing=runtime_agent_id in worker_snapshot.missing_agents,
                ledger_cursor=state.events.last_consumed_offset,
            )
            if evidence.get("authoritative_handoff"):
                # A late PR/completion/notify event is authoritative. Leave
                # the slice active so the normal event consumer can apply it;
                # pane absence never wins the race against lifecycle evidence.
                continue
            current = store.load()
            current_slice = current.slices[slice_state.id]
            if terminal and evidence.get("missing_handoff") and current_slice.recovery is not None:
                continue
            reconciliation = _missing_worker_reconciliation(terminal, evidence)
            if current_slice.reconciliation == reconciliation and not (
                terminal and current_slice.status in _active_statuses()
            ):
                continue
            if terminal:
                if isinstance(effects, ReadOnlyEffectClient):
                    raise HeartbeatError(
                        f"terminal evidence for missing slice {slice_state.id!r} requires "
                        "an active effect client to reconcile"
                    )
                emit_controller_event(
                    effects,
                    "worker.terminal_reconciled",
                    _worker_event_payload(evidence),
                )
                missing_handoff = bool(evidence.get("missing_handoff"))
                if missing_handoff:
                    recovery = current_slice.recovery or begin_recovery(
                        cause=ParkCause.HUMAN_DECISION_REQUIRED.value,
                        owner_run_id=current.run_id,
                        slice_attempt=current_slice.attempts,
                        owner_agent_id=current_slice.dispatch_agent_id,
                        invocation_generation=(
                            evidence.get("generation")
                            if type(evidence.get("generation")) is int
                            and evidence.get("generation") >= 0
                            else 0
                        ),
                        plan_revision=current.revision,
                        evidence=evidence,
                        next_action="diagnose",
                    )
                    if current_slice.recovery is None:
                        current = store.checkpoint(
                            current.fsm,
                            {
                                **current.slices,
                                slice_state.id: replace(
                                    current_slice,
                                    recovery=recovery,
                                ),
                            },
                            current.budgets,
                            current.events.last_consumed_offset,
                        )
                    emit_controller_event(
                        effects,
                        "agent.task_blocked",
                        _missing_handoff_payload(evidence),
                    )
                else:
                    park(
                        current_slice,
                        ParkCause.WORKER_TERMINAL,
                        store=store,
                        issue_creator=effects,
                        ledger=current.budgets,
                        audit=evidence,
                    )
                    parked.append(slice_state.id)
                progress = True
                events.append(
                    _event(
                        "worker.terminal_reconciled",
                        "invocation.finished",
                        slice_state.id,
                        evidence,
                    )
                )
            else:
                updated = replace(current_slice, reconciliation=reconciliation)
                current = store.checkpoint(
                    current.fsm,
                    {**current.slices, slice_state.id: updated},
                    current.budgets,
                    current.events.last_consumed_offset,
                )
                emit_controller_event(
                    effects,
                    "worker.missing",
                    _worker_event_payload(evidence),
                )
                events.append(
                    _event(
                        "worker.missing",
                        "heartbeat",
                        slice_state.id,
                        evidence,
                    )
                )
            continue
        if _retired_or_unrouted(row):
            continue
        budget_event = _enforce_task_budget(
            current=store.load(),
            slice_state=slice_state,
            row=row,
            effects=effects,
            config=config,
            now=current_time,
            project_root=Path(project_root),
            store=store,
        )
        if budget_event is not None:
            events.append(budget_event)
            parked.append(slice_state.id)
            progress = True
            continue
        if row.get("pane_alive") is False:
            if isinstance(effects, ReadOnlyEffectClient):
                raise HeartbeatError(
                    f"dead slice {slice_state.id!r} requires an active effect client to park"
                )
            park(
                slice_state,
                ParkCause.STALL_DETECTED,
                store=store,
                issue_creator=effects,
                ledger=state.budgets,
            )
            parked.append(slice_state.id)
            progress = True
            events.append(
                _event(
                    "worker.dead",
                    "poll_workers",
                    slice_state.id,
                    {"pane_alive": False},
                )
            )

    current = store.load()
    for slice_state in _active_slices(current):
        pr_number = slice_state.pr_number
        if pr_number is None:
            resolution, pr_number = _resolve_live_pr(effects, slice_state.id)
            if pr_number is None:
                events.append(
                    _event(
                        "pr.unresolved",
                        "resolve_live_pr_for_slice",
                        slice_state.id,
                        {"resolution": resolution},
                    )
                )
                continue
            current = store.load()
            current_slice = current.slices[slice_state.id]
            if current_slice.pr_number != pr_number:
                current_slice = replace(current_slice, pr_number=pr_number)
                current = store.checkpoint(
                    current.fsm,
                    {**current.slices, slice_state.id: current_slice},
                    current.budgets,
                    current.events.last_consumed_offset,
                )
                progress = True
            slice_state = current.slices[slice_state.id]
        watcher = _watch_pr(effects, pr_number, slice_state.id)
        terminal_cause = _pr_terminal_cause(watcher)
        if terminal_cause is not None:
            if isinstance(effects, ReadOnlyEffectClient):
                raise HeartbeatError(
                    f"authoritative PR terminal observation for {slice_state.id!r} "
                    "requires an active effect client to park"
                )
            park(
                slice_state,
                terminal_cause,
                store=store,
                issue_creator=effects,
                ledger=current.budgets,
                audit=_pr_payload(watcher),
            )
            current = store.load()
            parked_slice = current.slices[slice_state.id]
            parked_slice = replace(
                parked_slice,
                reconciliation={
                    "confirmed_stage": "lifecycle",
                    "authoritative_evidence": ["published_pr", "pr_state"],
                    "missing_evidence": [],
                    "conflicts": [],
                    "next_action": (
                        "park_closed_unmerged_pr"
                        if terminal_cause is ParkCause.PR_CLOSED_UNMERGED
                        else "park_unreachable_pr_head"
                    ),
                },
            )
            current = store.checkpoint(
                current.fsm,
                {**current.slices, slice_state.id: parked_slice},
                current.budgets,
                current.events.last_consumed_offset,
            )
            progress = True
            parked.append(slice_state.id)
            events.append(
                _event(
                    "pr.closed_unmerged"
                    if terminal_cause is ParkCause.PR_CLOSED_UNMERGED
                    else "pr.head_unreachable",
                    "watcher_pr_state",
                    slice_state.id,
                    _pr_payload(watcher),
                )
            )
            continue
        updated, observed = _reconcile_pr(slice_state, watcher)
        if updated == slice_state:
            continue
        current = store.checkpoint(
            current.fsm,
            {**current.slices, slice_state.id: updated},
            current.budgets,
            current.events.last_consumed_offset,
        )
        progress = True
        events.append(
            _event(
                observed,
                "watcher_pr_state",
                slice_state.id,
                _pr_payload(watcher),
            )
        )

    prior_progress = current.goals.last_progress_at
    progress_at = current_time if progress or prior_progress is None else prior_progress
    if (
        not progress
        and prior_progress is not None
        and current_time - prior_progress >= config.stall_threshold_seconds
    ):
        for slice_state in _active_slices(current):
            events.append(
                _event(
                    "wave.stalled",
                    "heartbeat",
                    slice_state.id,
                    {
                        "stall_threshold_seconds": config.stall_threshold_seconds,
                        "action": "observe",
                    },
                )
            )

    updated_goals = replace(
        current.goals,
        last_heartbeat_at=current_time,
        last_progress_at=progress_at,
    )
    current = store.set_goals(updated_goals)
    LOGGER.info(
        "[TL loop] waiting observation active_slices=%d progress=%s elapsed_since_progress=%.3fs",
        len(_active_slices(current)),
        progress,
        max(0.0, current_time - progress_at) if progress_at is not None else 0.0,
    )
    return HeartbeatResult(
        True,
        progress,
        current,
        tuple(events),
        tuple(dict.fromkeys(parked)),
    )


@dataclass(frozen=True)
class _BudgetIntent:
    operation: str
    target: str
    arguments: Mapping[str, object]
    active: bool = True


def _enforce_task_budget(
    *,
    current: RunState,
    slice_state: SliceState,
    row: JsonMapping,
    effects: LiveEffects,
    config: HeartbeatConfig,
    now: float,
    project_root: Path,
    store: RunStore,
) -> SyntheticHeartbeatEvent | None:
    budget = slice_state.task_timeout_seconds
    budget_source = slice_state.task_timeout_source
    if budget is None and budget_source is None:
        budget = config.task_timeout_seconds
        budget_source = "built_in"
    if budget is None or slice_state.dispatch_started_at is None:
        return None
    elapsed = max(0.0, now - slice_state.dispatch_started_at)
    if elapsed < budget:
        return None
    terminal, terminal_evidence = _missing_worker_evidence(
        project_root, slice_state, poll_workers_missing=False
    )
    if terminal:
        LOGGER.info(
            "[TL loop] authoritative terminal wins over task budget slice=%s elapsed=%.3fs",
            slice_state.id,
            elapsed,
        )
        return None
    if isinstance(effects, ReadOnlyEffectClient):
        raise HeartbeatError(
            f"task budget exceeded for {slice_state.id!r} requires an active effect client"
        )
    runtime_agent_id = slice_state.dispatch_agent_id or slice_state.id
    pane_id = _text(row, "pane_id") or _text(row, "window_id") or runtime_agent_id
    intent = _BudgetIntent(
        "close_worker_pane",
        slice_state.id,
        {"pane_id": pane_id, "runtime_agent_id": runtime_agent_id},
    )
    journal = EffectJournal(store.run_id, store.run_dir / "action-journal.json")
    existing = journal.existing(intent)
    if existing is not None:
        status = existing.get("status")
        if status == "confirmed":
            result = journal.replay(existing)
        elif status == "rejected":
            raise HeartbeatError(existing.get("error") or "task budget disposal was rejected")
        else:
            raise HeartbeatError(
                f"task budget disposal for {slice_state.id!r} has unresolved journal status {status!r}"
            )
    else:
        journal.append(intent)
        try:
            result = effects.close_worker_pane(pane_id=pane_id)
        except BaseException as error:
            journal.mark_unknown(intent, error)
            raise
        journal.mark_result(intent, result)
    if result.success is False:
        if result.error_kind == "tool_unavailable":
            raise ToolUnavailableError("close_worker_pane", result, target=slice_state.id)
        raise HeartbeatError(result.error or f"unable to dispose {slice_state.id!r}")
    record = _read_invocation_record(project_root, runtime_agent_id, slice_state.id)
    evidence = dict(terminal_evidence)
    if isinstance(record, Mapping):
        evidence.update(record)
    _mark_invocation_timed_out(project_root, runtime_agent_id, slice_state.id, evidence, now)
    payload = {
        "slice_id": slice_state.id,
        "runtime_agent_id": runtime_agent_id,
        "invocation_id": _text(evidence, "invocation_id"),
        "generation": evidence.get("generation"),
        "harness": slice_state.agent_type,
        "model": slice_state.model,
        "effort": _text(row, "effort") or _text(evidence, "effort"),
        "branch": slice_state.branch or _text(evidence, "branch"),
        "worktree": slice_state.worktree or _text(evidence, "worktree"),
        "pr_number": slice_state.pr_number,
        "configured_budget_seconds": budget,
        "budget_source_layer": budget_source or "unknown",
        "dispatch_started_at": slice_state.dispatch_started_at,
        "elapsed_seconds": elapsed,
        "last_authoritative_event_seq": current.goals.last_authoritative_event_seq,
        "stderr_tail": _bounded_text(evidence, "stderr_tail"),
    }
    emit_controller_event(effects, "agent.task_budget_exceeded", payload)
    park(
        slice_state,
        ParkCause.TASK_BUDGET_EXCEEDED,
        store=store,
        issue_creator=effects,
        ledger=current.budgets,
    )
    return _event("agent.task_budget_exceeded", "heartbeat", slice_state.id, payload)


def _mark_invocation_timed_out(
    project_root: Path,
    runtime_agent_id: str,
    slice_id: str,
    evidence: Mapping[str, object],
    now: float,
) -> None:
    if not _safe_agent_name(runtime_agent_id):
        return
    path = project_root / ".exo" / "agents" / runtime_agent_id / "invocation.json"
    if not path.is_file():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(document, dict):
        return
    recorded_slice = document.get("slice_id")
    if isinstance(recorded_slice, str) and recorded_slice and recorded_slice != slice_id:
        return
    document["status"] = "timed_out"
    document["exit_classification"] = "task_budget_exceeded"
    document["exit_reason"] = "declared_task_budget_exceeded"
    document["ended_at"] = now
    if evidence.get("stderr_tail") is not None:
        document["stderr_tail"] = _bounded_text(evidence, "stderr_tail")
    temporary = path.with_suffix(".timed-out.tmp")
    try:
        temporary.write_text(dumps_json(document, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        LOGGER.warning("Unable to persist timed-out invocation record: %s", path)


def _active_slices(state: RunState) -> tuple[SliceState, ...]:
    active_statuses = {
        SliceStatus.SPAWNED,
        SliceStatus.IN_REVIEW,
        SliceStatus.REPAIRING,
    }
    return tuple(
        slice_state
        for slice_state in state.slices.values()
        if slice_state.status in active_statuses
    )


def _poll_workers(
    effects: LiveEffects,
    agents: tuple[SliceState, ...],
) -> PollWorkersSnapshot:
    aliases: dict[str, str] = {}
    for slice_state in agents:
        runtime_name = slice_state.dispatch_agent_id or slice_state.id
        previous = aliases.get(runtime_name)
        if previous is not None and previous != slice_state.id:
            raise HeartbeatError(
                f"ambiguous runtime agent identity {runtime_name!r} for "
                f"slices {previous!r} and {slice_state.id!r}"
            )
        aliases[runtime_name] = slice_state.id
    result = effects.poll_workers(
        include_dead=True,
        agents=tuple(aliases),
    )
    payload = _result_object(result, "poll_workers")
    rows = payload.get("workers")
    if not isinstance(rows, list):
        raise HeartbeatError("poll_workers result has no workers array")
    missing_agents = payload.get("missing_agents", [])
    if not isinstance(missing_agents, list) or not all(
        isinstance(name, str) and name for name in missing_agents
    ):
        raise HeartbeatError("poll_workers missing_agents must be an array of names")
    requested_names = set(aliases)
    unexpected_missing = set(missing_agents) - requested_names
    if unexpected_missing:
        raise HeartbeatError(
            "poll_workers reported unrequested missing agents: "
            + ", ".join(sorted(unexpected_missing))
        )
    observed_names = {
        row.get("name")
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("name"), str)
    }
    if observed_names.intersection(missing_agents):
        raise HeartbeatError("poll_workers reported an agent as both present and missing")
    dead_rows = payload.get("dead_workers", [])
    if dead_rows is None:
        dead_rows = []
    elif not isinstance(dead_rows, list):
        raise HeartbeatError("poll_workers dead_workers must be an array")
    indexed: dict[str, JsonMapping] = {}
    _index_worker_rows(rows, indexed, aliases=aliases, dead=False)
    _index_worker_rows(dead_rows, indexed, aliases=aliases, dead=True)
    return PollWorkersSnapshot(indexed, frozenset(missing_agents))


def _index_worker_rows(
    rows: list[object],
    indexed: dict[str, JsonMapping],
    *,
    aliases: Mapping[str, str],
    dead: bool,
) -> None:
    for row in rows:
        if isinstance(row, str):
            slice_id = aliases.get(row)
            if slice_id is not None:
                indexed[slice_id] = {"name": row, "pane_alive": False}
            continue
        if not isinstance(row, Mapping):
            raise HeartbeatError("poll_workers worker array contains a non-object")
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        slice_id = aliases.get(name)
        if slice_id is None:
            continue
        indexed[slice_id] = {**row, "pane_alive": False} if dead else row


def _watch_pr(effects: LiveEffects, pr_number: int, slice_id: str) -> JsonMapping:
    return _result_object(
        effects.watcher_pr_state(pr_number=pr_number),
        "watcher_pr_state",
        target=slice_id,
    )


def _resolve_live_pr(effects: LiveEffects, slice_id: str) -> tuple[str, int | None]:
    result = _result_object(
        effects.resolve_live_pr_for_slice(slice_id=slice_id),
        "resolve_live_pr_for_slice",
        target=slice_id,
    )
    resolution = result.get("resolution")
    if resolution not in {"never_published", "all_attempts_abandoned", "live"}:
        raise HeartbeatError(
            "resolve_live_pr_for_slice resolution must be one of "
            "never_published, all_attempts_abandoned, live"
        )
    if resolution != "live":
        return resolution, None
    pr_number = result.get("pr_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise HeartbeatError("resolve_live_pr_for_slice live result requires a positive pr_number")
    return resolution, pr_number


def _result_object(
    result: ToolResult,
    operation: str,
    *,
    target: str | None = None,
) -> JsonMapping:
    if result.success is False:
        if result.error_kind == "tool_unavailable":
            raise ToolUnavailableError(operation, result, target=target)
        raise HeartbeatError(result.error or f"{operation} returned failure")
    if not isinstance(result.result, Mapping):
        raise HeartbeatError(f"{operation} result must be an object")
    return result.result


def _reconcile_pr(slice_state: SliceState, payload: JsonMapping) -> tuple[SliceState, str]:
    head_sha = payload.get("head_sha")
    if head_sha is not None and not isinstance(head_sha, str):
        raise HeartbeatError("watcher_pr_state head_sha must be a string or null")
    if head_sha and head_sha != slice_state.reviewed_head:
        return replace(
            slice_state, reviewed_head=head_sha, verdict=None, verdict_at=None
        ), "pr.updated"
    return slice_state, "pr.review"


def _pr_payload(payload: JsonMapping) -> dict[str, object]:
    return {
        key: payload[key]
        for key in (
            "head_sha",
            "review_state",
            "ci_status",
            "pr_state",
            "merged",
            "head_reachable",
            "evidence_error",
        )
        if key in payload
    }


def _pr_terminal_cause(payload: JsonMapping) -> ParkCause | None:
    """Classify only explicit Forgejo/head observations as terminal."""
    pr_state = payload.get("pr_state")
    if (
        isinstance(pr_state, str)
        and pr_state.lower() == "closed"
        and payload.get("merged") is False
    ):
        return ParkCause.PR_CLOSED_UNMERGED
    if payload.get("head_reachable") is False:
        return ParkCause.PR_HEAD_UNREACHABLE
    return None


def _retired_or_unrouted(row: JsonMapping) -> bool:
    lifecycle = row.get("lifecycle_status")
    if not isinstance(lifecycle, str):
        return False
    normalized = lifecycle.upper()
    return normalized.startswith("RETIRED") or normalized == "NO-ROUTING-RECORDED"


def _missing_worker_evidence(
    root_dir: Path,
    slice_state: SliceState,
    *,
    poll_workers_missing: bool,
    ledger_cursor: int = 0,
) -> tuple[bool, dict[str, object]]:
    runtime_agent_id = slice_state.dispatch_agent_id or slice_state.id
    record = _read_invocation_record(root_dir, runtime_agent_id, slice_state.id)
    if record is None:
        record = _read_terminal_invocation_event(root_dir, runtime_agent_id, slice_state.id)
    context = {
        "slice_id": slice_state.id,
        "runtime_agent_id": runtime_agent_id,
        "invocation_id": _text(record, "invocation_id"),
        "generation": record.get("generation") if isinstance(record, Mapping) else None,
        "exit_code": record.get("exit_code") if isinstance(record, Mapping) else None,
        "classification": _text(record, "exit_classification")
        if isinstance(record, Mapping)
        else None,
        "reason": _text(record, "exit_reason") if isinstance(record, Mapping) else None,
        "stderr_tail": _bounded_text(record, "stderr_tail")
        if isinstance(record, Mapping)
        else None,
        "branch": _text(record, "branch") if isinstance(record, Mapping) else slice_state.branch,
        "worktree": _text(record, "worktree")
        if isinstance(record, Mapping)
        else slice_state.worktree,
        "poll_workers_missing": poll_workers_missing,
        "attempt": slice_state.attempts,
        "has_pr": slice_state.pr_number is not None,
        "guidance_required": False,
    }
    context.update(_git_handoff_evidence(root_dir, context["worktree"]))
    handoff = _read_authoritative_handoff(
        root_dir,
        runtime_agent_id,
        slice_state.id,
        _text(record, "invocation_id"),
        ledger_cursor=ledger_cursor,
    )
    if handoff is not None:
        context["authoritative_handoff"] = True
        context["handoff_event_type"] = handoff
        return False, context
    if isinstance(record, Mapping):
        status = _text(record, "status")
        terminal = (
            status in {"exited", "failed", "killed", "timed_out"}
            or record.get("ended_at") is not None
        )
        if terminal:
            context["classification"] = _classify_missing_handoff(context)
            context["reason"] = context["reason"] or "durable_invocation_finished"
            context["missing_handoff"] = True
            context["guidance_required"] = True
            return True, context
    return False, context


def _worker_event_payload(evidence: Mapping[str, object]) -> dict[str, object]:
    return {
        "slice_id": evidence.get("slice_id"),
        "runtime_agent_id": evidence.get("runtime_agent_id"),
        "invocation_id": evidence.get("invocation_id"),
        "generation": evidence.get("generation"),
        "exit_code": evidence.get("exit_code"),
        "classification": evidence.get("classification"),
        "reason": evidence.get("reason"),
        "stderr_tail": evidence.get("stderr_tail"),
        "branch": evidence.get("branch"),
        "worktree": evidence.get("worktree"),
        "poll_workers_missing": evidence.get("poll_workers_missing"),
        "has_commit": evidence.get("has_commit"),
        "has_uncommitted_changes": evidence.get("has_uncommitted_changes"),
        "has_pr": evidence.get("has_pr"),
        "guidance_required": evidence.get("guidance_required"),
        "attempt": evidence.get("attempt"),
        "handoff_event_type": evidence.get("handoff_event_type"),
    }


def _classify_missing_handoff(context: Mapping[str, object]) -> str:
    """Classify an exited invocation without inferring success from exit code."""
    existing = context.get("classification")
    if isinstance(existing, str) and existing not in {"", "terminal_exit", "clean_exit"}:
        return existing
    if context.get("exit_code") is None:
        return "missing_exit_marker"
    if context.get("has_uncommitted_changes") is True:
        return "dirty_worktree_no_commit"
    if context.get("has_commit") is True and context.get("has_pr") is False:
        return "commit_no_pr"
    if context.get("exit_code") == 0:
        return "clean_no_op"
    return "unknown"


def _missing_handoff_payload(evidence: Mapping[str, object]) -> dict[str, object]:
    """Project a terminal no-handoff observation into the typed blocker contract."""
    classification = str(evidence.get("classification") or "unknown")
    return {
        "outcome": "blocked",
        "slice_id": evidence.get("slice_id"),
        "cause": "human_decision_required",
        "scope_attribution": "agent_lifecycle",
        "needs_human": True,
        "retryable": True,
        "recovery_action": "inspect preserved worktree and resume or abandon the invocation",
        "declared_difficulty": "standard",
        "matched_difficulty_rule": f"missing_handoff:{classification}",
        "attempt": evidence.get("attempt") or 1,
    }


def _read_invocation_record(
    root_dir: Path,
    runtime_agent_id: str,
    slice_id: str,
) -> JsonMapping | None:
    if not _safe_agent_name(runtime_agent_id):
        return None
    for base in (root_dir / ".exo/agents", root_dir / ".exo/worktrees"):
        path = base / runtime_agent_id / "invocation.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, Mapping):
            continue
        recorded_slice = document.get("slice_id")
        if isinstance(recorded_slice, str) and recorded_slice and recorded_slice != slice_id:
            continue
        return document
    return None


def _read_terminal_invocation_event(
    root_dir: Path,
    runtime_agent_id: str,
    slice_id: str,
) -> JsonMapping | None:
    try:
        return LedgerReader(root_dir / ".exo/ledger/segments").find_invocation_finished(
            runtime_agent_id,
            slice_id,
        )
    except LedgerReadError as error:
        LOGGER.warning("Unable to read terminal invocation evidence: %s", error)
        return None


def _read_authoritative_handoff(
    root_dir: Path,
    runtime_agent_id: str,
    slice_id: str,
    invocation_id: str | None,
    *,
    ledger_cursor: int = 0,
) -> str | None:
    """Return a later authoritative lifecycle event before pane death wins."""
    try:
        events = LedgerReader(root_dir / ".exo/ledger/segments").read_from(ledger_cursor).events
    except (LedgerReadError, ValueError) as error:
        LOGGER.warning("Unable to read authoritative handoff evidence: %s", error)
        return None
    authoritative = {
        "agent.completed",
        "agent.notify_parent",
        "pr.filed",
        "pr.published",
        "pr.updated",
        "pr.merged",
    }
    for event in reversed(events):
        if event.event_type not in authoritative:
            continue
        if invocation_id is not None and event.invocation_id != invocation_id:
            continue
        if event.slice_id == slice_id or event.agent_id == runtime_agent_id:
            return event.event_type
    return None


def _git_handoff_evidence(root_dir: Path, worktree: object) -> dict[str, object]:
    """Collect bounded Git booleans without mutating the preserved worktree."""
    if not isinstance(worktree, str) or not worktree:
        return {"has_commit": None, "has_uncommitted_changes": None}
    path = Path(worktree)
    if not path.is_absolute():
        path = root_dir / path
    try:
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        commit = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"has_commit": None, "has_uncommitted_changes": None}
    return {
        "has_commit": commit.returncode == 0,
        "has_uncommitted_changes": status.returncode == 0 and bool(status.stdout),
    }


def _missing_worker_reconciliation(
    terminal: bool,
    evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    missing_handoff = bool(evidence and evidence.get("missing_handoff"))
    return {
        "confirmed_stage": "missing_handoff"
        if missing_handoff
        else ("worker_terminal" if terminal else "worker_row_missing"),
        "authoritative_evidence": ["invocation.finished"] if terminal else [],
        "missing_evidence": [] if terminal else ["worker.row"],
        "conflicts": [],
        "next_action": "park_missing_handoff"
        if missing_handoff
        else ("park_slice" if terminal else "continue_observing"),
    }


def _active_statuses() -> frozenset[SliceStatus]:
    return frozenset(
        {
            SliceStatus.SPAWNED,
            SliceStatus.IN_REVIEW,
            SliceStatus.REPAIRING,
        }
    )


def _safe_agent_name(value: str) -> bool:
    return bool(value) and Path(value).name == value and value not in {".", ".."}


def _text(value: Mapping[str, object] | None, key: str) -> str | None:
    candidate = value.get(key) if isinstance(value, Mapping) else None
    return candidate if isinstance(candidate, str) and candidate else None


def _bounded_text(value: Mapping[str, object] | None, key: str) -> str | None:
    text = _text(value, key)
    return text[-4096:] if text is not None else None


def _event(kind: str, source: str, slice_id: str, payload: JsonMapping) -> SyntheticHeartbeatEvent:
    fingerprint = "|".join(f"{key}={payload[key]!r}" for key in sorted(payload))
    return SyntheticHeartbeatEvent(
        event_id=f"heartbeat:{source}:{kind}:{slice_id}:{fingerprint}",
        kind=kind,
        slice_id=slice_id,
        source=source,
        payload=payload,
    )


__all__ = [
    "HeartbeatConfig",
    "HeartbeatDeadlineExceeded",
    "HeartbeatError",
    "HeartbeatResult",
    "SyntheticHeartbeatEvent",
    "heartbeat_due",
    "heartbeat_once",
]
