"""Read-only operator projection over durable TL state and ledger events."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

from tl_loop.events.envelope import EventEnvelope
from tl_loop.events.reader import SequenceStatus
from tl_loop.ordered import IntegrationLifecycle

from .diagnostics import (
    ActionReadModel,
    LaneReadModel,
    PostMergeReadModel,
    RecoveryReadModel,
    ReplayReadModel,
    ScopeReadModel,
    SliceIntegrationReadModel,
    _integration_bookkeeping_commit,
    _integration_freshness,
    _integration_merge_receipt,
    _integration_next_transition,
    _safe_binding,
    project_action,
    project_lane,
    project_post_merge,
    project_recovery,
    project_replay,
    project_scope,
    project_slice_integration,
    slice_blocking_state,
    slice_next_transition,
    slice_status_classification,
    slice_waiting_reason,
)
from .schema import BudgetCharge, BudgetLedger, RunState, SliceState, Verdict

RecentLimit: TypeAlias = int


@dataclass(frozen=True)
class HeadEvidence:
    """Bounded review and CI evidence for one observed PR head."""

    head_sha: str
    review_state: str | None
    review_kind: str | None
    review_verdict: str | None
    review_finding_count: int
    ci_status: str | None
    reviewer_attempt: int | None
    is_current: bool
    last_event_seq: int | None

    def to_document(self) -> dict[str, object]:
        """Return the body-free JSON representation."""
        return {
            "head_sha": self.head_sha,
            "review_state": self.review_state,
            "review_kind": self.review_kind,
            "review_verdict": self.review_verdict,
            "review_finding_count": self.review_finding_count,
            "ci_status": self.ci_status,
            "reviewer_attempt": self.reviewer_attempt,
            "is_current": self.is_current,
            "last_event_seq": self.last_event_seq,
        }


@dataclass(frozen=True)
class SliceReadModel:
    """Operator-safe slice state with per-head evidence summaries."""

    id: str
    status: str
    paths: tuple[str, ...]
    depends_on: tuple[str, ...]
    base_ref: str | None
    agent_type: str | None
    model: str | None
    branch: str | None
    worktree: str | None
    pr_number: int | None
    reviewed_head: str | None
    attempts: int
    repair_attempts: int
    verdict: str | None
    heads: tuple[HeadEvidence, ...]
    park_cause: str | None
    park_issue_id: int | None
    blocked_by: str | None
    stall_classification: str | None
    dispatch_intent_id: str | None
    dispatch_started_at: float | None
    dispatch_last_boundary: str | None
    dispatch_error: str | None
    dispatch_agent_id: str | None
    dispatch_authoritative_event_seq: int | None
    task_started_at: float | None
    manifest_node_id: str | None = None
    manifest_revision: int | None = None
    authority: str = "unknown"
    blocking_state: str | None = None
    waiting_reason: str | None = None
    next_transition: str = "await_controller"
    integration: SliceIntegrationReadModel | None = None
    action: ActionReadModel | None = None
    post_merge: PostMergeReadModel | None = None
    recovery: RecoveryReadModel | None = None
    publication: Mapping[str, object] | None = None
    handoff: Mapping[str, object] | None = None
    observation_provenance: Mapping[str, object] | None = None

    def to_document(self) -> dict[str, object]:
        """Return the body-free JSON representation."""
        return {
            "status": self.status,
            "paths": list(self.paths),
            "depends_on": list(self.depends_on),
            "base_ref": self.base_ref,
            "agent_type": self.agent_type,
            "model": self.model,
            "branch": self.branch,
            "worktree": self.worktree,
            "pr_number": self.pr_number,
            "reviewed_head": self.reviewed_head,
            "attempts": self.attempts,
            "repair_attempts": self.repair_attempts,
            "verdict": self.verdict,
            "heads": [head.to_document() for head in self.heads],
            "park_cause": self.park_cause,
            "park_issue_id": self.park_issue_id,
            "blocked_by": self.blocked_by,
            "stall_classification": self.stall_classification,
            "dispatch_intent_id": self.dispatch_intent_id,
            "dispatch_started_at": self.dispatch_started_at,
            "dispatch_last_boundary": self.dispatch_last_boundary,
            "dispatch_error": self.dispatch_error,
            "dispatch_agent_id": self.dispatch_agent_id,
            "dispatch_authoritative_event_seq": self.dispatch_authoritative_event_seq,
            "task_started_at": self.task_started_at,
            "manifest_node_id": self.manifest_node_id,
            "manifest_revision": self.manifest_revision,
            "authority": self.authority,
            "blocking_state": self.blocking_state,
            "waiting_reason": self.waiting_reason,
            "next_transition": self.next_transition,
            "integration": (
                self.integration.to_document() if self.integration is not None else None
            ),
            "action": self.action.to_document() if self.action is not None else None,
            "post_merge": (self.post_merge.to_document() if self.post_merge is not None else None),
            "recovery": self.recovery.to_document() if self.recovery is not None else None,
            "publication": dict(self.publication) if self.publication is not None else None,
            "handoff": dict(self.handoff) if self.handoff is not None else None,
            "observation_provenance": (
                dict(self.observation_provenance)
                if self.observation_provenance is not None
                else None
            ),
        }


@dataclass(frozen=True)
class OrderedSubTLReadModel:
    """Operator-safe progress for one ordered sub-TL."""

    id: str
    lifecycle: str
    status: str
    aggregate_pr_number: int | None
    head_sha: str | None
    patch_digest: str | None
    validated_base_sha: str | None
    merge_tree_sha: str | None
    integration_evidence_at: str | None
    review_state: str | None
    integration_ci: str
    revalidation_count: int
    repair_count: int
    park_cause: str | None
    stage_verification: str
    owner_id: str | None
    owner_run_id: str | None
    owner_branch: str | None
    owner_worktree: str | None

    def to_document(self) -> dict[str, object]:
        """Return the bounded progress representation."""
        return {
            "id": self.id,
            "lifecycle": self.lifecycle,
            "status": self.status,
            "aggregate_pr_number": self.aggregate_pr_number,
            "head_sha": self.head_sha,
            "patch_digest": self.patch_digest,
            "validated_base_sha": self.validated_base_sha,
            "merge_tree_sha": self.merge_tree_sha,
            "integration_evidence_at": self.integration_evidence_at,
            "review_state": self.review_state,
            "integration_ci": self.integration_ci,
            "revalidation_count": self.revalidation_count,
            "repair_count": self.repair_count,
            "park_cause": self.park_cause,
            "stage_verification": self.stage_verification,
            "owner_id": self.owner_id,
            "owner_run_id": self.owner_run_id,
            "owner_branch": self.owner_branch,
            "owner_worktree": self.owner_worktree,
        }


@dataclass(frozen=True)
class OrderedStageReadModel:
    """One order and the sub-TLs that execute in that order."""

    order: int
    sub_tls: tuple[OrderedSubTLReadModel, ...]

    def to_document(self) -> dict[str, object]:
        """Return the grouped stage representation."""
        return {
            "order": self.order,
            "sub_tls": [sub_tl.to_document() for sub_tl in self.sub_tls],
        }


@dataclass(frozen=True)
class IntegrationReadModel:
    """Aggregate integration evidence visible to operators."""

    lifecycle: str
    aggregate_pr_number: int | None
    head_sha: str | None
    validated_base_sha: str | None
    merge_tree_sha: str | None
    ci_status: str
    base_revalidation_count: int
    merge_attempts: int
    stage_verification: str
    owner_id: str | None
    aggregate_patch_digest: str | None = None
    aggregate_original_base_sha: str | None = None
    owner_run_id: str | None = None
    owner_branch: str | None = None
    owner_worktree: str | None = None
    merge_receipt: str | None = None
    bookkeeping_commit: str | None = None
    freshness: str = "unknown"
    lanes: Mapping[str, LaneReadModel] = field(default_factory=dict)
    next_transition: str = "await_controller"

    def to_document(self) -> dict[str, object]:
        """Return the body-free integration representation."""
        return {
            "lifecycle": self.lifecycle,
            "aggregate_pr_number": self.aggregate_pr_number,
            "head_sha": self.head_sha,
            "validated_base_sha": self.validated_base_sha,
            "merge_tree_sha": self.merge_tree_sha,
            "ci_status": self.ci_status,
            "base_revalidation_count": self.base_revalidation_count,
            "merge_attempts": self.merge_attempts,
            "stage_verification": self.stage_verification,
            "owner_id": self.owner_id,
            "aggregate_patch_digest": self.aggregate_patch_digest,
            "aggregate_original_base_sha": self.aggregate_original_base_sha,
            "owner_run_id": self.owner_run_id,
            "owner_branch": self.owner_branch,
            "owner_worktree": self.owner_worktree,
            "merge_receipt": self.merge_receipt,
            "bookkeeping_commit": self.bookkeeping_commit,
            "freshness": self.freshness,
            "lanes": {key: lane.to_document() for key, lane in self.lanes.items()},
            "next_transition": self.next_transition,
        }


@dataclass(frozen=True)
class BudgetChargeReadModel:
    """One bounded budget charge in the operator view."""

    slice_id: str
    attempt: int
    role: str
    harness: str
    estimated_tokens: int
    actual: int | str
    delta_tokens: int | None
    warning: bool
    reconciled: bool

    def to_document(self) -> dict[str, object]:
        """Return the JSON representation."""
        return {
            "slice_id": self.slice_id,
            "attempt": self.attempt,
            "role": self.role,
            "harness": self.harness,
            "estimated_tokens": self.estimated_tokens,
            "actual": self.actual,
            "delta_tokens": self.delta_tokens,
            "warning": self.warning,
            "reconciled": self.reconciled,
        }


@dataclass(frozen=True)
class BudgetReadModel:
    """Read-only budget counters and immutable charge history."""

    tokens: int
    wall_seconds: int
    role_spent: Mapping[str, int]
    harness_spent: Mapping[str, int]
    role_reserved: Mapping[str, int]
    harness_reserved: Mapping[str, int]
    charges: tuple[BudgetChargeReadModel, ...]

    def to_document(self) -> dict[str, object]:
        """Return the JSON representation."""
        return {
            "tokens": self.tokens,
            "wall_seconds": self.wall_seconds,
            "role_spent": dict(self.role_spent),
            "harness_spent": dict(self.harness_spent),
            "role_reserved": dict(self.role_reserved),
            "harness_reserved": dict(self.harness_reserved),
            "charges": [charge.to_document() for charge in self.charges],
        }


@dataclass(frozen=True)
class GateReadModel:
    """One named human gate."""

    name: str
    status: str

    def to_document(self) -> dict[str, str]:
        """Return the JSON representation."""
        return {"name": self.name, "status": self.status}


@dataclass(frozen=True)
class TransitionReadModel:
    """Allowlisted metadata for one recent ledger transition."""

    run_seq: int
    event_type: str
    observed_at: str
    lifecycle_state: str
    agent_id: str | None
    slice_id: str | None
    harness: str | None
    role: str | None
    pr_number: int | None
    head_sha: str | None
    review_kind: str | None
    review_state: str | None
    ci_status: str | None
    stall_classification: str | None

    def to_document(self) -> dict[str, object]:
        """Return the body-free JSON representation."""
        return {
            "run_seq": self.run_seq,
            "event_type": self.event_type,
            "observed_at": self.observed_at,
            "lifecycle_state": self.lifecycle_state,
            "agent_id": self.agent_id,
            "slice_id": self.slice_id,
            "harness": self.harness,
            "role": self.role,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "review_kind": self.review_kind,
            "review_state": self.review_state,
            "ci_status": self.ci_status,
            "stall_classification": self.stall_classification,
        }


@dataclass(frozen=True)
class ReadModel:
    """Immutable operator view built from one durable state cursor."""

    run_id: str
    session_mode: str | None
    revision: int
    phase: str
    waiting: tuple[str, ...]
    ledger_cursor: int
    ledger_sequence_status: str | None
    slices: Mapping[str, SliceReadModel]
    budgets: BudgetReadModel
    gates: tuple[GateReadModel, ...]
    park_causes: Mapping[str, str]
    recent_transitions: tuple[TransitionReadModel, ...]
    controller_started_at: float | None = None
    elapsed_seconds: float | None = None
    last_authoritative_event_seq: int | None = None
    last_observed_progress_at: float | None = None
    schema_version: int = 2
    current_order: int = 1
    ordered_stages: tuple[OrderedStageReadModel, ...] = ()
    integration: IntegrationReadModel = field(
        default_factory=lambda: IntegrationReadModel(
            lifecycle=IntegrationLifecycle.RUNNING.value,
            aggregate_pr_number=None,
            head_sha=None,
            validated_base_sha=None,
            merge_tree_sha=None,
            ci_status="unknown",
            base_revalidation_count=0,
            merge_attempts=0,
            stage_verification="pending",
            owner_id=None,
        )
    )
    scope: ScopeReadModel | None = None
    lanes: Mapping[str, LaneReadModel] = field(default_factory=dict)
    replay: ReplayReadModel | None = None
    blocking: Mapping[str, str] = field(default_factory=dict)
    next_transition: str = "await_controller"

    def to_document(self) -> dict[str, object]:
        """Return a stable JSON document without agent-authored bodies."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "session_mode": self.session_mode,
            "revision": self.revision,
            "phase": self.phase,
            "waiting": list(self.waiting),
            "ledger_cursor": self.ledger_cursor,
            "ledger_sequence_status": self.ledger_sequence_status,
            "slices": {
                slice_id: slice_model.to_document() for slice_id, slice_model in self.slices.items()
            },
            "budgets": self.budgets.to_document(),
            "gates": [gate.to_document() for gate in self.gates],
            "park_causes": dict(self.park_causes),
            "recent_transitions": [
                transition.to_document() for transition in self.recent_transitions
            ],
            "controller_started_at": self.controller_started_at,
            "elapsed_seconds": self.elapsed_seconds,
            "last_authoritative_event_seq": self.last_authoritative_event_seq,
            "last_observed_progress_at": self.last_observed_progress_at,
            "current_order": self.current_order,
            "ordered_stages": [stage.to_document() for stage in self.ordered_stages],
            "integration": self.integration.to_document(),
            "scope": self.scope.to_document() if self.scope is not None else None,
            "lanes": {key: lane.to_document() for key, lane in self.lanes.items()},
            "replay": self.replay.to_document() if self.replay is not None else None,
            "blocking": dict(self.blocking),
            "recursive_phase": self.scope.phase if self.scope is not None else None,
            "scope_role": self.scope.role if self.scope is not None else None,
            "next_transition": self.next_transition,
        }


