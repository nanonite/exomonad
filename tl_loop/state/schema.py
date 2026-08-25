"""Closed, versioned schema for durable TL run state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, TypeAlias, cast

from tl_loop.fsm.phase import TLPhase
from tl_loop.fsm.recovery import RecoveryPhase, RecoveryState
from tl_loop.ordered import (
    CI_STATUSES,
    ChildRecoverySummary,
    IntegrationLifecycle,
    SubTLLifecycle,
)

BLOCK_CAUSE_VALUES = frozenset(
    {
        "base_ci_unstable",
        "external_dependency",
        "scope_boundary",
        "human_decision_required",
        "tooling_unavailable",
    }
)

SCHEMA_VERSION = 2


class SliceStatus(str, Enum):
    """Lifecycle status for one implementation slice."""

    PENDING = "pending"
    READY = "ready"
    DISPATCHING = "dispatching"
    DISPATCH_UNCONFIRMED = "dispatch_unconfirmed"
    DISPATCH_FAILED = "dispatch_failed"
    SPAWNED = "spawned"
    IN_REVIEW = "in_review"
    REPAIRING = "repairing"
    MERGED = "merged"
    FAILED = "failed"
    PARKED = "parked"
    BLOCKED = "blocked"


class WaitReason(str, Enum):
    """Why a nonterminal slice is temporarily withheld from scheduling."""

    DEPENDENCY_RECOVERY = "dependency_recovery"


@dataclass(frozen=True)
class SuspendedDependencyState:
    """Durable suspension metadata for a dependency in recovery."""

    blocked_by: str
    prior_status: SliceStatus
    recovery_generation: int

    def __post_init__(self) -> None:
        if not self.blocked_by.strip():
            raise ValueError("suspended dependency blocker must be non-empty")
        if type(self.prior_status) is not SliceStatus:
            raise ValueError("suspended dependency prior_status must be SliceStatus")
        if type(self.recovery_generation) is not int or self.recovery_generation < 0:
            raise ValueError("suspended dependency recovery_generation must be non-negative")


@dataclass(frozen=True)
class DeadlineLedger:
    """Deterministic execution, recovery, and run deadline telemetry."""

    execution_deadline_at: float | None
    recovery_deadline_at: float | None
    run_deadline_at: float | None
    suspended_at: float | None
    execution_seconds: float
    recovery_wait_seconds: float

    def __post_init__(self) -> None:
        for name in (
            "execution_deadline_at",
            "recovery_deadline_at",
            "run_deadline_at",
            "suspended_at",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"deadline {name} must be non-negative or null")
        for name in ("execution_seconds", "recovery_wait_seconds"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"deadline {name} must be non-negative")


class ParkCause(str, Enum):
    """Closed reasons that can park a slice for human action."""

    RETRIES_EXHAUSTED = "retries_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_CAPABLE_HARNESS = "no_capable_harness"
    SCHEDULE_DEADLOCK = "schedule_deadlock"
    REVIEW_STUCK = "review_stuck"
    REVIEW_ROUNDS_EXHAUSTED = "review_rounds_exhausted"
    HARNESS_SWITCH_REQUESTED = "harness_switch_requested"
    STALL_DETECTED = "stall_detected"
    WORKER_TERMINAL = "worker_terminal"
    MISSING_HANDOFF = "missing_handoff"
    TASK_BUDGET_EXCEEDED = "task_budget_exceeded"
    ATTEMPT_ABANDONED = "attempt_abandoned"
    PR_CLOSED_UNMERGED = "pr_closed_unmerged"
    PR_HEAD_UNREACHABLE = "pr_head_unreachable"
    PUBLICATION_OWNERSHIP_UNRESOLVED = "publication_ownership_unresolved"
    DISPATCH_UNCONFIRMED = "dispatch_unconfirmed"
    DISPATCH_FAILED = "dispatch_failed"
    CORRUPT_STATE = "corrupt_state"
    DURABLE_WRITE_FAILED = "durable_write_failed"
    TOOL_UNAVAILABLE = "tool_unavailable"
    BASE_CI_UNSTABLE = "base_ci_unstable"
    EXTERNAL_DEPENDENCY = "external_dependency"
    SCOPE_BOUNDARY = "scope_boundary"
    HUMAN_DECISION_REQUIRED = "human_decision_required"


class Verdict(str, Enum):
    """Closed review verdict set."""

    GO = "GO"
    GO_WITH_NITS = "GO-WITH-NITS"
    NO_GO = "NO-GO"


class GateStatus(str, Enum):
    """Closed human-approval result set."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ActionKind(str, Enum):
    """The controller effect represented by orthogonal action state."""

    DISPATCH = "dispatch"
    PUBLISH = "publish"
    REVIEWER_SPAWN = "reviewer_spawn"
    REPAIR = "repair"
    MERGE = "merge"


class ActionPhase(str, Enum):
    """Durable before/after phase for one controller action."""

    INTENDED = "intended"
    IN_FLIGHT = "in_flight"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    RECONCILED = "reconciled"


@dataclass(frozen=True)
class RepositoryIdentity:
    """Stable Forgejo repository identity for persisted observations."""

    owner: str
    repo: str
    base_branch: str
    forge_host: str | None = None
    remote_url: str | None = None

    def __post_init__(self) -> None:
        for name in ("owner", "repo", "base_branch"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"repository identity {name} must be non-empty")
        for name in ("forge_host", "remote_url"):
            value = getattr(self, name)
            if value is not None and not value:
                raise ValueError(f"repository identity {name} must be non-empty or null")


@dataclass(frozen=True)
class PublicationBinding:
    """A PR publication bound to one exact attempt and head."""

    pr_number: int
    head_sha: str
    head_branch: str
    base_branch: str
    attempt: int
    invocation_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.pr_number) is not int or self.pr_number <= 0:
            raise ValueError("publication pr_number must be positive")
        if type(self.attempt) is not int or self.attempt <= 0:
            raise ValueError("publication attempt must be positive")
        for name in ("head_sha", "head_branch", "base_branch"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"publication {name} must be non-empty")
        if self.invocation_id is not None and not self.invocation_id:
            raise ValueError("publication invocation_id must be non-empty or null")


@dataclass(frozen=True)
class HandoffEvidence:
    """Authoritative implementation completion evidence for one exact head."""

    pr_number: int
    head_sha: str
    attempt: int
    invocation_id: str
    agent_id: str
    observed_at: str

    def __post_init__(self) -> None:
        if type(self.pr_number) is not int or self.pr_number <= 0:
            raise ValueError("handoff pr_number must be positive")
        if type(self.attempt) is not int or self.attempt <= 0:
            raise ValueError("handoff attempt must be positive")
        for name in ("head_sha", "invocation_id", "agent_id", "observed_at"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"handoff {name} must be non-empty")


@dataclass(frozen=True)
class ObservationProvenance:
    """Source and cursor for the latest persisted repository observation."""

    source: str
    observed_at: str
    event_seq: int | None = None
    snapshot_id: str | None = None
    ledger_run_seq: int | None = None
    snapshot_high_watermark: int | None = None
    source_epoch: int = 0
    source_revision: int = 0
    coverage: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("source", "observed_at"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"observation provenance {name} must be non-empty")
        for name in ("event_seq", "ledger_run_seq", "snapshot_high_watermark"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"observation provenance {name} must be non-negative or null")
        for name in ("source_epoch", "source_revision"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"observation provenance {name} must be non-negative")
        if self.snapshot_id is not None and not self.snapshot_id:
            raise ValueError("observation provenance snapshot_id must be non-empty or null")
        if any(not isinstance(item, str) or not item for item in self.coverage):
            raise ValueError("observation provenance coverage must contain non-empty strings")
        if len(set(self.coverage)) != len(self.coverage):
            raise ValueError("observation provenance coverage must be unique")


