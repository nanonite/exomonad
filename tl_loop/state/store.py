"""Typed creation, checkpoint, and resume operations for TL runs."""

from __future__ import annotations

import copy
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias, cast

from tl_loop.fsm.child import ChildKind, ChildRecord
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
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.recovery import decode_recovery, encode_recovery
from tl_loop.fsm.scope import (
    TLAllMerged as RecursiveTLAllMerged,
)
from tl_loop.fsm.scope import (
    TLDone as RecursiveTLDone,
)
from tl_loop.fsm.scope import (
    TLFailed as RecursiveTLFailed,
)
from tl_loop.fsm.scope import (
    TLFinalizing as RecursiveTLFinalizing,
)
from tl_loop.fsm.scope import (
    TLParked as RecursiveTLParked,
)
from tl_loop.fsm.scope import (
    TLPlanning as RecursiveTLPlanning,
)
from tl_loop.fsm.scope import (
    TLPRFiled as RecursiveTLPRFiled,
)
from tl_loop.fsm.scope import (
    TLRunning as RecursiveTLRunning,
)
from tl_loop.fsm.scope_events import ScopeRole
from tl_loop.fsm.scope_projection import phase_tag as canonical_phase_tag
from tl_loop.ordered import ChildRecoverySummary, IntegrationLifecycle, SubTLLifecycle

from .lock import RunLock
from .migration import (
    MigrationError,
    install_migration,
    migrate_checkpoint_document,
    record_migration_failure,
)
from .plan_manifest import (
    ManifestError,
    ManifestNode,
    PlanManifest,
    build_legacy_manifest,
    validate_manifest_revision,
)
from .schema import (
    REDUCER_VERSION,
    SCHEMA_VERSION,
    ActionKind,
    ActionPhase,
    ActionState,
    ActualTokens,
    BudgetCharge,
    BudgetLedger,
    DeadlineLedger,
    DurableReviewEvidence,
    EventCursor,
    FSMState,
    GateState,
    GateStatus,
    GoalState,
    HandoffEvidence,
    IntegrationCandidateState,
    IntegrationRuntimeState,
    ObservationProvenance,
    OrderedStageState,
    ParkCause,
    PublicationBinding,
    RepositoryIdentity,
    ReviewPolicySource,
    ReviewValidationDisposition,
    RunState,
    SchemaError,
    SessionMode,
    SliceMap,
    SliceState,
    SliceStatus,
    SuspendedDependencyState,
    Verdict,
    validate,
)
from .serialization import dumps as dumps_json
from .serialization import to_jsonable
from .write import apply