def project_read_model(
    state: RunState,
    events: Iterable[EventEnvelope] = (),
    *,
    sequence_status: SequenceStatus | str | None = None,
    recent_transition_limit: RecentLimit = 20,
) -> ReadModel:
    """Project durable state and consumed ledger observations for operators.

    The persisted event cursor is the authority for the state snapshot. Events
    beyond that cursor are excluded so the model cannot present observations
    that the controller has not incorporated into run.json yet.
    """
    if type(recent_transition_limit) is not int or recent_transition_limit < 0:
        raise ValueError("recent_transition_limit must be a non-negative integer")
    event_list = _events_at_cursor(events, state.events.last_consumed_offset)
    event_index = _index_events(state, event_list)
    slices = {
        slice_id: _slice_model(slice_state, event_index.get(slice_id, {}))
        for slice_id, slice_state in sorted(state.slices.items())
    }
    park_causes = {
        slice_id: slice_model.park_cause
        for slice_id, slice_model in slices.items()
        if slice_model.park_cause is not None
    }
    status = _sequence_status_value(sequence_status)
    recent = (
        tuple(
            _transition(event)
            for event in event_list[-recent_transition_limit:]
            if event.run_seq is not None
        )
        if recent_transition_limit
        else ()
    )
    ordered_stages = _ordered_stage_models(state, slices)
    integration = _integration_model(state)
    scope = project_scope(state)
    replay_model = project_replay(
        state,
        consumed_event_count=len(event_list),
        last_event_seq=event_list[-1].run_seq if event_list else None,
        sequence_status=status,
    )
    blocking = MappingProxyType(
        {
            slice_id: slice_model.blocking_state
            for slice_id, slice_model in slices.items()
            if slice_model.blocking_state is not None
        }
    )
    lanes = MappingProxyType(
        {key: project_lane(key, lane) for key, lane in sorted(state.integration.lanes.items())}
    )
    return ReadModel(
        run_id=state.run_id,
        session_mode=state.session_mode.value if state.session_mode is not None else None,
        revision=state.revision,
        phase=state.fsm.phase.value,
        waiting=tuple(state.fsm.waiting),
        ledger_cursor=state.events.last_consumed_offset,
        ledger_sequence_status=status,
        slices=MappingProxyType(slices),
        budgets=_budget_model(state.budgets),
        gates=tuple(GateReadModel(gate.name, gate.status.value) for gate in state.gates),
        park_causes=MappingProxyType(park_causes),
        recent_transitions=recent,
        controller_started_at=state.goals.controller_started_at,
        elapsed_seconds=_elapsed_seconds(state.goals.controller_started_at),
        last_authoritative_event_seq=state.goals.last_authoritative_event_seq,
        last_observed_progress_at=state.goals.last_progress_at,
        current_order=state.current_order,
        ordered_stages=ordered_stages,
        integration=integration,
        scope=scope,
        lanes=lanes,
        replay=replay_model,
        blocking=blocking,
        next_transition=_next_transition(state, slices, ordered_stages, integration, scope),
    )