@dataclass(frozen=True)
class ActionState:
    """Action identity and phase, kept separate from slice lifecycle."""

    kind: ActionKind
    phase: ActionPhase
    state_version: int = 1
    intent_id: str | None = None
    head_sha: str | None = None
    attempt: int | None = None
    contract_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActionKind):
            raise TypeError("action kind must be an ActionKind")
        if not isinstance(self.phase, ActionPhase):
            raise TypeError("action phase must be an ActionPhase")
        if type(self.state_version) is not int or self.state_version < 1:
            raise ValueError("action state_version must be a positive integer")
        for name in ("intent_id", "head_sha", "contract_digest"):
            value = getattr(self, name)
            if value is not None and not value:
                raise ValueError(f"action {name} must be non-empty or null")
        if self.attempt is not None and (type(self.attempt) is not int or self.attempt <= 0):
            raise ValueError("action attempt must be positive or null")


REVIEW_FINDING_KEYS = frozenset({"severity", "path", "rationale"})
REVIEW_CONTRACT_KEYS = frozenset({"acceptance_criteria", "digest"})
CI_STATUS_VALUES = frozenset({"unknown", "pending", "success", "failure", "neutral"})
STALL_CLASSIFICATION_VALUES = frozenset(
    {
        "dev_not_pushing",
        "reviewer_not_responding",
        "reviewer_never_started",
        "ci_failed",
        "ci_base_unstable",
        "ci_indeterminate",
        "review_stuck",
    }
)


TERMINAL_SLICE_STATUSES = frozenset(
    {
        SliceStatus.MERGED.value,
        SliceStatus.FAILED.value,
        SliceStatus.DISPATCH_FAILED.value,
        SliceStatus.PARKED.value,
        SliceStatus.BLOCKED.value,
    }
)
RUN_KEYS = frozenset(
    {
        "version",
        "revision",
        "run_id",
        "ledger_run_id",
        "fsm",
        "slices",
        "budgets",
        "gates",
        "events",
        "owner_branch",
        "owner_worktree",
        "parent_branch",
        "parent_run_id",
        "parent_agent_id",
        "depth",
        "goals",
        "current_order",
        "ordered_stages",
        "integration",
        "repository_identity",
        "state_version",
        "controller_epoch",
        "reviewer_max_rounds",
        "reviewer_max_rounds_source",
    }
)
ORDERED_STAGE_KEYS = frozenset({"order", "sub_tls"})
INTEGRATION_KEYS = frozenset(
    {
        "lifecycle",
        "sub_tl_states",
        "sub_tl_recovery",
        "candidates",
        "aggregate_pr_number",
        "aggregate_head_sha",
        "aggregate_patch_digest",
        "aggregate_original_base_sha",
        "integration_owner_id",
        "integration_owner_run_id",
        "integration_owner_branch",
        "integration_owner_worktree",
        "head_sha",
        "patch_digest",
        "validated_base_sha",
        "merge_tree_sha",
        "integration_evidence_at",
        "ci_status",
        "merge_attempts",
        "base_revalidation_count",
        "stage_verification",
    }
)
CHILD_RECOVERY_KEYS = frozenset(
    {
        "owner_run_id",
        "child_path",
        "slice_id",
        "cause",
        "recovery_round",
        "next_probe_at",
    }
)
INTEGRATION_CANDIDATE_KEYS = frozenset(
    {
        "lifecycle",
        "aggregate_pr_number",
        "aggregate_head_sha",
        "aggregate_patch_digest",
        "aggregate_original_base_sha",
        "integration_owner_id",
        "integration_owner_run_id",
        "integration_owner_branch",
        "integration_owner_worktree",
        "head_sha",
        "patch_digest",
        "validated_base_sha",
        "merge_tree_sha",
        "integration_evidence_at",
        "ci_status",
        "merge_attempts",
        "base_revalidation_count",
        "stage_verification",
    }
)
INTEGRATION_VERIFICATION_VALUES = frozenset({"pending", "passed", "failed"})
FSM_KEYS = frozenset({"phase", "waiting"})
SLICE_KEYS = frozenset(
    {
        "id",
        "status",
        "paths",
        "depends_on",
        "base_ref",
        "test_plan",
        "agent_type",
        "model",
        "branch",
        "worktree",
        "pr_number",
        "review_findings",
        "review_patch_digests",
        "review_contract",
        "ci_state",
        "reviewer_attempt",
        "reviewer_agent_id",
        "repair_attempts",
        "review_rounds",
        "reviewed_head",
        "verdict_at",
        "attempts",
        "verdict",
        "park_cause",
        "park_issue_id",
        "park_audit",
        "blocked_by",
        "stall_classification",
        "dispatch_intent_id",
        "dispatch_started_at",
        "dispatch_last_boundary",
        "dispatch_error",
        "dispatch_agent_id",
        "dispatch_invocation_id",
        "dispatch_authoritative_event_seq",
        "dispatch_generation",
        "reconciliation",
        "task_timeout_seconds",
        "task_timeout_source",
        "recovery",
        "suspended_dependency",
        "deadline_ledger",
        "publication",
        "handoff",
        "observation_provenance",
        "action",
    }
)
RECONCILIATION_KEYS = frozenset(
    {
        "confirmed_stage",
        "authoritative_evidence",
        "missing_evidence",
        "conflicts",
        "next_action",
    }
)
PARK_AUDIT_KEYS = frozenset(
    {
        "attempts",
        "verdict",
        "harness",
        "model",
        "ledger",
        "from_harness",
        "to_harness",
        "reason",
        "effort",
        "agent_id",
        "recovered",
        "attempt",
        "gate_name",
        "recovery_action",
        "needs_human",
        "base_sha",
        "head_sha",
        "failed_checks",
        "attribution",
        "scope_attribution",
        "retryable",
        "declared_difficulty",
        "matched_difficulty_rule",
    }
)
RECOVERY_KEYS = frozenset(
    {
        "cause",
        "phase",
        "recovery_round",
        "next_action",
        "owner_run_id",
        "entered_at",
        "slice_attempt",
        "owner_agent_id",
        "invocation_generation",
        "plan_revision",
        "evidence",
        "last_probe_at",
        "next_probe_at",
        "probe_count",
    }
)
SUSPENDED_DEPENDENCY_KEYS = frozenset({"blocked_by", "prior_status", "recovery_generation"})
DEADLINE_LEDGER_KEYS = frozenset(
    {
        "execution_deadline_at",
        "recovery_deadline_at",
        "run_deadline_at",
        "suspended_at",
        "execution_seconds",
        "recovery_wait_seconds",
    }
)
REPOSITORY_IDENTITY_KEYS = frozenset({"owner", "repo", "base_branch", "forge_host", "remote_url"})
PUBLICATION_KEYS = frozenset(
    {"pr_number", "head_sha", "head_branch", "base_branch", "attempt", "invocation_id"}
)
HANDOFF_KEYS = frozenset(
    {"pr_number", "head_sha", "attempt", "invocation_id", "agent_id", "observed_at"}
)
OBSERVATION_PROVENANCE_KEYS = frozenset(
    {
        "source",
        "observed_at",
        "event_seq",
        "snapshot_id",
        "ledger_run_seq",
        "snapshot_high_watermark",
        "source_epoch",
        "source_revision",
        "coverage",
    }
)
ACTION_KEYS = frozenset(
    {"kind", "phase", "state_version", "intent_id", "head_sha", "attempt", "contract_digest"}
)
BUDGET_KEYS = frozenset({"ledger"})
LEDGER_KEYS = frozenset(
    {
        "tokens",
        "wall_seconds",
        "role_spent",
        "harness_spent",
        "role_reserved",
        "harness_reserved",
        "charges",
    }
)
CHARGE_KEYS = frozenset(
    {
        "slice_id",
        "attempt",
        "role",
        "harness",
        "estimated_tokens",
        "actual",
        "delta_tokens",
        "warning",
        "reconciled",
    }
)
GATE_KEYS = frozenset({"name", "status"})
EVENT_KEYS = frozenset({"last_consumed_offset"})
GOAL_KEYS = frozenset(
    {
        "objective",
        "deadline",
        "completion_predicate",
        "last_heartbeat_at",
        "last_progress_at",
        "controller_started_at",
        "last_authoritative_event_seq",
    }
)