DEFAULT_ROOT = Path(".exo/tl-loop")
RootSpec: TypeAlias = Mapping[str, object]
SliceInput: TypeAlias = SliceState | Mapping[str, object]
FSMInput: TypeAlias = (
    FSMState
    | PhaseValue
    | TLPhase
    | RecursiveTLPlanning
    | RecursiveTLRunning
    | RecursiveTLAllMerged
    | RecursiveTLFinalizing
    | RecursiveTLDone
    | RecursiveTLPRFiled
    | RecursiveTLFailed
    | RecursiveTLParked
)
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
    repository_identity: RepositoryIdentity | None = None
    state_version: int = 0
    reducer_version: int = REDUCER_VERSION
    # Kept as one snapshot so restart cannot silently change policy precedence.
    reviewer_max_rounds: int | None = None
    reviewer_max_rounds_source: ReviewPolicySource | None = None
    session_mode: SessionMode | None = None
    plan_manifest: PlanManifest | None = None
    recursive_fsm: object | None = None

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
        state_version: int | None = None,
        plan_manifest: PlanManifest | None = None,
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
            state_version=state_version,
            plan_manifest=plan_manifest,
        )

    def set_plan_manifest(
        self,
        manifest: PlanManifest,
        *,
        slices: Mapping[str, SliceInput] | None = None,
        protected_node_ids: set[str] | frozenset[str] = frozenset(),
    ) -> RunState:
        """Install the immutable continuation manifest and bind direct slices."""
        if not isinstance(manifest, PlanManifest):
            raise TypeError("manifest must be a PlanManifest")
        encoded = manifest.to_document()

        def mutate(document: dict[str, object]) -> dict[str, object]:
            existing = document.get("plan_manifest")
            replace_node_binding = False
            if isinstance(existing, dict):
                previous = PlanManifest.from_document(existing)
                if previous.digest != manifest.digest:
                    if _is_legacy_manifest(previous):
                        replace_node_binding = True
                    else:
                        validate_manifest_revision(
                            previous,
                            manifest,
                            protected_node_ids=protected_node_ids,
                        )
            document["plan_manifest"] = copy.deepcopy(encoded)
            encoded_slices = (
                _encode_slices(slices)
                if slices is not None
                else _activate_manifest_nodes(document.get("slices"), manifest)
            )
            document["slices"] = encoded_slices
            _bind_slice_records(
                encoded_slices,
                manifest,
                replace_revision=True,
                replace_node_binding=replace_node_binding,
            )
            return document

        apply(self.run_dir, mutate)
        return self.load()

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

    def set_controller_epoch(self, epoch: str) -> RunState:
        """Persist the controller lifecycle identity without touching the plan."""
        if not isinstance(epoch, str) or not epoch:
            raise ValueError("controller epoch must be a non-empty string")

        def mutate(document: dict[str, object]) -> dict[str, object]:
            document["controller_epoch"] = epoch
            return document

        apply(self.run_dir, mutate)
        return self.load()

    def set_review_policy(self, ceiling: int | None, source: str) -> RunState:
        """Persist the resolved review ceiling before processing events."""
        if ceiling is not None and (type(ceiling) is not int or ceiling < 1):
            raise ValueError("review ceiling must be a positive integer or null")
        try:
            canonical_source = ReviewPolicySource(source)
        except (TypeError, ValueError) as error:
            raise ValueError("review policy source is not recognised") from error
        if canonical_source is ReviewPolicySource.DISABLED and ceiling is not None:
            raise ValueError("disabled review policy must have a null ceiling")
        if canonical_source is not ReviewPolicySource.DISABLED and ceiling is None:
            raise ValueError("enabled review policy must have a positive ceiling")

        def mutate(document: dict[str, object]) -> dict[str, object]:
            document["reviewer_max_rounds"] = ceiling
            document["reviewer_max_rounds_source"] = canonical_source.value
            return document

        apply(self.run_dir, mutate)
        return self.load()

    def set_session_mode(self, mode: str | SessionMode) -> RunState:
        """Persist the host-selected lifecycle mode on the run checkpoint."""
        try:
            canonical_mode = mode if isinstance(mode, SessionMode) else SessionMode(mode)
        except (TypeError, ValueError) as error:
            raise ValueError("session mode is not recognised") from error

        def mutate(document: dict[str, object]) -> dict[str, object]:
            document["session_mode"] = canonical_mode.value
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
        normalized = to_jsonable(event)
        if not isinstance(normalized, dict):
            raise QuarantineStorageError(self.event_quarantine_path, "event must be an object")
        entries.append(normalized)
        temporary = self.event_quarantine_path.with_suffix(".tmp")
        temporary.write_text(dumps_json(entries, sort_keys=True), encoding="utf-8")
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
            temporary.write_text(dumps_json(entries, sort_keys=True), encoding="utf-8")
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
        temporary.write_text(dumps_json(payload, sort_keys=True), encoding="utf-8")
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
        temporary.write_text(dumps_json(payload, sort_keys=True), encoding="utf-8")
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

    def set_state_version(self, state_version: int) -> RunState:
        """Persist a convergence transition version monotonically."""
        if type(state_version) is not int or state_version < 0:
            raise ValueError("state_version must be a non-negative integer")

        def mutate(document: dict[str, object]) -> dict[str, object]:
            current = document.get("state_version", 0)
            if type(current) is not int or state_version < current:
                raise ValueError("state_version must not regress")
            document["state_version"] = state_version
            return document

        apply(self.run_dir, mutate)
        return self.load()

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
            # Migration is a read/transform/write transaction.  A controller
            # and ledger tailer can discover the same legacy checkpoint at
            # nearly the same time, so the first transform must be discarded
            # after taking the lock and the live file must be read again.
            with RunLock(target.with_name("migration.lock")):
                locked_data = _read_bytes(target)
                locked_value = json.loads(locked_data.decode("utf-8"))
                if not isinstance(locked_value, dict):
                    raise MigrationError("checkpoint must contain a JSON object")
                migration = migrate_checkpoint_document(
                    locked_value,
                    run_id=target.parent.name,
                )
                if migration.migrated:
                    validate(migration.document)
                    migrated_state = _decode(migration.document)
                    _assert_consistent(target, migrated_state)
                    install_migration(target, migration)
                    value = migration.document
                else:
                    value = locked_value
        except (OSError, TimeoutError, SchemaError, CorruptCheckpoint, MigrationError) as error:
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
    state_version: int | None = None,
    plan_manifest: PlanManifest | None = None,
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
    encoded_manifest = plan_manifest.to_document() if plan_manifest is not None else None
    if current_order is not None and (type(current_order) is not int or current_order <= 0):
        raise ValueError("current_order must be a positive integer")
    if state_version is not None and (type(state_version) is not int or state_version < 0):
        raise ValueError("state_version must be a non-negative integer")
    _assert_encoded_consistent(run_directory / "run.json", encoded_fsm, encoded_slices)

    def mutate(document: dict[str, object]) -> dict[str, object]:
        existing_fsm = document.get("fsm")
        legacy_terminal = isinstance(fsm, (TLDone, TLPRFiled, TLFailed))
        if (
            isinstance(existing_fsm, Mapping)
            and existing_fsm.get("kind") == "recursive"
            and not isinstance(
                fsm,
                (
                    RecursiveTLPlanning,
                    RecursiveTLRunning,
                    RecursiveTLAllMerged,
                    RecursiveTLFinalizing,
                    RecursiveTLDone,
                    RecursiveTLPRFiled,
                    RecursiveTLFailed,
                    RecursiveTLParked,
                ),
            )
            and not legacy_terminal
        ):
            # The compact phase/waiting fields are a compatibility projection.
            # Once a canonical scope FSM exists, slice-only checkpoints retain it.
            document["fsm"] = copy.deepcopy(existing_fsm)
        else:
            document["fsm"] = copy.deepcopy(encoded_fsm)
        document["slices"] = copy.deepcopy(encoded_slices)
        if encoded_manifest is not None:
            document["plan_manifest"] = copy.deepcopy(encoded_manifest)
        if isinstance(document.get("plan_manifest"), dict):
            manifest = PlanManifest.from_document(document["plan_manifest"])
            if any(
                slice_id not in {node.name for node in manifest.nodes}
                for slice_id in encoded_slices
            ):
                manifest = _extend_legacy_manifest(manifest, document)
                document["plan_manifest"] = manifest.to_document()
            _bind_slice_records(document["slices"], manifest)
        document["budgets"] = copy.deepcopy(encoded_budgets)
        document["events"] = {"last_consumed_offset": offset}
        document.setdefault("reducer_version", REDUCER_VERSION)
        if current_order is not None:
            document["current_order"] = current_order
        if encoded_stages is not None:
            document["ordered_stages"] = copy.deepcopy(encoded_stages)
        if encoded_integration is not None:
            document["integration"] = copy.deepcopy(encoded_integration)
        if state_version is not None:
            current_version = document.get("state_version", 0)
            if type(current_version) is not int or state_version < current_version:
                raise ValueError("state_version must not regress")
            document["state_version"] = state_version
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
        repository_identity=state.repository_identity,
        state_version=state.state_version,
        reducer_version=state.reducer_version,
        reviewer_max_rounds=state.reviewer_max_rounds,
        reviewer_max_rounds_source=state.reviewer_max_rounds_source,
        session_mode=state.session_mode,
        plan_manifest=state.plan_manifest,
        recursive_fsm=state.recursive_fsm,
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
        "repository_identity",
        "state_version",
        "reducer_version",
        "controller_epoch",
        "session_mode",
        "reviewer_max_rounds",
        "reviewer_max_rounds_source",
        "plan_manifest",
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
        "reducer_version": REDUCER_VERSION,
    }
    if "session_mode" in root_spec:
        document["session_mode"] = root_spec["session_mode"]
    for key, value in root_spec.items():
        if key == "fsm" and not isinstance(value, Mapping):
            document[key] = _encode_fsm(value)
        else:
            document[key] = copy.deepcopy(value)
    if isinstance(document.get("plan_manifest"), dict):
        manifest = PlanManifest.from_document(document["plan_manifest"])
        document["slices"] = _activate_manifest_nodes(document.get("slices"), manifest)
        _bind_slice_records(document.get("slices"), manifest)
    else:
        manifest = build_legacy_manifest(document, run_id=run_id)
        document["plan_manifest"] = manifest.to_document()
        _bind_slice_records(document["slices"], manifest)
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
    if isinstance(
        fsm,
        (
            RecursiveTLPlanning,
            RecursiveTLRunning,
            RecursiveTLAllMerged,
            RecursiveTLFinalizing,
            RecursiveTLDone,
            RecursiveTLPRFiled,
            RecursiveTLFailed,
            RecursiveTLParked,
        ),
    ):
        return _encode_recursive_fsm(fsm)
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


