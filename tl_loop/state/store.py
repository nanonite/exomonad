"""Typed creation, checkpoint, and resume operations for TL runs."""

from __future__ import annotations

import copy
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias, cast

from tl_loop.fsm.phase import (
    PhaseValue,
    TLAllMerged,
    TLDispatching,
    TLDone,
    TLFailed,
    TLMerging,
    TLPhase,
    TLPlanning,
    TLPRFiled,
    TLWaiting,
)
from tl_loop.ordered import IntegrationLifecycle

from .migration import (
    MigrationError,
    install_migration,
    migrate_checkpoint_document,
    record_migration_failure,
)
from .schema import (
    SCHEMA_VERSION,
    ActualTokens,
    BudgetCharge,
    BudgetLedger,
    EventCursor,
    FSMState,
    GateState,
    GateStatus,
    GoalState,
    IntegrationCandidateState,
    IntegrationRuntimeState,
    OrderedStageState,
    ParkCause,
    RunState,
    SchemaError,
    SliceMap,
    SliceState,
    SliceStatus,
    Verdict,
    validate,
)
from .write import apply

DEFAULT_ROOT = Path(".exo/tl-loop")
RootSpec: TypeAlias = Mapping[str, object]
SliceInput: TypeAlias = SliceState | Mapping[str, object]
FSMInput: TypeAlias = FSMState | PhaseValue | TLPhase
BudgetInput: TypeAlias = BudgetLedger | Mapping[str, object]
OrderedStagesInput: TypeAlias = (
    tuple[OrderedStageState, ...] | list[Mapping[str, object]] | Mapping[str, object]
)
IntegrationInput: TypeAlias = IntegrationRuntimeState | Mapping[str, object]


class QuarantineStorageError(RuntimeError):
    """The durable unresolved-event queue cannot be trusted for replay."""

    def __init__(self, path: Path, reason: str, *, cause: BaseException | None = None) -> None:
        message = f"invalid event quarantine {path}: {reason}"
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause


def _exception_chain(error: BaseException) -> tuple[str, ...]:
    chain: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _exception_context(error: BaseException) -> dict[str, object]:
    context: dict[str, object] = {}
    current: BaseException | None = error
    while current is not None:
        for name in (
            "cursor",
            "sequence_status",
            "segment",
            "line_number",
            "byte_length",
            "elapsed_seconds",
            "timeout_seconds",
        ):
            value = getattr(current, name, None)
            if value is None or name in context:
                continue
            value = getattr(value, "value", value)
            context[name] = str(value) if isinstance(value, Path) else value
        current = current.__cause__ or current.__context__
    return context


class CorruptCheckpoint(ValueError):
    """A persisted run state is valid JSON but cannot be resumed safely."""

    park_cause = ParkCause.CORRUPT_STATE


class WorktreeClaimError(RuntimeError):
    """A live run already owns the requested worktree."""


@dataclass(frozen=True)
class ResumeState:
    """The local state required to resume a controller without network I/O."""

    fsm: FSMState
    slices: SliceMap
    budgets: BudgetLedger
    offset: int
    goals: GoalState = field(default_factory=GoalState)
    current_order: int = 1
    ordered_stages: tuple[OrderedStageState, ...] = ()
    integration: IntegrationRuntimeState = field(default_factory=IntegrationRuntimeState)

    @property
    def phase(self) -> TLPhase:
        """Return the persisted FSM phase."""
        return self.fsm.phase