@dataclass(frozen=True)
class SliceState:
    """Typed view of one closed-key slice record."""

    id: str
    status: SliceStatus
    paths: tuple[str, ...]
    depends_on: tuple[str, ...]
    base_ref: str | None
    test_plan: tuple[str, ...]
    agent_type: str | None
    model: str | None
    branch: str | None
    worktree: str | None
    pr_number: int | None
    reviewed_head: str | None
    attempts: int
    verdict: Verdict | None
    review_findings: Mapping[str, tuple[Mapping[str, str], ...]] = field(default_factory=dict)
    review_patch_digests: Mapping[str, str] = field(default_factory=dict)
    review_contract: Mapping[str, object] | None = None
    ci_state: Mapping[str, str] = field(default_factory=dict)
    reviewer_attempt: Mapping[str, int] = field(default_factory=dict)
    reviewer_agent_id: str | None = None
    repair_attempts: int = 0
    review_rounds: int = 0
    verdict_at: str | None = None
    park_cause: ParkCause | None = None
    park_issue_id: int | None = None
    park_audit: Mapping[str, object] | None = None
    blocked_by: str | None = None
    stall_classification: str | None = None
    dispatch_intent_id: str | None = None
    dispatch_started_at: float | None = None
    dispatch_last_boundary: str | None = None
    dispatch_error: str | None = None
    dispatch_agent_id: str | None = None
    dispatch_invocation_id: str | None = None
    dispatch_authoritative_event_seq: int | None = None
    dispatch_generation: int = 0
    reconciliation: Mapping[str, object] | None = None
    task_timeout_seconds: float | None = None
    task_timeout_source: str | None = None
    recovery: RecoveryState | None = None
    suspended_dependency: SuspendedDependencyState | None = None
    deadline_ledger: DeadlineLedger | None = None
    publication: PublicationBinding | None = None
    handoff: HandoffEvidence | None = None
    observation_provenance: ObservationProvenance | None = None
    action: ActionState | None = None


@dataclass(frozen=True)
class FSMState:
    """Persisted phase and outstanding slice IDs."""

    phase: TLPhase
    waiting: tuple[str, ...]


ActualTokens: TypeAlias = int | Literal["unknown"]


@dataclass(frozen=True)
class BudgetCharge:
    """One immutable slice-attempt budget record and its reconciliation."""

    slice_id: str
    attempt: int
    role: str
    harness: str
    estimated_tokens: int
    actual: ActualTokens
    delta_tokens: int | None
    warning: bool
    reconciled: bool


@dataclass(frozen=True)
class BudgetLedger:
    """Monotonic resource counters charged by the controller."""

    tokens: int
    wall_seconds: int
    role_spent: Mapping[str, int] = field(default_factory=dict)
    harness_spent: Mapping[str, int] = field(default_factory=dict)
    role_reserved: Mapping[str, int] = field(default_factory=dict)
    harness_reserved: Mapping[str, int] = field(default_factory=dict)
    charges: tuple[BudgetCharge, ...] = ()


@dataclass(frozen=True)
class GateState:
    """One named human-approval result."""

    name: str
    status: GateStatus


@dataclass(frozen=True)
class EventCursor:
    """Last consumed global event-log offset."""

    last_consumed_offset: int


@dataclass(frozen=True)
class GoalState:
    """Durable objective and liveness timestamps for one long-running wave."""

    objective: str = ""
    deadline: float = 0.0
    completion_predicate: str = ""
    last_heartbeat_at: float | None = None
    last_progress_at: float | None = None
    controller_started_at: float | None = None
    last_authoritative_event_seq: int | None = None


@dataclass(frozen=True)
class OrderedStageState:
    """Persisted normalized stage order and its direct child IDs."""

    order: int
    sub_tls: tuple[str, ...]


@dataclass(frozen=True)
class IntegrationCandidateState:
    """Persisted lifecycle and evidence for one aggregate candidate."""

    lifecycle: IntegrationLifecycle = IntegrationLifecycle.RUNNING
    aggregate_pr_number: int | None = None
    aggregate_head_sha: str | None = None
    aggregate_patch_digest: str | None = None
    aggregate_original_base_sha: str | None = None
    integration_owner_id: str | None = None
    integration_owner_run_id: str | None = None
    integration_owner_branch: str | None = None
    integration_owner_worktree: str | None = None
    head_sha: str | None = None
    patch_digest: str | None = None
    validated_base_sha: str | None = None
    merge_tree_sha: str | None = None
    integration_evidence_at: str | None = None
    ci_status: str = "unknown"
    merge_attempts: int = 0
    base_revalidation_count: int = 0
    stage_verification: str = "pending"


@dataclass(frozen=True)
class IntegrationRuntimeState:
    """Persisted evidence and lifecycle for one parent stage fold."""

    lifecycle: IntegrationLifecycle = IntegrationLifecycle.RUNNING
    sub_tl_states: Mapping[str, IntegrationLifecycle | SubTLLifecycle] = field(default_factory=dict)
    sub_tl_recovery: Mapping[str, ChildRecoverySummary] = field(default_factory=dict)
    aggregate_pr_number: int | None = None
    aggregate_head_sha: str | None = None
    aggregate_patch_digest: str | None = None
    aggregate_original_base_sha: str | None = None
    integration_owner_id: str | None = None
    integration_owner_run_id: str | None = None
    integration_owner_branch: str | None = None
    integration_owner_worktree: str | None = None
    head_sha: str | None = None
    patch_digest: str | None = None
    validated_base_sha: str | None = None
    merge_tree_sha: str | None = None
    integration_evidence_at: str | None = None
    ci_status: str = "unknown"
    merge_attempts: int = 0
    base_revalidation_count: int = 0
    stage_verification: str = "pending"
    candidates: Mapping[str, IntegrationCandidateState] = field(default_factory=dict)


SliceMap: TypeAlias = Mapping[str, SliceState]


@dataclass(frozen=True)
class RunState:
    """Typed view of a validated durable run-state document."""

    version: int
    revision: int
    run_id: str
    fsm: FSMState
    slices: SliceMap
    budgets: BudgetLedger
    gates: tuple[GateState, ...]
    events: EventCursor
    ledger_run_id: str | None = None
    owner_branch: str | None = None
    owner_worktree: str | None = None
    parent_branch: str | None = None
    parent_run_id: str | None = None
    parent_agent_id: str | None = None
    depth: int = 0
    goals: GoalState = field(default_factory=GoalState)
    current_order: int = 1
    ordered_stages: tuple[OrderedStageState, ...] = ()
    integration: IntegrationRuntimeState = field(default_factory=IntegrationRuntimeState)
    repository_identity: RepositoryIdentity | None = None
    state_version: int = 0
    controller_epoch: str | None = None
    reviewer_max_rounds: int | None = None
    reviewer_max_rounds_source: str | None = None


