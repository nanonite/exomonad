"""Deterministic reconstruction of persisted TL slice lifecycle evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import cast

from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.schema import (
    CI_STATUS_VALUES,
    ActionKind,
    ActionPhase,
    ObservationProvenance,
    RunState,
    SliceState,
    SliceStatus,
    Verdict,
)


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


@dataclass(frozen=True)
class ObservationReduction:
    """Deterministic fold result for one watcher edge or snapshot."""

    state: SliceState
    provenance: ObservationProvenance
    changed: bool
    accepted: bool
    reason: str


@dataclass(frozen=True)
class InternalTransition:
    """A persisted lifecycle transition derived without issuing an effect."""

    transition: str
    reason: str

    @property
    def name(self) -> str:
        return self.transition


@dataclass(frozen=True)
class ExternalIntent:
    """An effect request whose arguments come only from persisted state."""

    operation: str
    target_id: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))

    @property
    def kind(self) -> str:
        return self.operation

    @property
    def target(self) -> str:
        return self.target_id


@dataclass(frozen=True)
class Quiescent:
    """A deterministic wait reason; no effect is safe from current evidence."""

    reason: str


MergeDecision = InternalTransition | ExternalIntent | Quiescent


def derive_next_action(persisted_state: SliceState | RunState) -> MergeDecision:
    """Derive one merge/review action from durable evidence only.

    The function deliberately accepts no watcher response or effect client.
    Repeated calls over the same checkpoint therefore produce the same result,
    independent of event arrival order.
    """
    if isinstance(persisted_state, RunState):
        return _derive_run_action(persisted_state)
    return _derive_slice_action(persisted_state)


def _derive_run_action(state: RunState) -> MergeDecision:
    active = tuple(
        current
        for _, current in sorted(state.slices.items())
        if current.status not in {SliceStatus.MERGED, SliceStatus.FAILED}
    )
    integration = state.integration
    if integration.lifecycle is IntegrationLifecycle.MERGED:
        return InternalTransition("terminal", "aggregate_merged")
    if integration.lifecycle in {IntegrationLifecycle.FAILED, IntegrationLifecycle.PARKED}:
        return Quiescent(f"aggregate_{integration.lifecycle.value.lower()}")
    if integration.lifecycle is IntegrationLifecycle.INTEGRATION_CONFLICT:
        return ExternalIntent(
            "repair_aggregate",
            integration.integration_owner_id or state.run_id,
            {"reason": "integration_conflict"},
        )
    if integration.lifecycle is IntegrationLifecycle.NEEDS_BASE_REVALIDATION:
        return ExternalIntent(
            "revalidate_base",
            integration.integration_owner_id or state.run_id,
            {"base_sha": integration.validated_base_sha},
        )
    if integration.lifecycle is IntegrationLifecycle.REPAIRING_AGGREGATE:
        return ExternalIntent(
            "repair_aggregate",
            integration.integration_owner_id or state.run_id,
            {"reason": "aggregate_repair"},
        )
    if integration.lifecycle is IntegrationLifecycle.INTEGRATION_VALIDATED:
        if not _aggregate_merge_gates_ready(integration):
            return Quiescent("await_integration_evidence")
        return ExternalIntent(
            "merge_aggregate",
            integration.integration_owner_id or state.run_id,
            {"pr_number": integration.aggregate_pr_number, "head_sha": integration.head_sha},
        )
    if integration.lifecycle is IntegrationLifecycle.READY_FOR_INTEGRATION:
        return ExternalIntent(
            "validate_integration",
            integration.integration_owner_id or state.run_id,
            {"head_sha": integration.head_sha},
        )
    if integration.lifecycle is IntegrationLifecycle.AGGREGATE_PR_OPEN:
        return Quiescent("await_aggregate_review")
    if integration.lifecycle is IntegrationLifecycle.CODE_REVIEWED:
        return Quiescent("await_integration_validation")
    if integration.lifecycle is IntegrationLifecycle.MERGING:
        return Quiescent("await_merge_recovery")
    if integration.lifecycle is IntegrationLifecycle.CHILDREN_MERGED:
        return ExternalIntent(
            "publish_aggregate",
            integration.integration_owner_id or state.run_id,
            {"patch_digest": integration.aggregate_patch_digest},
        )
    if active:
        return _derive_slice_action(active[0])
    return Quiescent("no_active_slices")


def _derive_slice_action(state: SliceState) -> MergeDecision:
    if state.status is SliceStatus.MERGED:
        return InternalTransition("terminal", "merged")
    if state.status in {
        SliceStatus.DISPATCH_FAILED,
        SliceStatus.FAILED,
        SliceStatus.PARKED,
        SliceStatus.BLOCKED,
    }:
        return Quiescent(f"closed_{state.status.value}")
    if _has_conflict(state):
        return InternalTransition("repairing", "conflict")
    current_head = _persisted_head(state)
    if (
        state.reviewed_head is not None
        and current_head is not None
        and state.reviewed_head != current_head
    ):
        return InternalTransition("in_review", "head_reset")
    if state.status is SliceStatus.REPAIRING:
        return ExternalIntent("repair", state.id, {"head_sha": current_head})
    if state.pr_number is None or current_head is None:
        return Quiescent("await_publication")
    if (
        state.handoff is None
        or state.handoff.pr_number != state.pr_number
        or state.handoff.head_sha != current_head
    ):
        return Quiescent("await_handoff")
    if state.publication is not None and state.publication.pr_number != state.pr_number:
        return Quiescent("await_publication")
    if state.reviewer_attempt.get(current_head, 0) == 0:
        return ExternalIntent(
            "spawn_reviewer",
            state.id,
            {"pr_number": state.pr_number, "head_sha": current_head},
        )
    if state.verdict is None or state.reviewed_head != current_head:
        return Quiescent("await_review")
    ci_status = state.ci_state.get(current_head, "unknown")
    if ci_status not in CI_STATUS_VALUES:
        return Quiescent("await_ci")
    if ci_status in {"unknown", "pending"}:
        return Quiescent("await_ci")
    if ci_status == "failure" or state.verdict is Verdict.NO_GO:
        return ExternalIntent("repair", state.id, {"head_sha": current_head})
    if state.action is not None and state.action.kind is ActionKind.MERGE:
        if state.action.phase in {
            ActionPhase.INTENDED,
            ActionPhase.IN_FLIGHT,
            ActionPhase.UNKNOWN,
        }:
            return Quiescent("await_merge_recovery")
        if state.action.phase in {ActionPhase.CONFIRMED, ActionPhase.RECONCILED}:
            return InternalTransition("terminal", "merge_confirmed")
    if state.verdict in {Verdict.GO, Verdict.GO_WITH_NITS} and ci_status in {
        "success",
        "neutral",
    }:
        return ExternalIntent(
            "merge",
            state.id,
            {"pr_number": state.pr_number, "head_sha": current_head},
        )
    return Quiescent("await_merge_gate")


def _persisted_head(state: SliceState) -> str | None:
    if state.publication is not None:
        return state.publication.head_sha
    if state.handoff is not None:
        return state.handoff.head_sha
    return state.reviewed_head


def _has_conflict(state: SliceState) -> bool:
    if state.dispatch_error is not None and "conflict" in state.dispatch_error.lower():
        return True
    reconciliation = state.reconciliation
    return isinstance(reconciliation, Mapping) and bool(reconciliation.get("conflicts"))


def _aggregate_merge_gates_ready(state: object) -> bool:
    required_text = (
        "head_sha",
        "aggregate_patch_digest",
        "patch_digest",
        "merge_tree_sha",
        "validated_base_sha",
        "integration_owner_id",
        "integration_owner_run_id",
        "integration_owner_branch",
        "integration_owner_worktree",
    )
    if any(not isinstance(getattr(state, name, None), str) for name in required_text):
        return False
    if not isinstance(getattr(state, "aggregate_pr_number", None), int):
        return False
    if getattr(state, "ci_status", "unknown") not in {"success", "neutral"}:
        return False
    return getattr(state, "stage_verification", "pending") == "passed"


def reduce_observation(
    slice_state: SliceState,
    observation: Mapping[str, object],
) -> ObservationReduction:
    """Fold one watcher observation without allowing delayed edges to regress state."""
    provenance = _observation_provenance(observation)
    current = slice_state.observation_provenance
    is_snapshot = observation.get("kind") == "snapshot" or observation.get("is_snapshot") is True
    reason = _ordering_reason(current, provenance, is_snapshot=is_snapshot)
    if reason != "accepted":
        return ObservationReduction(slice_state, provenance, False, False, reason)

    pr_number = observation.get("pr_number")
    if (
        pr_number is not None
        and pr_number != slice_state.pr_number
        and slice_state.pr_number is not None
    ):
        return ObservationReduction(slice_state, provenance, False, False, "identity_conflict")
    updated = _apply_head_evidence(slice_state, observation)
    updated = replace(updated, observation_provenance=provenance)
    return ObservationReduction(updated, provenance, updated != slice_state, True, "accepted")


def _observation_provenance(observation: Mapping[str, object]) -> ObservationProvenance:
    source = observation.get("source", "watcher")
    observed_at = observation.get("observed_at")
    if not isinstance(source, str) or not source:
        raise ValueError("observation source must be a non-empty string")
    if not isinstance(observed_at, str) or not observed_at:
        raise ValueError("observation observed_at must be a non-empty string")
    coverage = observation.get("coverage", ())
    if isinstance(coverage, list):
        coverage = tuple(coverage)
    if not isinstance(coverage, tuple):
        raise TypeError("observation coverage must be an array")
    return ObservationProvenance(
        source=source,
        observed_at=observed_at,
        event_seq=_optional_non_negative(observation.get("event_seq")),
        snapshot_id=_optional_text(observation.get("snapshot_id")),
        ledger_run_seq=_optional_non_negative(
            observation.get("ledger_run_seq", observation.get("run_seq"))
        ),
        snapshot_high_watermark=_optional_non_negative(observation.get("snapshot_high_watermark")),
        source_epoch=_non_negative(observation.get("source_epoch", 0), "source_epoch"),
        source_revision=_non_negative(observation.get("source_revision", 0), "source_revision"),
        coverage=cast(tuple[str, ...], coverage),
    )


def _ordering_reason(
    current: ObservationProvenance | None,
    incoming: ObservationProvenance,
    *,
    is_snapshot: bool,
) -> str:
    if current is None:
        return "accepted"
    if incoming.source_epoch < current.source_epoch:
        return "dominated_epoch"
    if incoming.source_epoch > current.source_epoch and not is_snapshot:
        return "baseline_required"
    if incoming.source_epoch == current.source_epoch:
        if (
            incoming.snapshot_high_watermark is not None
            and current.snapshot_high_watermark is not None
            and incoming.snapshot_high_watermark <= current.snapshot_high_watermark
        ):
            return "dominated_snapshot"
        if (
            incoming.ledger_run_seq is not None
            and current.ledger_run_seq is not None
            and incoming.ledger_run_seq <= current.ledger_run_seq
        ):
            return "dominated_sequence"
        if (
            current.snapshot_high_watermark is not None
            and incoming.ledger_run_seq is not None
            and incoming.ledger_run_seq <= current.snapshot_high_watermark
        ):
            return "dominated_sequence"
        if incoming.source_revision < current.source_revision:
            return "dominated_revision"
    return "accepted"


def _apply_head_evidence(slice_state: SliceState, observation: Mapping[str, object]) -> SliceState:
    head_sha = observation.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        return slice_state
    ci_status = observation.get("ci_status")
    ci_state = dict(slice_state.ci_state)
    if isinstance(ci_status, str) and ci_status:
        ci_state[head_sha] = ci_status
    findings = observation.get("review_findings")
    review_findings = dict(slice_state.review_findings)
    if isinstance(findings, list):
        review_findings[head_sha] = tuple(
            dict(item) for item in findings if isinstance(item, Mapping)
        )
    current_head = slice_state.reviewed_head
    if current_head is not None and head_sha != current_head:
        return replace(
            slice_state,
            review_findings=review_findings,
            ci_state=ci_state,
        )
    return replace(
        slice_state,
        pr_number=(
            cast(int, observation["pr_number"])
            if type(observation.get("pr_number")) is int
            else slice_state.pr_number
        ),
        reviewed_head=head_sha,
        review_findings=review_findings,
        ci_state=ci_state,
    )


def _optional_non_negative(value: object) -> int | None:
    if value is None:
        return None
    return _non_negative(value, "observation sequence")


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("observation text must be non-empty or null")
    return value


def _non_negative(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


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
            conflicts.append("authoritative owner disagrees with persisted dispatch owner")
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