@dataclass(frozen=True)
class RunStore:
    """Filesystem boundary for one run under ``.exo/tl-loop/<run_id>``."""

    run_id: str
    root_dir: Path = DEFAULT_ROOT

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_dir", Path(self.root_dir))
        _validate_run_id(self.run_id)

    @property
    def run_dir(self) -> Path:
        return self.root_dir / self.run_id

    @property
    def path(self) -> Path:
        return self.run_dir / "run.json"

    def checkpoint(
        self,
        fsm: FSMInput,
        slices: Mapping[str, SliceInput],
        budgets: BudgetInput,
        offset: int,
        *,
        current_order: int | None = None,
        ordered_stages: OrderedStagesInput | None = None,
        integration: IntegrationInput | None = None,
    ) -> RunState:
        """Persist one checkpoint through the shared atomic mutation path."""
        return checkpoint(
            fsm,
            slices,
            budgets,
            offset,
            path=self.run_dir,
            current_order=current_order,
            ordered_stages=ordered_stages,
            integration=integration,
        )

    def set_ordered_state(
        self,
        current_order: int,
        ordered_stages: OrderedStagesInput,
        integration: IntegrationInput,
    ) -> RunState:
        """Atomically persist stage progress and integration evidence."""
        encoded_stages = _encode_ordered_stages(ordered_stages)
        encoded_integration = _encode_integration(integration)
        if type(current_order) is not int or current_order <= 0:
            raise ValueError("current_order must be a positive integer")

        def mutate(document: dict[str, object]) -> dict[str, object]:
            document["current_order"] = current_order
            document["ordered_stages"] = copy.deepcopy(encoded_stages)
            document["integration"] = copy.deepcopy(encoded_integration)
            return document

        apply(self.run_dir, mutate)
        return self.load()

    def load(self) -> RunState:
        """Load and verify this run's checkpoint."""
        return load(self.path)

    @property
    def exit_reason_path(self) -> Path:
        """Return the diagnostic marker written when startup fails."""
        return self.run_dir / "controller-exit.json"

    @property
    def terminal_summary_path(self) -> Path:
        """Return the durable summary path for a terminal controller result."""
        return self.run_dir / "terminal-summary.json"

    @property
    def controller_output_path(self) -> Path:
        """Return the bounded-on-disk controller output capture path."""
        return self.run_dir / "controller-output.log"

    @property
    def event_quarantine_path(self) -> Path:
        """Return the durable queue for valid observations awaiting ownership."""
        return self.run_dir / "event-quarantine.json"

    def quarantine_event(self, event: Mapping[str, object]) -> None:
        """Persist one unresolved event without advancing its meaning."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        entries = list(self.quarantined_events())
        run_seq = event.get("run_seq")
        if any(item.get("run_seq") == run_seq for item in entries):
            return
        entries.append(copy.deepcopy(dict(event)))
        temporary = self.event_quarantine_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entries, sort_keys=True), encoding="utf-8")
        temporary.replace(self.event_quarantine_path)

    def quarantined_events(self) -> tuple[Mapping[str, object], ...]:
        """Read unresolved observations retained for a later controller run."""
        try:
            payload = json.loads(self.event_quarantine_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise QuarantineStorageError(
                self.event_quarantine_path,
                "read failed",
                cause=error,
            ) from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise QuarantineStorageError(
                self.event_quarantine_path,
                "invalid JSON or UTF-8",
                cause=error,
            ) from error
        if not isinstance(payload, list):
            raise QuarantineStorageError(self.event_quarantine_path, "root must be a JSON array")
        if any(not isinstance(item, dict) for item in payload):
            raise QuarantineStorageError(
                self.event_quarantine_path,
                "every entry must be a JSON object",
            )
        return tuple(MappingProxyType(dict(item)) for item in payload)

    def release_quarantined_event(self, run_seq: int) -> None:
        """Remove an event only after its ownership has been reconciled."""
        entries = [
            dict(item) for item in self.quarantined_events() if item.get("run_seq") != run_seq
        ]
        if entries:
            temporary = self.event_quarantine_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(entries, sort_keys=True), encoding="utf-8")
            temporary.replace(self.event_quarantine_path)
        else:
            try:
                self.event_quarantine_path.unlink()
            except FileNotFoundError:
                pass

    def record_terminal_summary(self, summary: Mapping[str, object]) -> None:
        """Persist terminal diagnostics independently of the tmux process."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        payload = {"recorded_at": time.time(), **dict(summary)}
        try:
            output = self.controller_output_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            output = ""
        if output:
            payload["recent_output"] = "\n".join(output.splitlines()[-20:])
        temporary = self.terminal_summary_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(self.terminal_summary_path)

    def terminal_summary(self) -> Mapping[str, object] | None:
        """Read the terminal summary without making it part of run state."""
        try:
            payload = json.loads(self.terminal_summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def record_exit_reason(
        self,
        reason: str,
        *,
        error: BaseException | None = None,
    ) -> None:
        """Persist a diagnostic-only controller exit reason outside run state."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        payload = {"reason": reason, "recorded_at": time.time()}
        if error is not None:
            payload["error_chain"] = list(_exception_chain(error))
            context = _exception_context(error)
            if context:
                payload["context"] = context
        try:
            output = self.controller_output_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            output = ""
        if output:
            payload["recent_output"] = "\n".join(output.splitlines()[-20:])
        temporary = self.exit_reason_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(self.exit_reason_path)

    def exit_reason(self) -> str | None:
        """Read the diagnostic marker without treating it as a checkpoint."""
        payload = self.exit_diagnostics()
        if payload is None:
            return None
        reason = payload.get("reason") if isinstance(payload, dict) else None
        return reason if isinstance(reason, str) and reason else None

    def exit_diagnostics(self) -> Mapping[str, object] | None:
        """Read the complete durable controller-exit diagnostic."""
        try:
            payload = json.loads(self.exit_reason_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def resume(self) -> ResumeState:
        """Return local replay state without contacting the runtime."""
        return resume(self.run_id, root_dir=self.root_dir)

    def set_goals(self, goals: GoalState) -> RunState:
        """Persist goal and heartbeat metadata through the atomic writer."""
        encoded = _encode_goals(goals)

        def mutate(document: dict[str, object]) -> dict[str, object]:
            document["goals"] = copy.deepcopy(encoded)
            return document

        apply(self.run_dir, mutate)
        return self.load()

    def set_gate(
        self,
        name: str,
        status: GateStatus = GateStatus.PENDING,
    ) -> RunState:
        """Create or update one named human gate through the atomic writer."""
        if not isinstance(name, str) or not name:
            raise ValueError("gate name must be a non-empty string")
        if not isinstance(status, GateStatus):
            raise TypeError("gate status must be a GateStatus")

        def mutate(document: dict[str, object]) -> dict[str, object]:
            gates = document.get("gates")
            if not isinstance(gates, list):
                raise CorruptCheckpoint("run state gates are not an array")
            for gate in gates:
                if isinstance(gate, dict) and gate.get("name") == name:
                    gate["status"] = status.value
                    return document
            gates.append({"name": name, "status": status.value})
            return document

        apply(self.run_dir, mutate)
        return self.load()

    def answer_gate(self, name: str, status: GateStatus) -> RunState:
        """Answer an existing named gate without creating a new one."""
        if not isinstance(name, str) or not name:
            raise ValueError("gate name must be a non-empty string")
        if not isinstance(status, GateStatus):
            raise TypeError("status must be a GateStatus")

        def mutate(document: dict[str, object]) -> dict[str, object]:
            gates = document.get("gates")
            if not isinstance(gates, list):
                raise CorruptCheckpoint("run state gates are not an array")
            for gate in gates:
                if isinstance(gate, dict) and gate.get("name") == name:
                    gate["status"] = status.value
                    return document
            raise ValueError(f"gate {name!r} does not exist")

        apply(self.run_dir, mutate)
        return self.load()


def create(run_id: str, root_spec: RootSpec, *, root_dir: str | Path = DEFAULT_ROOT) -> RunState:
    """Create a run at ``.exo/tl-loop/<run_id>/run.json``.

    ``root_spec`` supplies any of the persisted ``fsm``, ``slices``,
    ``budgets``, ``gates``, and ``events`` sections. Omitted sections use the
    empty, planning-state defaults.
    """
    _validate_run_id(run_id)
    directory = Path(root_dir) / run_id
    initial = _initial_document(run_id, root_spec)
    _assert_worktree_available(initial, directory, Path(root_dir))
    try:
        validate(initial)
        _assert_consistent(directory / "run.json", _decode(initial))
    except SchemaError as error:
        raise CorruptCheckpoint(
            f"{directory / 'run.json'}: schema inconsistency: {error}"
        ) from error
    apply(directory, _identity, initial=initial)
    return load(directory / "run.json")


def load(path: str | Path) -> RunState:
    """Read, validate, and structurally verify a checkpoint from disk."""
    target = _state_path(path)
    try:
        data = _read_bytes(target)
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        if target.exists():
            report = record_migration_failure(target, error)
            raise CorruptCheckpoint(
                f"{target}: could not read checkpoint: {error}; migration blocked, see {report}"
            ) from error
        raise CorruptCheckpoint(f"{target}: could not read checkpoint: {error}") from error
    if not isinstance(value, dict):
        raise CorruptCheckpoint(f"{target}: checkpoint must contain a JSON object")
    try:
        migration = migrate_checkpoint_document(value, run_id=target.parent.name)
    except MigrationError as error:
        report = record_migration_failure(target, error)
        raise CorruptCheckpoint(f"{target}: migration blocked: {error}; see {report}") from error
    if migration.migrated:
        try:
            validate(migration.document)
            migrated_state = _decode(migration.document)
            _assert_consistent(target, migrated_state)
            install_migration(target, migration)
            value = migration.document
        except (OSError, SchemaError, CorruptCheckpoint, MigrationError) as error:
            report = record_migration_failure(target, error)
            raise CorruptCheckpoint(
                f"{target}: migration validation failed: {error}; see {report}"
            ) from error
    try:
        validate(value)
    except SchemaError as error:
        raise CorruptCheckpoint(f"{target}: schema inconsistency: {error}") from error
    state = _decode(value)
    _assert_consistent(target, state)
    return state


def checkpoint(
    fsm: FSMInput,
    slices: Mapping[str, SliceInput],
    budgets: BudgetInput,
    offset: int,
    *,
    run_id: str | None = None,
    path: str | Path | None = None,
    root_dir: str | Path = DEFAULT_ROOT,
    current_order: int | None = None,
    ordered_stages: OrderedStagesInput | None = None,
    integration: IntegrationInput | None = None,
) -> RunState:
    """Atomically update a run's FSM, slices, budget ledger, and event offset."""
    run_directory = _resolve_run_directory(run_id, path, root_dir)
    if type(offset) is not int or offset < 0:
        raise ValueError("event-log offset must be a non-negative integer")
    encoded_fsm = _encode_fsm(fsm)
    encoded_slices = _encode_slices(slices)
    encoded_budgets = _encode_budgets(budgets)
    encoded_stages = _encode_ordered_stages(ordered_stages) if ordered_stages is not None else None
    encoded_integration = _encode_integration(integration) if integration is not None else None
    if current_order is not None and (type(current_order) is not int or current_order <= 0):
        raise ValueError("current_order must be a positive integer")
    _assert_encoded_consistent(run_directory / "run.json", encoded_fsm, encoded_slices)

    def mutate(document: dict[str, object]) -> dict[str, object]:
        document["fsm"] = copy.deepcopy(encoded_fsm)
        document["slices"] = copy.deepcopy(encoded_slices)
        document["budgets"] = copy.deepcopy(encoded_budgets)
        document["events"] = {"last_consumed_offset": offset}
        if current_order is not None:
            document["current_order"] = current_order
        if encoded_stages is not None:
            document["ordered_stages"] = copy.deepcopy(encoded_stages)
        if encoded_integration is not None:
            document["integration"] = copy.deepcopy(encoded_integration)
        return document

    apply(run_directory, mutate)
    return load(run_directory / "run.json")


def resume(run_id: str, *, root_dir: str | Path = DEFAULT_ROOT) -> ResumeState:
    """Reconstruct local controller state and replay offset from a checkpoint."""
    state = load(Path(root_dir) / run_id / "run.json")
    return ResumeState(
        fsm=state.fsm,
        slices=state.slices,
        budgets=state.budgets,
        offset=state.events.last_consumed_offset,
        goals=state.goals,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )


def _initial_document(run_id: str, root_spec: RootSpec) -> dict[str, object]:
    allowed = {
        "fsm",
        "slices",
        "budgets",
        "gates",
        "events",
        "ledger_run_id",
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
    }
    unknown = sorted(set(root_spec) - allowed)
    if unknown:
        raise ValueError(f"root_spec contains unknown sections: {', '.join(unknown)}")
    document: dict[str, object] = {
        "version": SCHEMA_VERSION,
        "revision": 0,
        "run_id": run_id,
        "fsm": {"phase": TLPhase.TLPlanning.value, "waiting": []},
        "slices": {},
        "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        "gates": [],
        "events": {"last_consumed_offset": 0},
    }
    for key, value in root_spec.items():
        document[key] = copy.deepcopy(value)
    return document


def _assert_worktree_available(initial: Mapping[str, object], target: Path, root_dir: Path) -> None:
    owner = initial.get("owner_worktree")
    if not isinstance(owner, str) or not owner:
        return
    normalized_owner = _normalize_worktree(owner)
    search_root = root_dir.parent
    if not search_root.exists():
        return
    for candidate in search_root.rglob("run.json"):
        if candidate == target / "run.json":
            continue
        existing = _read_existing_state(candidate)
        if existing is None:
            continue
        existing_owner = existing.get("owner_worktree")
        raw_fsm = existing.get("fsm")
        phase = raw_fsm.get("phase") if isinstance(raw_fsm, dict) else None
        if (
            isinstance(existing_owner, str)
            and _normalize_worktree(existing_owner) == normalized_owner
            and phase not in {TLPhase.TLDone.value, TLPhase.TLFailed.value}
        ):
            raise WorktreeClaimError(f"worktree {owner!r} is already claimed by {candidate.parent}")


def _resolve_run_directory(
    run_id: str | None,
    path: str | Path | None,
    root_dir: str | Path,
) -> Path:
    if path is not None and run_id is not None:
        raise ValueError("checkpoint accepts either path or run_id, not both")
    if path is not None:
        candidate = Path(path)
        return candidate.parent if candidate.name == "run.json" else candidate
    if run_id is None:
        raise TypeError("checkpoint requires path or run_id")
    _validate_run_id(run_id)
    return Path(root_dir) / run_id


def _state_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.name == "run.json" else candidate / "run.json"


def _validate_run_id(run_id: str) -> None:
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be a non-empty single path component")


def _identity(document: dict[str, object]) -> dict[str, object]:
    return document


def _encode_fsm(fsm: FSMInput) -> dict[str, object]:
    if isinstance(fsm, FSMState):
        return {"phase": fsm.phase.value, "waiting": list(fsm.waiting)}
    if isinstance(fsm, TLPhase):
        return {"phase": fsm.value, "waiting": []}
    if isinstance(fsm, TLWaiting):
        return {"phase": TLPhase.TLWaiting.value, "waiting": list(fsm.children)}
    if isinstance(fsm, TLMerging):
        return {"phase": TLPhase.TLMerging.value, "waiting": list(fsm.children)}
    phase_by_type: dict[type[object], TLPhase] = {
        TLPlanning: TLPhase.TLPlanning,
        TLDispatching: TLPhase.TLDispatching,
        TLAllMerged: TLPhase.TLAllMerged,
        TLPRFiled: TLPhase.TLPRFiled,
        TLDone: TLPhase.TLDone,
        TLFailed: TLPhase.TLFailed,
    }
    for phase_type, phase in phase_by_type.items():
        if isinstance(fsm, phase_type):
            return {"phase": phase.value, "waiting": []}
    raise TypeError(f"unsupported FSM value: {type(fsm).__name__}")


def _encode_slices(slices: Mapping[str, SliceInput]) -> dict[str, object]:
    return {slice_id: _encode_slice(slice_id, value) for slice_id, value in slices.items()}


def _encode_slice(slice_id: str, value: SliceInput) -> dict[str, object]:
    if isinstance(value, SliceState):
        record: dict[str, object] = {
            "id": value.id,
            "status": value.status.value,
            "paths": list(value.paths),
            "depends_on": list(value.depends_on),
            "base_ref": value.base_ref,
            "test_plan": list(value.test_plan),
            "agent_type": value.agent_type,
            "model": value.model,
            "branch": value.branch,
            "worktree": value.worktree,
            "pr_number": value.pr_number,
            "review_findings": _encode_review_findings(value.review_findings),
            "ci_state": dict(value.ci_state),
            "reviewer_attempt": dict(value.reviewer_attempt),
            "repair_attempts": value.repair_attempts,
            "reviewed_head": value.reviewed_head,
            "attempts": value.attempts,
            "verdict": value.verdict.value if value.verdict else None,
        }
        if value.review_patch_digests:
            record["review_patch_digests"] = dict(value.review_patch_digests)
        if value.verdict_at is not None:
            record["verdict_at"] = value.verdict_at
        if value.park_cause is not None:
            record["park_cause"] = value.park_cause.value
        if value.park_issue_id is not None:
            record["park_issue_id"] = value.park_issue_id
        if value.park_audit is not None:
            record["park_audit"] = copy.deepcopy(dict(value.park_audit))
        if value.blocked_by is not None:
            record["blocked_by"] = value.blocked_by
        if value.stall_classification is not None:
            record["stall_classification"] = value.stall_classification
        if value.dispatch_intent_id is not None:
            record["dispatch_intent_id"] = value.dispatch_intent_id
        if value.dispatch_started_at is not None:
            record["dispatch_started_at"] = value.dispatch_started_at
        if value.dispatch_last_boundary is not None:
            record["dispatch_last_boundary"] = value.dispatch_last_boundary
        if value.dispatch_error is not None:
            record["dispatch_error"] = value.dispatch_error
        if value.dispatch_agent_id is not None:
            record["dispatch_agent_id"] = value.dispatch_agent_id
        if value.dispatch_authoritative_event_seq is not None:
            record["dispatch_authoritative_event_seq"] = value.dispatch_authoritative_event_seq
        if value.reconciliation is not None:
            record["reconciliation"] = copy.deepcopy(dict(value.reconciliation))
        if value.task_timeout_seconds is not None:
            record["task_timeout_seconds"] = value.task_timeout_seconds
        if value.task_timeout_source is not None:
            record["task_timeout_source"] = value.task_timeout_source
        return record
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    raise TypeError(f"slice {slice_id!r} is not a SliceState or object")


def _encode_review_findings(
    findings: Mapping[str, tuple[Mapping[str, str], ...]],
) -> dict[str, list[dict[str, str]]]:
    return {
        head_sha: [dict(finding) for finding in values] for head_sha, values in findings.items()
    }


def _encode_budgets(budgets: BudgetInput) -> dict[str, object]:
    if isinstance(budgets, BudgetLedger):
        ledger: dict[str, object] = {
            "tokens": budgets.tokens,
            "wall_seconds": budgets.wall_seconds,
        }
        for key, counter in (
            ("role_spent", budgets.role_spent),
            ("harness_spent", budgets.harness_spent),
            ("role_reserved", budgets.role_reserved),
            ("harness_reserved", budgets.harness_reserved),
        ):
            if counter:
                ledger[key] = dict(counter)
        if budgets.charges:
            ledger["charges"] = [_encode_charge(charge) for charge in budgets.charges]
        return {"ledger": ledger}
    if not isinstance(budgets, Mapping):
        raise TypeError("budgets must be a BudgetLedger or object")
    value: dict[str, object] = {
        key: item for key, item in cast(Mapping[str, object], budgets).items()
    }
    if "ledger" in value:
        return copy.deepcopy(cast(dict[str, object], value))
    return {"ledger": copy.deepcopy(value)}


def _encode_ordered_stages(value: OrderedStagesInput) -> list[dict[str, object]]:
    if isinstance(value, Mapping):
        raw = value.get("stages", value)
        if isinstance(raw, list):
            value = raw
    if not isinstance(value, (list, tuple)):
        raise TypeError("ordered_stages must be an array")
    result: list[dict[str, object]] = []
    for stage in value:
        if isinstance(stage, OrderedStageState):
            result.append({"order": stage.order, "sub_tls": list(stage.sub_tls)})
        elif isinstance(stage, Mapping):
            result.append(copy.deepcopy(dict(stage)))
        else:
            raise TypeError("ordered stage must be an OrderedStageState or object")
    return result


def _encode_integration(value: IntegrationInput) -> dict[str, object]:
    if isinstance(value, IntegrationRuntimeState):
        record: dict[str, object] = {
            "lifecycle": value.lifecycle.value,
            "sub_tl_states": {
                name: lifecycle.value for name, lifecycle in value.sub_tl_states.items()
            },
            "aggregate_pr_number": value.aggregate_pr_number,
            "aggregate_head_sha": value.aggregate_head_sha,
            "aggregate_patch_digest": value.aggregate_patch_digest,
            "aggregate_original_base_sha": value.aggregate_original_base_sha,
            "integration_owner_id": value.integration_owner_id,
            "integration_owner_run_id": value.integration_owner_run_id,
            "integration_owner_branch": value.integration_owner_branch,
            "integration_owner_worktree": value.integration_owner_worktree,
            "head_sha": value.head_sha,
            "patch_digest": value.patch_digest,
            "validated_base_sha": value.validated_base_sha,
            "merge_tree_sha": value.merge_tree_sha,
            "ci_status": value.ci_status,
            "merge_attempts": value.merge_attempts,
            "base_revalidation_count": value.base_revalidation_count,
            "stage_verification": value.stage_verification,
            "candidates": {
                name: _encode_integration_candidate(candidate)
                for name, candidate in value.candidates.items()
            },
        }
        if value.integration_evidence_at is not None:
            record["integration_evidence_at"] = value.integration_evidence_at
        return record
    if not isinstance(value, Mapping):
        raise TypeError("integration must be an IntegrationRuntimeState or object")
    return copy.deepcopy(dict(value))


def _encode_integration_candidate(value: IntegrationCandidateState) -> dict[str, object]:
    return {
        "lifecycle": value.lifecycle.value,
        "aggregate_pr_number": value.aggregate_pr_number,
        "aggregate_head_sha": value.aggregate_head_sha,
        "aggregate_patch_digest": value.aggregate_patch_digest,
        "aggregate_original_base_sha": value.aggregate_original_base_sha,
        "integration_owner_id": value.integration_owner_id,
        "integration_owner_run_id": value.integration_owner_run_id,
        "integration_owner_branch": value.integration_owner_branch,
        "integration_owner_worktree": value.integration_owner_worktree,
        "head_sha": value.head_sha,
        "patch_digest": value.patch_digest,
        "validated_base_sha": value.validated_base_sha,
        "merge_tree_sha": value.merge_tree_sha,
        "integration_evidence_at": value.integration_evidence_at,
        "ci_status": value.ci_status,
        "merge_attempts": value.merge_attempts,
        "base_revalidation_count": value.base_revalidation_count,
        "stage_verification": value.stage_verification,
    }


def _read_bytes(target: Path) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, os.O_RDONLY | nofollow)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _decode(document: dict[str, object]) -> RunState:
    fsm = cast(dict[str, object], document["fsm"])
    raw_slices = cast(dict[str, object], document["slices"])
    budgets = cast(dict[str, object], cast(dict[str, object], document["budgets"])["ledger"])
    raw_gates = cast(list[object], document["gates"])
    events = cast(dict[str, object], document["events"])
    slices = {
        slice_id: _decode_slice(cast(dict[str, object], value))
        for slice_id, value in raw_slices.items()
    }
    return RunState(
        version=cast(int, document["version"]),
        revision=cast(int, document["revision"]),
        run_id=cast(str, document["run_id"]),
        ledger_run_id=cast(str | None, document.get("ledger_run_id")),
        fsm=FSMState(
            phase=TLPhase(cast(str, fsm["phase"])),
            waiting=tuple(cast(list[str], fsm["waiting"])),
        ),
        slices=MappingProxyType(slices),
        budgets=BudgetLedger(
            tokens=cast(int, budgets["tokens"]),
            wall_seconds=cast(int, budgets["wall_seconds"]),
            role_spent=_decode_counter_map(budgets.get("role_spent")),
            harness_spent=_decode_counter_map(budgets.get("harness_spent")),
            role_reserved=_decode_counter_map(budgets.get("role_reserved")),
            harness_reserved=_decode_counter_map(budgets.get("harness_reserved")),
            charges=tuple(
                _decode_charge(cast(dict[str, object], charge))
                for charge in cast(list[object], budgets.get("charges", []))
            ),
        ),
        gates=tuple(_decode_gate(cast(dict[str, object], gate)) for gate in raw_gates),
        events=EventCursor(last_consumed_offset=cast(int, events["last_consumed_offset"])),
        goals=_decode_goals(cast(dict[str, object], document.get("goals", {}))),
        owner_branch=cast(str | None, document.get("owner_branch")),
        owner_worktree=cast(str | None, document.get("owner_worktree")),
        parent_branch=cast(str | None, document.get("parent_branch")),
        parent_run_id=cast(str | None, document.get("parent_run_id")),
        parent_agent_id=cast(str | None, document.get("parent_agent_id")),
        depth=cast(int, document.get("depth", 0)),
        current_order=cast(int, document.get("current_order", 1)),
        ordered_stages=_decode_ordered_stages(document.get("ordered_stages")),
        integration=_decode_integration(document.get("integration")),
    )


