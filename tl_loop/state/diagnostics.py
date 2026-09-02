"""Bounded diagnostics for recursive TL scopes and durable recovery state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from tl_loop.fsm.child import ChildRecord
from tl_loop.fsm.lane import LanePhase, LaneState
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.recovery import RecoveryState
from tl_loop.fsm.scope import (
    TLAllMerged,
    TLDone,
    TLFailed,
    TLFinalizing,
    TLParked,
    TLPlanning,
    TLPRFiled,
    TLRunning,
)
from tl_loop.state.schema import ActionPhase, ActionState, RunState, SliceState, SliceStatus

_POST_MERGE_REQUIREMENTS: dict[PostMergePhase, tuple[str, ...]] = {
    PostMergePhase.REMOTE_MERGE_ADOPTED: (
        "repository",
        "parent_branch",
        "pr_number",
        "head_sha",
        "merge_journal_id",
        "lane_epoch",
    ),
    PostMergePhase.PARENT_BRANCH_SYNCED: ("parent_commit_sha",),
    PostMergePhase.ISSUE_CLOSE_PENDING: ("issue_id", "issue_close_intent_id"),
    PostMergePhase.ISSUE_CLOSE_CONFIRMED: ("issue_close_journal_id",),
    PostMergePhase.CHANGELOG_PENDING: ("changelog_intent_id", "changelog_generation"),
    PostMergePhase.CHANGELOG_COMMITTED: ("changelog_commit_sha",),
    PostMergePhase.PARENT_PUSH_PENDING: (
        "parent_push_intent_id",
        "push_journal_id",
        "expected_base_sha",
    ),
    PostMergePhase.COMPLETE: (
        "push_receipt_id",
        "pushed_commit",
        "bookkeeping_commit",
        "observed_remote_head",
        "ancestry_proof",
    ),
}

_POST_MERGE_NEXT: dict[PostMergePhase, str] = {
    PostMergePhase.NOT_STARTED: "await_remote_merge",
    PostMergePhase.NOT_REQUIRED: "complete_child",
    PostMergePhase.REMOTE_MERGE_ADOPTED: "sync_parent_branch",
    PostMergePhase.PARENT_BRANCH_SYNCED: "confirm_issue_close",
    PostMergePhase.ISSUE_CLOSE_PENDING: "confirm_issue_close",
    PostMergePhase.ISSUE_CLOSE_CONFIRMED: "commit_changelog",
    PostMergePhase.CHANGELOG_PENDING: "commit_changelog",
    PostMergePhase.CHANGELOG_COMMITTED: "push_parent_bookkeeping",
    PostMergePhase.PARENT_PUSH_PENDING: "confirm_parent_push",
    PostMergePhase.COMPLETE: "release_child_lane",
}


@dataclass(frozen=True)
class ActionReadModel:
    """Durable action identity without effect arguments or bodies."""

    kind: str
    phase: str
    state_version: int
    intent_id: str | None
    head_sha: str | None
    attempt: int | None
    contract_digest: str | None

    def to_document(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "phase": self.phase,
            "state_version": self.state_version,
            "intent_id": self.intent_id,
            "head_sha": self.head_sha,
            "attempt": self.attempt,
            "contract_digest": self.contract_digest,
        }


@dataclass(frozen=True)
class PostMergeReadModel:
    """One post-merge FSM step and its safe evidence identifiers."""

    phase: str
    evidence: Mapping[str, str]
    missing_evidence: tuple[str, ...]
    next_transition: str

    def to_document(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "evidence": dict(self.evidence),
            "missing_evidence": list(self.missing_evidence),
            "next_transition": self.next_transition,
        }


@dataclass(frozen=True)
class RecoveryReadModel:
    """Durable recovery ownership, round, and probe diagnostics."""

    cause: str
    phase: str
    recovery_round: int
    next_action: str
    owner_run_id: str
    owner_agent_id: str | None
    invocation_generation: int
    plan_revision: int
    last_probe_at: float | None
    next_probe_at: float | None
    probe_count: int
    evidence: Mapping[str, object]

    def to_document(self) -> dict[str, object]:
        return {
            "cause": self.cause,
            "phase": self.phase,
            "recovery_round": self.recovery_round,
            "next_action": self.next_action,
            "owner_run_id": self.owner_run_id,
            "owner_agent_id": self.owner_agent_id,
            "invocation_generation": self.invocation_generation,
            "plan_revision": self.plan_revision,
            "last_probe_at": self.last_probe_at,
            "next_probe_at": self.next_probe_at,
            "probe_count": self.probe_count,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class LaneReadModel:
    """One repository/parent-branch lane and its legal next transition."""

    key: str
    repository: str
    parent_branch: str
    phase: str
    child_id: str | None
    lane_epoch: int | None
    expected_base_sha: str | None
    head_sha: str | None
    merge_journal_id: str | None
    push_intent_id: str | None
    push_journal_id: str | None
    changelog_commit: str | None
    last_push_receipt_id: str | None
    last_remote_head: str | None
    last_ancestry_proof: str | None
    next_transition: str

    def to_document(self) -> dict[str, object]:
        return {
            "key": self.key,
            "repository": self.repository,
            "parent_branch": self.parent_branch,
            "phase": self.phase,
            "child_id": self.child_id,
            "lane_epoch": self.lane_epoch,
            "expected_base_sha": self.expected_base_sha,
            "head_sha": self.head_sha,
            "merge_journal_id": self.merge_journal_id,
            "push_intent_id": self.push_intent_id,
            "push_journal_id": self.push_journal_id,
            "changelog_commit": self.changelog_commit,
            "last_push_receipt_id": self.last_push_receipt_id,
            "last_remote_head": self.last_remote_head,
            "last_ancestry_proof": self.last_ancestry_proof,
            "next_transition": self.next_transition,
        }


@dataclass(frozen=True)
class SliceIntegrationReadModel:
    """Direct slice publication, review, CI, and merge evidence."""

    lifecycle: str
    pr_number: int | None
    head_sha: str | None
    reviewed_head: str | None
    base_sha: str | None
    tree_sha: str | None
    ci_status: str | None
    merge_receipt: str | None
    bookkeeping_commit: str | None
    freshness: str
    publication_ownership: str
    review_evidence: Mapping[str, object]
    next_transition: str

    def to_document(self) -> dict[str, object]:
        return {
            "lifecycle": self.lifecycle,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "reviewed_head": self.reviewed_head,
            "base_sha": self.base_sha,
            "tree_sha": self.tree_sha,
            "ci_status": self.ci_status,
            "merge_receipt": self.merge_receipt,
            "bookkeeping_commit": self.bookkeeping_commit,
            "freshness": self.freshness,
            "publication_ownership": self.publication_ownership,
            "review_evidence": dict(self.review_evidence),
            "next_transition": self.next_transition,
        }


@dataclass(frozen=True)
class ScopeReadModel:
    """Exact recursive scope position and active barrier."""

    scope_id: str
    parent_scope_id: str | None
    scope_path: tuple[str, ...]
    role: str
    owned_branch: str | None
    parent_integration_target: str | None
    plan_digest: str | None
    manifest_revision: int | None
    phase: str
    current_order: int | None
    active_barrier: tuple[str, ...]
    waiting: tuple[str, ...]
    parallel_pending: tuple[str, ...]
    pending_by_order: Mapping[str, tuple[str, ...]]
    completed_children: tuple[str, ...]
    child_records: Mapping[str, Mapping[str, object]]
    dispatch_intents: Mapping[str, str]
    lane_bindings: Mapping[str, str]
    evidence: Mapping[str, str]
    child_manifest_digests: Mapping[str, str]
    next_transition: str

    def to_document(self) -> dict[str, object]:
        return {
            "scope_id": self.scope_id,
            "parent_scope_id": self.parent_scope_id,
            "scope_path": list(self.scope_path),
            "role": self.role,
            "owned_branch": self.owned_branch,
            "parent_integration_target": self.parent_integration_target,
            "plan_digest": self.plan_digest,
            "manifest_revision": self.manifest_revision,
            "phase": self.phase,
            "current_order": self.current_order,
            "active_barrier": list(self.active_barrier),
            "waiting": list(self.waiting),
            "parallel_pending": list(self.parallel_pending),
            "pending_by_order": {
                order: list(children) for order, children in self.pending_by_order.items()
            },
            "completed_children": list(self.completed_children),
            "child_records": {
                child_id: dict(record) for child_id, record in self.child_records.items()
            },
            "dispatch_intents": dict(self.dispatch_intents),
            "lane_bindings": dict(self.lane_bindings),
            "evidence": dict(self.evidence),
            "child_manifest_digests": dict(self.child_manifest_digests),
            "next_transition": self.next_transition,
        }


@dataclass(frozen=True)
class ReplayReadModel:
    """Cursor-bounded ledger replay and reducer-version diagnostics."""

    cursor: int
    sequence_status: str | None
    consumed_event_count: int
    last_event_seq: int | None
    state_version: int
    reducer_version: int
    authority: str = "consumed_ledger_prefix"

    def to_document(self) -> dict[str, object]:
        return {
            "cursor": self.cursor,
            "sequence_status": self.sequence_status,
            "consumed_event_count": self.consumed_event_count,
            "last_event_seq": self.last_event_seq,
            "state_version": self.state_version,
            "reducer_version": self.reducer_version,
            "authority": self.authority,
        }


def project_action(action: ActionState | None) -> ActionReadModel | None:
    if action is None:
        return None
    return ActionReadModel(
        kind=action.kind.value,
        phase=action.phase.value,
        state_version=action.state_version,
        intent_id=action.intent_id,
        head_sha=action.head_sha,
        attempt=action.attempt,
        contract_digest=action.contract_digest,
    )


def project_post_merge(post_merge: PostMergeState | None) -> PostMergeReadModel | None:
    if post_merge is None:
        return None
    evidence = _safe_evidence(post_merge.evidence)
    required = _POST_MERGE_REQUIREMENTS.get(post_merge.phase, ())
    missing = tuple(field for field in required if not post_merge.evidence.get(field))
    return PostMergeReadModel(
        phase=post_merge.phase.value,
        evidence=evidence,
        missing_evidence=missing,
        next_transition=_POST_MERGE_NEXT[post_merge.phase],
    )


def project_recovery(recovery: RecoveryState | None) -> RecoveryReadModel | None:
    if recovery is None:
        return None
    return RecoveryReadModel(
        cause=recovery.cause,
        phase=recovery.phase.value,
        recovery_round=recovery.recovery_round,
        next_action=recovery.next_action,
        owner_run_id=recovery.owner_run_id,
        owner_agent_id=recovery.owner_agent_id,
        invocation_generation=recovery.invocation_generation,
        plan_revision=recovery.plan_revision,
        last_probe_at=recovery.last_probe_at,
        next_probe_at=recovery.next_probe_at,
        probe_count=recovery.probe_count,
        evidence=_safe_object_evidence(recovery.evidence),
    )


def project_lane(key: str, lane: LaneState) -> LaneReadModel:
    return LaneReadModel(
        key=key,
        repository=lane.repository,
        parent_branch=lane.parent_branch,
        phase=lane.phase.value,
        child_id=lane.child_id,
        lane_epoch=lane.lane_epoch,
        expected_base_sha=lane.expected_base_sha,
        head_sha=lane.head_sha,
        merge_journal_id=lane.merge_journal_id,
        push_intent_id=lane.push_intent_id,
        push_journal_id=lane.push_journal_id,
        changelog_commit=lane.changelog_commit,
        last_push_receipt_id=lane.last_push_receipt_id,
        last_remote_head=lane.last_remote_head,
        last_ancestry_proof=lane.last_ancestry_proof,
        next_transition=_lane_next_transition(lane.phase),
    )


def project_slice_integration(state: SliceState) -> SliceIntegrationReadModel:
    post_merge = state.post_merge
    evidence = post_merge.evidence if post_merge is not None else {}
    review = state.review_evidence
    review_document: dict[str, object] = {}
    if review is not None:
        review_document = {
            "review_id": review.review_id,
            "pr_number": review.pr_number,
            "head_sha": review.head_sha,
            "reviewer_agent_id": review.reviewer_agent_id,
            "verdict": review.verdict.value,
            "submitted_at": review.submitted_at,
            "validated_at": review.validated_at,
            "reviewer_account_authenticated": review.reviewer_account_authenticated,
            "dismissed": review.dismissed,
            "forgejo_stale": review.forgejo_stale,
            "reviewer_identity_unresolved": review.reviewer_identity_unresolved,
        }
    return SliceIntegrationReadModel(
        lifecycle=state.status.value,
        pr_number=state.pr_number,
        head_sha=(
            evidence.get("head_sha") or state.publication.head_sha
            if state.publication is not None
            else evidence.get("head_sha") or state.reviewed_head
        ),
        reviewed_head=state.reviewed_head,
        base_sha=evidence.get("expected_base_sha"),
        tree_sha=evidence.get("merge_tree_sha"),
        ci_status=state.ci_state.get(state.reviewed_head) if state.reviewed_head else None,
        merge_receipt=evidence.get("merge_journal_id"),
        bookkeeping_commit=evidence.get("bookkeeping_commit") or evidence.get("pushed_commit"),
        freshness=_freshness(state),
        publication_ownership=_publication_ownership(state),
        review_evidence=MappingProxyType(review_document),
        next_transition=slice_next_transition(state),
    )


def project_scope(state: RunState) -> ScopeReadModel:
    manifest = state.plan_manifest
    fsm = state.recursive_fsm
    scope_path = tuple(getattr(fsm, "scope_path", ()) or ())
    if not scope_path:
        scope_path = (manifest.scope_id,) if manifest is not None else (state.run_id,)
    scope_id = scope_path[-1]
    parent_scope_id = (
        scope_path[-2]
        if len(scope_path) > 1
        else (manifest.parent_scope_id if manifest is not None else None)
    )
    role = getattr(getattr(fsm, "role", None), "value", None)
    role = role or (
        manifest.role if manifest is not None else ("non_root" if state.parent_run_id else "root")
    )
    plan_digest = getattr(fsm, "plan_digest", None) or (
        manifest.digest if manifest is not None else None
    )
    phase = _scope_phase(fsm, state)
    current_order: int | None = None
    waiting: tuple[str, ...] = tuple(state.fsm.waiting)
    parallel: tuple[str, ...] = ()
    pending: dict[str, tuple[str, ...]] = {}
    completed: tuple[str, ...] = ()
    records: dict[str, Mapping[str, object]] = {}
    dispatch_intents: dict[str, str] = {}
    lane_bindings: dict[str, str] = {}
    scope_evidence: dict[str, str] = {}
    if isinstance(fsm, TLPlanning):
        parallel = _child_ids(fsm.parallel_children)
        pending = {str(order): _child_ids(children) for order, children in fsm.ordered_children}
        current_order = 0 if parallel else next(iter(pending), None)
        waiting = parallel + tuple(child for ids in pending.values() for child in ids)
        records = _child_documents(
            fsm.parallel_children
            + tuple(child for _, children in fsm.ordered_children for child in children)
        )
    elif isinstance(fsm, TLRunning):
        current_order = fsm.current_order
        parallel = _child_ids(fsm.parallel_pending)
        pending = {
            str(order): _child_ids(children)
            for order, children in sorted(fsm.pending_by_order.items())
        }
        completed = tuple(sorted(fsm.completed_children))
        waiting = parallel + tuple(child for ids in pending.values() for child in ids)
        all_records = (
            fsm.parallel_pending
            + tuple(child for _, children in fsm.pending_by_order.items() for child in children)
            + tuple(fsm.completed_children.values())
        )
        records = _child_documents(all_records)
        dispatch_intents = dict(fsm.dispatch_intents)
        lane_bindings = dict(fsm.lane_bindings)
        scope_evidence = dict(_safe_evidence(fsm.evidence))
    elif isinstance(fsm, TLAllMerged):
        completed = tuple(sorted(fsm.completed_children))
        waiting = ()
    elif isinstance(fsm, (TLDone, TLPRFiled, TLFailed, TLParked)):
        waiting = ()
    active = parallel + pending.get(str(current_order), ()) if current_order is not None else ()
    child_digests = dict(manifest.child_manifest_digests) if manifest is not None else {}
    return ScopeReadModel(
        scope_id=scope_id,
        parent_scope_id=parent_scope_id,
        scope_path=scope_path,
        role=role,
        owned_branch=(manifest.owned_branch if manifest is not None else state.owner_branch),
        parent_integration_target=(
            manifest.parent_integration_target if manifest is not None else state.parent_branch
        ),
        plan_digest=plan_digest,
        manifest_revision=manifest.manifest_revision if manifest is not None else None,
        phase=phase,
        current_order=current_order,
        active_barrier=tuple(active),
        waiting=tuple(waiting),
        parallel_pending=parallel,
        pending_by_order=MappingProxyType(pending),
        completed_children=completed,
        child_records=MappingProxyType(records),
        dispatch_intents=MappingProxyType(dispatch_intents),
        lane_bindings=MappingProxyType(lane_bindings),
        evidence=MappingProxyType(scope_evidence),
        child_manifest_digests=MappingProxyType(child_digests),
        next_transition=scope_next_transition(state, phase, waiting),
    )


def project_replay(
    state: RunState,
    consumed_event_count: int,
    last_event_seq: int | None,
    sequence_status: str | None,
) -> ReplayReadModel:
    return ReplayReadModel(
        cursor=state.events.last_consumed_offset,
        sequence_status=sequence_status,
        consumed_event_count=consumed_event_count,
        last_event_seq=last_event_seq,
        state_version=state.state_version,
        reducer_version=state.reducer_version,
    )


def slice_status_classification(state: SliceState, *, observed: bool = False) -> str:
    if state.status is SliceStatus.MERGED:
        return "authoritative"
    if state.status is SliceStatus.FAILED:
        return "failed"
    if state.status is SliceStatus.PARKED:
        return "parked"
    if state.action is not None and state.action.phase is ActionPhase.UNKNOWN:
        return "ambiguous"
    if state.review_evidence is not None and (
        state.review_evidence.dismissed
        or state.review_evidence.forgejo_stale
        or state.review_evidence.reviewer_identity_unresolved
        or not state.review_evidence.reviewer_account_authenticated
        or state.review_evidence.head_sha != state.reviewed_head
    ):
        return "stale"
    if state.recovery is not None:
        return "stale" if state.recovery.phase.value == "revalidating" else "unknown"
    if state.review_validation_required:
        return "stale"
    if state.observation_provenance is not None:
        return "observed"
    if observed:
        return "observed"
    return "unknown"


def slice_blocking_state(state: SliceState) -> str | None:
    if state.blocked_by:
        return f"dependency:{state.blocked_by}"
    if state.park_cause is not None:
        return f"parked:{state.park_cause.value}"
    if state.action is not None and state.action.phase is ActionPhase.UNKNOWN:
        return f"unknown_action:{state.action.intent_id or 'unidentified'}"
    if state.recovery is not None:
        return f"recovery:{state.recovery.next_action}"
    if state.review_validation_required:
        return "review_revalidation_required"
    if state.post_merge is not None and state.post_merge.phase is not PostMergePhase.COMPLETE:
        return f"post_merge:{state.post_merge.phase.value}"
    return None


def slice_waiting_reason(state: SliceState) -> str | None:
    if state.blocked_by:
        return f"awaiting dependency {state.blocked_by}"
    if state.action is not None and state.action.phase in {
        ActionPhase.INTENDED,
        ActionPhase.IN_FLIGHT,
    }:
        return f"awaiting {state.action.kind.value} confirmation"
    if state.post_merge is not None:
        return _POST_MERGE_NEXT[state.post_merge.phase]
    if state.recovery is not None:
        return state.recovery.next_action
    if state.review_validation_required:
        return "revalidate exact-head review"
    return None


def slice_next_transition(state: SliceState) -> str:
    if state.blocked_by:
        return f"await_dependency:{state.blocked_by}"
    if state.status is SliceStatus.PARKED:
        return "operator_recovery"
    if state.action is not None:
        if state.action.phase is ActionPhase.UNKNOWN:
            return f"reconcile_action:{state.action.intent_id or 'unknown'}"
        if state.action.phase in {ActionPhase.INTENDED, ActionPhase.IN_FLIGHT}:
            return f"confirm_effect:{state.action.kind.value}"
    if state.recovery is not None:
        return state.recovery.next_action
    if state.post_merge is not None:
        return _POST_MERGE_NEXT[state.post_merge.phase]
    if state.status is SliceStatus.IN_REVIEW:
        return "await_review_or_ci"
    if state.status in {SliceStatus.PENDING, SliceStatus.READY, SliceStatus.SPAWNED}:
        return "dispatch_child"
    if state.status is SliceStatus.MERGED:
        return "adopt_merged_slice"
    return "await_controller"


def scope_next_transition(state: RunState, phase: str, waiting: tuple[str, ...]) -> str:
    if any(gate.status.value == "pending" for gate in state.gates):
        gate = next(gate for gate in state.gates if gate.status.value == "pending")
        return f"answer_gate:{gate.name}"
    if phase in {"tl_failed", "tl_parked"}:
        return "operator_recovery"
    if waiting:
        return f"await_child:{waiting[0]}"
    if phase == "tl_finalizing":
        return "finalize_scope"
    if phase in {"tl_all_merged", "tl_allmerged"}:
        return "finalize_scope"
    if phase in {"tl_done", "tl_pr_filed"}:
        return "terminal"
    return "advance_scope"


def _scope_phase(fsm: object | None, state: RunState) -> str:
    if fsm is None:
        return state.fsm.phase.value
    names = {
        TLPlanning: "tl_planning",
        TLRunning: "tl_running",
        TLAllMerged: "tl_all_merged",
        TLFinalizing: "tl_finalizing",
        TLDone: "tl_done",
        TLPRFiled: "tl_pr_filed",
        TLFailed: "tl_failed",
        TLParked: "tl_parked",
    }
    return names.get(type(fsm), getattr(fsm, "phase", state.fsm.phase).value)


def _child_ids(records: tuple[ChildRecord, ...]) -> tuple[str, ...]:
    return tuple(record.child_id for record in records)


def _child_documents(records: tuple[ChildRecord, ...]) -> dict[str, Mapping[str, object]]:
    return {
        record.child_id: MappingProxyType(
            {
                "child_id": record.child_id,
                "kind": record.kind.value,
                "dispatch_intent_id": record.dispatch_intent_id,
                "invocation_id": record.invocation_id,
                "lane_id": record.lane_id,
                "manifest_node_id": record.manifest_node_id,
                "manifest_revision": record.manifest_revision,
                "evidence": dict(_safe_evidence(record.evidence)),
            }
        )
        for record in records
    }


def _lane_next_transition(phase: LanePhase) -> str:
    return {
        LanePhase.IDLE: "reserve_lane",
        LanePhase.RESERVED: "start_integration",
        LanePhase.INTEGRATING: "adopt_merge_or_start_post_merge",
        LanePhase.BOOKKEEPING: "confirm_parent_push",
        LanePhase.RECOVERY: "reconcile_or_gate_lane",
        LanePhase.PARKED: "operator_reconcile_or_abandon",
    }[phase]


def _freshness(state: SliceState) -> str:
    if state.review_validation_required:
        return "stale"
    if state.reviewed_head is None:
        return "missing"
    if state.review_evidence is not None and state.review_evidence.validated_at:
        return "validated"
    return "observed"


def _publication_ownership(state: SliceState) -> str:
    publication = state.publication
    if publication is None:
        return "unknown"
    return "bound"


def _integration_next_transition(integration: object) -> str:
    lifecycle = getattr(getattr(integration, "lifecycle", None), "value", "")
    if lifecycle == "INTEGRATION_CONFLICT":
        return "resume_aggregate_pr"
    if lifecycle == "NEEDS_BASE_REVALIDATION":
        return "revalidate_base_and_ci"
    if lifecycle == "READY_FOR_INTEGRATION":
        return "validate_merge_evidence"
    if lifecycle == "MERGED":
        return "complete_ordered_stage"
    for lane in getattr(integration, "lanes", {}).values():
        if lane.phase is LanePhase.RECOVERY:
            return "reconcile_lane"
        if lane.phase is LanePhase.BOOKKEEPING:
            return "confirm_parent_push"
    return "await_controller"


def _integration_merge_receipt(integration: object) -> str | None:
    for lane in getattr(integration, "lanes", {}).values():
        if lane.merge_journal_id:
            return lane.merge_journal_id
    return None


def _integration_bookkeeping_commit(integration: object) -> str | None:
    for lane in getattr(integration, "lanes", {}).values():
        if lane.changelog_commit:
            return lane.changelog_commit
    return None


def _integration_freshness(integration: object) -> str:
    if getattr(integration, "stage_verification", "") == "passed":
        return "verified"
    if getattr(integration, "validated_base_sha", None):
        return "validated"
    return "unknown"


def _safe_binding(value: object | None) -> Mapping[str, object] | None:
    if value is None:
        return None
    fields = (
        "pr_number",
        "head_sha",
        "head_branch",
        "base_branch",
        "attempt",
        "invocation_id",
        "agent_id",
        "observed_at",
        "source",
        "event_seq",
        "snapshot_id",
        "ledger_run_seq",
        "snapshot_high_watermark",
        "source_epoch",
        "source_revision",
        "coverage",
    )
    return MappingProxyType(
        {
            name: list(getattr(value, name)) if name == "coverage" else getattr(value, name)
            for name in fields
            if hasattr(value, name)
        }
    )


def _safe_evidence(evidence: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(
        {key: value for key, value in evidence.items() if _safe_key(key) and isinstance(value, str)}
    )


def _safe_object_evidence(evidence: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {
            key: value
            for key, value in evidence.items()
            if _safe_key(key) and isinstance(value, (str, int, float, bool))
        }
    )


def _safe_key(key: str) -> bool:
    lowered = key.lower()
    return any(
        marker in lowered
        for marker in (
            "id",
            "sha",
            "branch",
            "base",
            "commit",
            "receipt",
            "epoch",
            "generation",
            "proof",
            "pr_number",
            "head",
        )
    )


__all__ = [
    "ActionReadModel",
    "LaneReadModel",
    "PostMergeReadModel",
    "RecoveryReadModel",
    "ReplayReadModel",
    "ScopeReadModel",
    "SliceIntegrationReadModel",
    "project_action",
    "project_lane",
    "project_post_merge",
    "project_recovery",
    "project_replay",
    "project_scope",
    "project_slice_integration",
    "slice_blocking_state",
    "slice_next_transition",
    "slice_status_classification",
    "slice_waiting_reason",
]
