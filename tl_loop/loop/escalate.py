"""Durable escalation and human-parking decisions for the TL loop.

The module keeps cause classification pure and makes the state mutation explicit:
an issue is created through the effect boundary, then one atomic run-state write
parks the slice and blocks its dependents.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
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
    """Effect capabilities required for durable needs-human issue creation."""

    def chainlink_issue_create(
        self,
        *,
        title: str,
        description: str | None = None,
        labels: Sequence[str] | None = None,
        priority: str | None = None,
    ) -> ToolResult:
        """Create one issue through the effect boundary."""

    def chainlink_issue_list(
        self, *, labels: Sequence[str], status: str
    ) -> ToolResult:
        """List open and closed issues for retry reconciliation."""


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


def _slice_publication_head(slice: SliceState) -> str | None:
    publication = getattr(slice, "publication", None)
    return getattr(publication, "head_sha", None) if publication is not None else None


def _assert_caller_slice_current(persisted: SliceState, caller: SliceState) -> None:
    """Refuse a caller slice that has advanced past, or diverged from, the checkpoint."""
    if (
        persisted.attempts != caller.attempts
        or persisted.pr_number != caller.pr_number
        or _slice_publication_head(persisted) != _slice_publication_head(caller)
    ):
        raise EscalationError(
            f"caller slice {caller.id!r} (attempt {caller.attempts}, PR {caller.pr_number}, "
            f"head {_slice_publication_head(caller)!r}) is stale or inconsistent with the "
            f"persisted checkpoint (attempt {persisted.attempts}, PR {persisted.pr_number}, "
            f"head {_slice_publication_head(persisted)!r}); reload the run state before "
            "escalating"
        )


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
    if not isinstance(current_slice, SliceState):
        raise EscalationError(f"slice {slice.id!r} is missing from run state")
    _assert_caller_slice_current(current_slice, slice)
    # The attempt is owned by the persisted checkpoint, never by the audit
    # payload: an audit attempt from an older attempt must not select an older
    # issue or intent.
    attempt = current_slice.attempts if current_slice.attempts > 0 else 1
    if audit is not None and "attempt" in audit:
        # A present attempt (even None) must be a real positive int equal to the
        # persisted attempt; bool, float, None, and mismatches are rejected
        # before any issue lookup or creation.
        audit_attempt = audit["attempt"]
        if type(audit_attempt) is not int or audit_attempt <= 0:
            raise EscalationError(
                f"audit attempt {audit_attempt!r} must be a positive integer for slice "
                f"{slice.id!r}; refusing to escalate"
            )
        if audit_attempt != attempt:
            raise EscalationError(
                f"audit attempt {audit_attempt!r} does not match the persisted slice attempt "
                f"{attempt} for slice {slice.id!r}; refusing to escalate"
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

    # A legacy untagged issue (#816) is reused only through an operator-verified
    # durable binding. The failure reason and a slice/cause title cannot prove
    # which attempt or publication created an issue, so a recorded legacy id is
    # never auto-bound: it fails closed with the binding instructions.
    publication = getattr(slice, "publication", None)
    bound_head = (
        getattr(publication, "head_sha", None) if publication is not None else None
    )
    pr_number = slice.pr_number
    legacy_binding_id: int | None = None
    if store is not None:
        legacy_binding_id = _read_legacy_binding(
            store,
            slice.id,
            parsed_cause,
            attempt,
            pr_number=pr_number,
            head_sha=bound_head,
        )
        if legacy_binding_id is None:
            recorded = _recorded_legacy_issue_id(store)
            if recorded is not None:
                raise EscalationError(
                    f"run failure reason references Chainlink issue {recorded}, but it cannot "
                    f"be proven to belong to slice {slice.id!r} attempt {attempt} and "
                    f"publication PR {pr_number} head {bound_head!r}. Verify the issue for this "
                    "publication, then bind it before retrying: bind_legacy_escalation(store, "
                    f"slice_id={slice.id!r}, cause={parsed_cause!r}, attempt={attempt}, "
                    f"issue_id={recorded}, pr_number=..., head_sha=...)"
                )
    issue_id = _create_issue(
        issue_creator,
        slice,
        parsed_cause,
        parked_audit,
        attempt=attempt,
        pr_number=pr_number,
        head_sha=bound_head,
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


def _escalation_identity(
    run_id: str, slice_id: str, attempt: int, cause: ParkCause
) -> dict[str, object]:
    """The full durable identity of one slice escalation."""
    return {
        "run_id": run_id,
        "slice_id": slice_id,
        "attempt": attempt,
        "cause": cause.value,
    }


def _escalation_key(run_id: str, slice_id: str, attempt: int, cause: ParkCause) -> str:
    """Canonical per-run, per-attempt identity for one slice escalation."""
    return dumps_json(_escalation_identity(run_id, slice_id, attempt, cause), sort_keys=True)


def _intent_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _escalation_intent_path(store: RunStore, key: str) -> Path:
    """Collision-resistant intent path hashed from the full canonical identity."""
    return Path(store.run_dir) / "escalations" / f"intent-{_intent_digest(key)}.json"


def _pre_389_escalation_key(
    run_id: str, slice_id: str, attempt: int, cause: ParkCause
) -> str:
    """The exact pre-389 key format, reconstructed from commit f80df647."""
    return f"task-escalation:{run_id}:{slice_id}:{attempt}:{cause.value}"


def _legacy_escalation_intent_path(
    store: RunStore, run_id: str, slice_id: str, attempt: int, cause: ParkCause
) -> Path:
    """Pre-389 sanitized intent path for the same escalation (for migration)."""
    key = _pre_389_escalation_key(run_id, slice_id, attempt, cause)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in key)
    return Path(store.run_dir) / "escalations" / f"{safe}.json"


def _legacy_binding_path(
    store: RunStore, slice_id: str, cause: ParkCause, attempt: int
) -> Path:
    safe = "".join(
        ch if ch.isalnum() or ch in "-_." else "_"
        for ch in f"{slice_id}:{cause.value}:{attempt}"
    )
    return Path(store.run_dir) / "escalations" / f"legacy-{safe}.json"


def bind_legacy_escalation(
    store: RunStore,
    *,
    slice_id: str,
    cause: ParkCause,
    attempt: int,
    issue_id: int,
    pr_number: int,
    head_sha: str,
) -> Path:
    """Bind a pre-run-identity escalation issue (for example Beast #816).

    This is the narrowly verified migration path for legacy untagged issues. The
    caller must supply the exact publication PR number and head SHA that the
    issue belongs to, plus the attempt it was filed for; reconciliation only
    reuses the issue when all of those match the slice's publication. An
    untagged issue is never adopted without a fully verified binding.
    """
    if type(issue_id) is not int or issue_id <= 0:
        raise ValueError("legacy escalation binding requires a positive issue id")
    if type(attempt) is not int or attempt <= 0:
        raise ValueError("legacy escalation binding requires a positive attempt")
    if type(pr_number) is not int or pr_number <= 0:
        raise ValueError("legacy escalation binding requires a positive pr_number")
    if not isinstance(head_sha, str) or not head_sha:
        raise ValueError("legacy escalation binding requires a non-empty head_sha")
    path = _legacy_binding_path(store, slice_id, cause, attempt)
    _write_intent(
        path,
        {
            "slice_id": slice_id,
            "cause": cause.value,
            "attempt": attempt,
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
    attempt: int,
    *,
    pr_number: int | None,
    head_sha: str | None,
) -> int | None:
    """Return a fully verified legacy issue id, or None when none applies.

    A binding is only usable when the slice still records the same publication
    PR and head and the binding is scoped to the current attempt.
    """
    if pr_number is None or not head_sha:
        return None
    path = _legacy_binding_path(store, slice_id, cause, attempt)
    record = _read_intent(path)
    if record is None:
        return None
    bound_id = _issue_id(record)
    if bound_id is None:
        raise EscalationError(f"legacy escalation binding at {path} has no issue id")
    if (
        record.get("slice_id") != slice_id
        or record.get("cause") != cause.value
        or record.get("attempt") != attempt
    ):
        return None
    if record.get("pr_number") != pr_number or record.get("head_sha") != head_sha:
        return None
    return bound_id


_LEGACY_ISSUE_ID_PATTERN = re.compile(
    r"(?:cicoIssueId|issue_id|['\"]id['\"])\s*['\"]?\s*[:=]\s*['\"]?(\d+)"
)


def _recorded_legacy_issue_id(store: RunStore) -> int | None:
    """Recover an issue id recorded in this run's failure diagnostics.

    The pre-fix Beast checkpoint persisted
    `chainlink issue result has no positive issue ID: {'cicoIssueId': 816}` in
    its recursive failure reason, so the issue created before the crash is
    durably recoverable. This is deliberately narrow: only a run whose failure
    reason names a Chainlink-created issue and that has exactly one slice (the
    slice being recovered) is eligible.
    """
    state = store.load()
    if len(state.slices) != 1:
        return None
    reason = getattr(getattr(state, "recursive_fsm", None), "reason", None)
    if not isinstance(reason, str) or "chainlink issue" not in reason.lower():
        return None
    match = _LEGACY_ISSUE_ID_PATTERN.search(reason)
    if match is None:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


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
    is published atomically under an exclusive lock: a temporary file is written
    in full and hard-linked into place, so the token path is never visible empty
    or partial and a fallback replace cannot clobber another writer's token.
    """
    path = Path(store.run_dir) / "escalations" / "run.token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return _read_run_token(path)
    lock_path = path.with_name(f"{path.name}.lock")
    with open(lock_path, "w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            # Re-check under the lock: another writer may have published first.
            if path.exists():
                return _read_run_token(path)
            token = os.urandom(8).hex()
            temporary = path.with_name(
                f"{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
            )
            try:
                with open(temporary, "w", encoding="utf-8") as handle:
                    handle.write(token)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    return _read_run_token(path)
                except OSError:
                    # Hard links are unavailable. The lock is held, so this
                    # replace cannot clobber a concurrent writer's token.
                    os.replace(temporary, path)
            finally:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            return token
        except OSError as error:
            raise EscalationError(
                f"could not publish run token at {path}: {error}"
            ) from error
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _escalation_locks(
    store: RunStore, run_id: str, slice_id: str, attempt: int, cause: ParkCause
):
    """Serialize one escalation across both the pre-389 and digest lock files.

    A pre-389 invocation holds only the historical lock, so a new invocation
    must acquire that lock as well. New invocations always take the historical
    lock first, then the digest lock, giving a consistent order. Both are held
    across inspection, reconciliation, and remote issue creation.
    """
    key = _escalation_key(run_id, slice_id, attempt, cause)
    digest_path = _escalation_intent_path(store, key)
    legacy_path = _legacy_escalation_intent_path(store, run_id, slice_id, attempt, cause)
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    with open(legacy_path.with_suffix(".lock"), "w", encoding="utf-8") as legacy_handle:
        fcntl.flock(legacy_handle.fileno(), fcntl.LOCK_EX)
        try:
            with open(
                digest_path.with_suffix(".lock"), "w", encoding="utf-8"
            ) as digest_handle:
                fcntl.flock(digest_handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield digest_path, legacy_path
                finally:
                    fcntl.flock(digest_handle.fileno(), fcntl.LOCK_UN)
        finally:
            fcntl.flock(legacy_handle.fileno(), fcntl.LOCK_UN)


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
    """List needs-human issues (open and closed), failing closed on uncertainty.

    Closed issues are included so a crash after creation is still reconciled
    when the issue is closed by an operator before the retry.
    """
    list_issues = getattr(creator, "chainlink_issue_list", None)
    if not callable(list_issues):
        raise IssueLookupUnavailable(
            "durable escalation requires chainlink_issue_list with status='all' "
            "to reconcile an issue created before its ID was recorded"
        )
    try:
        result = list_issues(labels=("needs-human",), status="all")
    except Exception as error:
        # A creator that cannot filter by status would only see open issues,
        # which could duplicate an issue closed before the retry. Fail closed.
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


def _find_run_scoped_issue(creator: object, title: str) -> int | None:
    """Return the issue whose title is this run's exact escalation title."""
    wanted = title.strip()
    for issue in _list_needs_human_issues(creator):
        issue_id = _issue_id(issue)
        if issue_id is None:
            continue
        issue_title = issue.get("title") if isinstance(issue, Mapping) else None
        if isinstance(issue_title, str) and issue_title.strip() == wanted:
            return issue_id
    return None


def _intent_title_matches(intent: Mapping[str, object], title: str) -> bool:
    """Whether a stored intent carries the exact expected escalation title."""
    stored = intent.get("title")
    return isinstance(stored, str) and stored == title


def _intent_identity_matches(
    intent: Mapping[str, object],
    run_id: str,
    slice_id: str,
    attempt: int,
    cause: ParkCause,
) -> bool:
    """Whether a stored intent carries this exact full identity.

    Missing keys never match; only a fully recorded identity is accepted.
    """
    return _escalation_identity(run_id, slice_id, attempt, cause) == {
        "run_id": intent.get("run_id"),
        "slice_id": intent.get("slice_id"),
        "attempt": intent.get("attempt"),
        "cause": intent.get("cause"),
    }


def _intent_provenance_matches(
    intent: Mapping[str, object], pr_number: int | None, head_sha: str | None
) -> bool:
    """Whether a pre-failure intent recorded the current publication identity.

    The provenance keys must be present; an absent key is not explicit null
    provenance, so it fails closed.
    """
    if "pr_number" not in intent or "head_sha" not in intent:
        return False
    return intent.get("pr_number") == pr_number and intent.get("head_sha") == head_sha


def _write_escalation_intent(
    path: Path | None,
    *,
    run_id: str,
    slice_id: str,
    attempt: int,
    cause: ParkCause,
    title: str,
    pr_number: int | None,
    head_sha: str | None,
    issue_id: int | None,
    state: str,
) -> None:
    if path is None:
        return
    document: dict[str, object] = {
        "run_id": run_id,
        "slice_id": slice_id,
        "attempt": attempt,
        "cause": cause.value,
        "title": title,
        "state": state,
        "pr_number": pr_number,
        "head_sha": head_sha,
    }
    if issue_id is not None:
        document["issue_id"] = issue_id
    _write_intent(path, document)


def _create_issue(
    creator: IssueCreator | EffectClient | Callable[[str, str], int],
    slice: SliceState,
    cause: ParkCause,
    audit: Mapping[str, object],
    *,
    attempt: int = 1,
    pr_number: int | None = None,
    head_sha: str | None = None,
    store: RunStore | None = None,
    legacy_binding_id: int | None = None,
) -> int:
    base_title = _attempt_scoped_title(slice.id, cause, attempt)
    if store is not None:
        run_id = store.run_id
        title = f"{base_title} [run {_run_token(store)}]"
        with _escalation_locks(
            store, run_id, slice.id, attempt, cause
        ) as (intent_path, legacy_intent_path):
            return _create_issue_locked(
                creator,
                slice,
                cause,
                audit,
                run_id,
                title,
                attempt,
                pr_number,
                head_sha,
                legacy_binding_id,
                intent_path,
                legacy_intent_path,
            )
    return _create_issue_locked(
        creator,
        slice,
        cause,
        audit,
        "",
        base_title,
        attempt,
        pr_number,
        head_sha,
        legacy_binding_id,
        None,
        None,
    )


def _create_issue_locked(
    creator: IssueCreator | EffectClient | Callable[[str, str], int],
    slice: SliceState,
    cause: ParkCause,
    audit: Mapping[str, object],
    run_id: str,
    title: str,
    attempt: int,
    pr_number: int | None,
    head_sha: str | None,
    legacy_binding_id: int | None,
    intent_path: Path | None,
    legacy_intent_path: Path | None,
) -> int:
    # Inspect both intent formats under the lock. A digest intent must never
    # cause a pre-digest intent to be skipped, or the mixed-version upgrade
    # state could duplicate an issue.
    digest_intent = _read_intent(intent_path) if intent_path is not None else None
    legacy_intent = (
        _read_intent(legacy_intent_path) if legacy_intent_path is not None else None
    )
    # An intent whose title differs from the expected title must never authorize
    # reuse, reconciliation, migration, or creation.
    if digest_intent is not None and not _intent_title_matches(digest_intent, title):
        raise EscalationError(
            f"escalation intent at {intent_path} has title {digest_intent.get('title')!r}, "
            f"expected {title!r}; refusing to reuse or create an issue"
        )
    if legacy_intent is not None and not _intent_title_matches(legacy_intent, title):
        raise EscalationError(
            f"pre-upgrade escalation intent at {legacy_intent_path} has title "
            f"{legacy_intent.get('title')!r}, expected {title!r}; refusing to reuse or create "
            "an issue"
        )
    intent = digest_intent
    if legacy_intent is not None:
        legacy_proven = _intent_identity_matches(
            legacy_intent, run_id, slice.id, attempt, cause
        ) and _intent_provenance_matches(legacy_intent, pr_number, head_sha)
        if not legacy_proven:
            raise EscalationError(
                f"existing pre-upgrade escalation intent at {legacy_intent_path} cannot be "
                f"proven to belong to run {run_id!r} slice {slice.id!r} attempt {attempt} "
                f"for PR {pr_number} head {head_sha!r}; refusing to create a possible "
                "duplicate. Verify the recorded issue and rebind it explicitly."
            )
        legacy_recorded = _issue_id(legacy_intent)
        digest_recorded = _issue_id(digest_intent) if digest_intent is not None else None
        if (
            legacy_recorded is not None
            and digest_recorded is not None
            and legacy_recorded != digest_recorded
        ):
            raise EscalationError(
                f"conflicting escalation intents at {legacy_intent_path} and {intent_path} "
                f"record different issues ({legacy_recorded} vs {digest_recorded}); refusing "
                "to reuse or create an issue"
            )
        if digest_intent is not None and not (
            _intent_identity_matches(
                digest_intent, run_id, slice.id, attempt, cause
            )
            and _intent_provenance_matches(digest_intent, pr_number, head_sha)
        ):
            raise EscalationError(
                f"escalation intent at {intent_path} cannot be proven for run {run_id!r} "
                f"slice {slice.id!r} attempt {attempt}; refusing to reuse or create an issue"
            )
        # Proven consistent: prefer the record that carries an issue id and
        # migrate it into the digest path.
        if legacy_recorded is not None or digest_intent is None:
            _write_intent(intent_path, legacy_intent)
            intent = legacy_intent
    if intent is not None:
        if not _intent_identity_matches(intent, run_id, slice.id, attempt, cause):
            raise EscalationError(
                f"escalation intent at {intent_path} does not match run {run_id!r} slice "
                f"{slice.id!r} attempt {attempt} cause {cause.value!r}; refusing to reuse it"
            )
        recorded = _issue_id(intent)
        if recorded is not None:
            # A durable pre-failure record is authoritative only when it also
            # proves the same publication identity for this attempt.
            if _intent_provenance_matches(intent, pr_number, head_sha):
                return recorded
            raise EscalationError(
                f"escalation intent for slice {slice.id!r} records issue {recorded} but does "
                "not match the current publication PR/head; refusing to reuse it without "
                "operator verification"
            )
    # Operator-verified legacy binding (for example Beast #816).
    if legacy_binding_id is not None:
        _write_escalation_intent(
            intent_path,
            run_id=run_id,
            slice_id=slice.id,
            attempt=attempt,
            cause=cause,
            title=title,
            pr_number=pr_number,
            head_sha=head_sha,
            issue_id=legacy_binding_id,
            state="bound_legacy",
        )
        return legacy_binding_id
    # A run-scoped issue may be reused only when the pre-failure intent proves
    # the same publication PR/head for this attempt.
    if intent_path is not None:
        matched = _find_run_scoped_issue(creator, title)
        if matched is not None:
            if intent is not None and _intent_provenance_matches(
                intent, pr_number, head_sha
            ):
                _write_escalation_intent(
                    intent_path,
                    run_id=run_id,
                    slice_id=slice.id,
                    attempt=attempt,
                    cause=cause,
                    title=title,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    issue_id=matched,
                    state="reconciled",
                )
                return matched
            raise EscalationError(
                f"found escalation issue {matched} for slice {slice.id!r} but cannot prove it "
                "belongs to this attempt's publication; refusing to reuse it without operator "
                "verification"
            )
    _write_escalation_intent(
        intent_path,
        run_id=run_id,
        slice_id=slice.id,
        attempt=attempt,
        cause=cause,
        title=title,
        pr_number=pr_number,
        head_sha=head_sha,
        issue_id=None,
        state="requested",
    )
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
    _write_escalation_intent(
        intent_path,
        run_id=run_id,
        slice_id=slice.id,
        attempt=attempt,
        cause=cause,
        title=title,
        pr_number=pr_number,
        head_sha=head_sha,
        issue_id=issue_id,
        state="created",
    )
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