def _decode_ordered_stages(value: object) -> tuple[OrderedStageState, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        OrderedStageState(
            order=cast(int, stage["order"]),
            sub_tls=tuple(cast(list[str], stage["sub_tls"])),
        )
        for stage in value
        if isinstance(stage, dict)
    )


def _decode_integration(value: object) -> IntegrationRuntimeState:
    if not isinstance(value, dict):
        return IntegrationRuntimeState()
    raw_states = value.get("sub_tl_states")
    states = (
        {
            name: IntegrationLifecycle(cast(str, lifecycle))
            for name, lifecycle in raw_states.items()
            if isinstance(name, str) and isinstance(lifecycle, str)
        }
        if isinstance(raw_states, dict)
        else {}
    )
    return IntegrationRuntimeState(
        lifecycle=IntegrationLifecycle(cast(str, value.get("lifecycle", "RUNNING"))),
        sub_tl_states=MappingProxyType(states),
        aggregate_pr_number=cast(int | None, value.get("aggregate_pr_number")),
        aggregate_head_sha=cast(str | None, value.get("aggregate_head_sha")),
        aggregate_patch_digest=cast(str | None, value.get("aggregate_patch_digest")),
        aggregate_original_base_sha=cast(str | None, value.get("aggregate_original_base_sha")),
        integration_owner_id=cast(str | None, value.get("integration_owner_id")),
        integration_owner_run_id=cast(str | None, value.get("integration_owner_run_id")),
        integration_owner_branch=cast(str | None, value.get("integration_owner_branch")),
        integration_owner_worktree=cast(str | None, value.get("integration_owner_worktree")),
        head_sha=cast(str | None, value.get("head_sha")),
        patch_digest=cast(str | None, value.get("patch_digest")),
        validated_base_sha=cast(str | None, value.get("validated_base_sha")),
        merge_tree_sha=cast(str | None, value.get("merge_tree_sha")),
        integration_evidence_at=cast(str | None, value.get("integration_evidence_at")),
        ci_status=cast(str, value.get("ci_status", "unknown")),
        merge_attempts=cast(int, value.get("merge_attempts", 0)),
        base_revalidation_count=cast(int, value.get("base_revalidation_count", 0)),
        stage_verification=cast(str, value.get("stage_verification", "pending")),
        candidates=MappingProxyType(
            {
                name: _decode_integration_candidate(cast(dict[str, object], candidate))
                for name, candidate in cast(dict[str, object], value.get("candidates", {})).items()
                if isinstance(name, str) and isinstance(candidate, dict)
            }
        ),
    )