def _encode_recursive_fsm(fsm: object) -> dict[str, object]:
    """Encode the complete #1048 scope value behind a stable discriminator."""
    if isinstance(fsm, RecursiveTLPlanning):
        payload = {
            "kind": "tl_planning",
            "scope_path": list(fsm.scope_path),
            "plan_digest": fsm.plan_digest,
            "parallel_children": _encode_child_records(fsm.parallel_children),
            "ordered_children": [
                {"order": order, "children": _encode_child_records(children)}
                for order, children in fsm.ordered_children
            ],
        }
        return {
            "kind": "recursive",
            "phase": TLPhase.TLPlanning.value,
            "waiting": [],
            "payload": payload,
        }
    if isinstance(fsm, RecursiveTLRunning):
        waiting = [record.child_id for record in fsm.parallel_pending] + [
            record.child_id
            for _, records in sorted(fsm.pending_by_order.items())
            for record in records
        ]
        payload = {
            "kind": "tl_running",
            "current_order": fsm.current_order,
            "pending_by_order": {
                str(order): _encode_child_records(records)
                for order, records in sorted(fsm.pending_by_order.items())
            },
            "scope_path": list(fsm.scope_path),
            "plan_digest": fsm.plan_digest,
            "parallel_pending": _encode_child_records(fsm.parallel_pending),
            "completed_children": {
                child_id: _encode_child_record(record)
                for child_id, record in fsm.completed_children.items()
            },
            "post_merge": {
                child_id: _encode_post_merge_state(state)
                for child_id, state in fsm.post_merge.items()
            },
            "dispatch_intents": dict(fsm.dispatch_intents),
            "evidence": dict(fsm.evidence),
            "lane_bindings": dict(fsm.lane_bindings),
        }
        return {
            "kind": "recursive",
            "phase": TLPhase.TLRunning.value,
            "waiting": waiting,
            "payload": payload,
        }
    if isinstance(fsm, RecursiveTLAllMerged):
        payload = {
            "kind": "tl_all_merged",
            "scope_path": list(fsm.scope_path),
            "plan_digest": fsm.plan_digest,
            "completed_children": {
                child_id: _encode_post_merge_state(state)
                for child_id, state in fsm.completed_children.items()
            },
        }
        return {
            "kind": "recursive",
            "phase": TLPhase.TLAllMerged.value,
            "waiting": [],
            "payload": payload,
        }
    if isinstance(fsm, RecursiveTLFinalizing):
        payload = {
            "kind": "tl_finalizing",
            "role": fsm.role.value,
            "scope_path": list(fsm.scope_path),
            "plan_digest": fsm.plan_digest,
            "evidence": dict(fsm.evidence),
        }
        return {
            "kind": "recursive",
            "phase": TLPhase.TLFinalizing.value,
            "waiting": [],
            "payload": payload,
        }
    if isinstance(fsm, RecursiveTLDone):
        payload = {
            "kind": "tl_done",
            "scope_path": list(fsm.scope_path),
            "plan_digest": fsm.plan_digest,
            "finalization_evidence": dict(fsm.finalization_evidence),
        }
        return {
            "kind": "recursive",
            "phase": TLPhase.TLDone.value,
            "waiting": [],
            "payload": payload,
        }
    if isinstance(fsm, RecursiveTLPRFiled):
        payload = {
            "kind": "tl_pr_filed",
            "aggregate_pr": fsm.aggregate_pr,
            "head_sha": fsm.head_sha,
            "base_sha": fsm.base_sha,
            "parent_branch": fsm.parent_branch,
            "handoff": fsm.handoff,
            "scope_path": list(fsm.scope_path),
            "plan_digest": fsm.plan_digest,
        }
        return {
            "kind": "recursive",
            "phase": TLPhase.TLPRFiled.value,
            "waiting": [],
            "payload": payload,
        }
    if isinstance(fsm, RecursiveTLFailed):
        payload = {
            "kind": "tl_failed",
            "reason": fsm.reason,
            "scope_path": list(fsm.scope_path),
            "last_evidence": dict(fsm.last_evidence),
            "next_transition": fsm.next_transition,
        }
        return {
            "kind": "recursive",
            "phase": TLPhase.TLFailed.value,
            "waiting": [],
            "payload": payload,
        }
    if isinstance(fsm, RecursiveTLParked):
        payload = {
            "kind": "tl_parked",
            "cause": fsm.cause,
            "diagnostic": fsm.diagnostic,
            "scope_path": list(fsm.scope_path),
            "next_transition": fsm.next_transition,
        }
        return {
            "kind": "recursive",
            "phase": TLPhase.TLParked.value,
            "waiting": [],
            "payload": payload,
        }
    raise TypeError(f"unsupported recursive FSM value: {type(fsm).__name__}")


def _encode_child_records(records: object) -> list[dict[str, object]]:
    if not isinstance(records, (tuple, list)):
        raise TypeError("recursive child records must be a sequence")
    return [_encode_child_record(record) for record in records]


def _encode_child_record(record: ChildRecord) -> dict[str, object]:
    return {
        "child_id": record.child_id,
        "kind": record.kind.value,
        "dispatch_intent_id": record.dispatch_intent_id,
        "invocation_id": record.invocation_id,
        "evidence": dict(record.evidence),
        "lane_id": record.lane_id,
        "manifest_node_id": record.manifest_node_id,
        "manifest_revision": record.manifest_revision,
    }


