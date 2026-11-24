"""Durable host ledger rows that can decide one launch-capable controller effect.

The controller never infers a launch from a client disconnect. These are the
only host rows that can resolve a timed-out blocked-resume request, and each is
read from the immutable ledger without interpretation beyond its documented
identity:
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from tl_loop.events.reader import LedgerReader

from .prelaunch_refusal import PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS

__all__ = [
    "BLOCKED_RESUME_OPERATION",
    "HOST_EVIDENCE_BOUNDED_WINDOW_SECONDS",
    "LAUNCH_EVIDENCE_EVENT_TYPES",
    "PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS",
    "event_type",
    "evidence_label",
    "format_evidence",
    "is_failure_row",
    "is_launch_row",
    "is_refusal_row",
    "refusal_intent",
    "row_data",
    "scoped_launch_evidence",
]

BLOCKED_RESUME_OPERATION = "resume_blocked_leaf"

#: Ledger rows that can decide or describe one blocked-resume launch.
LAUNCH_EVIDENCE_EVENT_TYPES = ("agent.spawned", "tool.called", "agent.spawn_failed")

#: After this long with no host row at all the hold diagnostic says so. The
#: entry still stays held: absence of evidence never authorizes a relaunch.
HOST_EVIDENCE_BOUNDED_WINDOW_SECONDS = 300.0


def scoped_launch_evidence(
    project_root: Path,
    arguments: Mapping[str, object],
    *,
    segments_dir: str | Path | None,
    ledger_run_id: str | None,
) -> tuple[Mapping[str, object], ...]:
    """Read the ledger rows in this request's run and dispatch floor."""
    if not ledger_run_id:
        raise ValueError("no authoritative ledger run is configured for host evidence")
    segments = (
        Path(segments_dir)
        if segments_dir is not None
        else project_root / ".exo" / "ledger" / "segments"
    )
    documents = LedgerReader(segments, ledger_run_id=ledger_run_id).lifecycle_records(
        LAUNCH_EVIDENCE_EVENT_TYPES
    )
    floor = arguments.get("dispatch_ledger_floor")
    floor_value = floor if type(floor) is int else None
    scoped: list[Mapping[str, object]] = []
    for document in documents:
        if document.get("run_id") != ledger_run_id:
            continue
        run_seq = document.get("run_seq")
        if floor_value is not None and (type(run_seq) is not int or run_seq < floor_value):
            continue
        scoped.append(document)
    return tuple(scoped)


def is_launch_row(document: Mapping[str, object], intent_id: str) -> bool:
    """One host launch confirmation for this exact intent."""
    return (
        event_type(document) == "agent.spawned"
        and row_data(document).get("intent_id") == intent_id
    )


def is_refusal_row(document: Mapping[str, object], intent_id: str) -> bool:
    """One host-recorded request completion that refused before launch."""
    data = row_data(document)
    return (
        event_type(document) == "tool.called"
        and data.get("tool_name") == BLOCKED_RESUME_OPERATION
        and data.get("success") is False
        and data.get("cancelled") is False
        and isinstance(data.get("error"), str)
        and cast(str, data["error"]) in PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS
        and refusal_intent(document) == intent_id
    )


def is_failure_row(document: Mapping[str, object], intent_id: str) -> bool:
    """One host terminal failure for this blocked-resume operation."""
    data = row_data(document)
    return (
        event_type(document) == "agent.spawn_failed"
        and data.get("intent_id") == intent_id
        and data.get("operation") == BLOCKED_RESUME_OPERATION
    )


def refusal_intent(document: Mapping[str, object]) -> str | None:
    """Return the dispatch intent a refusal row names, if any."""
    data = row_data(document)
    direct = data.get("dispatch_intent_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    arguments = data.get("arguments")
    if not isinstance(arguments, Mapping):
        return None
    route = arguments.get("role_route")
    if isinstance(route, Mapping):
        nested = route.get("dispatch_intent_id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return None


def evidence_label(document: Mapping[str, object]) -> str:
    """Return one short durable-evidence label for diagnostics."""
    run_seq = document.get("run_seq")
    suffix = f"@{run_seq}" if type(run_seq) is int else ""
    return f"{event_type(document) or '<unknown>'}{suffix}"


def format_evidence(evidence: tuple[str, ...]) -> str:
    """Render the evidence labels a diagnostic saw."""
    return ", ".join(evidence) if evidence else "none"


def event_type(document: Mapping[str, object]) -> str:
    """Return the raw ledger event type without parsing it into the TL union."""
    value = document.get("type", document.get("event_type"))
    return value if isinstance(value, str) else ""


def row_data(document: Mapping[str, object]) -> Mapping[str, object]:
    """Return one row's data object, or an empty mapping when malformed."""
    value = document.get("data")
    return value if isinstance(value, Mapping) else {}