def _decode_integration_candidate(value: dict[str, object]) -> IntegrationCandidateState:
    return IntegrationCandidateState(
        lifecycle=IntegrationLifecycle(cast(str, value.get("lifecycle", "RUNNING"))),
        aggregate_pr_number=cast(int | None, value.get("aggregate_pr_number")),
        aggregate_head_sha=cast(str | None, value.get("aggregate_head_sha")),
        aggregate_patch_digest=cast(str | None, value.get("aggregate_patch_digest")),
        aggregate_original_base_sha=cast(str | None, value.get("aggregate_original_base_sha")),
        integration_owner_id=cast(str | None, value.get("integration_owner_id")),
        integration_owner_run_id=cast(str | None, value.get("integration_owner_run_id")),
        integration_owner_branch=cast(str | None, value.get("integration_owner_branch")),
        integration_owner_worktree=cast(str | None, value.get("integration_owner_worktree")),
        head_sha=cast(str | None, value.get("head_sha")),
        patch_digest=cast(str | None, value.get("patch_digest")),
        validated_base_sha=cast(str | None, value.get("validated_base_sha")),
        merge_tree_sha=cast(str | None, value.get("merge_tree_sha")),
        integration_evidence_at=cast(str | None, value.get("integration_evidence_at")),
        ci_status=cast(str, value.get("ci_status", "unknown")),
        merge_attempts=cast(int, value.get("merge_attempts", 0)),
        base_revalidation_count=cast(int, value.get("base_revalidation_count", 0)),
        stage_verification=cast(str, value.get("stage_verification", "pending")),
    )