def _encode_post_merge_state(state: PostMergeState) -> dict[str, object]:
    return {"phase": state.phase.value, "evidence": dict(state.evidence)}


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
        if value.review_rounds:
            record["review_rounds"] = value.review_rounds
        if value.reviewer_agent_id is not None:
            record["reviewer_agent_id"] = value.reviewer_agent_id
        if value.review_patch_digests:
            record["review_patch_digests"] = dict(value.review_patch_digests)
        if value.review_contract is not None:
            record["review_contract"] = copy.deepcopy(dict(value.review_contract))
        if value.verdict_at is not None:
            record["verdict_at"] = value.verdict_at
        if value.review_evidence is not None:
            record["review_evidence"] = _encode_review_evidence(value.review_evidence)
        if value.review_validation_required:
            record["review_validation_required"] = True
        if value.review_validation_disposition is not None:
            record["review_validation_disposition"] = value.review_validation_disposition.value
        if value.review_validation_failure_reason is not None:
            record["review_validation_failure_reason"] = value.review_validation_failure_reason
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
        if value.dispatch_invocation_id is not None:
            record["dispatch_invocation_id"] = value.dispatch_invocation_id
        if value.dispatch_authoritative_event_seq is not None:
            record["dispatch_authoritative_event_seq"] = value.dispatch_authoritative_event_seq
        if value.dispatch_generation:
            record["dispatch_generation"] = value.dispatch_generation
        if value.reconciliation is not None:
            record["reconciliation"] = copy.deepcopy(dict(value.reconciliation))
        if value.task_timeout_seconds is not None:
            record["task_timeout_seconds"] = value.task_timeout_seconds
        if value.task_timeout_source is not None:
            record["task_timeout_source"] = value.task_timeout_source
        if value.recovery is not None:
            record["recovery"] = encode_recovery(value.recovery)
        if value.suspended_dependency is not None:
            record["suspended_dependency"] = {
                "blocked_by": value.suspended_dependency.blocked_by,
                "prior_status": value.suspended_dependency.prior_status.value,
                "recovery_generation": value.suspended_dependency.recovery_generation,
            }
        if value.deadline_ledger is not None:
            record["deadline_ledger"] = {
                "execution_deadline_at": value.deadline_ledger.execution_deadline_at,
                "recovery_deadline_at": value.deadline_ledger.recovery_deadline_at,
                "run_deadline_at": value.deadline_ledger.run_deadline_at,
                "suspended_at": value.deadline_ledger.suspended_at,
                "execution_seconds": value.deadline_ledger.execution_seconds,
                "recovery_wait_seconds": value.deadline_ledger.recovery_wait_seconds,
            }
        if value.publication is not None:
            record["publication"] = _encode_publication(value.publication)
        if value.handoff is not None:
            record["handoff"] = _encode_handoff(value.handoff)
        if value.observation_provenance is not None:
            record["observation_provenance"] = _encode_observation_provenance(
                value.observation_provenance
            )
        if value.action is not None:
            record["action"] = _encode_action(value.action)
        if value.post_merge is not None:
            record["post_merge"] = _encode_post_merge_state(value.post_merge)
        if value.manifest_node_id is not None:
            record["manifest_node_id"] = value.manifest_node_id
        if value.manifest_revision is not None:
            record["manifest_revision"] = value.manifest_revision
        return record
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    raise TypeError(f"slice {slice_id!r} is not a SliceState or object")


def _bind_slice_records(
    value: object,
    manifest: PlanManifest,
    *,
    replace_revision: bool = False,
    replace_node_binding: bool = False,
) -> None:
    """Bind each persisted direct slice to one immutable manifest node."""
    if not isinstance(value, dict):
        raise ManifestError("manifest-backed slices must be an object")
    by_name: dict[str, str] = {node.name: node.node_id for node in manifest.nodes}
    if len(by_name) != len(manifest.nodes):
        raise ManifestError("manifest node names must be unique within a scope")
    for slice_id, raw in value.items():
        if not isinstance(raw, dict):
            raise ManifestError(f"slice {slice_id!r} must be an object")
        node_id = by_name.get(slice_id)
        if node_id is None:
            raise ManifestError(f"slice {slice_id!r} is not declared by the plan manifest")
        prior_id = raw.get("manifest_node_id")
        prior_revision = raw.get("manifest_revision")
        if not replace_node_binding and prior_id is not None and prior_id != node_id:
            raise ManifestError(f"slice {slice_id!r} is bound to a different manifest node")
        if (
            not replace_revision
            and prior_revision is not None
            and prior_revision != manifest.manifest_revision
        ):
            raise ManifestError(f"slice {slice_id!r} has a stale manifest revision")
        raw["manifest_node_id"] = node_id
        raw["manifest_revision"] = manifest.manifest_revision


def _activate_manifest_nodes(value: object, manifest: PlanManifest) -> dict[str, object]:
    """Retain runtime records and create safe pending records for new nodes."""
    if not isinstance(value, dict):
        raise ManifestError("manifest-backed slices must be an object")
    activated = copy.deepcopy(value)
    existing = set(activated)
    for node in manifest.nodes:
        if node.name not in existing:
            activated[node.name] = _pending_manifest_slice(node)
    return activated


def _is_legacy_manifest(manifest: PlanManifest) -> bool:
    return manifest.role == "root" and manifest.owned_branch == "legacy"


def _extend_legacy_manifest(manifest: PlanManifest, document: Mapping[str, object]) -> PlanManifest:
    """Keep legacy callers usable while making newly persisted slices explicit."""
    if manifest.owned_branch != "legacy" or manifest.role != "root":
        raise ManifestError("checkpoint contains a slice absent from its immutable plan manifest")
    expanded = build_legacy_manifest(document, run_id=manifest.scope_id)
    expanded = replace(expanded, manifest_revision=manifest.manifest_revision + 1, digest=None)
    slices = document.get("slices")
    protected = {
        node.node_id
        for node in manifest.nodes
        if isinstance(slices, Mapping)
        and isinstance(slices.get(node.name), Mapping)
        and slices[node.name].get("status")
        not in {SliceStatus.PENDING.value, SliceStatus.READY.value}
    }
    validate_manifest_revision(manifest, expanded, protected_node_ids=protected)
    return expanded


