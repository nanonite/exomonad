"""Typed creation, checkpoint, and resume operations for TL runs."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
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

from .schema import (
    ActualTokens,
    BudgetCharge,
    BudgetLedger,
    EventCursor,
    FSMState,
    GateState,
    GateStatus,
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


class CorruptCheckpoint(ValueError):
    """A persisted run state is valid JSON but cannot be resumed safely."""


@dataclass(frozen=True)
class ResumeState:
    """The local state required to resume a controller without network I/O."""

    fsm: FSMState
    slices: SliceMap
    budgets: BudgetLedger
    offset: int

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
    ) -> RunState:
        """Persist one checkpoint through the shared atomic mutation path."""
        return checkpoint(fsm, slices, budgets, offset, path=self.run_dir)

    def load(self) -> RunState:
        """Load and verify this run's checkpoint."""
        return load(self.path)

    def resume(self) -> ResumeState:
        """Return local replay state without contacting the runtime."""
        return resume(self.run_id, root_dir=self.root_dir)


def create(run_id: str, root_spec: RootSpec, *, root_dir: str | Path = DEFAULT_ROOT) -> RunState:
    """Create a run at ``.exo/tl-loop/<run_id>/run.json``.

    ``root_spec`` supplies any of the persisted ``fsm``, ``slices``,
    ``budgets``, ``gates``, and ``events`` sections. Omitted sections use the
    empty, planning-state defaults.
    """
    _validate_run_id(run_id)
    directory = Path(root_dir) / run_id
    initial = _initial_document(run_id, root_spec)
    try:
        validate(initial)
        _assert_consistent(directory / "run.json", _decode(initial))
    except SchemaError as error:
        raise CorruptCheckpoint(f"{directory / 'run.json'}: schema inconsistency: {error}") from error
    apply(directory, _identity, initial=initial)
    return load(directory / "run.json")


def load(path: str | Path) -> RunState:
    """Read, validate, and structurally verify a checkpoint from disk."""
    target = _state_path(path)
    try:
        data = _read_bytes(target)
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CorruptCheckpoint(f"{target}: could not read checkpoint: {error}") from error
    if not isinstance(value, dict):
        raise CorruptCheckpoint(f"{target}: checkpoint must contain a JSON object")
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
) -> RunState:
    """Atomically update a run's FSM, slices, budget ledger, and event offset."""
    run_directory = _resolve_run_directory(run_id, path, root_dir)
    if type(offset) is not int or offset < 0:
        raise ValueError("event-log offset must be a non-negative integer")
    encoded_fsm = _encode_fsm(fsm)
    encoded_slices = _encode_slices(slices)
    encoded_budgets = _encode_budgets(budgets)
    _assert_encoded_consistent(run_directory / "run.json", encoded_fsm, encoded_slices)

    def mutate(document: dict[str, object]) -> dict[str, object]:
        document["fsm"] = copy.deepcopy(encoded_fsm)
        document["slices"] = copy.deepcopy(encoded_slices)
        document["budgets"] = copy.deepcopy(encoded_budgets)
        document["events"] = {"last_consumed_offset": offset}
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
    )


def _initial_document(run_id: str, root_spec: RootSpec) -> dict[str, object]:
    allowed = {"fsm", "slices", "budgets", "gates", "events"}
    unknown = sorted(set(root_spec) - allowed)
    if unknown:
        raise ValueError(f"root_spec contains unknown sections: {', '.join(unknown)}")
    document: dict[str, object] = {
        "version": 1,
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
            "reviewed_head": value.reviewed_head,
            "attempts": value.attempts,
            "verdict": value.verdict.value if value.verdict else None,
        }
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
        return record
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    raise TypeError(f"slice {slice_id!r} is not a SliceState or object")


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
    )


def _decode_counter_map(value: object) -> Mapping[str, int]:
    if not isinstance(value, dict):
        return MappingProxyType({})
    return MappingProxyType({key: cast(int, amount) for key, amount in value.items()})


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
    active_statuses = {status.value for status in (SliceStatus.SPAWNED, SliceStatus.IN_REVIEW, SliceStatus.REPAIRING)}
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


__all__ = [
    "DEFAULT_ROOT",
    "CorruptCheckpoint",
    "ResumeState",
    "RunStore",
    "checkpoint",
    "create",
    "load",
    "resume",
]