def _integration_model(state: RunState) -> IntegrationReadModel:
    integration = state.integration
    return IntegrationReadModel(
        lifecycle=integration.lifecycle.value,
        aggregate_pr_number=integration.aggregate_pr_number,
        head_sha=integration.head_sha or integration.aggregate_head_sha,
        validated_base_sha=integration.validated_base_sha,
        merge_tree_sha=integration.merge_tree_sha,
        ci_status=integration.ci_status,
        base_revalidation_count=integration.base_revalidation_count,
        merge_attempts=integration.merge_attempts,
        stage_verification=integration.stage_verification,
        owner_id=integration.integration_owner_id,
        aggregate_patch_digest=integration.aggregate_patch_digest,
        aggregate_original_base_sha=integration.aggregate_original_base_sha,
        owner_run_id=integration.integration_owner_run_id,
        owner_branch=integration.integration_owner_branch,
        owner_worktree=integration.integration_owner_worktree,
        merge_receipt=_integration_merge_receipt(integration),
        bookkeeping_commit=_integration_bookkeeping_commit(integration),
        freshness=_integration_freshness(integration),
        lanes=MappingProxyType(
            {key: project_lane(key, lane) for key, lane in sorted(integration.lanes.items())}
        ),
        next_transition=_integration_next_transition(integration),
    )