def _decode_counter_map(value: object) -> Mapping[str, int]:
    if not isinstance(value, dict):
        return MappingProxyType({})
    return MappingProxyType({key: cast(int, amount) for key, amount in value.items()})


def _encode_goals(goals: GoalState) -> dict[str, object]:
    encoded = {
        "objective": goals.objective,
        "deadline": goals.deadline,
        "completion_predicate": goals.completion_predicate,
        "last_heartbeat_at": goals.last_heartbeat_at,
        "last_progress_at": goals.last_progress_at,
    }
    if goals.controller_started_at is not None:
        encoded["controller_started_at"] = goals.controller_started_at
    if goals.last_authoritative_event_seq is not None:
        encoded["last_authoritative_event_seq"] = goals.last_authoritative_event_seq
    return encoded


def _decode_goals(value: dict[str, object]) -> GoalState:
    return GoalState(
        objective=cast(str, value.get("objective", "")),
        deadline=cast(float, value.get("deadline", 0.0)),
        completion_predicate=cast(str, value.get("completion_predicate", "")),
        last_heartbeat_at=cast(float | None, value.get("last_heartbeat_at")),
        last_progress_at=cast(float | None, value.get("last_progress_at")),
        controller_started_at=cast(float | None, value.get("controller_started_at")),
        last_authoritative_event_seq=cast(
            int | None,
            value.get("last_authoritative_event_seq"),
        ),
    )