class SchemaError(ValueError):
    """Raised when a run-state document violates its closed schema."""

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        self.errors = tuple(errors)
        message = "; ".join(f"{path}: {reason}" for path, reason in errors)
        super().__init__(message)


def validate(doc: object) -> None:
    """Validate a complete run-state document, rejecting every unknown key."""
    errors: list[tuple[str, str]] = []
    root = _object(doc, "run", RUN_KEYS, errors)
    if root is None:
        raise SchemaError(errors)

    _version(root, errors)
    _non_negative_int(root, "revision", "run", errors)
    _non_empty_string(root, "run_id", "run", errors)
    for key in (
        "ledger_run_id",
        "owner_branch",
        "owner_worktree",
        "parent_branch",
        "parent_run_id",
        "parent_agent_id",
    ):
        _nullable_string(root, key, "run", errors)
    if "depth" in root:
        _non_negative_int(root, "depth", "run", errors)
    _nullable_non_negative_int(root, "state_version", "run", errors)
    _nullable_string(root, "controller_epoch", "run", errors)
    _nullable_positive_int(root, "reviewer_max_rounds", "run", errors)
    _nullable_string(root, "reviewer_max_rounds_source", "run", errors)
    _validate_repository_identity(root.get("repository_identity"), "run", errors)

    fsm = _object(root.get("fsm"), "run.fsm", FSM_KEYS, errors)
    if fsm is not None:
        _enum_value(fsm, "phase", "run.fsm", TLPhase, errors)
        _string_list(fsm, "waiting", "run.fsm", errors, allow_empty=True)
        _unique_strings(fsm.get("waiting"), "run.fsm.waiting", errors)

    slices = _slice_map(root.get("slices"), errors)
    _goals(root.get("goals"), errors)
    _ordered_state(root, errors)
    _budgets(root.get("budgets"), errors)
    _gates(root.get("gates"), errors)
    _events(root.get("events"), errors)

    if fsm is not None and slices is not None:
        _validate_waiting(fsm.get("waiting"), slices, errors)
    if slices is not None:
        _validate_dependencies(slices, errors)
        _validate_path_ownership(slices, errors)

    if errors:
        raise SchemaError(errors)


def _version(root: dict[str, object], errors: list[tuple[str, str]]) -> None:
    value = root.get("version")
    if type(value) is not int:
        errors.append(("run.version", "must be an integer"))
    elif value not in {SCHEMA_VERSION}:
        errors.append(("run.version", f"unrecognised version {value}"))


def _ordered_state(root: dict[str, object], errors: list[tuple[str, str]]) -> None:
    if "current_order" in root:
        _positive_int(root, "current_order", "run", errors)
    stages = root.get("ordered_stages")
    if stages is not None:
        if not isinstance(stages, list):
            errors.append(("run.ordered_stages", "must be an array"))
        else:
            expected = 1
            seen: set[str] = set()
            for index, raw_stage in enumerate(stages):
                path = f"run.ordered_stages[{index}]"
                stage = _object(raw_stage, path, ORDERED_STAGE_KEYS, errors)
                if stage is None:
                    continue
                _positive_int(stage, "order", path, errors)
                if stage.get("order") != expected:
                    errors.append((f"{path}.order", "must be contiguous and sorted from 1"))
                expected += 1
                _string_list(stage, "sub_tls", path, errors, allow_empty=False)
                for name in stage.get("sub_tls", []):
                    if isinstance(name, str):
                        if name in seen:
                            errors.append(
                                (f"{path}.sub_tls", "sub-TL occurs in more than one stage")
                            )
                        seen.add(name)
    if "integration" not in root:
        return
    integration = _object(root.get("integration"), "run.integration", INTEGRATION_KEYS, errors)
    if integration is None:
        return
    _enum_value(integration, "lifecycle", "run.integration", IntegrationLifecycle, errors)
    states = integration.get("sub_tl_states")
    if not isinstance(states, dict):
        errors.append(("run.integration.sub_tl_states", "must be an object"))
    else:
        for name, lifecycle in states.items():
            if not isinstance(name, str) or not name:
                errors.append(("run.integration.sub_tl_states", "keys must be non-empty strings"))
            if not isinstance(lifecycle, str) or lifecycle not in {
                item.value for item in IntegrationLifecycle
            } | {item.value for item in SubTLLifecycle}:
                errors.append(
                    (f"run.integration.sub_tl_states[{name!r}]", "is not a lifecycle state")
                )
    recoveries_value = integration.get("sub_tl_recovery")
    if recoveries_value is not None and not isinstance(recoveries_value, dict):
        errors.append(("run.integration.sub_tl_recovery", "must be an object"))
    recoveries = recoveries_value if isinstance(recoveries_value, dict) else None
    if recoveries is not None:
        for name, raw_recovery in recoveries.items():
            path = f"run.integration.sub_tl_recovery[{name!r}]"
            if not isinstance(name, str) or not name:
                errors.append(("run.integration.sub_tl_recovery", "keys must be non-empty strings"))
                continue
            recovery = _object(raw_recovery, path, CHILD_RECOVERY_KEYS, errors)
            if recovery is None:
                continue
            _non_empty_string(recovery, "owner_run_id", path, errors)
            _string_list(recovery, "child_path", path, errors, allow_empty=False)
            _non_empty_string(recovery, "slice_id", path, errors)
            if recovery.get("cause") not in BLOCK_CAUSE_VALUES:
                errors.append((f"{path}.cause", "is not a recognised block cause"))
            _non_negative_int(recovery, "recovery_round", path, errors)
            _nullable_number(recovery, "next_probe_at", path, errors)
    _nullable_positive_int(integration, "aggregate_pr_number", "run.integration", errors)
    for key in (
        "aggregate_head_sha",
        "aggregate_patch_digest",
        "aggregate_original_base_sha",
        "integration_owner_id",
        "integration_owner_run_id",
        "integration_owner_branch",
        "integration_owner_worktree",
        "head_sha",
        "patch_digest",
        "validated_base_sha",
        "merge_tree_sha",
        "integration_evidence_at",
    ):
        _nullable_string(integration, key, "run.integration", errors)
    if "ci_status" in integration and integration["ci_status"] not in CI_STATUSES:
        errors.append(("run.integration.ci_status", "is not a recognised CI status"))
    _non_negative_int(integration, "merge_attempts", "run.integration", errors)
    _non_negative_int(integration, "base_revalidation_count", "run.integration", errors)
    if integration.get("stage_verification") not in INTEGRATION_VERIFICATION_VALUES:
        errors.append(
            ("run.integration.stage_verification", "is not a recognised verification result")
        )
    _validate_integration_evidence_contract(integration, "run.integration", errors)
    candidates = integration.get("candidates")
    if candidates is not None:
        if not isinstance(candidates, dict):
            errors.append(("run.integration.candidates", "must be an object"))
        else:
            for candidate_id, raw_candidate in candidates.items():
                path = f"run.integration.candidates[{candidate_id!r}]"
                if not isinstance(candidate_id, str) or not candidate_id:
                    errors.append(("run.integration.candidates", "keys must be non-empty strings"))
                    continue
                candidate = _object(raw_candidate, path, INTEGRATION_CANDIDATE_KEYS, errors)
                if candidate is None:
                    continue
                _enum_value(candidate, "lifecycle", path, IntegrationLifecycle, errors)
                _nullable_positive_int(candidate, "aggregate_pr_number", path, errors)
                for key in (
                    "aggregate_head_sha",
                    "aggregate_patch_digest",
                    "aggregate_original_base_sha",
                    "integration_owner_id",
                    "integration_owner_run_id",
                    "integration_owner_branch",
                    "integration_owner_worktree",
                    "head_sha",
                    "patch_digest",
                    "validated_base_sha",
                    "merge_tree_sha",
                    "integration_evidence_at",
                ):
                    _nullable_string(candidate, key, path, errors)
                if "ci_status" in candidate and candidate["ci_status"] not in CI_STATUSES:
                    errors.append((f"{path}.ci_status", "is not a recognised CI status"))
                _non_negative_int(candidate, "merge_attempts", path, errors)
                _non_negative_int(candidate, "base_revalidation_count", path, errors)
                if candidate.get("stage_verification") not in INTEGRATION_VERIFICATION_VALUES:
                    errors.append(
                        (f"{path}.stage_verification", "is not a recognised verification result")
                    )
                _validate_integration_evidence_contract(candidate, path, errors)