def _ordered_stage_models(
    state: RunState,
    slices: Mapping[str, SliceReadModel],
) -> tuple[OrderedStageReadModel, ...]:
    integration = state.integration
    models: list[OrderedStageReadModel] = []
    for stage in state.ordered_stages:
        children: list[OrderedSubTLReadModel] = []
        for slice_id in stage.sub_tls:
            slice_model = slices.get(slice_id)
            if slice_model is None:
                continue
            candidate = integration.candidates.get(slice_id)
            lifecycle = (
                candidate.lifecycle
                if candidate is not None
                else integration.sub_tl_states.get(slice_id)
            )
            lifecycle_value = (
                lifecycle.value if lifecycle is not None else _lifecycle_for_slice(slice_model)
            )
            aggregate_pr_number = (
                candidate.aggregate_pr_number
                if candidate is not None
                else integration.aggregate_pr_number
            )
            head_sha = (
                (candidate.head_sha or candidate.aggregate_head_sha)
                if candidate is not None
                else integration.head_sha or integration.aggregate_head_sha
            ) or slice_model.reviewed_head
            patch_digest = (
                candidate.patch_digest or candidate.aggregate_patch_digest
                if candidate is not None
                else integration.patch_digest or integration.aggregate_patch_digest
            )
            validated_base_sha = (
                candidate.validated_base_sha
                if candidate is not None
                else integration.validated_base_sha
            )
            merge_tree_sha = (
                candidate.merge_tree_sha if candidate is not None else integration.merge_tree_sha
            )
            integration_evidence_at = (
                candidate.integration_evidence_at
                if candidate is not None
                else integration.integration_evidence_at
            )
            review_state = _current_review_state(slice_model)
            ci_status = (
                candidate.ci_status
                if candidate is not None
                else _current_ci_status(slice_model) or integration.ci_status
            )
            stage_verification = (
                (
                    candidate.stage_verification
                    if candidate is not None
                    else integration.stage_verification
                )
                if stage.order == state.current_order
                else "pending"
            )
            children.append(
                OrderedSubTLReadModel(
                    id=slice_id,
                    lifecycle=lifecycle_value,
                    status=slice_model.status,
                    aggregate_pr_number=aggregate_pr_number or slice_model.pr_number,
                    head_sha=head_sha,
                    patch_digest=patch_digest,
                    validated_base_sha=validated_base_sha,
                    merge_tree_sha=merge_tree_sha,
                    integration_evidence_at=integration_evidence_at,
                    review_state=review_state,
                    integration_ci=ci_status,
                    revalidation_count=(
                        candidate.base_revalidation_count
                        if candidate is not None
                        else integration.base_revalidation_count
                    ),
                    repair_count=slice_model.repair_attempts,
                    park_cause=slice_model.park_cause,
                    stage_verification=stage_verification,
                    owner_id=(
                        candidate.integration_owner_id
                        if candidate is not None
                        else integration.integration_owner_id
                    ),
                    owner_run_id=(
                        candidate.integration_owner_run_id
                        if candidate is not None
                        else integration.integration_owner_run_id
                    ),
                    owner_branch=(
                        candidate.integration_owner_branch
                        if candidate is not None
                        else integration.integration_owner_branch
                    ),
                    owner_worktree=(
                        candidate.integration_owner_worktree
                        if candidate is not None
                        else integration.integration_owner_worktree
                    ),
                )
            )
        models.append(OrderedStageReadModel(stage.order, tuple(children)))
    return tuple(models)