def _encode_charge(charge: BudgetCharge) -> dict[str, object]:
    return {
        "slice_id": charge.slice_id,
        "attempt": charge.attempt,
        "role": charge.role,
        "harness": charge.harness,
        "estimated_tokens": charge.estimated_tokens,
        "actual": charge.actual,
        "delta_tokens": charge.delta_tokens,
        "warning": charge.warning,
        "reconciled": charge.reconciled,
    }


def _decode_charge(value: dict[str, object]) -> BudgetCharge:
    return BudgetCharge(
        slice_id=cast(str, value["slice_id"]),
        attempt=cast(int, value["attempt"]),
        role=cast(str, value["role"]),
        harness=cast(str, value["harness"]),
        estimated_tokens=cast(int, value["estimated_tokens"]),
        actual=cast(ActualTokens, value["actual"]),
        delta_tokens=cast(int | None, value["delta_tokens"]),
        warning=cast(bool, value["warning"]),
        reconciled=cast(bool, value["reconciled"]),
    )


def _decode_review_findings(
    value: object,
) -> Mapping[str, tuple[Mapping[str, str], ...]]:
    if not isinstance(value, dict):
        return MappingProxyType({})
    result: dict[str, tuple[Mapping[str, str], ...]] = {}
    for head_sha, raw_findings in value.items():
        if not isinstance(head_sha, str) or not isinstance(raw_findings, list):
            continue
        result[head_sha] = tuple(
            MappingProxyType(copy.deepcopy(dict(finding)))
            for finding in raw_findings
            if isinstance(finding, dict)
        )
    return MappingProxyType(result)