def _validate_integration_evidence_contract(
    value: Mapping[str, object], path: str, errors: list[tuple[str, str]]
) -> None:
    """Reject terminal integration states without the evidence that proves them."""
    lifecycle = value.get("lifecycle")
    if lifecycle == IntegrationLifecycle.MERGED.value:
        required = (
            "aggregate_pr_number",
            "integration_owner_id",
            "integration_owner_run_id",
            "integration_owner_branch",
            "integration_owner_worktree",
            "head_sha",
            "patch_digest",
            "validated_base_sha",
            "merge_tree_sha",
            "integration_evidence_at",
        )
        for key in required:
            item = value.get(key)
            if item is None or item == "":
                errors.append((f"{path}.{key}", "is required when lifecycle is MERGED"))
        if value.get("ci_status") not in {"success", "neutral"}:
            errors.append((f"{path}.ci_status", "must be successful when lifecycle is MERGED"))
        if value.get("stage_verification") != "passed":
            errors.append((f"{path}.stage_verification", "must be passed when lifecycle is MERGED"))
    if lifecycle in {
        IntegrationLifecycle.INTEGRATION_VALIDATED.value,
        IntegrationLifecycle.MERGING.value,
    }:
        for key in ("head_sha", "patch_digest", "validated_base_sha", "merge_tree_sha"):
            if not value.get(key):
                errors.append((f"{path}.{key}", "is required before merge"))


def _slice_map(value: object, errors: list[tuple[str, str]]) -> dict[str, dict[str, object]] | None:
    if not isinstance(value, dict):
        errors.append(("run.slices", "must be an object"))
        return None
    result: dict[str, dict[str, object]] = {}
    for slice_id, raw_slice in value.items():
        path = f"run.slices[{slice_id!r}]"
        if not isinstance(slice_id, str) or not slice_id:
            errors.append(("run.slices", "keys must be non-empty strings"))
            continue
        parsed = _object(raw_slice, path, SLICE_KEYS, errors)
        if parsed is None:
            continue
        result[slice_id] = parsed
        _validate_slice(slice_id, parsed, path, errors)
    return result


def _validate_slice(
    slice_id: str,
    value: dict[str, object],
    path: str,
    errors: list[tuple[str, str]],
) -> None:
    _non_empty_string(value, "id", path, errors)
    if value.get("id") != slice_id:
        errors.append((f"{path}.id", "must match its map key"))
    _enum_value(value, "status", path, SliceStatus, errors)
    _string_list(value, "paths", path, errors, allow_empty=False)
    _string_list(value, "depends_on", path, errors, allow_empty=True)
    _string_list(value, "test_plan", path, errors, allow_empty=True)
    for key in ("base_ref", "agent_type", "model", "branch", "worktree", "reviewed_head"):
        _nullable_string(value, key, path, errors)
    _nullable_positive_int(value, "pr_number", path, errors)
    _non_negative_int(value, "attempts", path, errors)
    _nullable_enum_value(value, "verdict", path, Verdict, errors)
    _nullable_string(value, "verdict_at", path, errors)
    if value.get("verdict") is not None and (
        not isinstance(value.get("reviewed_head"), str) or not value.get("reviewed_head")
    ):
        errors.append((f"{path}.reviewed_head", "is required when verdict is present"))
    _review_findings(value.get("review_findings"), path, errors)
    _string_map(value.get("review_patch_digests"), f"{path}.review_patch_digests", errors)
    _review_contract(value.get("review_contract"), path, errors)
    _ci_state(value.get("ci_state"), path, errors)
    _reviewer_attempt(value.get("reviewer_attempt"), path, errors)
    _nullable_string(value, "reviewer_agent_id", path, errors)
    if "repair_attempts" in value:
        _non_negative_int(value, "repair_attempts", path, errors)
    if "review_rounds" in value:
        _non_negative_int(value, "review_rounds", path, errors)
    _nullable_enum_value(value, "park_cause", path, ParkCause, errors)
    _nullable_positive_int(value, "park_issue_id", path, errors)
    _nullable_string(value, "blocked_by", path, errors)
    _nullable_string(value, "stall_classification", path, errors)
    _nullable_string(value, "dispatch_intent_id", path, errors)
    _nullable_number(value, "dispatch_started_at", path, errors)
    _nullable_string(value, "dispatch_last_boundary", path, errors)
    _nullable_string(value, "dispatch_error", path, errors)
    _nullable_string(value, "dispatch_agent_id", path, errors)
    _nullable_non_negative_int(value, "dispatch_authoritative_event_seq", path, errors)
    if "dispatch_generation" in value:
        _non_negative_int(value, "dispatch_generation", path, errors)
    _reconciliation(value.get("reconciliation"), path, errors)
    _nullable_number(value, "task_timeout_seconds", path, errors)
    _nullable_string(value, "task_timeout_source", path, errors)
    _validate_recovery(value.get("recovery"), path, errors)
    _validate_suspended_dependency(value.get("suspended_dependency"), path, errors)
    _validate_deadline_ledger(value.get("deadline_ledger"), path, errors)
    _validate_publication(value.get("publication"), path, errors)
    _validate_handoff(value.get("handoff"), path, errors)
    _validate_observation_provenance(value.get("observation_provenance"), path, errors)
    _validate_action(value.get("action"), path, errors)
    if value.get("status") == SliceStatus.SPAWNED.value:
        _non_empty_string(value, "dispatch_intent_id", path, errors)
        _non_empty_string(value, "dispatch_agent_id", path, errors)
        if value.get("dispatch_authoritative_event_seq") is None:
            errors.append(
                (
                    f"{path}.dispatch_authoritative_event_seq",
                    "is required for spawned slices",
                )
            )
    classification = value.get("stall_classification")
    if classification is not None and classification not in STALL_CLASSIFICATION_VALUES:
        errors.append(
            (f"{path}.stall_classification", "is not a recognised review-stall classification")
        )
    _park_audit(value.get("park_audit"), path, errors)