def _lifecycle_for_slice(slice_model: SliceReadModel) -> str:
    if slice_model.status == "merged":
        return IntegrationLifecycle.MERGED.value
    if slice_model.status == "parked":
        return IntegrationLifecycle.PARKED.value
    if slice_model.status in {"failed", "dispatch_failed"}:
        return IntegrationLifecycle.FAILED.value
    if slice_model.status == "repairing":
        return IntegrationLifecycle.REPAIRING_AGGREGATE.value
    if slice_model.status == "in_review":
        if slice_model.verdict in {"GO", "GO-WITH-NITS"}:
            return IntegrationLifecycle.READY_FOR_INTEGRATION.value
        return IntegrationLifecycle.CODE_REVIEWED.value
    return IntegrationLifecycle.RUNNING.value


def _current_review_state(slice_model: SliceReadModel) -> str | None:
    for head in slice_model.heads:
        if head.is_current:
            return head.review_state
    return None


def _current_ci_status(slice_model: SliceReadModel) -> str | None:
    for head in slice_model.heads:
        if head.is_current:
            return head.ci_status
    return None


def _next_transition(
    state: RunState,
    slices: Mapping[str, SliceReadModel],
    stages: tuple[OrderedStageReadModel, ...],
    integration: IntegrationReadModel,
    scope: ScopeReadModel,
) -> str:
    pending_gate = next(
        (gate.name for gate in state.gates if gate.status.value == "pending"),
        None,
    )
    if pending_gate is not None:
        return f"answer_gate:{pending_gate}"
    blocking_slice = next(
        (
            (slice_id, slice_model)
            for slice_id, slice_model in sorted(slices.items())
            if slice_model.blocking_state is not None
        ),
        None,
    )
    if blocking_slice is not None:
        slice_id, slice_model = blocking_slice
        return f"slice:{slice_id}:{slice_model.next_transition}"
    blocking_lane = next(
        (
            (lane_key, lane)
            for lane_key, lane in sorted(integration.lanes.items())
            if lane.phase in {"recovery", "bookkeeping", "parked"}
        ),
        None,
    )
    if blocking_lane is not None:
        lane_key, lane = blocking_lane
        return f"lane:{lane_key}:{lane.next_transition}"
    sub_tls = [sub_tl for stage in stages for sub_tl in stage.sub_tls]
    conflict = next(
        (
            sub_tl
            for sub_tl in sub_tls
            if sub_tl.lifecycle == IntegrationLifecycle.INTEGRATION_CONFLICT.value
        ),
        None,
    )
    if conflict is not None:
        return f"resume_pr:{conflict.owner_id or 'aggregate'}"
    lifecycles = [sub_tl.lifecycle for sub_tl in sub_tls]
    if IntegrationLifecycle.NEEDS_BASE_REVALIDATION.value in lifecycles:
        return "revalidate_base_and_integration_ci"
    if IntegrationLifecycle.READY_FOR_INTEGRATION.value in lifecycles:
        return "validate_integration_evidence"
    if integration.lifecycle == IntegrationLifecycle.INTEGRATION_CONFLICT.value:
        return f"resume_pr:{integration.owner_id or 'aggregate'}"
    if any(
        slice_model.status in {"pending", "ready", "spawned", "dispatching"}
        for slice_model in state.slices.values()
    ):
        return "await_sub_tl_completion"
    if any(
        slice_model.status in {"in_review", "repairing"} for slice_model in state.slices.values()
    ):
        return "await_review_or_ci"
    if scope.next_transition != "advance_scope":
        return scope.next_transition
    if integration.lifecycle == IntegrationLifecycle.MERGED.value and stages:
        return "complete_ordered_stage"
    return "await_controller"