def _decode_string_map(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict):
        return MappingProxyType({})
    return MappingProxyType(
        {key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)}
    )


def _decode_int_map(value: object) -> Mapping[str, int]:
    if not isinstance(value, dict):
        return MappingProxyType({})
    return MappingProxyType(
        {key: item for key, item in value.items() if isinstance(key, str) and type(item) is int}
    )


def _decode_slice(value: dict[str, object]) -> SliceState:
    return SliceState(
        id=cast(str, value["id"]),
        status=SliceStatus(cast(str, value["status"])),
        paths=tuple(cast(list[str], value["paths"])),
        depends_on=tuple(cast(list[str], value["depends_on"])),
        base_ref=cast(str | None, value["base_ref"]),
        test_plan=tuple(cast(list[str], value["test_plan"])),
        agent_type=cast(str | None, value["agent_type"]),
        model=cast(str | None, value["model"]),
        branch=cast(str | None, value["branch"]),
        worktree=cast(str | None, value["worktree"]),
        pr_number=cast(int | None, value["pr_number"]),
        reviewed_head=cast(str | None, value["reviewed_head"]),
        review_findings=_decode_review_findings(value.get("review_findings")),
        review_patch_digests=_decode_string_map(value.get("review_patch_digests")),
        ci_state=_decode_string_map(value.get("ci_state")),
        reviewer_attempt=_decode_int_map(value.get("reviewer_attempt")),
        repair_attempts=cast(int, value.get("repair_attempts", 0)),
        attempts=cast(int, value["attempts"]),
        verdict=Verdict(cast(str, value["verdict"])) if value["verdict"] is not None else None,
        verdict_at=cast(str | None, value.get("verdict_at")),
        park_cause=(
            ParkCause(cast(str, value["park_cause"]))
            if value.get("park_cause") is not None
            else None
        ),
        park_issue_id=cast(int | None, value.get("park_issue_id")),
        park_audit=(
            MappingProxyType(copy.deepcopy(cast(dict[str, object], value["park_audit"])))
            if isinstance(value.get("park_audit"), dict)
            else None
        ),
        blocked_by=cast(str | None, value.get("blocked_by")),
        stall_classification=cast(str | None, value.get("stall_classification")),
        dispatch_intent_id=cast(str | None, value.get("dispatch_intent_id")),
        dispatch_started_at=cast(float | None, value.get("dispatch_started_at")),
        dispatch_last_boundary=cast(str | None, value.get("dispatch_last_boundary")),
        dispatch_error=cast(str | None, value.get("dispatch_error")),
        dispatch_agent_id=cast(str | None, value.get("dispatch_agent_id")),
        dispatch_authoritative_event_seq=cast(
            int | None, value.get("dispatch_authoritative_event_seq")
        ),
        reconciliation=(
            MappingProxyType(copy.deepcopy(cast(dict[str, object], value["reconciliation"])))
            if isinstance(value.get("reconciliation"), dict)
            else None
        ),
        task_timeout_seconds=cast(float | None, value.get("task_timeout_seconds")),
        task_timeout_source=cast(str | None, value.get("task_timeout_source")),
    )