def _review_findings(
    value: object,
    path: str,
    errors: list[tuple[str, str]],
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append((f"{path}.review_findings", "must be an object"))
        return
    for head_sha, raw_findings in value.items():
        head_path = f"{path}.review_findings[{head_sha!r}]"
        if not isinstance(head_sha, str) or not head_sha:
            errors.append((f"{path}.review_findings", "keys must be non-empty head SHAs"))
            continue
        if not isinstance(raw_findings, list):
            errors.append((head_path, "must be an array"))
            continue
        for index, raw_finding in enumerate(raw_findings):
            finding_path = f"{head_path}[{index}]"
            finding = _object(raw_finding, finding_path, REVIEW_FINDING_KEYS, errors)
            if finding is None:
                continue
            for key in REVIEW_FINDING_KEYS:
                _non_empty_string(finding, key, finding_path, errors)


def _review_contract(
    value: object,
    path: str,
    errors: list[tuple[str, str]],
) -> None:
    if value is None:
        return
    contract = _object(value, f"{path}.review_contract", REVIEW_CONTRACT_KEYS, errors)
    if contract is None:
        return
    _string_list(
        contract,
        "acceptance_criteria",
        f"{path}.review_contract",
        errors,
        allow_empty=False,
    )
    _non_empty_string(contract, "digest", f"{path}.review_contract", errors)


def _string_map(
    value: object,
    path: str,
    errors: list[tuple[str, str]],
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append((path, "must be an object"))
        return
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            errors.append((path, "keys must be non-empty strings"))
        if not isinstance(item, str) or not item:
            errors.append((f"{path}[{key!r}]", "must be a non-empty string"))


def _ci_state(
    value: object,
    path: str,
    errors: list[tuple[str, str]],
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append((f"{path}.ci_state", "must be an object"))
        return
    for head_sha, status in value.items():
        status_path = f"{path}.ci_state[{head_sha!r}]"
        if not isinstance(head_sha, str) or not head_sha:
            errors.append((f"{path}.ci_state", "keys must be non-empty head SHAs"))
            continue
        if not isinstance(status, str) or status not in CI_STATUS_VALUES:
            errors.append((status_path, f"must be one of {sorted(CI_STATUS_VALUES)}"))


def _reviewer_attempt(
    value: object,
    path: str,
    errors: list[tuple[str, str]],
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append((f"{path}.reviewer_attempt", "must be an object"))
        return
    for head_sha, attempt in value.items():
        attempt_path = f"{path}.reviewer_attempt[{head_sha!r}]"
        if not isinstance(head_sha, str) or not head_sha:
            errors.append((f"{path}.reviewer_attempt", "keys must be non-empty head SHAs"))
            continue
        if type(attempt) is not int or attempt < 0:
            errors.append((attempt_path, "must be a non-negative integer"))


def _park_audit(value: object, path: str, errors: list[tuple[str, str]]) -> None:
    if value is None:
        return
    audit = _object(value, f"{path}.park_audit", PARK_AUDIT_KEYS, errors)
    if audit is None:
        return
    if "attempts" in audit:
        _non_negative_int(audit, "attempts", f"{path}.park_audit", errors)
    if "verdict" in audit:
        _nullable_enum_value(audit, "verdict", f"{path}.park_audit", Verdict, errors)
    if "attempt" in audit:
        _non_negative_int(audit, "attempt", f"{path}.park_audit", errors)
    for key in (
        "gate_name",
        "recovery_action",
        "base_sha",
        "head_sha",
        "scope_attribution",
        "declared_difficulty",
        "matched_difficulty_rule",
    ):
        if key in audit:
            _nullable_string(audit, key, f"{path}.park_audit", errors)
    for key in ("needs_human", "retryable"):
        if key in audit:
            _boolean(audit, key, f"{path}.park_audit", errors)
    for key in ("harness", "model", "from_harness", "to_harness", "reason", "effort"):
        if key in audit:
            _nullable_string(audit, key, f"{path}.park_audit", errors)
    ledger = audit.get("ledger")
    if ledger is not None:
        parsed = _object(ledger, f"{path}.park_audit.ledger", LEDGER_KEYS, errors)
        if parsed is not None:
            _non_negative_int(parsed, "tokens", f"{path}.park_audit.ledger", errors)
            _non_negative_int(parsed, "wall_seconds", f"{path}.park_audit.ledger", errors)
            for key in ("role_spent", "harness_spent", "role_reserved", "harness_reserved"):
                _counter_map(parsed, key, f"{path}.park_audit.ledger", errors)
            _charges(parsed.get("charges"), f"{path}.park_audit.ledger", errors)


def _reconciliation(value: object, path: str, errors: list[tuple[str, str]]) -> None:
    if value is None:
        return
    reconciliation = _object(value, f"{path}.reconciliation", RECONCILIATION_KEYS, errors)
    if reconciliation is None:
        return
    for key in ("confirmed_stage", "next_action"):
        _non_empty_string(reconciliation, key, f"{path}.reconciliation", errors)
    for key in ("authoritative_evidence", "missing_evidence", "conflicts"):
        _string_list(reconciliation, key, f"{path}.reconciliation", errors, allow_empty=True)


def _validate_recovery(value: object, path: str, errors: list[tuple[str, str]]) -> None:
    if value is None:
        return
    recovery = _object(value, f"{path}.recovery", RECOVERY_KEYS, errors)
    if recovery is None:
        return
    _non_empty_string(recovery, "cause", f"{path}.recovery", errors)
    _enum_value(recovery, "phase", f"{path}.recovery", RecoveryPhase, errors)
    _non_negative_int(recovery, "recovery_round", f"{path}.recovery", errors)
    _non_empty_string(recovery, "next_action", f"{path}.recovery", errors)
    _non_empty_string(recovery, "owner_run_id", f"{path}.recovery", errors)
    _nullable_number(recovery, "entered_at", f"{path}.recovery", errors)
    _non_negative_int(recovery, "slice_attempt", f"{path}.recovery", errors)
    _nullable_string(recovery, "owner_agent_id", f"{path}.recovery", errors)
    _non_negative_int(recovery, "invocation_generation", f"{path}.recovery", errors)
    _non_negative_int(recovery, "plan_revision", f"{path}.recovery", errors)
    _nullable_number(recovery, "last_probe_at", f"{path}.recovery", errors)
    _nullable_number(recovery, "next_probe_at", f"{path}.recovery", errors)
    _non_negative_int(recovery, "probe_count", f"{path}.recovery", errors)
    evidence = recovery.get("evidence", {})
    if not isinstance(evidence, dict):
        errors.append((f"{path}.recovery.evidence", "must be an object"))


def _validate_suspended_dependency(value: object, path: str, errors: list[tuple[str, str]]) -> None:
    if value is None:
        return
    suspended = _object(value, f"{path}.suspended_dependency", SUSPENDED_DEPENDENCY_KEYS, errors)
    if suspended is None:
        return
    _non_empty_string(suspended, "blocked_by", f"{path}.suspended_dependency", errors)
    _enum_value(
        suspended,
        "prior_status",
        f"{path}.suspended_dependency",
        SliceStatus,
        errors,
    )
    _non_negative_int(
        suspended,
        "recovery_generation",
        f"{path}.suspended_dependency",
        errors,
    )


def _validate_repository_identity(value: object, path: str, errors: list[tuple[str, str]]) -> None:
    if value is None:
        return
    identity = _object(value, f"{path}.repository_identity", REPOSITORY_IDENTITY_KEYS, errors)
    if identity is None:
        return
    for key in ("owner", "repo", "base_branch"):
        _non_empty_string(identity, key, f"{path}.repository_identity", errors)
    _nullable_string(identity, "forge_host", f"{path}.repository_identity", errors)
    _nullable_string(identity, "remote_url", f"{path}.repository_identity", errors)


def _validate_publication(value: object, path: str, errors: list[tuple[str, str]]) -> None:
    if value is None:
        return
    publication = _object(value, f"{path}.publication", PUBLICATION_KEYS, errors)
    if publication is None:
        return
    _positive_int(publication, "pr_number", f"{path}.publication", errors)
    _positive_int(publication, "attempt", f"{path}.publication", errors)
    for key in ("head_sha", "head_branch", "base_branch"):
        _non_empty_string(publication, key, f"{path}.publication", errors)
    _nullable_string(publication, "invocation_id", f"{path}.publication", errors)


def _validate_handoff(value: object, path: str, errors: list[tuple[str, str]]) -> None:
    if value is None:
        return
    handoff = _object(value, f"{path}.handoff", HANDOFF_KEYS, errors)
    if handoff is None:
        return
    _positive_int(handoff, "pr_number", f"{path}.handoff", errors)
    _positive_int(handoff, "attempt", f"{path}.handoff", errors)
    for key in ("head_sha", "invocation_id", "agent_id", "observed_at"):
        _non_empty_string(handoff, key, f"{path}.handoff", errors)


def _validate_observation_provenance(
    value: object, path: str, errors: list[tuple[str, str]]
) -> None:
    if value is None:
        return
    provenance = _object(
        value,
        f"{path}.observation_provenance",
        OBSERVATION_PROVENANCE_KEYS,
        errors,
    )
    if provenance is None:
        return
    for key in ("source", "observed_at"):
        _non_empty_string(provenance, key, f"{path}.observation_provenance", errors)
    for key in ("event_seq", "ledger_run_seq", "snapshot_high_watermark"):
        _nullable_non_negative_int(provenance, key, f"{path}.observation_provenance", errors)
    for key in ("source_epoch", "source_revision"):
        if key in provenance:
            _non_negative_int(provenance, key, f"{path}.observation_provenance", errors)
    _nullable_string(provenance, "snapshot_id", f"{path}.observation_provenance", errors)
    if "coverage" in provenance:
        _string_list(
            provenance,
            "coverage",
            f"{path}.observation_provenance",
            errors,
            allow_empty=True,
        )
        _unique_strings(
            provenance.get("coverage"),
            f"{path}.observation_provenance.coverage",
            errors,
        )


def _validate_action(value: object, path: str, errors: list[tuple[str, str]]) -> None:
    if value is None:
        return
    action = _object(value, f"{path}.action", ACTION_KEYS, errors)
    if action is None:
        return
    _enum_value(action, "kind", f"{path}.action", ActionKind, errors)
    _enum_value(action, "phase", f"{path}.action", ActionPhase, errors)
    _positive_int(action, "state_version", f"{path}.action", errors)
    _nullable_string(action, "intent_id", f"{path}.action", errors)
    _nullable_string(action, "head_sha", f"{path}.action", errors)
    _nullable_positive_int(action, "attempt", f"{path}.action", errors)
    _nullable_string(action, "contract_digest", f"{path}.action", errors)


def _validate_deadline_ledger(value: object, path: str, errors: list[tuple[str, str]]) -> None:
    if value is None:
        return
    ledger = _object(value, f"{path}.deadline_ledger", DEADLINE_LEDGER_KEYS, errors)
    if ledger is None:
        return
    for key in (
        "execution_deadline_at",
        "recovery_deadline_at",
        "run_deadline_at",
        "suspended_at",
    ):
        _nullable_number(ledger, key, f"{path}.deadline_ledger", errors)
    for key in ("execution_seconds", "recovery_wait_seconds"):
        _non_negative_number(ledger, key, f"{path}.deadline_ledger", errors)


def _budgets(value: object, errors: list[tuple[str, str]]) -> None:
    budgets = _object(value, "run.budgets", BUDGET_KEYS, errors)
    if budgets is None:
        return
    ledger = _object(budgets.get("ledger"), "run.budgets.ledger", LEDGER_KEYS, errors)
    if ledger is None:
        return
    _non_negative_int(ledger, "tokens", "run.budgets.ledger", errors)
    _non_negative_int(ledger, "wall_seconds", "run.budgets.ledger", errors)
    for key in ("role_spent", "harness_spent", "role_reserved", "harness_reserved"):
        _counter_map(ledger, key, "run.budgets.ledger", errors)
    _charges(ledger.get("charges"), "run.budgets.ledger", errors)


def _counter_map(
    holder: dict[str, object], key: str, path: str, errors: list[tuple[str, str]]
) -> None:
    if key not in holder:
        return
    value = holder[key]
    if not isinstance(value, dict):
        errors.append((f"{path}.{key}", "must be an object"))
        return
    for name, amount in value.items():
        if not isinstance(name, str) or not name:
            errors.append((f"{path}.{key}", "keys must be non-empty strings"))
        if type(amount) is not int or amount < 0:
            errors.append((f"{path}.{key}.{name}", "must be a non-negative integer"))


def _charges(value: object, path: str, errors: list[tuple[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        errors.append((f"{path}.charges", "must be an array"))
        return
    seen: set[tuple[str, int]] = set()
    for index, raw_charge in enumerate(value):
        charge_path = f"{path}.charges[{index}]"
        charge = _object(raw_charge, charge_path, CHARGE_KEYS, errors)
        if charge is None:
            continue
        _non_empty_string(charge, "slice_id", charge_path, errors)
        _non_negative_int(charge, "attempt", charge_path, errors)
        if type(charge.get("attempt")) is int and cast(int, charge["attempt"]) < 1:
            errors.append((f"{charge_path}.attempt", "must be a positive integer"))
        _non_empty_string(charge, "role", charge_path, errors)
        _non_empty_string(charge, "harness", charge_path, errors)
        _non_negative_int(charge, "estimated_tokens", charge_path, errors)
        actual = charge.get("actual")
        if actual != "unknown" and (type(actual) is not int or actual < 0):
            errors.append((f"{charge_path}.actual", "must be a non-negative integer or 'unknown'"))
        delta = charge.get("delta_tokens")
        if delta is not None and type(delta) is not int:
            errors.append((f"{charge_path}.delta_tokens", "must be null or an integer"))
        _boolean(charge, "warning", charge_path, errors)
        _boolean(charge, "reconciled", charge_path, errors)
        slice_id = charge.get("slice_id")
        attempt = charge.get("attempt")
        if isinstance(slice_id, str) and type(attempt) is int:
            identity = (slice_id, cast(int, attempt))
            if identity in seen:
                errors.append((f"{charge_path}", "duplicate slice_id and attempt"))
            seen.add(identity)


def _boolean(holder: dict[str, object], key: str, path: str, errors: list[tuple[str, str]]) -> None:
    if type(holder.get(key)) is not bool:
        errors.append((f"{path}.{key}", "must be a boolean"))


def _gates(value: object, errors: list[tuple[str, str]]) -> None:
    if not isinstance(value, list):
        errors.append(("run.gates", "must be an array"))
        return
    names: set[str] = set()
    for index, raw_gate in enumerate(value):
        path = f"run.gates[{index}]"
        gate = _object(raw_gate, path, GATE_KEYS, errors)
        if gate is None:
            continue
        _non_empty_string(gate, "name", path, errors)
        _enum_value(gate, "status", path, GateStatus, errors)
        name = gate.get("name")
        if isinstance(name, str):
            if name in names:
                errors.append((f"{path}.name", "duplicate gate name"))
            names.add(name)


def _events(value: object, errors: list[tuple[str, str]]) -> None:
    events = _object(value, "run.events", EVENT_KEYS, errors)
    if events is not None:
        _non_negative_int(events, "last_consumed_offset", "run.events", errors)


def _goals(value: object, errors: list[tuple[str, str]]) -> None:
    if value is None:
        return
    goals = _object(value, "run.goals", GOAL_KEYS, errors)
    if goals is None:
        return
    _goal_text(goals, "objective", "run.goals", errors)
    _non_negative_number(goals, "deadline", "run.goals", errors)
    _goal_text(goals, "completion_predicate", "run.goals", errors)
    for key in (
        "last_heartbeat_at",
        "last_progress_at",
        "controller_started_at",
    ):
        if goals.get(key) is not None:
            _non_negative_number(goals, key, "run.goals", errors)
    if goals.get("last_authoritative_event_seq") is not None:
        _non_negative_int(goals, "last_authoritative_event_seq", "run.goals", errors)


def _goal_text(
    holder: dict[str, object], key: str, path: str, errors: list[tuple[str, str]]
) -> None:
    if not isinstance(holder.get(key), str):
        errors.append((f"{path}.{key}", "must be a string"))


def _non_negative_number(
    holder: dict[str, object], key: str, path: str, errors: list[tuple[str, str]]
) -> None:
    value = holder.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        errors.append((f"{path}.{key}", "must be a non-negative number"))


def _validate_waiting(
    waiting: object,
    slices: Mapping[str, dict[str, object]],
    errors: list[tuple[str, str]],
) -> None:
    if not isinstance(waiting, list):
        return
    for index, slice_id in enumerate(waiting):
        if isinstance(slice_id, str) and slice_id not in slices:
            errors.append((f"run.fsm.waiting[{index}]", f"unknown slice {slice_id!r}"))


def _validate_dependencies(
    slices: Mapping[str, dict[str, object]],
    errors: list[tuple[str, str]],
) -> None:
    graph: dict[str, list[str]] = {}
    for slice_id, value in slices.items():
        dependencies = value.get("depends_on")
        if not isinstance(dependencies, list):
            continue
        graph[slice_id] = [dependency for dependency in dependencies if isinstance(dependency, str)]
        for dependency in graph[slice_id]:
            if dependency not in slices:
                errors.append(
                    (f"run.slices[{slice_id!r}].depends_on", f"unknown slice {dependency!r}")
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(slice_id: str, trail: tuple[str, ...]) -> None:
        if slice_id in visiting:
            cycle = " -> ".join((*trail, slice_id))
            errors.append(("run.slices", f"depends_on cycle: {cycle}"))
            return
        if slice_id in visited:
            return
        visiting.add(slice_id)
        for dependency in graph.get(slice_id, []):
            if dependency in graph:
                visit(dependency, (*trail, slice_id))
        visiting.remove(slice_id)
        visited.add(slice_id)

    for slice_id in graph:
        visit(slice_id, ())


def _validate_path_ownership(
    slices: Mapping[str, dict[str, object]],
    errors: list[tuple[str, str]],
) -> None:
    active: list[tuple[str, str]] = []
    for slice_id, value in slices.items():
        if value.get("status") in TERMINAL_SLICE_STATUSES:
            continue
        paths = value.get("paths")
        if not isinstance(paths, list):
            continue
        for path in paths:
            if not isinstance(path, str):
                continue
            for other_id, other_path in active:
                if _patterns_overlap(path, other_path):
                    errors.append(
                        (
                            f"run.slices[{slice_id!r}].paths",
                            f"overlaps {other_id!r} path {other_path!r}",
                        )
                    )
            active.append((slice_id, path))


def _patterns_overlap(left: str, right: str) -> bool:
    """Conservatively detect overlap for exact paths and common globs."""
    if left == right:
        return True
    from fnmatch import fnmatchcase

    return fnmatchcase(left, right) or fnmatchcase(right, left)


def _object(
    value: object,
    path: str,
    allowed: frozenset[str],
    errors: list[tuple[str, str]],
) -> dict[str, object] | None:
    if not isinstance(value, dict):
        errors.append((path, "must be an object"))
        return None
    unknown = sorted(set(value) - allowed)
    if unknown:
        errors.append((path, f"unknown keys: {', '.join(unknown)}"))
    return cast(dict[str, object], value)


def _enum_value(
    holder: dict[str, object],
    key: str,
    path: str,
    enum_type: type[Enum],
    errors: list[tuple[str, str]],
) -> None:
    value = holder.get(key)
    allowed = {member.value for member in enum_type}
    if value not in allowed:
        errors.append(
            (
                f"{path}.{key}",
                f"must be one of {', '.join(sorted(cast(str, item) for item in allowed))}",
            )
        )


def _nullable_enum_value(
    holder: dict[str, object],
    key: str,
    path: str,
    enum_type: type[Enum],
    errors: list[tuple[str, str]],
) -> None:
    if holder.get(key) is not None:
        _enum_value(holder, key, path, enum_type, errors)


def _non_empty_string(
    holder: dict[str, object], key: str, path: str, errors: list[tuple[str, str]]
) -> None:
    value = holder.get(key)
    if not isinstance(value, str) or not value:
        errors.append((f"{path}.{key}", "must be a non-empty string"))


def _nullable_string(
    holder: dict[str, object], key: str, path: str, errors: list[tuple[str, str]]
) -> None:
    value = holder.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        errors.append((f"{path}.{key}", "must be null or a non-empty string"))


def _string_list(
    holder: dict[str, object],
    key: str,
    path: str,
    errors: list[tuple[str, str]],
    *,
    allow_empty: bool,
) -> None:
    value = holder.get(key)
    if not isinstance(value, list) or (not allow_empty and not value):
        suffix = "non-empty " if not allow_empty else ""
        errors.append((f"{path}.{key}", f"must be a {suffix}array of strings"))
        return
    if any(not isinstance(item, str) or not item for item in value):
        errors.append((f"{path}.{key}", "must contain only non-empty strings"))


def _unique_strings(value: object, path: str, errors: list[tuple[str, str]]) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return
    if len(value) != len(set(value)):
        errors.append((path, "must not contain duplicate IDs"))


def _non_negative_int(
    holder: dict[str, object], key: str, path: str, errors: list[tuple[str, str]]
) -> None:
    value = holder.get(key)
    if type(value) is not int or value < 0:
        errors.append((f"{path}.{key}", "must be a non-negative integer"))


def _positive_int(
    holder: dict[str, object], key: str, path: str, errors: list[tuple[str, str]]
) -> None:
    value = holder.get(key)
    if type(value) is not int or value <= 0:
        errors.append((f"{path}.{key}", "must be a positive integer"))


def _nullable_positive_int(
    holder: dict[str, object], key: str, path: str, errors: list[tuple[str, str]]
) -> None:
    value = holder.get(key)
    if value is not None and (type(value) is not int or value < 1):
        errors.append((f"{path}.{key}", "must be null or a positive integer"))


def _nullable_non_negative_int(
    holder: dict[str, object], key: str, path: str, errors: list[tuple[str, str]]
) -> None:
    value = holder.get(key)
    if value is not None and (type(value) is not int or value < 0):
        errors.append((f"{path}.{key}", "must be null or a non-negative integer"))


def _nullable_number(
    holder: dict[str, object], key: str, path: str, errors: list[tuple[str, str]]
) -> None:
    value = holder.get(key)
    if value is not None and (type(value) not in {int, float} or value < 0):
        errors.append((f"{path}.{key}", "must be null or a non-negative number"))