def _events_at_cursor(events: Iterable[EventEnvelope], cursor: int) -> tuple[EventEnvelope, ...]:
    selected = [event for event in events if event.run_seq is not None and event.run_seq <= cursor]
    return tuple(sorted(selected, key=lambda event: event.run_seq or 0))


def _index_events(
    state: RunState, events: Iterable[EventEnvelope]
) -> dict[str, dict[str, EventEnvelope]]:
    indexed: dict[str, dict[str, EventEnvelope]] = {}
    for event in events:
        if event.head_sha is None:
            continue
        slice_id = _event_slice_id(state, event)
        if slice_id is None:
            continue
        by_head = indexed.setdefault(slice_id, {})
        previous = by_head.get(event.head_sha)
        if previous is None or (previous.run_seq or -1) <= (event.run_seq or -1):
            by_head[event.head_sha] = _merge_head_event(previous, event)
    return indexed


def _merge_head_event(previous: EventEnvelope | None, current: EventEnvelope) -> EventEnvelope:
    """Retain the latest value for each bounded evidence dimension."""
    if previous is None:
        return current
    return EventEnvelope(
        kind=current.kind,
        event_type=current.event_type,
        run_seq=current.run_seq,
        run_id=current.run_id,
        agent_id=current.agent_id or previous.agent_id,
        slice_id=current.slice_id or previous.slice_id,
        session_id=current.session_id or previous.session_id,
        invocation_id=current.invocation_id or previous.invocation_id,
        generation=current.generation,
        harness=current.harness or previous.harness,
        role=current.role or previous.role,
        lifecycle_state=current.lifecycle_state,
        observed_at=current.observed_at,
        pr_number=current.pr_number or previous.pr_number,
        head_sha=current.head_sha,
        review_kind=current.review_kind or previous.review_kind,
        notification=None,
        review_state=current.review_state or previous.review_state,
        ci_status=current.ci_status or previous.ci_status,
        data=MappingProxyType({}),
        parent_agent_id=current.parent_agent_id or previous.parent_agent_id,
    )


