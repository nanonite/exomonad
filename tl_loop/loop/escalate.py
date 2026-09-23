"""Durable escalation and human-parking decisions for the TL loop.

The module keeps cause classification pure and makes the state mutation explicit:
an issue is created through the effect boundary, then one atomic run-state write
parks the slice and blocks its dependents.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, cast

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.observability import emit_controller_event
from tl_loop.select.ledger import LedgerInput
from tl_loop.state.schema import BudgetLedger, ParkCause, SliceState, SliceStatus
from tl_loop.state.serialization import dumps as dumps_json
from tl_loop.state.serialization import to_jsonable
from tl_loop.state.slice_transition import SliceStatusChanged, slice_transition
from tl_loop.state.store import RunStore
from tl_loop.state.write import apply

_TERMINAL_STATUSES = frozenset(
    {
        SliceStatus.MERGED.value,
        SliceStatus.FAILED.value,
        SliceStatus.PARKED.value,
        SliceStatus.BLOCKED.value,
    }
)
_AUDIT_FIELDS = frozenset(
    {
        "from_harness",
        "to_harness",
        "reason",
        "model",
        "effort",
        "invariant",
        "action_key",
        "action",
        "target_id",
    }
)
_BLOCKED_AUDIT_FIELDS = frozenset(
    {
        "attempt",
        "recovery_action",
        "needs_human",
        "base_sha",
        "head_sha",
        "failed_checks",
        "attribution",
    }
)
_BLOCKED_GATE_CAUSES = frozenset(
    {
        ParkCause.BASE_CI_UNSTABLE,
        ParkCause.EXTERNAL_DEPENDENCY,
        ParkCause.SCOPE_BOUNDARY,
        ParkCause.HUMAN_DECISION_REQUIRED,
        ParkCause.MISSING_HANDOFF,
        ParkCause.PUBLICATION_OWNERSHIP_UNRESOLVED,
        ParkCause.REVIEW_ROUNDS_EXHAUSTED,
        ParkCause.REPEATED_ACTION_NO_PROGRESS,
    }
)


class EscalationError(RuntimeError):
    """An escalation could not be recorded safely."""


class IssueCreationError(EscalationError):
    """The needs-human issue was not created with a usable ID."""


def blocked_gate_name(run_id: str, slice_id: str, attempt: int, cause: str) -> str:
    """Return the stable identity for one externally blocked attempt."""
    if not run_id or not slice_id or type(attempt) is not int or attempt <= 0 or not cause:
        raise ValueError("blocked gate identity requires run, slice, positive attempt, and cause")
    return f"task-blocked:{run_id}:{slice_id}:{attempt}:{cause}"


class IssueCreator(Protocol):
    """The effect capability required to create a needs-human issue."""

    def chainlink_issue_create(
        self,
        *,
        title: str,
        description: str | None = None,
        labels: Sequence[str] | None = None,
        priority: str | None = None,
    ) -> ToolResult:
        """Create one issue through the effect boundary."""


@dataclass(frozen=True)
class ParkResult:
    """The durable result of parking one slice and blocking its dependents."""

    issue_id: int
    parked_slice_id: str
    blocked_slice_ids: tuple[str, ...]


@dataclass(frozen=True)
class HarnessSwitchDecision:
    """An auditable allow-or-park decision for changing harnesses."""

    allowed: bool
    from_harness: str
    to_harness: str
    reason: str
    model: str
    effort: str
    cause: ParkCause | None
    audit: Mapping[str, object]


def park(
    slice: SliceState,
    cause: ParkCause | str,
    *,
    store: RunStore | None = None,
    issue_creator: IssueCreator | EffectClient | Callable[[str, str], int] | None = None,
    ledger: LedgerInput | None = None,
    audit: Mapping[str, object] | None = None,
) -> SliceState | ParkResult:
    """Park a slice for a closed cause and block every pending dependent.

    Without a store this returns the pure parked state for classification tests.
    With a store, issue creation and one state.write.apply call are required;
    no dependent is spawned after the mutation.
    """
    parsed_cause = _cause(cause)
    parked_audit = _build_audit(slice, ledger, audit)
    if store is None:
        return replace(
            slice_transition(slice, SliceStatusChanged(SliceStatus.PARKED)),
            park_cause=parsed_cause,
            park_issue_id=None,
            park_audit=parked_audit,
            blocked_by=None,
        )
    gate_name: str | None = None
    gate_created = False
    current_state = store.load()
    current_slice = current_state.slices.get(slice.id)
    raw_attempt = (audit or {}).get("attempt", slice.attempts)
    attempt = (
        raw_attempt
        if type(raw_attempt) is int and raw_attempt > 0
        else max(slice.attempts, 1)
    )
    if parsed_cause in _BLOCKED_GATE_CAUSES:
        gate_name = blocked_gate_name(store.run_id, slice.id, attempt, parsed_cause.value)
        parked_audit = {**dict(parked_audit), "gate_name": gate_name, "attempt": attempt}
        if isinstance(current_slice, SliceState) and (
            current_slice.park_cause is parsed_cause and current_slice.park_issue_id is not None
        ):
            gate_created = _ensure_gate(store, gate_name)
            if gate_created and isinstance(issue_creator, EffectClient):
                _emit_gate_opened(issue_creator, store.run_id, gate_name, parsed_cause)
            return ParkResult(current_slice.park_issue_id, slice.id, ())
    if issue_creator is None:
        raise EscalationError("a needs-human issue creator is required for durable parking")

    # A legacy untagged issue (#816) is reused only through a durable binding
    # that is verified against this slice's publication PR and head.
    legacy_binding_id: int | None = None
    if store is not None:
        publication = getattr(slice, "publication", None)
        bound_head = (
            getattr(publication, "head_sha", None) if publication is not None else None
        )
        legacy_binding_id = _read_legacy_binding(
            store,
            slice.id,
            parsed_cause,
            pr_number=slice.pr_number,
            head_sha=bound_head,
        )
    issue_id = _create_issue(
        issue_creator,
        slice,
        parsed_cause,
        parked_audit,
        attempt=attempt,
        store=store,
        legacy_binding_id=legacy_binding_id,
    )
    blocked: list[str] = []
    blocked_statuses: dict[str, str] = {}

    def mutate(document: dict[str, object]) -> dict[str, object]:
        raw_slices = document.get("slices")
        if not isinstance(raw_slices, dict):
            raise EscalationError("run state slices are not an object")
        target = raw_slices.get(slice.id)
        if not isinstance(target, dict):
            raise EscalationError(f"slice {slice.id!r} is missing from run state")

        target["status"] = SliceStatus.PARKED.value
        target["park_cause"] = parsed_cause.value
        target["park_issue_id"] = issue_id
        target["park_audit"] = copy.deepcopy(dict(parked_audit))
        target.pop("blocked_by", None)

        blocked_ids = {slice.id}
        changed = True
        while changed:
            changed = False
            for dependent_id, raw_dependent in raw_slices.items():
                if dependent_id in blocked_ids or not isinstance(raw_dependent, dict):
                    continue
                if raw_dependent.get("status") in _TERMINAL_STATUSES:
                    continue
                dependencies = raw_dependent.get("depends_on")
                if not isinstance(dependencies, list):
                    continue
                if not any(dependency in blocked_ids for dependency in dependencies):
                    continue
                blocked_statuses[dependent_id] = str(raw_dependent.get("status", "pending"))
                raw_dependent["status"] = SliceStatus.BLOCKED.value
                raw_dependent["blocked_by"] = slice.id
                raw_dependent["park_issue_id"] = issue_id
                raw_dependent.pop("suspended_dependency", None)
                blocked_ids.add(dependent_id)
                blocked.append(dependent_id)
                changed = True
        raw_fsm = document.get("fsm")
        if isinstance(raw_fsm, dict):
            waiting = raw_fsm.get("waiting")
            if isinstance(waiting, list):
                waiting[:] = [item for item in waiting if item not in blocked_ids]
                if (
                    not waiting
                    and raw_fsm.get("phase") in {TLPhase.TLWaiting.value, TLPhase.TLMerging.value}
                    and parsed_cause not in _BLOCKED_GATE_CAUSES
                ):
                    raw_fsm["phase"] = TLPhase.TLFailed.value
        return document

    prior_phase = store.load().fsm.phase.value
    apply(store.run_dir, mutate)
    if gate_name is not None:
        gate_created = _ensure_gate(store, gate_name)
    if isinstance(issue_creator, EffectClient):
        _emit_park_events(
            issue_creator,
            slice,
            parsed_cause,
            blocked_statuses,
            prior_phase,
            store.load().fsm.phase.value,
            store.run_id,
        )
        if gate_name is not None and gate_created:
            _emit_gate_opened(issue_creator, store.run_id, gate_name, parsed_cause)
    return ParkResult(issue_id, slice.id, tuple(blocked))


def authorize_harness_switch(
    from_harness: str,
    to_harness: str,
    reason: str,
    model: str,
    effort: str,
    allow: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> HarnessSwitchDecision:
    """Authorize a declared harness or require the explicit operator flag."""
    audit: dict[str, object] = {
        "from_harness": from_harness,
        "to_harness": to_harness,
        "reason": reason,
        "model": model,
        "effort": effort,
    }
    source_env = os.environ if env is None else env
    allowed = to_harness in allow or source_env.get("EXOMONAD_ALLOW_HARNESS_SWITCH") == "1"
    return HarnessSwitchDecision(
        allowed=allowed,
        from_harness=from_harness,
        to_harness=to_harness,
        reason=reason,
        model=model,
        effort=effort,
        cause=None if allowed else ParkCause.HARNESS_SWITCH_REQUESTED,
        audit=audit,
    )


def switch_harness(
    slice: SliceState,
    from_harness: str,
    to_harness: str,
    reason: str,
    model: str,
    effort: str,
    allow: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    store: RunStore | None = None,
    issue_creator: IssueCreator | EffectClient | Callable[[str, str], int] | None = None,
    ledger: LedgerInput | None = None,
) -> HarnessSwitchDecision | ParkResult:
    """Return an audited switch decision, parking ungated switches durably."""
    decision = authorize_harness_switch(
        from_harness,
        to_harness,
        reason,
        model,
        effort,
        allow,
        env=env,
    )
    if decision.allowed or store is None:
        return decision
    if issue_creator is None:
        raise EscalationError("a needs-human issue creator is required for an ungated switch")
    return cast(
        ParkResult,
        park(
            slice,
            ParkCause.HARNESS_SWITCH_REQUESTED,
            store=store,
            issue_creator=issue_creator,
            ledger=ledger,
            audit=decision.audit,
        ),
    )


def _cause(value: ParkCause | str) -> ParkCause:
    try:
        return value if isinstance(value, ParkCause) else ParkCause(value)
    except ValueError as error:
        raise EscalationError(f"unsupported parking cause: {value!r}") from error


def _emit_park_events(
    effects: EffectClient,
    slice: SliceState,
    cause: ParkCause,
    blocked_statuses: Mapping[str, str],
    before_phase: str,
    after_phase: str,
    run_id: str,
) -> None:
    """Publish parking and related status changes after durable mutation."""
    if slice.status.value != SliceStatus.PARKED.value:
        emit_controller_event(
            effects,
            "tl.slice_status_changed",
            {
                "slice_id": slice.id,
                "from_status": slice.status.value,
                "to_status": SliceStatus.PARKED.value,
            },
        )
    for slice_id in sorted(blocked_statuses):
        emit_controller_event(
            effects,
            "tl.slice_status_changed",
            {
                "slice_id": slice_id,
                "from_status": blocked_statuses[slice_id],
                "to_status": SliceStatus.BLOCKED.value,
            },
        )
    emit_controller_event(
        effects,
        "tl.slice_parked",
        {
            "slice_id": slice.id,
            "park_cause": cause.value,
            "attempts": slice.attempts,
        },
    )
    if before_phase != after_phase:
        emit_controller_event(
            effects,
            "tl.phase_changed",
            {
                "from_phase": before_phase,
                "to_phase": after_phase,
                "run_id": run_id,
            },
        )


def _build_audit(
    slice: SliceState,
    ledger: LedgerInput | None,
    extra: Mapping[str, object] | None,
) -> Mapping[str, object]:
    result: dict[str, object] = {
        "attempts": slice.attempts,
        "verdict": slice.verdict.value if slice.verdict is not None else None,
        "harness": slice.agent_type,
        "model": slice.model,
        "ledger": _ledger_snapshot(ledger),
    }
    if extra is not None:
        for key in _AUDIT_FIELDS | _BLOCKED_AUDIT_FIELDS:
            if key in extra:
                result[key] = to_jsonable(extra[key])
    return result


def _ledger_snapshot(ledger: LedgerInput | None) -> dict[str, object]:
    if ledger is None:
        return {"tokens": 0, "wall_seconds": 0}
    if isinstance(ledger, BudgetLedger):
        return {
            "tokens": ledger.tokens,
            "wall_seconds": ledger.wall_seconds,
            "role_spent": dict(ledger.role_spent),
            "harness_spent": dict(ledger.harness_spent),
            "role_reserved": dict(ledger.role_reserved),
            "harness_reserved": dict(ledger.harness_reserved),
            "charges": [
                {
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
                for charge in ledger.charges
            ],
        }
    value = to_jsonable(ledger)
    if not isinstance(value, dict):
        raise EscalationError("ledger audit must be an object")
    nested = value.get("ledger")
    if isinstance(nested, Mapping):
        return copy.deepcopy(dict(nested))
    return value


def _ensure_gate(store: RunStore, gate_name: str) -> bool:
    """Create a pending gate only when this identity has not been seen."""
    if any(gate.name == gate_name for gate in store.load().gates):
        return False
    store.set_gate(gate_name)
    return True


def _emit_gate_opened(effects: EffectClient, run_id: str, gate_name: str, cause: ParkCause) -> None:
    emit_controller_event(
        effects,
        "tl.gate_opened",
        {
            "gate_name": gate_name,
            "run_id": run_id,
            "reason": f"externally blocked slice: {cause.value}",
        },
    )


class IssueLookupUnavailable(EscalationError):
    """Existing escalation issues could not be inspected, so creating is unsafe."""


def _legacy_escalation_title(slice_id: str, cause: ParkCause) -> str:
    """The pre-attempt title used by issues created before this change (#816)."""
    return f"Escalate slice {slice_id}: {cause.value}"


def _attempt_scoped_title(slice_id: str, cause: ParkCause, attempt: int) -> str:
    """Stable title that scopes one escalation to a single slice attempt."""
    return f"{_legacy_escalation_title(slice_id, cause)} (attempt {attempt})"


def _escalation_key(run_id: str, slice_id: str, attempt: int, cause: ParkCause) -> str:
    """Stable per-run, per-attempt identity for one slice escalation."""
    return f"task-escalation:{run_id}:{slice_id}:{attempt}:{cause.value}"


def _escalation_intent_path(store: RunStore, key: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in key)
    return Path(store.run_dir) / "escalations" / f"{safe}.json"


def _legacy_binding_path(store: RunStore, slice_id: str, cause: ParkCause) -> Path:
    safe = "".join(
        ch if ch.isalnum() or ch in "-_." else "_"
        for ch in f"{slice_id}:{cause.value}"
    )
    return Path(store.run_dir) / "escalations" / f"legacy-{safe}.json"


def bind_legacy_escalation(
    store: RunStore,
    *,
    slice_id: str,
    cause: ParkCause,
    issue_id: int,
    pr_number: int | None = None,
    head_sha: str | None = None,
) -> Path:
    """Bind a pre-run-identity escalation issue (for example Beast #816).

    This is the narrowly verified migration path for legacy untagged issues:
    an operator or recovery step records the exact issue for a slice and cause,
    optionally pinning the publication PR number and head SHA that the issue
    belongs to. Reconciliation never adopts an untagged issue without it.
    """
    if type(issue_id) is not int or issue_id <= 0:
        raise ValueError("legacy escalation binding requires a positive issue id")
    path = _legacy_binding_path(store, slice_id, cause)
    _write_intent(
        path,
        {
            "slice_id": slice_id,
            "cause": cause.value,
            "issue_id": issue_id,
            "pr_number": pr_number,
            "head_sha": head_sha,
        },
    )
    return path


def _read_legacy_binding(
    store: RunStore,
    slice_id: str,
    cause: ParkCause,
    *,
    pr_number: int | None,
    head_sha: str | None,
) -> int | None:
    """Return a verified legacy issue id, or None when there is no valid binding."""
    path = _legacy_binding_path(store, slice_id, cause)
    record = _read_intent(path)
    if record is None:
        return None
    bound_id = _issue_id(record)
    if bound_id is None:
        raise EscalationError(f"legacy escalation binding at {path} has no issue id")
    if record.get("slice_id") != slice_id or record.get("cause") != cause.value:
        raise EscalationError(
            f"legacy escalation binding at {path} does not match slice {slice_id!r}"
        )
    bound_pr = record.get("pr_number")
    if bound_pr is not None and bound_pr != pr_number:
        return None
    bound_head = record.get("head_sha")
    if bound_head is not None and bound_head != head_sha:
        return None
    return bound_id


_RUN_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{16}$")


def _valid_run_token(token: str) -> bool:
    return bool(_RUN_TOKEN_PATTERN.match(token))


def _read_run_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise EscalationError(f"run token is unreadable at {path}: {error}") from error
    if not _valid_run_token(token):
        raise EscalationError(f"run token at {path} is empty or malformed")
    return token


def _run_token(store: RunStore) -> str:
    """Return this run directory's durable identity token, creating it once.

    The token is embedded in escalation titles so a later run cannot reconcile
    against this run's issues even when the slice, cause, and attempt match. It
    is published atomically: a temporary file is written in full and hard-linked
    into place, so the token path is never visible as empty or partial.
    """
    path = Path(store.run_dir) / "escalations" / "run.token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return _read_run_token(path)
    token = os.urandom(8).hex()
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            # Another writer published first; use its complete token.
            return _read_run_token(path)
        except OSError:
            # Filesystems without hard links: fall back to an atomic replace,
            # retaining the winner's token if one already exists.
            if path.exists():
                return _read_run_token(path)
            os.replace(temporary, path)
    except OSError as error:
        raise EscalationError(f"could not publish run token at {path}: {error}") from error
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return token


@contextmanager
def _escalation_intent_lock(store: RunStore, key: str):
    """Serialize reconciliation and creation for one durable escalation key."""
    path = _escalation_intent_path(store, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_intent(path: Path) -> dict[str, object] | None:
    """Return the intent, or None only when the file does not exist.

    A present-but-unusable intent (unreadable, malformed, or not an object) is
    an error: the caller must not treat it as a fresh attempt.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise EscalationError(
            f"escalation intent is unreadable at {path}: {error}"
        ) from error
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise EscalationError(
            f"escalation intent is malformed at {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise EscalationError(f"escalation intent at {path} is not an object")
    return value


def _write_intent(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)


def _list_needs_human_issues(creator: object) -> list[object]:
    """List open needs-human issues, failing closed when that is not possible."""
    list_issues = getattr(creator, "chainlink_issue_list", None)
    if list_issues is None:
        return []
    try:
        result = list_issues(labels=("needs-human",))
    except Exception as error:
        raise IssueLookupUnavailable(
            f"could not list existing needs-human issues: {error}"
        ) from error
    if result.success is not True:
        raise IssueLookupUnavailable(
            result.error
            or "chainlink issue list failed; refusing to create a possible duplicate"
        )
    payload = result.result
    if payload is None:
        return []
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, Mapping):
        raw = payload.get("issues")
        if isinstance(raw, list):
            return list(raw)
        # An empty object is an unambiguous "no issues". Any other unrecognized
        # shape is ambiguous and must stop creation.
        if not payload:
            return []
    raise IssueLookupUnavailable(f"unrecognized issue list response: {payload!r}")


def _reconcile_existing_issue(
    creator: object,
    title: str,
    legacy_binding_id: int | None,
) -> int | None:
    """Reuse this run's escalation for the attempt, or a verified legacy issue.

    The run-scoped title carries a durable run token, so another run can never
    match it. Legacy issues created before run tokens (for example Beast #816)
    are reused only through a durable, publication-verified binding created by
    bind_legacy_escalation; an untagged title alone is never sufficient.
    """
    wanted = title.strip()
    for issue in _list_needs_human_issues(creator):
        issue_id = _issue_id(issue)
        if issue_id is None:
            continue
        issue_title = issue.get("title") if isinstance(issue, Mapping) else None
        if isinstance(issue_title, str) and issue_title.strip() == wanted:
            return issue_id
    return legacy_binding_id


def _create_issue(
    creator: IssueCreator | EffectClient | Callable[[str, str], int],
    slice: SliceState,
    cause: ParkCause,
    audit: Mapping[str, object],
    *,
    attempt: int = 1,
    store: RunStore | None = None,
    legacy_binding_id: int | None = None,
) -> int:
    base_title = _attempt_scoped_title(slice.id, cause, attempt)
    if store is not None:
        title = f"{base_title} [run {_run_token(store)}]"
        key = _escalation_key(store.run_id, slice.id, attempt, cause)
        with _escalation_intent_lock(store, key) as intent_path:
            return _create_issue_locked(
                creator,
                slice,
                cause,
                audit,
                title,
                attempt,
                legacy_binding_id,
                intent_path,
            )
    return _create_issue_locked(
        creator,
        slice,
        cause,
        audit,
        base_title,
        attempt,
        legacy_binding_id,
        None,
    )


def _create_issue_locked(
    creator: IssueCreator | EffectClient | Callable[[str, str], int],
    slice: SliceState,
    cause: ParkCause,
    audit: Mapping[str, object],
    title: str,
    attempt: int,
    legacy_binding_id: int | None,
    intent_path: Path | None,
) -> int:
    # A durable intent that already recorded an issue ID is authoritative.
    if intent_path is not None:
        intent = _read_intent(intent_path)
        if intent is not None:
            recorded = _issue_id(intent)
            if recorded is not None:
                return recorded
    # Always reconcile before creating. This covers a crash after remote
    # creation and a verified legacy binding. An unavailable lookup stops
    # creation instead of risking a duplicate.
    existing = _reconcile_existing_issue(creator, title, legacy_binding_id)
    if existing is not None:
        if intent_path is not None:
            _write_intent(intent_path, {"issue_id": existing, "title": title})
        return existing
    if intent_path is not None:
        _write_intent(intent_path, {"title": title, "state": "requested"})
    description = (
        f"Slice {slice.id} is parked for human action. "
        f"Cause: {cause.value}. Audit: {dumps_json(audit, sort_keys=True)}"
    )
    if cause in _BLOCKED_GATE_CAUSES:
        description += (
            "\n\nOperator choices: retry same owner; wait for recovery; "
            "authorize scope expansion; or abandon the attempt."
        )
    if callable(creator) and not hasattr(creator, "chainlink_issue_create"):
        value: object = creator(title, description)
    else:
        effect = cast(IssueCreator, creator)
        result = effect.chainlink_issue_create(
            title=title,
            description=description,
            labels=("needs-human",),
            priority="high",
        )
        if result.success is not True:
            raise IssueCreationError(result.error or "chainlink issue creation failed")
        value = result.result
    issue_id = _issue_id(value)
    if issue_id is None:
        raise IssueCreationError(f"chainlink issue result has no positive issue ID: {value!r}")
    if intent_path is not None:
        _write_intent(intent_path, {"issue_id": issue_id, "title": title})
    return issue_id


def _issue_id(value: object) -> int | None:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, Mapping):
        # `issue_id` is canonical; `cicoIssueId` is the legacy Haskell shape.
        for key in ("issue_id", "id", "number", "cicoIssueId"):
            candidate = value.get(key)
            if type(candidate) is int and candidate > 0:
                return candidate
    return None


__all__ = [
    "EscalationError",
    "HarnessSwitchDecision",
    "IssueCreationError",
    "ParkCause",
    "ParkResult",
    "authorize_harness_switch",
    "bind_legacy_escalation",
    "blocked_gate_name",
    "park",
    "switch_harness",
]
