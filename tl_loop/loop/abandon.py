"""Operator-authorized abandonment of one live TL slice attempt."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import TransportClient
from tl_loop.events.reader import LedgerReader
from tl_loop.loop.driver import _invoke
from tl_loop.loop.journal import EffectJournal
from tl_loop.state.schema import ParkCause, SliceState, SliceStatus
from tl_loop.state.store import RunStore

ABANDONABLE_STATUSES = frozenset(
    {
        SliceStatus.DISPATCHING,
        SliceStatus.DISPATCH_UNCONFIRMED,
        SliceStatus.SPAWNED,
        SliceStatus.IN_REVIEW,
        SliceStatus.REPAIRING,
    }
)


class AbandonmentError(RuntimeError):
    """The requested attempt cannot be abandoned safely."""


def abandon_slice(
    project_root: Path,
    run_id: str,
    slice_id: str,
    *,
    effects: EffectClient | None = None,
) -> dict[str, object]:
    """Abandon exactly the current live attempt after operator confirmation."""
    store = RunStore(run_id, project_root / ".exo" / "tl-loop")
    state = store.load()
    current = state.slices.get(slice_id)
    if current is None:
        raise AbandonmentError(f"slice {slice_id!r} does not exist")
    if (
        current.status is SliceStatus.PARKED
        and current.park_cause is ParkCause.ATTEMPT_ABANDONED
    ):
        return {"status": "already_abandoned", "slice_id": slice_id, "attempt": current.attempts}
    _validate_live_attempt(current)
    agent_id = current.dispatch_agent_id
    if not agent_id:
        raise AbandonmentError(
            f"slice {slice_id!r} attempt {current.attempts} has no unambiguous runtime agent identity"
        )

    client = effects or EffectClient(
        TransportClient(project_root=project_root),
        role="tl",
        name="root",
    )
    journal = EffectJournal(run_id, store.run_dir / "action-journal.json")
    payload = _abandonment_payload(current)
    recovery = _has_abandonment_event(project_root, run_id, current)
    if not recovery:
        _invoke(
            "emit_controller_event",
            f"abandon:{slice_id}:{current.attempts}",
            {"event_type": "tl.slice_abandoned", "payload": payload},
            True,
            client,
            lambda live: live.emit_controller_event(
                event_type="tl.slice_abandoned",
                payload=payload,
            ),
            journal,
        )

    _invoke(
        "cleanup",
        f"abandon:{agent_id}:{slice_id}:{current.attempts}",
        {"issue": agent_id, "force": False, "subrepo": ""},
        True,
        client,
        lambda live: live.cleanup(issue=agent_id, force=False, subrepo=""),
        journal,
    )

    latest = store.load()
    latest_slice = latest.slices.get(slice_id)
    if latest_slice is None:
        raise AbandonmentError(f"slice {slice_id!r} disappeared during abandonment")
    if (
        latest_slice.status is SliceStatus.PARKED
        and latest_slice.park_cause is ParkCause.ATTEMPT_ABANDONED
    ):
        return {"status": "already_abandoned", "slice_id": slice_id, "attempt": current.attempts}
    parked = replace(
        latest_slice,
        status=SliceStatus.PARKED,
        park_cause=ParkCause.ATTEMPT_ABANDONED,
        park_issue_id=None,
        park_audit={
            "attempts": current.attempts,
            "reason": "operator_requested",
            "agent_id": agent_id,
            "recovered": recovery,
        },
        blocked_by=None,
    )
    store.checkpoint(
        latest.fsm,
        {**latest.slices, slice_id: parked},
        latest.budgets,
        latest.events.last_consumed_offset,
    )
    return {
        "status": "abandoned",
        "slice_id": slice_id,
        "attempt": current.attempts,
        "agent_id": agent_id,
        "recovered": recovery,
    }


def _validate_live_attempt(slice_state: SliceState) -> None:
    if slice_state.attempts <= 0:
        raise AbandonmentError(f"slice {slice_state.id!r} has no dispatched attempt")
    if slice_state.status not in ABANDONABLE_STATUSES:
        raise AbandonmentError(
            f"slice {slice_state.id!r} attempt {slice_state.attempts} is not live "
            f"(status={slice_state.status.value})"
        )


def _abandonment_payload(slice_state: SliceState) -> dict[str, object]:
    payload: dict[str, object] = {
        "slice_id": slice_state.id,
        "attempt": slice_state.attempts,
        "operator_source": "cli",
        "cause": "operator_requested",
    }
    if slice_state.pr_number is not None:
        payload["pr_number"] = slice_state.pr_number
    if slice_state.reviewed_head:
        payload["head_sha"] = slice_state.reviewed_head
    if slice_state.dispatch_invocation_id:
        payload["invocation_id"] = slice_state.dispatch_invocation_id
    return payload


def _has_abandonment_event(
    project_root: Path,
    run_id: str,
    slice_state: SliceState,
) -> bool:
    reader = LedgerReader(
        project_root / ".exo" / "ledger" / "segments",
        run_id=run_id,
    )
    result = reader.read_from()
    for event in result.events:
        if event.event_type != "tl.slice_abandoned":
            continue
        data = event.data
        if data.get("slice_id") != slice_state.id:
            continue
        if data.get("attempt") != slice_state.attempts:
            continue
        event_invocation = data.get("invocation_id") or event.invocation_id
        if (
            slice_state.dispatch_invocation_id
            and event_invocation
            and event_invocation != slice_state.dispatch_invocation_id
        ):
            continue
        return True
    return False