def _pending_manifest_slice(node: ManifestNode) -> dict[str, object]:
    """Build the inert runtime record for a newly declared manifest node."""
    return {
        "id": node.name,
        "status": SliceStatus.PENDING.value,
        "paths": list(node.boundary or (f"tl-loop/{node.name}",)),
        "depends_on": [],
        "base_ref": None,
        "test_plan": ["controller"],
        "agent_type": node.agent_type,
        "model": None,
        "branch": node.owned_branch,
        "worktree": node.worktree,
        "pr_number": None,
        "review_findings": {},
        "ci_state": {},
        "reviewer_attempt": {},
        "repair_attempts": 0,
        "reviewed_head": None,
        "attempts": 0,
        "verdict": None,
    }


def _encode_review_findings(
    findings: Mapping[str, tuple[Mapping[str, str], ...]],
) -> dict[str, list[dict[str, str]]]:
    return {
        head_sha: [dict(finding) for finding in values] for head_sha, values in findings.items()
    }


def _encode_publication(value: PublicationBinding) -> dict[str, object]:
    return {
        "pr_number": value.pr_number,
        "head_sha": value.head_sha,
        "head_branch": value.head_branch,
        "base_branch": value.base_branch,
        "attempt": value.attempt,
        "invocation_id": value.invocation_id,
    }


def _encode_handoff(value: HandoffEvidence) -> dict[str, object]:
    return {
        "pr_number": value.pr_number,
        "head_sha": value.head_sha,
        "attempt": value.attempt,
        "invocation_id": value.invocation_id,
        "agent_id": value.agent_id,
        "observed_at": value.observed_at,
    }


def _encode_observation_provenance(value: ObservationProvenance) -> dict[str, object]:
    return {
        "source": value.source,
        "observed_at": value.observed_at,
        "event_seq": value.event_seq,
        "snapshot_id": value.snapshot_id,
        "ledger_run_seq": value.ledger_run_seq,
        "snapshot_high_watermark": value.snapshot_high_watermark,
        "source_epoch": value.source_epoch,
        "source_revision": value.source_revision,
        "coverage": list(value.coverage),
    }


def _encode_review_evidence(value: DurableReviewEvidence) -> dict[str, object]:
    return {
        "review_id": value.review_id,
        "pr_number": value.pr_number,
        "head_sha": value.head_sha,
        "reviewer_agent_id": value.reviewer_agent_id,
        "verdict": value.verdict.value,
        "submitted_at": value.submitted_at,
        "validated_at": value.validated_at,
        "reviewer_account_authenticated": value.reviewer_account_authenticated,
        "dismissed": value.dismissed,
        "forgejo_stale": value.forgejo_stale,
        "reviewer_identity_unresolved": value.reviewer_identity_unresolved,
    }