def _event_slice_id(state: RunState, event: EventEnvelope) -> str | None:
    if event.slice_id in state.slices:
        return event.slice_id
    if event.pr_number is None:
        return None
    matches = [
        slice_id
        for slice_id, slice_state in state.slices.items()
        if slice_state.pr_number == event.pr_number
    ]
    return matches[0] if len(matches) == 1 else None


def _slice_model(state: SliceState, events: Mapping[str, EventEnvelope]) -> SliceReadModel:
    heads = set(state.review_findings) | set(state.ci_state) | set(state.reviewer_attempt)
    if state.reviewed_head is not None:
        heads.add(state.reviewed_head)
    heads.update(events)
    head_models = tuple(
        _head_model(state, head_sha, events.get(head_sha)) for head_sha in sorted(heads)
    )
    return SliceReadModel(
        id=state.id,
        status=state.status.value,
        paths=tuple(state.paths),
        depends_on=tuple(state.depends_on),
        base_ref=state.base_ref,
        agent_type=state.agent_type,
        model=state.model,
        branch=state.branch,
        worktree=state.worktree,
        pr_number=state.pr_number,
        reviewed_head=state.reviewed_head,
        attempts=state.attempts,
        repair_attempts=state.repair_attempts,
        verdict=state.verdict.value if state.verdict is not None else None,
        heads=head_models,
        park_cause=state.park_cause.value if state.park_cause is not None else None,
        park_issue_id=state.park_issue_id,
        blocked_by=state.blocked_by,
        stall_classification=state.stall_classification,
        dispatch_intent_id=state.dispatch_intent_id,
        dispatch_started_at=state.dispatch_started_at,
        dispatch_last_boundary=state.dispatch_last_boundary,
        dispatch_error=state.dispatch_error,
        dispatch_agent_id=state.dispatch_agent_id,
        dispatch_authoritative_event_seq=state.dispatch_authoritative_event_seq,
        task_started_at=state.dispatch_started_at,
        manifest_node_id=state.manifest_node_id,
        manifest_revision=state.manifest_revision,
        authority=slice_status_classification(state, observed=bool(events)),
        blocking_state=slice_blocking_state(state),
        waiting_reason=slice_waiting_reason(state),
        next_transition=slice_next_transition(state),
        integration=project_slice_integration(state),
        action=project_action(state.action),
        post_merge=project_post_merge(state.post_merge),
        recovery=project_recovery(state.recovery),
        publication=_safe_binding(state.publication),
        handoff=_safe_binding(state.handoff),
        observation_provenance=_safe_binding(state.observation_provenance),
    )