def _decode_gate(value: dict[str, object]) -> GateState:
    return GateState(name=cast(str, value["name"]), status=GateStatus(cast(str, value["status"])))


def _assert_consistent(path: Path, state: RunState) -> None:
    _assert_fsm_slices_consistent(path, state.fsm.phase, state.fsm.waiting, state.slices)


def _assert_encoded_consistent(
    path: Path,
    fsm: Mapping[str, object],
    slices: Mapping[str, object],
) -> None:
    phase = TLPhase(cast(str, fsm["phase"]))
    waiting = tuple(cast(list[str], fsm["waiting"]))
    statuses = {
        slice_id: value.get("status")
        for slice_id, value in slices.items()
        if isinstance(value, dict)
    }
    _assert_fsm_slices_consistent(path, phase, waiting, statuses)


def _assert_fsm_slices_consistent(
    path: Path,
    phase: TLPhase,
    waiting_ids: tuple[str, ...],
    slices: Mapping[str, SliceState | object],
) -> None:
    waiting = set(waiting_ids)
    active_statuses = {
        status.value
        for status in (SliceStatus.SPAWNED, SliceStatus.IN_REVIEW, SliceStatus.REPAIRING)
    }
    for slice_id in waiting_ids:
        value = slices.get(slice_id)
        status = value.status if isinstance(value, SliceState) else value
        if status not in active_statuses and status not in {item.value for item in SliceStatus}:
            continue
        if status not in active_statuses:
            status_value = status.value if isinstance(status, SliceStatus) else status
            raise CorruptCheckpoint(
                f"{path}: waiting set is inconsistent: slice {slice_id!r} has {status_value!r} status"
            )
    phases_without_waiting = {
        TLPhase.TLPlanning,
        TLPhase.TLAllMerged,
        TLPhase.TLPRFiled,
        TLPhase.TLDone,
        TLPhase.TLFailed,
    }
    if phase in phases_without_waiting and waiting:
        raise CorruptCheckpoint(
            f"{path}: phase {phase.value!r} is inconsistent with waiting IDs {sorted(waiting)!r}"
        )
    if phase in {TLPhase.TLWaiting, TLPhase.TLMerging} and not waiting:
        raise CorruptCheckpoint(
            f"{path}: phase {phase.value!r} requires at least one waiting slice"
        )


def _read_existing_state(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(_read_bytes(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _normalize_worktree(value: str) -> str:
    return str(Path(value).expanduser().resolve())


__all__ = [
    "DEFAULT_ROOT",
    "CorruptCheckpoint",
    "ResumeState",
    "RunStore",
    "WorktreeClaimError",
    "checkpoint",
    "create",
    "load",
    "resume",
]