def _encode_action(value: ActionState) -> dict[str, object]:
    record: dict[str, object] = {
        "kind": value.kind.value,
        "phase": value.phase.value,
        "state_version": value.state_version,
        "intent_id": value.intent_id,
        "head_sha": value.head_sha,
        "attempt": value.attempt,
    }
    if value.contract_digest is not None:
        record["contract_digest"] = value.contract_digest
    return record


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
            "sub_tl_recovery": {
                name: _encode_child_recovery(summary)
                for name, summary in value.sub_tl_recovery.items()
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
    recursive_fsm = _decode_recursive_fsm(fsm) if fsm.get("kind") == "recursive" else None
    integration = _decode_integration(document.get("integration"))
    phase = TLPhase(cast(str, fsm["phase"]))
    if recursive_fsm is not None:
        phase = canonical_phase_tag(recursive_fsm)
        if isinstance(recursive_fsm, RecursiveTLRunning):
            raw_integration = document.get("integration")
            has_candidates = (
                isinstance(raw_integration, Mapping)
                and isinstance(raw_integration.get("candidates"), Mapping)
                and any(
                    isinstance(candidate, Mapping)
                    and any(
                        candidate.get(key)
                        for key in ("aggregate_pr_number", "aggregate_head_sha", "head_sha")
                    )
                    for candidate in raw_integration["candidates"].values()
                )
            )
            lifecycle = (
                raw_integration.get("lifecycle") if isinstance(raw_integration, Mapping) else None
            )
            has_recovery = (
                isinstance(raw_integration, Mapping)
                and isinstance(raw_integration.get("sub_tl_recovery"), Mapping)
                and bool(raw_integration["sub_tl_recovery"])
            )
            has_reviewing_slice = any(
                isinstance(value, Mapping)
                and value.get("status")
                in {SliceStatus.IN_REVIEW.value, SliceStatus.REPAIRING.value}
                for value in raw_slices.values()
            )
            if (
                not has_candidates
                and not has_reviewing_slice
                and (lifecycle == IntegrationLifecycle.RUNNING.value or has_recovery)
            ):
                phase = TLPhase.TLRunning
    return RunState(
        version=cast(int, document["version"]),
        revision=cast(int, document["revision"]),
        run_id=cast(str, document["run_id"]),
        ledger_run_id=cast(str | None, document.get("ledger_run_id")),
        fsm=FSMState(
            phase=phase,
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
        integration=integration,
        repository_identity=_decode_repository_identity(document.get("repository_identity")),
        state_version=cast(int, document.get("state_version", 0)),
        reducer_version=cast(int, document.get("reducer_version", REDUCER_VERSION)),
        controller_epoch=cast(str | None, document.get("controller_epoch")),
        reviewer_max_rounds=cast(int | None, document.get("reviewer_max_rounds")),
        reviewer_max_rounds_source=(
            ReviewPolicySource(cast(str, document["reviewer_max_rounds_source"]))
            if document.get("reviewer_max_rounds_source") is not None
            else None
        ),
        session_mode=(
            SessionMode(cast(str, document["session_mode"]))
            if document.get("session_mode") is not None
            else None
        ),
        plan_manifest=(
            PlanManifest.from_document(cast(dict[str, object], document["plan_manifest"]))
            if document.get("plan_manifest") is not None
            else None
        ),
        recursive_fsm=recursive_fsm,
    )


def _decode_recursive_fsm(value: Mapping[str, object]) -> object:
    """Decode the complete target scope value retained beside legacy fields."""
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        raise TypeError("recursive FSM payload must be an object")
    kind = _recursive_text(payload, "kind")
    scope_path = _optional_recursive_text_list(payload, "scope_path", ("root",))
    if kind == "tl_failed":
        return RecursiveTLFailed(
            reason=_recursive_text(payload, "reason"),
            scope_path=scope_path,
            last_evidence=_optional_recursive_string_map(payload, "last_evidence"),
            next_transition=_optional_recursive_text(
                payload,
                "next_transition",
                "operator_recovery",
            ),
        )
    if kind == "tl_parked":
        return RecursiveTLParked(
            cause=_recursive_text(payload, "cause"),
            diagnostic=_recursive_text(payload, "diagnostic"),
            scope_path=scope_path,
            next_transition=_optional_recursive_text(
                payload,
                "next_transition",
                "operator_recovery",
            ),
        )
    plan_digest = _recursive_text(payload, "plan_digest")
    if kind == "tl_planning":
        ordered = tuple(
            (
                _recursive_int(stage, "order"),
                tuple(_decode_child_record(child) for child in _recursive_list(stage, "children")),
            )
            for stage in _recursive_list(payload, "ordered_children")
        )
        return RecursiveTLPlanning(
            ordered_children=ordered,
            scope_path=scope_path,
            plan_digest=plan_digest,
            parallel_children=tuple(
                _decode_child_record(child)
                for child in _recursive_list(payload, "parallel_children")
            ),
        )
    if kind == "tl_running":
        pending_raw = payload.get("pending_by_order")
        if not isinstance(pending_raw, Mapping):
            raise ValueError("recursive pending_by_order must be an object")
        pending = {
            _recursive_order(order): tuple(
                _decode_child_record(child) for child in _recursive_array(children, "pending order")
            )
            for order, children in pending_raw.items()
        }
        completed_raw = _recursive_mapping(payload, "completed_children")
        post_raw = _recursive_mapping(payload, "post_merge")
        return RecursiveTLRunning(
            current_order=_recursive_int(payload, "current_order"),
            pending_by_order=pending,
            scope_path=scope_path,
            plan_digest=plan_digest,
            parallel_pending=tuple(
                _decode_child_record(child)
                for child in _recursive_list(payload, "parallel_pending")
            ),
            completed_children={
                child_id: _decode_child_record(child) for child_id, child in completed_raw.items()
            },
            post_merge={
                child_id: _decode_post_merge_state(state) for child_id, state in post_raw.items()
            },
            dispatch_intents=_recursive_string_map(payload, "dispatch_intents"),
            evidence=_recursive_string_map(payload, "evidence"),
            lane_bindings=_recursive_string_map(payload, "lane_bindings"),
        )
    if kind == "tl_all_merged":
        return RecursiveTLAllMerged(
            scope_path=scope_path,
            plan_digest=plan_digest,
            completed_children={
                child_id: _decode_post_merge_state(state)
                for child_id, state in _recursive_mapping(payload, "completed_children").items()
            },
        )
    if kind == "tl_finalizing":
        return RecursiveTLFinalizing(
            role=ScopeRole(_recursive_text(payload, "role")),
            scope_path=scope_path,
            plan_digest=plan_digest,
            evidence=_recursive_string_map(payload, "evidence"),
        )
    if kind == "tl_done":
        return RecursiveTLDone(
            scope_path=scope_path,
            plan_digest=plan_digest,
            finalization_evidence=_recursive_string_map(payload, "finalization_evidence"),
        )
    if kind == "tl_pr_filed":
        return RecursiveTLPRFiled(
            aggregate_pr=_recursive_text(payload, "aggregate_pr"),
            head_sha=_recursive_text(payload, "head_sha"),
            base_sha=_recursive_text(payload, "base_sha"),
            parent_branch=_recursive_text(payload, "parent_branch"),
            handoff=_recursive_text(payload, "handoff"),
            scope_path=scope_path,
            plan_digest=plan_digest,
        )
    raise ValueError(f"recursive FSM kind {kind!r} is not recognized")


def _decode_child_record(value: object) -> ChildRecord:
    if not isinstance(value, Mapping):
        raise TypeError("recursive child record must be an object")
    return ChildRecord(
        child_id=_recursive_text(value, "child_id"),
        kind=ChildKind(_recursive_text(value, "kind")),
        dispatch_intent_id=_recursive_optional_text(value, "dispatch_intent_id"),
        invocation_id=_recursive_optional_text(value, "invocation_id"),
        evidence=_recursive_string_map(value, "evidence"),
        lane_id=_recursive_optional_text(value, "lane_id"),
        manifest_node_id=_recursive_optional_text(value, "manifest_node_id"),
        manifest_revision=_recursive_optional_int(value, "manifest_revision"),
    )


def _decode_post_merge_state(value: object) -> PostMergeState:
    if not isinstance(value, Mapping):
        raise TypeError("recursive post-merge state must be an object")
    return PostMergeState(
        phase=PostMergePhase(_recursive_text(value, "phase")),
        evidence=_recursive_string_map(value, "evidence"),
    )


def _recursive_text(value: Mapping[str, object], field_name: str) -> str:
    raw = value.get(field_name)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"recursive FSM {field_name} must be a non-empty string")
    return raw


def _recursive_optional_text(value: Mapping[str, object], field_name: str) -> str | None:
    raw = value.get(field_name)
    if raw is None:
        return None
    return _recursive_text(value, field_name)


def _optional_recursive_text(
    value: Mapping[str, object],
    field_name: str,
    default: str,
) -> str:
    """Read a compatibility string while keeping new payloads strict."""
    raw = value.get(field_name)
    if raw is None:
        return default
    return _recursive_text(value, field_name)


def _optional_recursive_text_list(
    value: Mapping[str, object],
    field_name: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    """Read a legacy-optional scope path without guessing non-default data."""
    if field_name not in value:
        return default
    return _recursive_text_list(value, field_name)


def _optional_recursive_string_map(
    value: Mapping[str, object],
    field_name: str,
) -> dict[str, str]:
    """Read optional terminal evidence from pre-target checkpoints."""
    if field_name not in value:
        return {}
    return _recursive_string_map(value, field_name)


def _recursive_optional_int(value: Mapping[str, object], field_name: str) -> int | None:
    """Decode an optional integer from a recursive child record."""
    raw = value.get(field_name)
    if raw is None:
        return None
    if type(raw) is not int:
        raise ValueError(f"recursive FSM {field_name} must be an integer")
    return raw


def _recursive_text_list(value: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    return tuple(
        _recursive_text({"value": item}, "value") for item in _recursive_list(value, field_name)
    )


def _recursive_int(value: Mapping[str, object], field_name: str) -> int:
    raw = value.get(field_name)
    if type(raw) is not int:
        raise ValueError(f"recursive FSM {field_name} must be an integer")
    return raw


def _recursive_order(value: object) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ValueError("recursive FSM pending order must be an integer key")


def _recursive_list(value: object, field_name: str) -> list[object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"recursive FSM {field_name} must be an object containing an array")
    raw = value.get(field_name)
    if not isinstance(raw, list):
        raise TypeError(f"recursive FSM {field_name} must be an array")
    return raw


def _recursive_array(value: object, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"recursive FSM {field_name} must be an array")
    return value


def _recursive_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"recursive FSM {field_name} must be an object containing a mapping")
    raw = value.get(field_name)
    if not isinstance(raw, Mapping):
        raise TypeError(f"recursive FSM {field_name} must be an object")
    return raw


def _recursive_string_map(value: Mapping[str, object], field_name: str) -> dict[str, str]:
    raw = _recursive_mapping(value, field_name)
    result: dict[str, str] = {}
    for key, item in raw.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise TypeError(f"recursive FSM {field_name} must map strings to strings")
        result[key] = item
    return result


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
            name: _decode_sub_tl_lifecycle(cast(str, lifecycle))
            for name, lifecycle in raw_states.items()
            if isinstance(name, str) and isinstance(lifecycle, str)
        }
        if isinstance(raw_states, dict)
        else {}
    )
    return IntegrationRuntimeState(
        lifecycle=IntegrationLifecycle(cast(str, value.get("lifecycle", "RUNNING"))),
        sub_tl_states=MappingProxyType(states),
        sub_tl_recovery=MappingProxyType(
            {
                name: _decode_child_recovery(cast(dict[str, object], summary))
                for name, summary in cast(
                    dict[str, object], value.get("sub_tl_recovery", {})
                ).items()
                if isinstance(name, str) and isinstance(summary, dict)
            }
        ),
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


def _decode_sub_tl_lifecycle(value: str) -> IntegrationLifecycle | SubTLLifecycle:
    try:
        return IntegrationLifecycle(value)
    except ValueError:
        return SubTLLifecycle(value)


def _encode_child_recovery(value: ChildRecoverySummary) -> dict[str, object]:
    return {
        "owner_run_id": value.owner_run_id,
        "child_path": list(value.child_path),
        "slice_id": value.slice_id,
        "cause": getattr(value.cause, "value", value.cause),
        "recovery_round": value.recovery_round,
        "next_probe_at": value.next_probe_at,
    }


def _decode_child_recovery(value: dict[str, object]) -> ChildRecoverySummary:
    from tl_loop.events.envelope import BlockCause

    return ChildRecoverySummary(
        owner_run_id=cast(str, value["owner_run_id"]),
        child_path=tuple(cast(list[str], value["child_path"])),
        slice_id=cast(str, value["slice_id"]),
        cause=BlockCause(cast(str, value["cause"])),
        recovery_round=cast(int, value["recovery_round"]),
        next_probe_at=cast(float | None, value.get("next_probe_at")),
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
        review_contract=(
            MappingProxyType(copy.deepcopy(cast(dict[str, object], value["review_contract"])))
            if isinstance(value.get("review_contract"), dict)
            else None
        ),
        ci_state=_decode_string_map(value.get("ci_state")),
        reviewer_attempt=_decode_int_map(value.get("reviewer_attempt")),
        reviewer_agent_id=cast(str | None, value.get("reviewer_agent_id")),
        repair_attempts=cast(int, value.get("repair_attempts", 0)),
        review_rounds=cast(int, value.get("review_rounds", 0)),
        attempts=cast(int, value["attempts"]),
        verdict=Verdict(cast(str, value["verdict"])) if value["verdict"] is not None else None,
        verdict_at=cast(str | None, value.get("verdict_at")),
        review_evidence=_decode_review_evidence(value.get("review_evidence")),
        review_validation_required=value.get("review_validation_required") is True,
        review_validation_disposition=(
            ReviewValidationDisposition(cast(str, value["review_validation_disposition"]))
            if value.get("review_validation_disposition") is not None
            else None
        ),
        review_validation_failure_reason=cast(
            str | None, value.get("review_validation_failure_reason")
        ),
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
        dispatch_invocation_id=cast(str | None, value.get("dispatch_invocation_id")),
        dispatch_authoritative_event_seq=cast(
            int | None, value.get("dispatch_authoritative_event_seq")
        ),
        dispatch_generation=cast(int, value.get("dispatch_generation", 0)),
        reconciliation=(
            MappingProxyType(copy.deepcopy(cast(dict[str, object], value["reconciliation"])))
            if isinstance(value.get("reconciliation"), dict)
            else None
        ),
        task_timeout_seconds=cast(float | None, value.get("task_timeout_seconds")),
        task_timeout_source=cast(str | None, value.get("task_timeout_source")),
        recovery=decode_recovery(value.get("recovery")),
        suspended_dependency=_decode_suspended_dependency(value.get("suspended_dependency")),
        deadline_ledger=_decode_deadline_ledger(value.get("deadline_ledger")),
        publication=_decode_publication(value.get("publication")),
        handoff=_decode_handoff(value.get("handoff")),
        observation_provenance=_decode_observation_provenance(value.get("observation_provenance")),
        action=_decode_action(value.get("action")),
        post_merge=_decode_post_merge_state(value.get("post_merge")),
        manifest_node_id=cast(str | None, value.get("manifest_node_id")),
        manifest_revision=cast(int | None, value.get("manifest_revision")),
    )


def _decode_post_merge_state(value: object) -> PostMergeState | None:
    if not isinstance(value, Mapping):
        return None
    phase = value.get("phase")
    evidence = value.get("evidence")
    if not isinstance(phase, str) or not isinstance(evidence, Mapping):
        return None
    return PostMergeState(PostMergePhase(phase), dict(evidence))


def _decode_repository_identity(value: object) -> RepositoryIdentity | None:
    if not isinstance(value, Mapping):
        return None
    return RepositoryIdentity(
        owner=cast(str, value["owner"]),
        repo=cast(str, value["repo"]),
        base_branch=cast(str, value["base_branch"]),
        forge_host=cast(str | None, value.get("forge_host")),
        remote_url=cast(str | None, value.get("remote_url")),
    )


def _decode_publication(value: object) -> PublicationBinding | None:
    if not isinstance(value, Mapping):
        return None
    return PublicationBinding(
        pr_number=cast(int, value["pr_number"]),
        head_sha=cast(str, value["head_sha"]),
        head_branch=cast(str, value["head_branch"]),
        base_branch=cast(str, value["base_branch"]),
        attempt=cast(int, value["attempt"]),
        invocation_id=cast(str | None, value.get("invocation_id")),
    )


def _decode_handoff(value: object) -> HandoffEvidence | None:
    if not isinstance(value, Mapping):
        return None
    return HandoffEvidence(
        pr_number=cast(int, value["pr_number"]),
        head_sha=cast(str, value["head_sha"]),
        attempt=cast(int, value["attempt"]),
        invocation_id=cast(str, value["invocation_id"]),
        agent_id=cast(str, value["agent_id"]),
        observed_at=cast(str, value["observed_at"]),
    )


def _decode_observation_provenance(value: object) -> ObservationProvenance | None:
    if not isinstance(value, Mapping):
        return None
    return ObservationProvenance(
        source=cast(str, value["source"]),
        observed_at=cast(str, value["observed_at"]),
        event_seq=cast(int | None, value.get("event_seq")),
        snapshot_id=cast(str | None, value.get("snapshot_id")),
        ledger_run_seq=cast(int | None, value.get("ledger_run_seq")),
        snapshot_high_watermark=cast(int | None, value.get("snapshot_high_watermark")),
        source_epoch=cast(int, value.get("source_epoch", 0)),
        source_revision=cast(int, value.get("source_revision", 0)),
        coverage=tuple(cast(list[str], value.get("coverage", []))),
    )


def _decode_review_evidence(value: object) -> DurableReviewEvidence | None:
    if not isinstance(value, Mapping):
        return None
    return DurableReviewEvidence(
        review_id=cast(int, value["review_id"]),
        pr_number=cast(int, value["pr_number"]),
        head_sha=cast(str, value["head_sha"]),
        reviewer_agent_id=cast(str, value["reviewer_agent_id"]),
        verdict=Verdict(cast(str, value["verdict"])),
        submitted_at=cast(str, value["submitted_at"]),
        validated_at=cast(str | None, value.get("validated_at")),
        reviewer_account_authenticated=cast(
            bool, value.get("reviewer_account_authenticated", True)
        ),
        dismissed=cast(bool, value.get("dismissed", False)),
        forgejo_stale=cast(bool, value.get("forgejo_stale", False)),
        reviewer_identity_unresolved=cast(bool, value.get("reviewer_identity_unresolved", False)),
    )


def _decode_action(value: object) -> ActionState | None:
    if not isinstance(value, Mapping):
        return None
    return ActionState(
        kind=ActionKind(cast(str, value["kind"])),
        phase=ActionPhase(cast(str, value["phase"])),
        state_version=cast(int, value["state_version"]),
        intent_id=cast(str | None, value.get("intent_id")),
        head_sha=cast(str | None, value.get("head_sha")),
        attempt=cast(int | None, value.get("attempt")),
        contract_digest=cast(str | None, value.get("contract_digest")),
    )


def _decode_suspended_dependency(value: object) -> SuspendedDependencyState | None:
    if not isinstance(value, Mapping):
        return None
    return SuspendedDependencyState(
        blocked_by=cast(str, value["blocked_by"]),
        prior_status=SliceStatus(cast(str, value["prior_status"])),
        recovery_generation=cast(int, value["recovery_generation"]),
    )


def _decode_deadline_ledger(value: object) -> DeadlineLedger | None:
    if not isinstance(value, Mapping):
        return None
    return DeadlineLedger(
        execution_deadline_at=cast(float | None, value.get("execution_deadline_at")),
        recovery_deadline_at=cast(float | None, value.get("recovery_deadline_at")),
        run_deadline_at=cast(float | None, value.get("run_deadline_at")),
        suspended_at=cast(float | None, value.get("suspended_at")),
        execution_seconds=cast(float, value.get("execution_seconds", 0.0)),
        recovery_wait_seconds=cast(float, value.get("recovery_wait_seconds", 0.0)),
    )


def _decode_gate(value: dict[str, object]) -> GateState:
    return GateState(name=cast(str, value["name"]), status=GateStatus(cast(str, value["status"])))


def _assert_consistent(path: Path, state: RunState) -> None:
    if state.recursive_fsm is not None:
        return
    _assert_fsm_slices_consistent(path, state.fsm.phase, state.fsm.waiting, state.slices)


def _assert_encoded_consistent(
    path: Path,
    fsm: Mapping[str, object],
    slices: Mapping[str, object],
) -> None:
    if fsm.get("kind") == "recursive":
        return
    if path.exists():
        try:
            existing = json.loads(_read_bytes(path).decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing = None
        if (
            isinstance(existing, Mapping)
            and isinstance(existing.get("fsm"), Mapping)
            and existing["fsm"].get("kind") == "recursive"
        ):
            return
    phase = TLPhase(cast(str, fsm["phase"]))
    waiting = tuple(cast(list[str], fsm["waiting"]))
    _assert_fsm_slices_consistent(path, phase, waiting, slices)


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
        status = (
            value.status
            if isinstance(value, SliceState)
            else value.get("status")
            if isinstance(value, Mapping)
            else value
        )
        if status not in active_statuses and status not in {item.value for item in SliceStatus}:
            continue
        if status not in active_statuses:
            if (
                isinstance(value, SliceState)
                and value.status is SliceStatus.MERGED
                and value.post_merge is not None
                and value.post_merge.phase is not PostMergePhase.COMPLETE
            ):
                continue
            if (
                isinstance(value, Mapping)
                and value.get("status") == SliceStatus.MERGED.value
                and isinstance(value.get("post_merge"), Mapping)
                and value["post_merge"].get("phase") != PostMergePhase.COMPLETE.value
            ):
                continue
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
