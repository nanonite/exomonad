"""Plan reconciliation support."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from .plan_manifest import ManifestNode, PlanManifest
from .plan_reconciliation_guards import _blocker, _node_is_protected, _scope_blockers
from .plan_reconciliation_models import ReconciliationScope
from .plan_reconciliation_report import _safe_text
from .plan_revision_files import read_regular_file, relative_to_root, safe_target
from .plan_revision_state import decode_checkpoint_bytes
from .plan_revision_types import PlanRevisionBlocker, PlanRevisionError
from .schema import RunState, SliceStatus


def _read_scope(
    project: Path,
    relative_dir: str,
    run_id: str,
    parent_manifest: PlanManifest | None,
    inputs: dict[str, bytes | None],
) -> ReconciliationScope:
    checkpoint = f".exo/tl-loop/{relative_dir}/run.json"
    path = safe_target(project, checkpoint)
    raw, _mode = read_regular_file(path)
    if raw is None:
        raise PlanRevisionError(
            "reconciliation_input_missing", f"required input is missing: {checkpoint}"
        )
    inputs[checkpoint] = raw
    try:
        state = decode_checkpoint_bytes(path, cast(bytes, raw))
    except PlanRevisionError as error:
        raise PlanRevisionError(
            "runstore_invalid", f"RunStore for {_safe_text(run_id)} failed validation"
        ) from error
    manifest = state.plan_manifest
    if manifest is None:
        raise PlanRevisionError(
            "manifest_missing", f"RunStore {_safe_text(relative_dir)} has no plan manifest"
        )
    action_journal_entries, action_journal_status, action_journal_sha256 = _read_action_journal(
        project, relative_dir, inputs
    )
    dispatch_event_records, dispatch_event_status = _read_dispatch_events(project, state, inputs)
    parent_matches = parent_manifest is None or manifest == parent_manifest
    is_root = relative_dir == run_id
    identity_matches = (
        state.run_id == run_id
        and state.parent_run_id == manifest.parent_scope_id
        and (
            not is_root
            or (
                manifest.scope_id == run_id
                and manifest.role == "root"
                and manifest.parent_scope_id is None
            )
        )
    )
    return ReconciliationScope(
        run_dir=path.parent,
        relative_checkpoint=checkpoint,
        raw_checkpoint=cast(bytes, raw),
        state=state,
        current=manifest,
        accepted=None,
        parent_manifest_matches=parent_matches,
        run_identity_matches=identity_matches,
        action_journal_entries=action_journal_entries,
        action_journal_status=action_journal_status,
        action_journal_sha256=action_journal_sha256,
        dispatch_event_records=dispatch_event_records,
        dispatch_event_status=dispatch_event_status,
        protected_node_ids=tuple(
            node.node_id for node in manifest.nodes if _node_is_protected(state, node.name)
        ),
    )


def _read_action_journal(
    project: Path,
    relative_dir: str,
    inputs: dict[str, bytes | None],
) -> tuple[tuple[Mapping[str, object], ...] | None, str, str | None]:
    relative = f".exo/tl-loop/{relative_dir}/action-journal.json"
    raw, _mode = read_regular_file(safe_target(project, relative))
    inputs[relative] = raw
    if raw is None:
        return None, "missing", None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None, "invalid", digest
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None, "invalid", digest
    return tuple(cast(Mapping[str, object], item) for item in value), "valid", digest


def _read_dispatch_events(
    project: Path,
    state: RunState,
    inputs: dict[str, bytes | None],
) -> tuple[
    Mapping[int, tuple[Mapping[str, object], ...]],
    Mapping[int, str],
]:
    sequences = _legacy_dispatch_sequences(state)
    if not sequences:
        return {}, {}
    if state.ledger_run_id is None:
        return {}, {sequence: "ledger_run_id_missing" for sequence in sequences}
    matches, failure = _scan_dispatch_segments(project, state, sequences, inputs)
    if failure is not None or matches is None:
        status = failure or "ledger_unreadable"
        return {}, {sequence: status for sequence in sequences}
    statuses = _dispatch_match_statuses(matches)
    return {sequence: tuple(rows) for sequence, rows in matches.items()}, statuses


def _scan_dispatch_segments(
    project: Path,
    state: RunState,
    sequences: set[int],
    inputs: dict[str, bytes | None],
) -> tuple[dict[int, list[Mapping[str, object]]] | None, str | None]:
    if state.ledger_run_id is None:
        return None, "ledger_run_id_missing"
    segments, failure = _ledger_segments(project)
    if failure is not None:
        return None, failure
    matches: dict[int, list[Mapping[str, object]]] = {sequence: [] for sequence in sequences}
    for segment_index, segment in enumerate(segments):
        rows, raw, failure = _read_segment_rows(segment, segment_index == len(segments) - 1)
        if failure is not None or raw is None:
            return None, failure or "ledger_unreadable"
        inputs[relative_to_root(project, segment)] = raw
        _retain_dispatch_rows(rows, state.ledger_run_id, matches)
    return matches, None


def _dispatch_match_statuses(
    matches: Mapping[int, list[Mapping[str, object]]],
) -> dict[int, str]:
    return {
        sequence: "found" if len(rows) == 1 else "ambiguous" if rows else "missing"
        for sequence, rows in matches.items()
    }


def _legacy_dispatch_sequences(state: RunState) -> set[int]:
    return {
        record.dispatch_authoritative_event_seq
        for record in state.slices.values()
        if record.status is SliceStatus.MERGED
        and record.dispatch_invocation_id is None
        and type(record.dispatch_authoritative_event_seq) is int
    }


def _ledger_segments(project: Path) -> tuple[tuple[Path, ...], str | None]:
    root = project / ".exo" / "ledger" / "segments"
    if any(path.is_symlink() for path in (project / ".exo", root.parent, root)):
        return (), "ledger_invalid"
    try:
        segments = sorted(
            path for path in root.iterdir() if path.is_file() and path.suffix == ".jsonl"
        )
        return tuple(segments), None
    except FileNotFoundError:
        return (), "ledger_missing"
    except OSError:
        return (), "ledger_unreadable"


def _read_segment_rows(
    segment: Path,
    is_active: bool,
) -> tuple[tuple[Mapping[str, object], ...], bytes | None, str | None]:
    try:
        raw, _mode = read_regular_file(segment)
    except PlanRevisionError:
        return (), None, "ledger_unreadable"
    if raw is None:
        return (), None, "ledger_unreadable"
    rows: list[Mapping[str, object]] = []
    lines = raw.splitlines(keepends=True)
    for line_index, line in enumerate(lines):
        if not line.strip():
            continue
        if not line.endswith(b"\n"):
            if is_active and line_index == len(lines) - 1:
                continue
            return (), None, "ledger_invalid"
        row = _decode_ledger_row(line)
        if row is None:
            return (), None, "ledger_invalid"
        rows.append(row)
    return tuple(rows), raw, None


def _decode_ledger_row(line: bytes) -> Mapping[str, object] | None:
    try:
        value = json.loads(line)
    except (UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _retain_dispatch_rows(
    rows: tuple[Mapping[str, object], ...],
    ledger_run_id: str,
    matches: dict[int, list[Mapping[str, object]]],
) -> None:
    for row in rows:
        sequence = row.get("run_seq")
        if type(sequence) is int and sequence in matches and row.get("run_id") == ledger_run_id:
            matches[sequence].append(row)


def _validate_plan_run_id(
    snapshot: dict[str, object],
    candidate: dict[str, object],
    run_id: str,
) -> None:
    for label, document in (("accepted snapshot", snapshot), ("candidate", candidate)):
        if document.get("run_id") not in (None, run_id):
            raise PlanRevisionError("run_id_changed", f"{label} run_id differs from the active run")


def _collect_scope_tree(
    project: Path,
    view: ReconciliationScope,
    accepted: PlanManifest,
    *,
    parent_manifest_matches: bool,
    inputs: dict[str, bytes | None],
    scopes: list[ReconciliationScope],
    blockers: list[PlanRevisionBlocker],
) -> None:
    accepted_view = ReconciliationScope(
        run_dir=view.run_dir,
        relative_checkpoint=view.relative_checkpoint,
        raw_checkpoint=view.raw_checkpoint,
        state=view.state,
        current=view.current,
        accepted=accepted,
        parent_manifest_matches=parent_manifest_matches,
        run_identity_matches=view.run_identity_matches,
        action_journal_entries=view.action_journal_entries,
        action_journal_status=view.action_journal_status,
        action_journal_sha256=view.action_journal_sha256,
        dispatch_event_records=view.dispatch_event_records,
        dispatch_event_status=view.dispatch_event_status,
        protected_node_ids=view.protected_node_ids,
    )
    scopes.append(accepted_view)
    blockers.extend(_scope_blockers(accepted_view))
    _check_child_directories(view, accepted, project, blockers)
    for node in view.current.nodes:
        if node.kind != "sub_tl":
            continue
        accepted_child = accepted.child_manifests.get(node.node_id)
        current_child = view.current.child_manifests.get(node.node_id)
        child_dir = _child_relative_dir(view.relative_checkpoint, node)
        child_file = f".exo/tl-loop/{child_dir}/run.json"
        child_path = safe_target(project, child_file)
        if not child_path.exists():
            if _node_is_protected(view.state, node.name):
                blockers.append(
                    _blocker("materialized_scope_missing", node.node_id, view.current.scope_id)
                )
            continue
        if accepted_child is None or current_child is None:
            blockers.append(_blocker("scope_topology_drift", node.node_id, view.current.scope_id))
            continue
        child_view = _read_scope(
            project,
            child_dir,
            node.name,
            current_child,
            inputs,
        )
        _collect_scope_tree(
            project,
            child_view,
            accepted_child,
            parent_manifest_matches=child_view.parent_manifest_matches,
            inputs=inputs,
            scopes=scopes,
            blockers=blockers,
        )


def _child_relative_dir(checkpoint: str, node: ManifestNode) -> str:
    if Path(node.name).name != node.name or node.name in {".", ".."}:
        raise PlanRevisionError("scope_path_invalid", "sub-TL name is not a safe path component")
    parent_dir = str(Path(checkpoint).parent.relative_to(Path(".exo/tl-loop")))
    return str(Path(parent_dir) / node.name)


def _check_child_directories(
    view: ReconciliationScope,
    accepted: PlanManifest,
    project: Path,
    blockers: list[PlanRevisionBlocker],
) -> None:
    known = {
        node.name
        for manifest in (view.current, accepted)
        for node in manifest.nodes
        if node.kind == "sub_tl"
    }
    try:
        children = tuple(view.run_dir.iterdir())
    except OSError as error:
        raise PlanRevisionError(
            "runstore_unreadable", "cannot inspect materialized scopes"
        ) from error
    for child in children:
        if child.is_symlink():
            raise PlanRevisionError("runstore_symlink", "refusing symlink in materialized scopes")
        if not child.is_dir() or child.name in known:
            continue
        extra = child / "run.json"
        relative = relative_to_root(project, extra)
        if safe_target(project, relative).exists():
            blockers.append(_blocker("orphan_runstore", child.name, view.current.scope_id))
