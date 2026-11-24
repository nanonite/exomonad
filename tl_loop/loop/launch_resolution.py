"""Operator resolution for one held, unresolved launch-capable child effect.

A child controller journals a launch-capable request as ``unknown`` when the
client stopped waiting. The child's own hold is correct: absence of host
evidence never authorizes a relaunch. The parent controller must not translate
that hold into a controller-fatal failure, because an operator can judge an
unobserved launch after inspecting the owner registry, worktree and ledger.

This module classifies the one case an operator may resolve:

* exactly one journaled launch effect is pending;
* it is a ``resume_blocked_leaf`` request recorded as ``unknown``;
* the durable ledger was read and contains no ``agent.spawned`` row, refusal or
  terminal failure row for that exact intent;
* the journaled owner, predecessor and worktree still bind to exactly one
  terminal (not live) owner invocation, and no successor was launched.

Everything else stays a fail-closed hard failure: conflicting or duplicate
evidence, a live owner, a forged or stale intent, an already-launched successor,
or any non-launch effect. Resolutions are append-only records linked to the
original journal entry; the original ``unknown`` bytes are never edited.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from .blocked_leaf_resume import (
    BlockedLeafOwnerError,
    blocked_resume_intent_id,
    resolve_owner_for_resume_retry,
)
from .journal import ActionJournalError, EffectJournal
from .launch_evidence import BLOCKED_RESUME_OPERATION, format_evidence
from .launch_reconciliation import reconcile_unknown_blocked_resume

#: Gate names are scoped to the child, the intent and the operator action so a
#: later launch attempt never inherits an earlier decision.
HELD_LAUNCH_GATE_PREFIX = "tl-held-launch-effect-"

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


@dataclass(frozen=True)
class HeldLaunchEffect:
    """One unresolved launch effect an operator may abandon or retry."""

    child_id: str
    slice_id: str
    entry_key: str
    journal_index: int
    intent_id: str
    operation: str
    journal_status: str
    owner_agent_name: str
    expected_invocation_id: str
    expected_branch: str
    expected_worktree: str
    expected_worktree_fingerprint: str
    owner_invocation_status: str
    last_host_evidence: str
    diagnostic: str
    arguments: Mapping[str, object] = field(default_factory=dict, compare=False)

    @property
    def abandon_gate(self) -> str:
        return _gate_name(self, "abandon")

    @property
    def retry_gate(self) -> str:
        return _gate_name(self, "retry")

    @property
    def abandoned(self) -> bool:
        """Whether an earlier approval already abandoned the unproven launch."""
        return self.journal_status == "operator_abandoned"

    def proof(self) -> dict[str, object]:
        """The machine-checked facts an operator resolution is bound to."""
        return {
            "child_id": self.child_id,
            "slice_id": self.slice_id,
            "intent_id": self.intent_id,
            "journal_index": self.journal_index,
            "operation": self.operation,
            "owner_agent_name": self.owner_agent_name,
            "expected_invocation_id": self.expected_invocation_id,
            "owner_invocation_status": self.owner_invocation_status,
            "expected_branch": self.expected_branch,
            "expected_worktree_fingerprint": self.expected_worktree_fingerprint,
            "last_host_evidence": self.last_host_evidence,
            "launch_row_for_intent": False,
        }


@dataclass(frozen=True)
class HeldLaunchClassification:
    """The actionable hold, or the fail-closed reason it is not actionable."""

    effect: HeldLaunchEffect | None = None
    reason: str | None = None


def _intent_suffix(intent_id: str) -> str:
    text = intent_id.rpartition(":")[2] or intent_id
    return "".join(character for character in text if character.isalnum())[:12] or "intent"


def _gate_name(effect: HeldLaunchEffect, action: str) -> str:
    return (
        f"{HELD_LAUNCH_GATE_PREFIX}{effect.child_id}-"
        f"{_intent_suffix(effect.intent_id)}-{action}"
    )


def _required_text(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"journaled {key} is missing")
    return value


def _required_int(arguments: Mapping[str, object], key: str) -> int:
    value = arguments.get(key)
    if type(value) is not int:
        raise ValueError(f"journaled {key} is not an integer")
    return value


def _actionable(
    child_id: str,
    entry: Mapping[str, object],
    journal_index: int,
    *,
    project_root: str | Path,
    ledger_run_id: str | None,
    expected_parent_branch: str,
    journal_status: str = "unknown",
) -> HeldLaunchClassification:
    arguments = entry.get("arguments")
    if not isinstance(arguments, Mapping):
        return HeldLaunchClassification(
            reason="held launch effect journal arguments are malformed"
        )
    try:
        for key in _REQUIRED_TEXT_ARGUMENTS:
            _required_text(arguments, key)
        _required_int(arguments, "chainlink_issue_id")
        _required_int(arguments, "prior_generation")
        intent_id = cast(str, arguments["dispatch_intent_id"])
    except ValueError as error:
        return HeldLaunchClassification(
            reason=f"held launch effect identity is not provable: {error}"
        )
    target = entry.get("target")
    if not isinstance(target, str) or not target:
        return HeldLaunchClassification(
            reason="held launch effect journal entry names no slice"
        )
    slice_id = target

    decision = reconcile_unknown_blocked_resume(
        project_root,
        entry,
        ledger_run_id=ledger_run_id,
    )
    if not decision.evidence_read:
        return HeldLaunchClassification(
            reason=(
                f"held launch effect intent {intent_id} cannot be resolved because the "
                f"durable host ledger is unreadable ({decision.diagnostic})"
            )
        )
    if decision.outcome != "hold" or decision.evidence:
        return HeldLaunchClassification(
            reason=(
                f"held launch effect intent {intent_id} has host evidence that no operator "
                f"answer may override (last seen: {format_evidence(decision.evidence)}; "
                f"{decision.diagnostic})"
            )
        )
    try:
        owner, successor = resolve_owner_for_resume_retry(
            project_root,
            slice_id=slice_id,
            issue_id=_required_int(arguments, "chainlink_issue_id"),
            expected_parent_branch=expected_parent_branch,
            expected_agent_name=cast(str, arguments["owner_agent_name"]),
            prior_invocation_id=cast(str, arguments["expected_invocation_id"]),
            prior_generation=_required_int(arguments, "prior_generation"),
            expected_branch=cast(str, arguments["expected_branch"]),
            expected_worktree=cast(str, arguments["expected_worktree"]),
            expected_worktree_recorded=cast(str, arguments["expected_worktree_recorded"]),
            expected_worktree_fingerprint=cast(str, arguments["expected_worktree_fingerprint"]),
            dispatch_intent_id=intent_id,
        )
    except (BlockedLeafOwnerError, OSError, ValueError, KeyError) as error:
        return HeldLaunchClassification(
            reason=(
                f"held launch effect intent {intent_id} cannot prove a dormant, bound owner: "
                f"{error}"
            )
        )
    if successor is not None:
        return HeldLaunchClassification(
            reason=(
                f"held launch effect intent {intent_id} has a successor owner invocation; "
                "a host launch may already have landed and no operator answer may relaunch it"
            )
        )
    if blocked_resume_intent_id(owner) != intent_id:
        return HeldLaunchClassification(
            reason=(
                f"held launch effect intent {intent_id} is not the owner's derived "
                "blocked-resume idempotency identity; a forged intent is never resolved"
            )
        )
    invocation = owner.invocation_record
    owner_status = invocation.get("status")
    status_text = owner_status if isinstance(owner_status, str) else "unknown"
    last_evidence = format_evidence(decision.evidence)
    effect = HeldLaunchEffect(
        child_id=child_id,
        slice_id=slice_id,
        entry_key=cast(str, entry.get("key")),
        journal_index=journal_index,
        intent_id=intent_id,
        operation=BLOCKED_RESUME_OPERATION,
        journal_status=journal_status,
        owner_agent_name=cast(str, arguments["owner_agent_name"]),
        expected_invocation_id=cast(str, arguments["expected_invocation_id"]),
        expected_branch=cast(str, arguments["expected_branch"]),
        expected_worktree=cast(str, arguments["expected_worktree"]),
        expected_worktree_fingerprint=cast(str, arguments["expected_worktree_fingerprint"]),
        owner_invocation_status=status_text,
        last_host_evidence=last_evidence,
        diagnostic=_diagnostic(
            child_id=child_id,
            slice_id=slice_id,
            intent_id=intent_id,
            journal_index=journal_index,
            last_evidence=last_evidence,
            owner_agent_name=cast(str, arguments["owner_agent_name"]),
            journal_status=journal_status,
        ),
        arguments=dict(arguments),
    )
    return HeldLaunchClassification(effect=effect)


def _diagnostic(
    *,
    child_id: str,
    slice_id: str,
    intent_id: str,
    journal_index: int,
    last_evidence: str,
    owner_agent_name: str,
    journal_status: str,
) -> str:
    state = (
        "abandoned by an earlier operator answer; the retry gate is still open"
        if journal_status == "operator_abandoned"
        else "held; no host launch row, refusal or failure row exists for the intent"
    )
    return (
        f"child {child_id!r} slice {slice_id!r} has an unresolved launch effect: "
        f"intent {intent_id} at journal index {journal_index}, owner {owner_agent_name!r}, "
        f"last host evidence {last_evidence}; {state}. "
        "Available operator actions: approve the abandon gate to record that no launch "
        "happened, approve the retry gate to record that decision and launch exactly once "
        "under the current configuration, or reject to keep the slice parked."
    )


def classify_held_launch_effect(
    child_id: str,
    child_store: object,
    *,
    project_root: str | Path | None,
    ledger_run_id: str | None,
    expected_parent_branch: str,
) -> HeldLaunchClassification:
    """Classify one child journal without dispatching or editing any entry."""
    run_dir = getattr(child_store, "run_dir", None)
    if not isinstance(run_dir, Path):
        return HeldLaunchClassification()
    journal = EffectJournal(child_id, run_dir / "action-journal.json")
    try:
        entries = journal.snapshot()
        pending = journal.pending_entries()
        resolutions = journal.launch_resolutions()
    except ActionJournalError as error:
        return HeldLaunchClassification(
            reason=f"child action journal cannot be verified: {error}"
        )
    launch_entries = [
        (index, entry)
        for index, entry in enumerate(entries)
        if entry.get("kind") != "launch_resolution"
        and entry.get("operation") == BLOCKED_RESUME_OPERATION
        and entry.get("status") in {"intended", "unknown"}
    ]
    if not launch_entries:
        return HeldLaunchClassification()
    pending_keys = {
        entry.get("key")
        for entry in pending
        if isinstance(entry.get("key"), str)
    }
    launch_keys = {entry.get("key") for _, entry in launch_entries}
    pending_launch = [entry for _, entry in launch_entries if entry.get("key") in pending_keys]
    if pending and not pending_launch:
        return HeldLaunchClassification(
            reason=(
                "child action journal contains an unreconciled non-launch effect; "
                "no operator answer may resolve or relaunch it"
            )
        )
    if pending_launch:
        if len(pending) != 1 or len(pending_launch) != 1:
            return HeldLaunchClassification(
                reason=(
                    "child action journal contains more than one unreconciled effect; "
                    "no operator answer may resolve an ambiguous set"
                )
            )
        index, entry = launch_entries[-1]
        if entry.get("status") != "unknown":
            return HeldLaunchClassification(
                reason=(
                    "child action journal contains an intended launch effect whose dispatch "
                    "cannot be proven; no operator answer may relaunch it"
                )
            )
        if project_root is None:
            return HeldLaunchClassification(
                reason=(
                    "held launch effect cannot be proven without the project root and "
                    "durable host evidence"
                )
            )
        return _actionable(
            child_id,
            entry,
            index,
            project_root=project_root,
            ledger_run_id=ledger_run_id,
            expected_parent_branch=expected_parent_branch,
        )
    # Nothing is pending, but an abandonment alone keeps the slice parked. Only a
    # linked retry authorization releases it; the retry gate itself is the
    # durable authorization.
    last_index, last_entry = launch_entries[-1]
    if last_entry.get("key") not in launch_keys:
        return HeldLaunchClassification()
    linked = [
        resolution
        for resolution in resolutions
        if resolution.get("resolves_key") == last_entry.get("key")
    ]
    outcomes = {cast(str, resolution.get("outcome")) for resolution in linked}
    if outcomes & {"retry_authorized"}:
        return HeldLaunchClassification()
    if "operator_abandoned" not in outcomes:
        return HeldLaunchClassification()
    if project_root is None:
        return HeldLaunchClassification(
            reason=(
                "held launch effect cannot be re-proven without the project root and "
                "durable host evidence"
            )
        )
    return _actionable(
        child_id,
        last_entry,
        last_index,
        project_root=project_root,
        ledger_run_id=ledger_run_id,
        expected_parent_branch=expected_parent_branch,
        journal_status="operator_abandoned",
    )


__all__ = [
    "HELD_LAUNCH_GATE_PREFIX",
    "HeldLaunchClassification",
    "HeldLaunchEffect",
    "classify_held_launch_effect",
]
