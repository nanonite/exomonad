"""Reconcile a timed-out launch-capable blocked-resume from host evidence.

A launch-capable effect (``resume_blocked_leaf``) may exceed any client
deadline: the host first queues the request behind its own work and then runs a
handler that launches a process. A client disconnect therefore says nothing
about whether the effect happened. The controller journals the request as
``unknown`` and this module resolves that entry from the host's durable ledger,
keyed by the exact ``dispatch_intent_id`` and bound to the journaled
owner/predecessor coordinates.

Only two outcomes are authoritative:

* ``adopt`` - exactly one ``agent.spawned`` row for the intent names the
  journaled owner, branch, and a successor invocation whose lineage the owner
  records prove.
* ``reject`` - exactly one host-recorded ``tool.called`` refusal row for the
  intent carries one of the shared prelaunch refusal errors and reproduces the
  journaled owner/predecessor binding.

Everything else is a ``hold``: no evidence yet, a terminal failure row, a
duplicate row, or conflicting rows. A hold never becomes a relaunch by itself;
the caller keeps the journal entry and re-evaluates it on a later tick.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from tl_loop.events.envelope import EnvelopeError
from tl_loop.events.reader import LedgerReadError

from .blocked_leaf_resume import BlockedLeafOwnerError, resolve_owner_for_resume_retry
from .launch_evidence import (
    BLOCKED_RESUME_OPERATION,
    HOST_EVIDENCE_BOUNDED_WINDOW_SECONDS,
    LAUNCH_EVIDENCE_EVENT_TYPES,
    evidence_label,
    format_evidence,
    is_failure_row,
    is_launch_row,
    is_refusal_row,
    row_data,
    scoped_launch_evidence,
)
from .prelaunch_refusal import (
    is_role_route_configuration_fingerprint,
)

#: Journal arguments a blocked-resume entry must carry before host evidence can
#: be bound to it. These are written by the controller before dispatch.
_REQUIRED_TEXT_ARGUMENTS = (
    "owner_agent_name",
    "expected_invocation_id",
    "expected_branch",
    "expected_worktree",
    "expected_worktree_recorded",
    "expected_worktree_fingerprint",
    "dispatch_intent_id",
    "task",
)

LaunchOutcome = Literal["adopt", "reject", "hold"]


class LaunchReconciliationError(ValueError):
    """The journaled request cannot be reconciled as a blocked-leaf resume."""


@dataclass(frozen=True)
class LaunchReconciliation:
    """One decision about a journaled unknown launch-capable effect."""

    outcome: LaunchOutcome
    intent_id: str
    diagnostic: str
    evidence: tuple[str, ...] = ()
    result: Mapping[str, object] | None = None
    error: str | None = None
    invocation_id: str | None = None
    #: False only when the durable ledger could not be read. An empty
    #: ``evidence`` tuple then means "unknown", not "proven absent".
    evidence_read: bool = True

    def audit(self, *, evaluated_at: float, evaluations: int) -> dict[str, object]:
        """Return the durable journal audit record for this evaluation."""
        return {
            "outcome": self.outcome,
            "intent_id": self.intent_id,
            "diagnostic": self.diagnostic,
            "evidence": list(self.evidence),
            "evaluated_at": evaluated_at,
            "evaluations": evaluations,
        }


def reconcile_unknown_blocked_resume(
    project_root: str | Path,
    entry: Mapping[str, object],
    *,
    segments_dir: str | Path | None = None,
    ledger_run_id: str | None = None,
    now: float | None = None,
) -> LaunchReconciliation:
    """Classify one unknown blocked-resume entry from durable host evidence.

    Reads only the immutable ledger and the owner registry. Never dispatches,
    never mutates the journal, and never turns absent or ambiguous evidence into
    an authorized launch.
    """
    observed_at = time.time() if now is None else now
    try:
        arguments = _validated_arguments(entry)
        intent_id = cast(str, arguments["dispatch_intent_id"])
    except LaunchReconciliationError as error:
        return LaunchReconciliation(
            "hold",
            "",
            f"blocked-resume journal entry is unusable: {error}",
            evidence_read=False,
        )
    try:
        documents = scoped_launch_evidence(
            Path(project_root),
            arguments,
            segments_dir=segments_dir,
            ledger_run_id=ledger_run_id,
        )
    except (LedgerReadError, EnvelopeError, OSError, ValueError) as error:
        return LaunchReconciliation(
            "hold",
            intent_id,
            (
                f"blocked-resume intent {intent_id} cannot read durable host evidence "
                f"({type(error).__name__}); the journal entry stays held"
            ),
            evidence_read=False,
        )

    launches = [
        document for document in documents if is_launch_row(document, intent_id)
    ]
    refusals = [
        document for document in documents if is_refusal_row(document, intent_id)
    ]
    failures = [
        document for document in documents if is_failure_row(document, intent_id)
    ]
    evidence = tuple(
        evidence_label(document) for document in (*launches, *refusals, *failures)
    )
    if launches and refusals:
        return _hold_conflict(intent_id, evidence, "launch and prelaunch refusal rows")
    if len(launches) > 1:
        return _hold_conflict(intent_id, evidence, "duplicate launch rows")
    if len(refusals) > 1:
        return _hold_conflict(intent_id, evidence, "duplicate prelaunch refusal rows")
    if failures and not launches and not refusals:
        return LaunchReconciliation(
            "hold",
            intent_id,
            (
                f"blocked-resume intent {intent_id} has a host terminal failure row but no "
                "prelaunch refusal or launch proof (last seen: "
                f"{format_evidence(evidence)}); the journal entry stays held"
            ),
            evidence=evidence,
        )
    if launches:
        return _adopt(
            Path(project_root),
            entry,
            arguments,
            launches[0],
            intent_id,
            evidence,
        )
    if refusals:
        return _reject(
            arguments, refusals[0], cast(str, entry["target"]), intent_id, evidence
        )
    return _hold_without_evidence(arguments, intent_id, observed_at)


def _validated_arguments(entry: Mapping[str, object]) -> Mapping[str, object]:
    if entry.get("operation") != BLOCKED_RESUME_OPERATION:
        raise LaunchReconciliationError("operation is not resume_blocked_leaf")
    status = entry.get("status")
    if status not in {"unknown", "intended"}:
        raise LaunchReconciliationError(f"journal status {status!r} is not unresolved")
    if not isinstance(entry.get("target"), str) or not cast(str, entry["target"]).strip():
        raise LaunchReconciliationError("target slice id is missing")
    arguments = entry.get("arguments")
    if not isinstance(arguments, Mapping):
        raise LaunchReconciliationError("arguments are missing")
    for key in _REQUIRED_TEXT_ARGUMENTS:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise LaunchReconciliationError(f"argument {key} is missing")
    issue_id = arguments.get("chainlink_issue_id")
    prior_generation = arguments.get("prior_generation")
    if type(issue_id) is not int or issue_id <= 0:
        raise LaunchReconciliationError("chainlink_issue_id is invalid")
    if type(prior_generation) is not int or prior_generation < 1:
        raise LaunchReconciliationError("prior_generation is invalid")
    started_at = arguments.get("dispatch_started_at")
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        raise LaunchReconciliationError("dispatch_started_at is missing")
    return arguments


def _adopt(
    project_root: Path,
    entry: Mapping[str, object],
    arguments: Mapping[str, object],
    launch: Mapping[str, object],
    intent_id: str,
    evidence: tuple[str, ...],
) -> LaunchReconciliation:
    data = row_data(launch)
    try:
        caller = _required_row_text(launch, "agent_id")
        owner_name = cast(str, arguments["owner_agent_name"])
        if caller == owner_name:
            raise LaunchReconciliationError("launch row caller is the owner, not a controller")
        if data.get("child_agent") != owner_name:
            raise LaunchReconciliationError("launch row names a different owner")
        if data.get("spawn_type") != "resume_blocked":
            raise LaunchReconciliationError("launch row is not a blocked-resume launch")
        if data.get("branch") != arguments["expected_branch"]:
            raise LaunchReconciliationError("launch row branch conflicts with the journal")
        invocation_id = data.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id.strip():
            raise LaunchReconciliationError("launch row has no invocation id")
        identity = _read_object(
            project_root / ".exo" / "agents" / owner_name / "identity.json",
            "owner identity",
        )
        parent_branch = identity.get("parent_branch")
        if not isinstance(parent_branch, str) or not parent_branch.strip():
            raise LaunchReconciliationError("owner identity has no parent branch")
        owner, successor = resolve_owner_for_resume_retry(
            project_root,
            slice_id=cast(str, entry["target"]),
            issue_id=cast(int, arguments["chainlink_issue_id"]),
            expected_parent_branch=parent_branch,
            expected_agent_name=owner_name,
            prior_invocation_id=cast(str, arguments["expected_invocation_id"]),
            prior_generation=cast(int, arguments["prior_generation"]),
            expected_branch=cast(str, arguments["expected_branch"]),
            expected_worktree=cast(str, arguments["expected_worktree"]),
            expected_worktree_recorded=cast(str, arguments["expected_worktree_recorded"]),
            expected_worktree_fingerprint=cast(
                str, arguments["expected_worktree_fingerprint"]
            ),
            dispatch_intent_id=intent_id,
        )
        routing = _read_object(owner.directory / "routing.json", "owner routing")
        if (
            routing.get("owner_agent_id") != owner.agent_name
            or routing.get("owner_invocation_id") != invocation_id
            or routing.get("owner_generation") != owner.generation + 1
        ):
            raise LaunchReconciliationError(
                "owner routing does not name the journaled successor"
            )
    except (BlockedLeafOwnerError, LaunchReconciliationError, OSError, ValueError, TypeError) as error:
        return LaunchReconciliation(
            "hold",
            intent_id,
            (
                f"blocked-resume intent {intent_id} has a launch row that does not bind to "
                f"the journaled owner/predecessor ({_safe_detail(error)}); the journal entry "
                "stays held"
            ),
            evidence=evidence,
        )
    if successor is None:
        return LaunchReconciliation(
            "hold",
            intent_id,
            (
                f"blocked-resume intent {intent_id} has a launch row but the owner registry "
                "still names the predecessor; the journal entry stays held"
            ),
            evidence=evidence,
        )
    recorded_successor = successor.get("invocation_id")
    if recorded_successor != invocation_id:
        return LaunchReconciliation(
            "hold",
            intent_id,
            (
                f"blocked-resume intent {intent_id} launch row invocation {invocation_id!r} "
                f"differs from the owner successor {recorded_successor!r}; the journal entry "
                "stays held"
            ),
            evidence=evidence,
        )
    successor_id = recorded_successor
    runtime = successor.get("runtime")
    role_route = successor.get("role_route")
    if not isinstance(runtime, str) or not runtime.strip():
        return LaunchReconciliation(
            "hold",
            intent_id,
            (
                f"blocked-resume intent {intent_id} successor has no runtime; the journal "
                "entry stays held"
            ),
            evidence=evidence,
        )
    if not isinstance(role_route, Mapping):
        return LaunchReconciliation(
            "hold",
            intent_id,
            (
                f"blocked-resume intent {intent_id} successor has no route receipt; the "
                "journal entry stays held"
            ),
            evidence=evidence,
        )
    result = _launch_result(entry, arguments, owner_name, successor_id, runtime, role_route)
    return LaunchReconciliation(
        "adopt",
        intent_id,
        (
            f"adopted blocked-resume intent {intent_id} successor {successor_id} from "
            f"{evidence_label(launch)}; no relaunch is issued"
        ),
        evidence=evidence,
        result=result,
        invocation_id=successor_id,
    )


def _launch_result(
    entry: Mapping[str, object],
    arguments: Mapping[str, object],
    owner_name: str,
    successor_id: str,
    runtime: str,
    role_route: Mapping[str, object],
) -> dict[str, object]:
    return {
        "success": True,
        "error": None,
        "result": {
            "chainlink_issue_id": cast(int, arguments["chainlink_issue_id"]),
            "agent": {
                "id": owner_name,
                "issue": cast(str, entry["target"]),
                "intent_id": cast(str, arguments["dispatch_intent_id"]),
                "agent_type": runtime,
                "branch_name": cast(str, arguments["expected_branch"]),
                "worktree_path": cast(str, arguments["expected_worktree"]),
                "role_route": dict(role_route),
            },
            "invocation": {
                "invocation_id": successor_id,
                "trigger": "resume_blocked",
                "runtime": runtime,
                "ready": True,
                "fresh": False,
                "outcome": "adopted_after_response_loss",
                "branch_name": cast(str, arguments["expected_branch"]),
                "role_route": dict(role_route),
            },
        },
    }


def _reject(
    arguments: Mapping[str, object],
    refusal: Mapping[str, object],
    slice_id: str,
    intent_id: str,
    evidence: tuple[str, ...],
) -> LaunchReconciliation:
    data = row_data(refusal)
    error = cast(str, data["error"])
    try:
        if not is_role_route_configuration_fingerprint(
            arguments.get("route_configuration_fingerprint")
        ):
            raise LaunchReconciliationError(
                "journal entry carries no versioned route configuration receipt"
            )
        caller = _required_row_text(refusal, "agent_id")
        if caller == arguments["owner_agent_name"]:
            raise LaunchReconciliationError("refusal row caller is the owner, not a controller")
        _validate_refusal_binding(arguments, data, slice_id)
    except LaunchReconciliationError as binding_error:
        return LaunchReconciliation(
            "hold",
            intent_id,
            (
                f"blocked-resume intent {intent_id} has a prelaunch refusal row that does not "
                f"bind to the journaled owner/predecessor ({binding_error}); the journal entry "
                "stays held"
            ),
            evidence=evidence,
        )
    return LaunchReconciliation(
        "reject",
        intent_id,
        (
            f"blocked-resume intent {intent_id} was refused before launch at "
            f"{evidence_label(refusal)}: {_safe_detail(error)}; retry policy is the shared "
            "prelaunch refusal contract"
        ),
        evidence=evidence,
        result={
            "success": False,
            "result": None,
            "error": error,
            "error_kind": data.get("error_kind"),
            "error_context": data.get("error_context"),
        },
        error=error,
    )


def _validate_refusal_binding(
    arguments: Mapping[str, object], data: Mapping[str, object], slice_id: str
) -> None:
    request = data.get("arguments")
    if not isinstance(request, Mapping):
        raise LaunchReconciliationError("refusal row has no request arguments")
    for key in ("expected_invocation_id", "expected_branch", "expected_worktree_fingerprint"):
        if request.get(key) != arguments.get(key):
            raise LaunchReconciliationError(f"refusal row {key} conflicts with the journal")
    if request.get("chainlink_issue_id") != arguments.get("chainlink_issue_id"):
        raise LaunchReconciliationError("refusal row issue conflicts with the journal")
    route = request.get("role_route")
    if not isinstance(route, Mapping):
        raise LaunchReconciliationError("refusal row has no role route")
    if route.get("dispatch_intent_id") != arguments.get("dispatch_intent_id"):
        raise LaunchReconciliationError("refusal row intent conflicts with the journal")
    if route.get("scope_id") != slice_id:
        raise LaunchReconciliationError("refusal row scope conflicts with the journal")


def _hold_conflict(
    intent_id: str, evidence: tuple[str, ...], detail: str
) -> LaunchReconciliation:
    return LaunchReconciliation(
        "hold",
        intent_id,
        (
            f"blocked-resume intent {intent_id} has conflicting host evidence "
            f"({detail}; last seen: {format_evidence(evidence)}); the journal entry stays held"
        ),
        evidence=evidence,
    )


def _hold_without_evidence(
    arguments: Mapping[str, object],
    intent_id: str,
    observed_at: float,
) -> LaunchReconciliation:
    started_at = float(cast(float, arguments["dispatch_started_at"]))
    waited = max(0.0, observed_at - started_at)
    searched = ", ".join(LAUNCH_EVIDENCE_EVENT_TYPES)
    if waited >= HOST_EVIDENCE_BOUNDED_WINDOW_SECONDS:
        diagnostic = (
            f"blocked-resume intent {intent_id} has no host evidence after the "
            f"{HOST_EVIDENCE_BOUNDED_WINDOW_SECONDS:.0f}s bounded window "
            f"(waited {waited:.1f}s; searched {searched}; last seen: none); the journal "
            "entry stays held and is never relaunched on absence of evidence"
        )
    else:
        diagnostic = (
            f"blocked-resume intent {intent_id} has no host evidence yet "
            f"(waited {waited:.1f}s; searched {searched}; last seen: none); the journal "
            "entry stays held"
        )
    return LaunchReconciliation("hold", intent_id, diagnostic)


def _required_row_text(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LaunchReconciliationError(f"evidence row has no {key}")
    return value


def _read_object(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LaunchReconciliationError(f"{label} is unavailable or malformed") from error
    if not isinstance(value, Mapping):
        raise LaunchReconciliationError(f"{label} is not an object")
    return value


def _safe_detail(detail: object) -> str:
    text = "".join(character if character.isprintable() else " " for character in str(detail))
    return " ".join(text.split())[:200]
