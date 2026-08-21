"""Deterministic reconstruction of persisted TL slice lifecycle evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tl_loop.state.schema import SliceState, SliceStatus


@dataclass(frozen=True)
class ReconciliationResult:
    """Durable decision made from authoritative runtime observations."""

    slice_id: str
    confirmed_stage: str
    authoritative_evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    conflicts: tuple[str, ...]
    next_action: str

    def as_state(self) -> dict[str, object]:
        return {
            "confirmed_stage": self.confirmed_stage,
            "authoritative_evidence": list(self.authoritative_evidence),
            "missing_evidence": list(self.missing_evidence),
            "conflicts": list(self.conflicts),
            "next_action": self.next_action,
        }


def reconcile_slice(
    slice_state: SliceState,
    *,
    authoritative_owner_id: str | None,
    watcher: Mapping[str, object] | None,
) -> ReconciliationResult:
    """Choose one safe next action without mutating lifecycle state."""
    evidence: list[str] = []
    missing: list[str] = []
    conflicts: list[str] = []

    if slice_state.status in {
        SliceStatus.DISPATCHING,
        SliceStatus.DISPATCH_UNCONFIRMED,
    }:
        if slice_state.dispatch_intent_id:
            evidence.append("dispatch_intent")
        else:
            missing.append("dispatch_intent")
        return _result(
            slice_state,
            "dispatch",
            evidence,
            missing,
            conflicts,
            "await_authoritative_spawn_event",
        )

    if slice_state.status not in {
        SliceStatus.SPAWNED,
        SliceStatus.IN_REVIEW,
        SliceStatus.REPAIRING,
    }:
        return _result(
            slice_state,
            slice_state.status.value,
            (),
            (),
            (),
            "no_action",
        )

    if slice_state.dispatch_agent_id:
        evidence.append("dispatch_owner")
    else:
        missing.append("dispatch_owner")
    if authoritative_owner_id is not None:
        if (
            slice_state.dispatch_agent_id
            and authoritative_owner_id != slice_state.dispatch_agent_id
        ):
            conflicts.append(
                "authoritative owner disagrees with persisted dispatch owner"
            )
        else:
            evidence.append("runtime_owner")
    else:
        missing.append("runtime_owner")

    if watcher is not None and watcher.get("found") is True:
        # Evidence may have been recovered via slice_id lookup even when
        # slice_state.pr_number was never persisted (e.g. a crash between
        # pr.filed being acknowledged and identity association).
        evidence.append("published_pr")
        _append_watcher_evidence(slice_state, watcher, evidence, missing, conflicts)
    elif slice_state.pr_number is None:
        missing.append("pr_number")
    else:
        missing.append("published_pr")

    pr_state = _pr_state(watcher)
    closed_unmerged = (
        watcher is not None
        and watcher.get("found") is True
        and pr_state == "closed"
        and watcher.get("merged") is False
    )
    head_unreachable = (
        watcher is not None
        and watcher.get("found") is True
        and watcher.get("head_reachable") is False
    )
    if closed_unmerged:
        action = "park_closed_unmerged_pr"
    elif head_unreachable:
        action = "park_unreachable_pr_head"
    elif conflicts:
        action = "open_integrity_gate"
    elif missing:
        action = "await_authoritative_evidence"
    elif watcher and watcher.get("merged") is True:
        action = "adopt_merged_state"
    elif slice_state.status is SliceStatus.SPAWNED:
        action = "await_review_event"
    elif slice_state.status is SliceStatus.REPAIRING:
        action = "await_repair_event"
    else:
        action = "await_merge_event"
    return _result(slice_state, "lifecycle", evidence, missing, conflicts, action)


def _append_watcher_evidence(
    slice_state: SliceState,
    watcher: Mapping[str, object],
    evidence: list[str],
    missing: list[str],
    conflicts: list[str],
) -> None:
    head_sha = watcher.get("head_sha")
    if isinstance(head_sha, str) and head_sha:
        evidence.append("published_head")
        if slice_state.reviewed_head and slice_state.reviewed_head != head_sha:
            conflicts.append("authoritative head disagrees with review evidence")
    else:
        missing.append("published_head")
    review_state = watcher.get("review_state")
    if isinstance(review_state, str) and review_state:
        evidence.append("review_state")
    else:
        missing.append("review_state")
    ci_status = watcher.get("ci_status")
    if isinstance(ci_status, str) and ci_status:
        evidence.append("ci_state")
    else:
        missing.append("ci_state")
    pr_state = _pr_state(watcher)
    if pr_state == "unknown":
        evidence.append("pr_state_unknown")
    else:
        evidence.append("pr_state")
    if watcher.get("head_reachable") is False:
        evidence.append("pr_head_unreachable")


def _pr_state(watcher: Mapping[str, object] | None) -> str:
    """Return an explicit compatibility state for older watcher payloads."""
    if watcher is None:
        return "unknown"
    value = watcher.get("pr_state")
    if isinstance(value, str) and value.lower() in {"open", "closed"}:
        return value.lower()
    return "unknown"


def _result(
    slice_state: SliceState,
    stage: str,
    evidence: tuple[str, ...] | list[str],
    missing: tuple[str, ...] | list[str],
    conflicts: tuple[str, ...] | list[str],
    action: str,
) -> ReconciliationResult:
    return ReconciliationResult(
        slice_id=slice_state.id,
        confirmed_stage=stage,
        authoritative_evidence=tuple(dict.fromkeys(evidence)),
        missing_evidence=tuple(dict.fromkeys(missing)),
        conflicts=tuple(dict.fromkeys(conflicts)),
        next_action=action,
    )
