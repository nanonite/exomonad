"""Closed, versioned schema for durable TL run state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, TypeAlias, cast

from tl_loop.fsm.phase import TLPhase

SCHEMA_VERSION = 1


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


class ParkCause(str, Enum):
    """Closed reasons that can park a slice for human action."""

    RETRIES_EXHAUSTED = "retries_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_CAPABLE_HARNESS = "no_capable_harness"
    SCHEDULE_DEADLOCK = "schedule_deadlock"
    REVIEW_STUCK = "review_stuck"
    HARNESS_SWITCH_REQUESTED = "harness_switch_requested"
    STALL_DETECTED = "stall_detected"
    DISPATCH_TIMEOUT = "dispatch_timeout"
    DISPATCH_UNCONFIRMED = "dispatch_unconfirmed"
    DISPATCH_FAILED = "dispatch_failed"


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


REVIEW_FINDING_KEYS = frozenset({"severity", "path", "rationale"})
CI_STATUS_VALUES = frozenset({"unknown", "pending", "success", "failure", "neutral"})
STALL_CLASSIFICATION_VALUES = frozenset(
    {
        "dev_not_pushing",
        "reviewer_not_responding",
        "reviewer_never_started",
        "ci_failed",
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
    }
)
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
        "ci_state",
        "reviewer_attempt",
        "repair_attempts",
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
        "dispatch_authoritative_event_seq",
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
    }
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
    ci_state: Mapping[str, str] = field(default_factory=dict)
    reviewer_attempt: Mapping[str, int] = field(default_factory=dict)
    repair_attempts: int = 0
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
    dispatch_authoritative_event_seq: int | None = None


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
    owner_branch: str | None = None
    owner_worktree: str | None = None
    parent_branch: str | None = None
    parent_run_id: str | None = None
    parent_agent_id: str | None = None
    depth: int = 0
    goals: GoalState = field(default_factory=GoalState)


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
        "owner_branch",
        "owner_worktree",
        "parent_branch",
        "parent_run_id",
        "parent_agent_id",
    ):
        _nullable_string(root, key, "run", errors)
    if "depth" in root:
        _non_negative_int(root, "depth", "run", errors)

    fsm = _object(root.get("fsm"), "run.fsm", FSM_KEYS, errors)
    if fsm is not None:
        _enum_value(fsm, "phase", "run.fsm", TLPhase, errors)
        _string_list(fsm, "waiting", "run.fsm", errors, allow_empty=True)
        _unique_strings(fsm.get("waiting"), "run.fsm.waiting", errors)

    slices = _slice_map(root.get("slices"), errors)
    _goals(root.get("goals"), errors)
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
    _ci_state(value.get("ci_state"), path, errors)
    _reviewer_attempt(value.get("reviewer_attempt"), path, errors)
    if "repair_attempts" in value:
        _non_negative_int(value, "repair_attempts", path, errors)
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
    for key in ("last_heartbeat_at", "last_progress_at"):
        if goals.get(key) is not None:
            _non_negative_number(goals, key, "run.goals", errors)


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
