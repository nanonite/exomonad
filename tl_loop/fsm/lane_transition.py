"""Guarded transitions for serialized repository integration lanes."""

from __future__ import annotations

from dataclasses import replace

from .evidence import require_positive as _require_positive
from .evidence import require_text as _require_text
from .lane import (
    LaneBookkeepingStarted,
    LaneIntegrationStarted,
    LaneParkRequested,
    LanePhase,
    LaneRecoveryRequested,
    LaneReleased,
    LaneReserved,
    LaneState,
)


def transition_lane(lane: LaneState, event: object) -> LaneState:
    """Apply one serialized lane event."""
    active = (LanePhase.IDLE, LanePhase.RESERVED, LanePhase.INTEGRATING, LanePhase.BOOKKEEPING)
    if isinstance(event, LaneRecoveryRequested) and lane.phase in active:
        _require_text(event.diagnostic, "lane recovery diagnostic")
        return replace(lane, phase=LanePhase.RECOVERY)
    if isinstance(event, LaneParkRequested) and lane.phase in active + (LanePhase.RECOVERY,):
        return _park_lane(lane, event)
    if isinstance(event, LaneReserved) and lane.phase in (LanePhase.IDLE, LanePhase.RECOVERY):
        return _reserve_lane(lane, event)
    if lane.phase is LanePhase.RESERVED and isinstance(event, LaneIntegrationStarted):
        return _start_integration(lane, event)
    if lane.phase is LanePhase.INTEGRATING and isinstance(event, LaneBookkeepingStarted):
        return _start_bookkeeping(lane, event)
    if lane.phase is LanePhase.BOOKKEEPING and isinstance(event, LaneReleased):
        return _release_lane(lane, event)
    raise ValueError(f"no lane transition for {lane.phase.value} and {type(event).__name__}")


def _park_lane(lane: LaneState, event: LaneParkRequested) -> LaneState:
    _require_text(event.cause, "lane park cause")
    _require_text(event.diagnostic, "lane park diagnostic")
    return replace(lane, phase=LanePhase.PARKED)


def _reserve_lane(lane: LaneState, event: LaneReserved) -> LaneState:
    _require_text(event.child_id, "lane child ID")
    _require_positive(event.lane_epoch, "lane epoch")
    _require_text(event.expected_base_sha, "lane expected base SHA")
    return replace(
        lane,
        phase=LanePhase.RESERVED,
        child_id=event.child_id,
        lane_epoch=event.lane_epoch,
        expected_base_sha=event.expected_base_sha,
        head_sha=None,
        merge_journal_id=None,
        push_intent_id=None,
        push_journal_id=None,
        changelog_commit=None,
    )


def _start_integration(lane: LaneState, event: LaneIntegrationStarted) -> LaneState:
    _require_lane_child(lane, event.child_id)
    _require_text(event.head_sha, "lane integration head SHA")
    return replace(lane, phase=LanePhase.INTEGRATING, head_sha=event.head_sha)


def _start_bookkeeping(lane: LaneState, event: LaneBookkeepingStarted) -> LaneState:
    _require_lane_child(lane, event.child_id)
    for value, field in (
        (event.merge_journal_id, "merge journal ID"),
        (event.push_intent_id, "push intent ID"),
        (event.push_journal_id, "push journal ID"),
        (event.changelog_commit, "changelog commit"),
    ):
        _require_text(value, f"lane {field}")
    return replace(
        lane,
        phase=LanePhase.BOOKKEEPING,
        merge_journal_id=event.merge_journal_id,
        push_intent_id=event.push_intent_id,
        push_journal_id=event.push_journal_id,
        changelog_commit=event.changelog_commit,
    )


def _release_lane(lane: LaneState, event: LaneReleased) -> LaneState:
    _require_lane_child(lane, event.child_id)
    receipt = event.receipt
    checks = (
        (receipt.repository, lane.repository, "repository"),
        (receipt.parent_branch, lane.parent_branch, "parent branch"),
        (receipt.child_id, lane.child_id, "child"),
        (receipt.lane_epoch, lane.lane_epoch, "lane epoch"),
        (receipt.push_intent_id, lane.push_intent_id, "intent"),
        (receipt.push_journal_id, lane.push_journal_id, "journal"),
        (receipt.expected_base_sha, lane.expected_base_sha, "base"),
        (receipt.pushed_commit, lane.changelog_commit, "changelog commit"),
    )
    for actual, expected, label in checks:
        if actual != expected:
            raise ValueError(f"push receipt {label} does not match lane")
    return replace(
        lane,
        phase=LanePhase.IDLE,
        child_id=None,
        lane_epoch=None,
        expected_base_sha=None,
        head_sha=None,
        merge_journal_id=None,
        push_intent_id=None,
        push_journal_id=None,
        changelog_commit=None,
        last_push_receipt_id=receipt.push_receipt_id,
        last_remote_head=receipt.observed_remote_head,
        last_ancestry_proof=receipt.ancestry_proof,
    )


def _require_lane_child(lane: LaneState, child_id: str) -> None:
    if lane.child_id != child_id:
        raise ValueError("lane event does not match reserved child")


__all__ = ["transition_lane"]
