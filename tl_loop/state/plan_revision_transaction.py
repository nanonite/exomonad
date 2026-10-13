"""Journaled multi-file commit and restart recovery for plan revisions."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from .lock import RunLock
from .plan_revision_files import (
    atomic_write_bytes,
    controller_lifecycle_lock_path,
    fsync_directory,
    read_regular_file,
    safe_target,
)
from .plan_revision_types import PlanRevisionError

JOURNAL_NAME = "plan-revision-transition.json"
TRANSACTION_DIR_NAME = "plan-revision-transactions"
_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class PreparedRevision:
    """Prepared transaction data waiting for participant replacement."""

    project_root: Path
    journal_path: Path
    transaction_dir: Path
    document: Mapping[str, object]


def prepare_revision_transaction(
    project_root: Path,
    changes: Mapping[Path, bytes],
    audit_record: Mapping[str, object],
    *,
    transition_id: str,
    expected_before: Mapping[Path, str | None] | None = None,
) -> PreparedRevision:
    """Persist before/after payloads, then publish the prepared journal last."""
    _validate_prepare_request(transition_id, changes, expected_before)
    transaction_root, transaction_dir, journal_path = _prepare_paths(project_root, transition_id)
    _create_transaction_directories(transaction_root, transaction_dir)
    return _build_prepared_revision(
        project_root,
        transaction_root,
        transaction_dir,
        journal_path,
        changes,
        audit_record,
        transition_id,
        expected_before or {},
    )


def _build_prepared_revision(
    project_root: Path,
    transaction_root: Path,
    transaction_dir: Path,
    journal_path: Path,
    changes: Mapping[Path, bytes],
    audit_record: Mapping[str, object],
    transition_id: str,
    expected_before: Mapping[Path, str | None],
) -> PreparedRevision:
    try:
        document = _prepare_journal_document(
            project_root,
            transaction_root,
            transaction_dir,
            journal_path,
            changes,
            audit_record,
            transition_id,
            expected_before,
        )
    except BaseException as error:
        _cleanup_failed_preparation(journal_path, transaction_dir, error)
        raise
    return PreparedRevision(project_root, journal_path, transaction_dir, document)


def _validate_prepare_request(
    transition_id: str,
    changes: Mapping[Path, bytes],
    expected_before: Mapping[Path, str | None] | None,
) -> None:
    if not _IDENTIFIER.fullmatch(transition_id):
        raise PlanRevisionError("transition_id_invalid", "transition ID is invalid")
    if expected_before is not None and set(expected_before) - set(changes):
        raise PlanRevisionError(
            "transition_precondition_invalid",
            "transition preconditions must name participant files",
        )


def _prepare_paths(
    project_root: Path,
    transition_id: str,
) -> tuple[Path, Path, Path]:
    state_root = project_root / ".exo" / "tl-loop"
    transaction_root = state_root / TRANSACTION_DIR_NAME
    transaction_dir = transaction_root / transition_id
    journal_path = state_root / JOURNAL_NAME
    if transaction_root.is_symlink() or state_root.is_symlink():
        raise PlanRevisionError(
            "transition_path_symlink", "plan revision transaction path is a symlink"
        )
    if journal_path.exists() or journal_path.is_symlink():
        raise PlanRevisionError("transition_pending", "another plan revision needs recovery")
    if transaction_dir.exists() or transaction_dir.is_symlink():
        raise PlanRevisionError(
            "transition_collision", "plan revision transaction ID already exists"
        )
    return transaction_root, transaction_dir, journal_path


def _create_transaction_directories(transaction_root: Path, transaction_dir: Path) -> None:
    state_root = transaction_root.parent
    transaction_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_private_directory(transaction_root)
    fsync_directory(state_root)
    transaction_dir.mkdir(mode=0o700)
    _require_private_directory(transaction_dir)


def _prepare_journal_document(
    project_root: Path,
    transaction_root: Path,
    transaction_dir: Path,
    journal_path: Path,
    changes: Mapping[Path, bytes],
    audit_record: Mapping[str, object],
    transition_id: str,
    expected_before: Mapping[Path, str | None],
) -> dict[str, object]:
    if audit_record.get("transition_id") != transition_id:
        raise PlanRevisionError(
            "transition_audit_mismatch",
            "audit record does not identify the prepared transition",
        )
    entries = _stage_entries(project_root, transaction_dir, changes, expected_before)
    document = _journal_document(transition_id, entries, audit_record)
    fsync_directory(transaction_root)
    _write_journal(journal_path, document)
    return document


def _journal_document(
    transition_id: str,
    entries: list[dict[str, object]],
    audit_record: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "transition_id": transition_id,
        "phase": "prepared",
        "entries": entries,
        "audit": dict(audit_record),
    }


def _cleanup_failed_preparation(
    journal_path: Path,
    transaction_dir: Path,
    error: BaseException,
) -> None:
    if journal_path.exists() or journal_path.is_symlink():
        raise PlanRevisionError(
            "transition_recovery_required",
            "plan revision journal was published but preparation did not finish",
        ) from error
    shutil.rmtree(transaction_dir, ignore_errors=True)


def commit_prepared_revision(
    prepared: PreparedRevision,
    *,
    after_target: Callable[[int, str], None] | None = None,
) -> None:
    """Roll the prepared data files forward and mark the transaction committed."""
    document = _read_journal(prepared.journal_path)
    if document["phase"] != "prepared":
        raise PlanRevisionError("transition_phase_invalid", "transaction is not prepared")
    _apply_entries(prepared.project_root, prepared.transaction_dir, document, after_target)
    _verify_targets_after(prepared.project_root, document)
    committed = {**document, "phase": "committed"}
    _write_journal(prepared.journal_path, committed)
    _verify_targets_after(prepared.project_root, committed)
    _cleanup_committed(prepared.journal_path, prepared.transaction_dir)


def recover_plan_revision(
    project_dir: str | Path,
    *,
    lock_timeout: float = 5.0,
    controller_locked: bool = False,
) -> bool:
    """Recover a pending revision under the controller lifecycle and state locks."""
    project_root = Path(project_dir).resolve()
    if controller_locked:
        return _recover_plan_revision_locked(project_root, lock_timeout)
    controller_lock = controller_lifecycle_lock_path(project_root)
    with RunLock(controller_lock, timeout=lock_timeout):
        return _recover_plan_revision_locked(project_root, lock_timeout)


def _recover_plan_revision_locked(project_root: Path, lock_timeout: float) -> bool:
    """Recover while the caller holds the controller lifecycle lock."""
    journal_path = project_root / ".exo" / "tl-loop" / JOURNAL_NAME
    revision_lock = journal_path.parent / "plan-revision.lock"
    _validate_recovery_paths(project_root, journal_path, revision_lock)
    with RunLock(revision_lock, timeout=lock_timeout):
        return _recover_journal_with_scope_locks(
            project_root,
            journal_path,
            lock_timeout,
        )


def _validate_recovery_paths(
    project_root: Path,
    journal_path: Path,
    revision_lock: Path,
) -> None:
    if any(
        path.is_symlink() for path in (project_root / ".exo", journal_path.parent, revision_lock)
    ):
        raise PlanRevisionError(
            "transition_path_symlink",
            "plan revision state directory or lock path is a symlink",
        )


def _recover_journal_with_scope_locks(
    project_root: Path,
    journal_path: Path,
    lock_timeout: float,
) -> bool:
    document = _load_pending_journal(journal_path)
    if document is None:
        return False
    _with_run_locks(
        project_root,
        document,
        lock_timeout,
        lambda: _recover_locked(project_root, journal_path, document),
    )
    return True


def _load_pending_journal(journal_path: Path) -> dict[str, object] | None:
    if journal_path.is_symlink():
        raise PlanRevisionError("transition_journal_symlink", "plan revision journal is a symlink")
    raw, _ = read_regular_file(journal_path)
    if raw is None:
        return None
    return _decode_journal(raw, journal_path)


def _stage_entries(
    project_root: Path,
    transaction_dir: Path,
    changes: Mapping[Path, bytes],
    expected_before: Mapping[Path, str | None],
) -> list[dict[str, object]]:
    if not changes:
        raise PlanRevisionError("transition_empty", "plan revision has no durable file updates")
    entries: list[dict[str, object]] = []
    ordered = sorted(
        changes,
        key=lambda path: (path.name == "plan.json", path.as_posix()),
    )
    for index, target in enumerate(ordered):
        entries.append(
            _stage_entry(
                project_root, transaction_dir, target, changes[target], index, expected_before
            )
        )
    if not entries:
        raise PlanRevisionError("transition_empty", "plan revision has no changed files")
    return entries


def _stage_entry(
    project_root: Path,
    transaction_dir: Path,
    target: Path,
    after: bytes,
    index: int,
    expected_before: Mapping[Path, str | None],
) -> dict[str, object]:
    relative = target.relative_to(project_root).as_posix()
    before, mode = read_regular_file(safe_target(project_root, relative))
    _check_stage_precondition(target, relative, before, expected_before)
    before_name, after_name = _stage_backup_payloads(transaction_dir, before, after, index)
    return _stage_entry_document(relative, before, after, mode, before_name, after_name)


def _check_stage_precondition(
    target: Path,
    relative: str,
    before: bytes | None,
    expected_before: Mapping[Path, str | None],
) -> None:
    if target not in expected_before or _digest(before) == expected_before[target]:
        return
    raise PlanRevisionError(
        "transition_precondition_failed",
        f"transition target changed while preparing: {relative}",
    )


def _stage_backup_payloads(
    transaction_dir: Path,
    before: bytes | None,
    after: bytes,
    index: int,
) -> tuple[str | None, str]:
    before_name = f"{index:04d}.before" if before is not None else None
    after_name = f"{index:04d}.after"
    if before is not None and before_name is not None:
        atomic_write_bytes(transaction_dir / before_name, before)
    atomic_write_bytes(transaction_dir / after_name, after)
    return before_name, after_name


def _stage_entry_document(
    target: str,
    before: bytes | None,
    after: bytes,
    mode: int | None,
    before_name: str | None,
    after_name: str,
) -> dict[str, object]:
    return {
        "target": target,
        "before_sha256": _digest(before),
        "after_sha256": _digest(after),
        "before_backup": before_name,
        "after_backup": after_name,
        "mode": mode if mode is not None else 0o600,
    }


def _recover_locked(
    project_root: Path,
    journal_path: Path,
    document: Mapping[str, object],
) -> None:
    transition_id = cast(str, document["transition_id"])
    transaction_dir = journal_path.parent / TRANSACTION_DIR_NAME / transition_id
    phase = document["phase"]
    if phase == "prepared":
        _apply_entries(project_root, transaction_dir, document, None)
        document = {**document, "phase": "committed"}
        _write_journal(journal_path, document)
    _verify_targets_after(project_root, document)
    _cleanup_committed(journal_path, transaction_dir)


def _apply_entries(
    project_root: Path,
    transaction_dir: Path,
    document: Mapping[str, object],
    after_target: Callable[[int, str], None] | None,
) -> None:
    entries = cast(list[dict[str, object]], document["entries"])
    _preflight_entries(project_root, transaction_dir, entries)
    for index, entry in enumerate(entries):
        _apply_entry(project_root, transaction_dir, entry, index, after_target)


def _apply_entry(
    project_root: Path,
    transaction_dir: Path,
    entry: Mapping[str, object],
    index: int,
    after_target: Callable[[int, str], None] | None,
) -> None:
    target_name = cast(str, entry["target"])
    target = safe_target(project_root, target_name)
    current, _ = read_regular_file(target)
    current_digest = _digest(current)
    expected_after = cast(str, entry["after_sha256"])
    if current_digest == expected_after:
        return
    _require_before_digest(entry, current_digest)
    after = _read_after_image(transaction_dir, entry, expected_after)
    atomic_write_bytes(target, after, cast(int, entry["mode"]))
    if after_target is not None:
        after_target(index, target_name)


def _require_before_digest(entry: Mapping[str, object], observed: str | None) -> None:
    if observed == entry["before_sha256"]:
        return
    raise PlanRevisionError(
        "transition_target_drift",
        f"refusing to overwrite changed transition target {entry['target']}",
    )


def _read_after_image(
    transaction_dir: Path,
    entry: Mapping[str, object],
    expected_digest: str,
) -> bytes:
    after_path = _backup_path(transaction_dir, entry, "after_backup")
    after, _ = read_regular_file(after_path)
    if after is None or _digest(after) != expected_digest:
        raise PlanRevisionError(
            "transition_backup_invalid",
            f"after-image for {entry['target']} is missing or corrupt",
        )
    return after


def _preflight_entries(
    project_root: Path,
    transaction_dir: Path,
    entries: list[dict[str, object]],
) -> None:
    for entry in entries:
        target_path = safe_target(project_root, cast(str, entry["target"]))
        current, _ = read_regular_file(target_path)
        current_digest = _digest(current) if current is not None else None
        expected_before = cast(str | None, entry["before_sha256"])
        expected_after = cast(str, entry["after_sha256"])
        if current_digest not in {expected_before, expected_after}:
            raise PlanRevisionError(
                "transition_target_drift",
                f"refusing to update changed transition target {entry['target']}",
            )
        if current_digest == expected_before and current_digest != expected_after:
            after_path = _backup_path(transaction_dir, entry, "after_backup")
            after, _ = read_regular_file(after_path)
            if after is None or _digest(after) != expected_after:
                raise PlanRevisionError(
                    "transition_backup_invalid",
                    f"after-image for {entry['target']} is missing or corrupt",
                )


def _verify_targets_after(project_root: Path, document: Mapping[str, object]) -> None:
    entries = cast(list[dict[str, object]], document["entries"])
    for entry in entries:
        target = safe_target(project_root, cast(str, entry["target"]))
        contents, _ = read_regular_file(target)
        if contents is None or _digest(contents) != entry["after_sha256"]:
            raise PlanRevisionError(
                "transition_target_unresolved",
                f"transition target {entry['target']} does not match its committed image",
            )


def _with_run_locks(
    project_root: Path,
    document: Mapping[str, object],
    timeout: float,
    operation: Callable[[], None],
) -> None:
    lock_paths = _run_lock_paths(project_root, document)
    with ExitStack() as stack:
        for lock_path in lock_paths:
            if lock_path.is_symlink():
                raise PlanRevisionError(
                    "run_lock_symlink", f"RunStore lock is a symlink: {lock_path}"
                )
            stack.enter_context(RunLock(lock_path, timeout=timeout))
        operation()


def _run_lock_paths(project_root: Path, document: Mapping[str, object]) -> tuple[Path, ...]:
    lock_paths: set[Path] = set()
    entries = cast(list[dict[str, object]], document["entries"])
    for entry in entries:
        target = PurePosixPath(cast(str, entry["target"]))
        if target.parts[:2] == (".exo", "tl-loop") and target.name == "run.json":
            run_dir = project_root.joinpath(*target.parts[:-1])
            lock_paths.add(run_dir.parent / "run.lock")
    return tuple(sorted(lock_paths, key=lambda path: path.as_posix()))


def _backup_path(
    transaction_dir: Path,
    entry: Mapping[str, object],
    backup_key: str,
) -> Path:
    name = entry.get(backup_key)
    if not isinstance(name, str) or not re.fullmatch(r"\d{4}\.after", name):
        raise PlanRevisionError("transition_backup_invalid", "journal backup filename is invalid")
    if transaction_dir.is_symlink() or transaction_dir.parent.is_symlink():
        raise PlanRevisionError(
            "transition_backup_invalid", "transaction path must not be a symlink"
        )
    _require_private_directory(transaction_dir)
    path = transaction_dir / name
    if path.is_symlink():
        raise PlanRevisionError("transition_backup_invalid", "journal backup must not be a symlink")
    return path


def _read_journal(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise PlanRevisionError("transition_journal_symlink", "plan revision journal is a symlink")
    raw, _ = read_regular_file(path)
    if raw is None:
        raise PlanRevisionError(
            "transition_journal_missing", "prepared plan revision journal is missing"
        )
    return _decode_journal(raw, path)


def _decode_journal(raw: bytes, path: Path) -> dict[str, object]:
    if len(raw) > 4 * 1024 * 1024:
        raise PlanRevisionError("transition_journal_invalid", "plan revision journal is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PlanRevisionError("transition_journal_invalid", f"invalid journal {path}") from error
    if not isinstance(value, dict):
        raise PlanRevisionError(
            "transition_journal_invalid", "plan revision journal has an invalid shape"
        )
    _validate_journal_header(value)
    _validate_journal_entries(value["entries"])
    return value


def _validate_journal_header(value: dict[str, object]) -> None:
    _validate_journal_shape(value)
    _validate_journal_identity(value)
    _validate_journal_audit(value)


def _validate_journal_shape(value: dict[str, object]) -> None:
    if set(value) != {
        "schema_version",
        "transition_id",
        "phase",
        "entries",
        "audit",
    }:
        raise PlanRevisionError(
            "transition_journal_invalid", "plan revision journal has an invalid shape"
        )


def _validate_journal_identity(value: dict[str, object]) -> None:
    if value["schema_version"] != 1 or not isinstance(value["transition_id"], str):
        raise PlanRevisionError(
            "transition_journal_invalid", "plan revision journal version is invalid"
        )
    if not _IDENTIFIER.fullmatch(value["transition_id"]):
        raise PlanRevisionError("transition_journal_invalid", "plan revision journal ID is invalid")
    if value["phase"] not in {"prepared", "committed"}:
        raise PlanRevisionError(
            "transition_journal_invalid", "plan revision journal phase is invalid"
        )


def _validate_journal_audit(value: dict[str, object]) -> None:
    if not isinstance(value["audit"], dict):
        raise PlanRevisionError(
            "transition_journal_invalid", "plan revision audit record is invalid"
        )
    if value["audit"].get("transition_id") != value["transition_id"]:
        raise PlanRevisionError("transition_journal_invalid", "audit transition ID does not match")


def _validate_journal_entries(value: object) -> None:
    if not isinstance(value, list) or not value:
        raise PlanRevisionError(
            "transition_journal_invalid", "plan revision journal has no entries"
        )
    targets: set[str] = set()
    for index, entry in enumerate(value):
        _validate_journal_entry(entry)
        target = cast(str, entry["target"])
        if target in targets:
            raise PlanRevisionError("transition_journal_invalid", "journal targets must be unique")
        targets.add(target)
        if entry["after_backup"] != f"{index:04d}.after":
            raise PlanRevisionError("transition_journal_invalid", "after backup index is invalid")
        before_name = entry["before_backup"]
        if before_name is not None and before_name != f"{index:04d}.before":
            raise PlanRevisionError("transition_journal_invalid", "before backup index is invalid")


def _validate_journal_entry(value: object) -> None:
    required = {
        "target",
        "before_sha256",
        "after_sha256",
        "before_backup",
        "after_backup",
        "mode",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise PlanRevisionError("transition_journal_invalid", "plan revision target is malformed")
    _validate_journal_target(value["target"])
    _validate_journal_digests(value["before_sha256"], value["after_sha256"])
    _validate_journal_backups(value)


def _validate_journal_target(target: object) -> None:
    if not isinstance(target, str):
        raise PlanRevisionError("transition_journal_invalid", "plan revision target is invalid")
    path = PurePosixPath(target)
    if path.as_posix() != target:
        raise PlanRevisionError(
            "transition_journal_invalid", "plan revision target is not canonical"
        )
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.parts[:2] != (".exo", "tl-loop")
        or len(path.parts) < 3
    ):
        raise PlanRevisionError(
            "transition_journal_invalid", "plan revision target escapes TL state"
        )
    if not _allowed_target(path):
        raise PlanRevisionError(
            "transition_journal_invalid", "plan revision target is not a participant"
        )


def _validate_journal_digests(before: object, after: object) -> None:
    if before is not None and (not isinstance(before, str) or not _SHA256.fullmatch(before)):
        raise PlanRevisionError("transition_journal_invalid", "before digest is invalid")
    if not isinstance(after, str) or not _SHA256.fullmatch(after):
        raise PlanRevisionError("transition_journal_invalid", "after digest is invalid")


def _validate_journal_backups(value: Mapping[str, object]) -> None:
    before = value["before_sha256"]
    if type(value["mode"]) is not int or not 0 <= value["mode"] <= 0o777:
        raise PlanRevisionError("transition_journal_invalid", "target mode is invalid")
    before_backup = value["before_backup"]
    if before is None and before_backup is not None:
        raise PlanRevisionError("transition_journal_invalid", "absent before-image has a backup")
    if before is not None and not _valid_backup_name(before_backup, ".before"):
        raise PlanRevisionError("transition_journal_invalid", "before backup filename is invalid")
    if not _valid_backup_name(value["after_backup"], ".after"):
        raise PlanRevisionError("transition_journal_invalid", "after backup filename is invalid")


def _valid_backup_name(value: object, suffix: str) -> bool:
    return (
        isinstance(value, str)
        and suffix in {".before", ".after"}
        and re.fullmatch(rf"\d{{4}}{re.escape(suffix)}", value) is not None
    )


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise PlanRevisionError(
            "transition_backup_invalid", "transaction directory is unavailable"
        ) from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PlanRevisionError(
            "transition_backup_invalid",
            "transaction directories must be private to the current user",
        )


def _allowed_target(path: PurePosixPath) -> bool:
    root_participants = {
        "plan.json",
        "plan.snapshot",
        "plan.snapshot.sha256",
        "plan-revisions.jsonl",
    }
    if len(path.parts) == 3 and path.parts[2] in root_participants:
        return True
    return (
        len(path.parts) >= 4
        and path.parts[2] == "root"
        and path.name == "run.json"
        and all(part not in {".", ".."} for part in path.parts[2:-1])
    )


def _write_journal(path: Path, document: Mapping[str, object]) -> None:
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    atomic_write_bytes(path, payload)


def _cleanup_committed(journal_path: Path, transaction_dir: Path) -> None:
    if transaction_dir.is_symlink():
        raise PlanRevisionError("transition_backup_invalid", "transaction directory is a symlink")
    if transaction_dir.exists():
        shutil.rmtree(transaction_dir)
        fsync_directory(transaction_dir.parent)
    try:
        journal_path.unlink()
    except FileNotFoundError:
        pass
    fsync_directory(journal_path.parent)


def _digest(payload: bytes | None) -> str | None:
    return hashlib.sha256(payload).hexdigest() if payload is not None else None