def _elapsed_seconds(started_at: float | None) -> float | None:
    if started_at is None:
        return None
    return max(0.0, time.time() - started_at)


def _head_model(state: SliceState, head_sha: str, event: EventEnvelope | None) -> HeadEvidence:
    finding_count = len(state.review_findings.get(head_sha, ()))
    review_state = event.review_state if event is not None else None
    if review_state is None and state.verdict is not None and state.reviewed_head == head_sha:
        review_state = _review_state_for_verdict(state.verdict)
    if review_state is None and finding_count:
        review_state = "changes_requested"
    return HeadEvidence(
        head_sha=head_sha,
        review_state=review_state,
        review_kind=event.review_kind if event is not None else None,
        review_verdict=(
            state.verdict.value
            if state.verdict is not None and state.reviewed_head == head_sha
            else None
        ),
        review_finding_count=finding_count,
        ci_status=(
            state.ci_state.get(head_sha) or (event.ci_status if event is not None else None)
        ),
        reviewer_attempt=state.reviewer_attempt.get(head_sha),
        is_current=state.reviewed_head == head_sha,
        last_event_seq=event.run_seq if event is not None else None,
    )


def _review_state_for_verdict(verdict: Verdict) -> str:
    if verdict is Verdict.NO_GO:
        return "changes_requested"
    if verdict is Verdict.GO_WITH_NITS:
        return "approved_with_nits"
    return "approved"


def _budget_model(ledger: BudgetLedger) -> BudgetReadModel:
    return BudgetReadModel(
        tokens=ledger.tokens,
        wall_seconds=ledger.wall_seconds,
        role_spent=MappingProxyType(dict(ledger.role_spent)),
        harness_spent=MappingProxyType(dict(ledger.harness_spent)),
        role_reserved=MappingProxyType(dict(ledger.role_reserved)),
        harness_reserved=MappingProxyType(dict(ledger.harness_reserved)),
        charges=tuple(_charge_model(charge) for charge in ledger.charges),
    )


def _charge_model(charge: BudgetCharge) -> BudgetChargeReadModel:
    return BudgetChargeReadModel(
        slice_id=charge.slice_id,
        attempt=charge.attempt,
        role=charge.role,
        harness=charge.harness,
        estimated_tokens=charge.estimated_tokens,
        actual=charge.actual,
        delta_tokens=charge.delta_tokens,
        warning=charge.warning,
        reconciled=charge.reconciled,
    )


def _sequence_status_value(value: SequenceStatus | str | None) -> str | None:
    if value is None:
        return None
    return value.value if isinstance(value, Enum) else value


def _transition(event: EventEnvelope) -> TransitionReadModel:
    if event.run_seq is None:
        raise ValueError("recent transition requires a ledger sequence")
    classification = event.stall_classification
    return TransitionReadModel(
        run_seq=event.run_seq,
        event_type=event.event_type,
        observed_at=event.observed_at,
        lifecycle_state=event.lifecycle_state,
        agent_id=event.agent_id,
        slice_id=event.slice_id,
        harness=event.harness,
        role=event.role,
        pr_number=event.pr_number,
        head_sha=event.head_sha,
        review_kind=event.review_kind,
        review_state=event.review_state,
        ci_status=event.ci_status,
        stall_classification=classification.value if classification is not None else None,
    )


__all__ = [
    "ActionReadModel",
    "BudgetChargeReadModel",
    "BudgetReadModel",
    "GateReadModel",
    "HeadEvidence",
    "IntegrationReadModel",
    "LaneReadModel",
    "OrderedStageReadModel",
    "OrderedSubTLReadModel",
    "PostMergeReadModel",
    "ReadModel",
    "RecentLimit",
    "RecoveryReadModel",
    "ReplayReadModel",
    "ScopeReadModel",
    "SliceIntegrationReadModel",
    "SliceReadModel",
    "TransitionReadModel",
    "project_read_model",
]
