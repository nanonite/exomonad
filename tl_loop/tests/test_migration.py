"""Legacy checkpoint migration and evidence-preservation coverage."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

import tl_loop.state.migration as migration_module
from tl_loop.state.migration import install_migration, migrate_checkpoint_document
from tl_loop.state.schema import SCHEMA_VERSION
from tl_loop.state.store import CorruptCheckpoint, RunStore


def _legacy_spawned() -> dict[str, object]:
    return {
        "run_id": "legacy",
        "fsm": {"phase": "tl_planning", "waiting": []},
        "slices": {
            "slice-a": {
                "state": "spawned",
                "paths": ["src/a.py"],
                "branch": "task/slice-a",
            }
        },
    }


def test_legacy_checkpoint_is_migrated_without_losing_original(tmp_path) -> None:
    path = tmp_path / "legacy" / "run.json"
    path.parent.mkdir()
    original = json.dumps(_legacy_spawned(), sort_keys=True)
    path.write_text(original, encoding="utf-8")

    state = RunStore("legacy", tmp_path).load()

    assert state.version == SCHEMA_VERSION
    assert state.slices["slice-a"].status.value == "dispatch_unconfirmed"
    assert path.read_text(encoding="utf-8") != original
    backups = tuple(path.parent.glob("run.json.legacy-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    report = json.loads((path.parent / "migration-report.json").read_text())
    assert report["status"] == "complete"

    RunStore("legacy", tmp_path).load()
    assert len(tuple(path.parent.glob("run.json.legacy-*"))) == 1


def test_malformed_checkpoint_is_blocked_without_overwriting_evidence(tmp_path) -> None:
    path = tmp_path / "broken" / "run.json"
    path.parent.mkdir()
    original = b"{not-json"
    path.write_bytes(original)

    with pytest.raises(CorruptCheckpoint, match="migration blocked"):
        RunStore("broken", tmp_path).load()

    assert path.read_bytes() == original
    report = json.loads((path.parent / "migration-error.json").read_text())
    assert report["status"] == "blocked"


def test_deployed_pre_898_version_1_checkpoint_is_migrated(tmp_path) -> None:
    """Chainlink #906: a checkpoint already stamped version=1 (the shape used
    before #898's identity-routing fields existed) must still be migrated,
    not passed through unchanged as if it were already current. Before this
    fix, CURRENT_CHECKPOINT_VERSION duplicated the literal 1 independently
    of schema.py's SCHEMA_VERSION, so a genuinely legacy version=1 document
    was treated as current and skipped _migrate_slices entirely — including
    the safety net that demotes an under-evidenced "spawned" slice to
    dispatch_unconfirmed, which #898 made a hard validation requirement."""
    path = tmp_path / "pre898" / "run.json"
    path.parent.mkdir()
    legacy_document = {
        "version": 1,
        "run_id": "pre898",
        "fsm": {"phase": "tl_planning", "waiting": []},
        "slices": {
            "slice-a": {
                "state": "spawned",
                "paths": ["src/a.py"],
                "branch": "task/slice-a",
            }
        },
    }
    original = json.dumps(legacy_document, sort_keys=True)
    path.write_text(original, encoding="utf-8")

    state = RunStore("pre898", tmp_path).load()

    assert state.version == SCHEMA_VERSION
    assert SCHEMA_VERSION > 1, "regression only proves anything once the target moved past 1"
    assert state.slices["slice-a"].status.value == "dispatch_unconfirmed"
    assert state.slices["slice-a"].dispatch_error is not None
    report = json.loads((path.parent / "migration-report.json").read_text())
    assert report["status"] == "complete"
    assert report["source_version"] == 1
    assert report["target_version"] == SCHEMA_VERSION


def test_legacy_reviewed_head_never_becomes_handoff_evidence() -> None:
    legacy = _legacy_spawned()
    legacy["slices"]["slice-a"].update({"reviewed_head": "head-from-review", "verdict": "GO"})

    result = migrate_checkpoint_document(legacy, run_id="legacy")

    migrated = result.document["slices"]["slice-a"]
    assert "handoff" not in migrated
    assert migrated["reviewed_head"] == "head-from-review"
    assert migrated["review_validation_required"] is True


def test_install_migration_interrupted_before_report_leaves_checkpoint_untouched_and_retries_cleanly(
    tmp_path, monkeypatch
) -> None:
    """chainlink #906 follow-up: an interruption between the backup write and
    the report write must not leave the live checkpoint migrated with no
    report, and a retry must not accumulate a second backup file."""
    path = tmp_path / "run.json"
    original = json.dumps(_legacy_spawned(), sort_keys=True)
    path.write_text(original, encoding="utf-8")
    result = migrate_checkpoint_document(json.loads(original), run_id="legacy")
    assert result.migrated

    real_atomic_write_json = migration_module._atomic_write_json
    calls: list[object] = []

    def crash_on_report(write_path, payload):
        calls.append(write_path)
        raise OSError("simulated interruption before the report write lands")

    monkeypatch.setattr(migration_module, "_atomic_write_json", crash_on_report)
    with pytest.raises(OSError, match="simulated interruption"):
        install_migration(path, result)
    monkeypatch.setattr(migration_module, "_atomic_write_json", real_atomic_write_json)

    # The checkpoint must still be the pre-migration document -- otherwise
    # the next load() would see source_version == CURRENT and never retry.
    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "migration-report.json").exists()
    backups = tuple(tmp_path.glob("run.json.legacy-*"))
    assert len(backups) == 1

    # Retry from scratch: must converge cleanly with exactly one backup.
    install_migration(path, result)
    assert path.read_text(encoding="utf-8") != original
    report = json.loads((tmp_path / "migration-report.json").read_text())
    assert report["status"] == "complete"
    assert len(tuple(tmp_path.glob("run.json.legacy-*"))) == 1


def test_install_migration_interrupted_after_report_before_checkpoint_retries_cleanly(
    tmp_path, monkeypatch
) -> None:
    """An interruption after the report is written but before the live
    checkpoint is overwritten must still be recoverable: the next load sees
    the checkpoint as not-yet-migrated and retries, converging on one
    backup and one up-to-date report rather than a stale report."""
    path = tmp_path / "run.json"
    original = json.dumps(_legacy_spawned(), sort_keys=True)
    path.write_text(original, encoding="utf-8")
    result = migrate_checkpoint_document(json.loads(original), run_id="legacy")
    assert result.migrated

    real_atomic_write_json = migration_module._atomic_write_json
    call_count = 0

    def crash_on_second_json_write(write_path, payload):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("simulated interruption before the checkpoint write lands")
        return real_atomic_write_json(write_path, payload)

    monkeypatch.setattr(migration_module, "_atomic_write_json", crash_on_second_json_write)
    with pytest.raises(OSError, match="simulated interruption"):
        install_migration(path, result)
    monkeypatch.setattr(migration_module, "_atomic_write_json", real_atomic_write_json)

    # The report landed durably before the interrupted checkpoint write.
    assert json.loads((tmp_path / "migration-report.json").read_text())["status"] == "complete"
    assert path.read_text(encoding="utf-8") == original
    assert len(tuple(tmp_path.glob("run.json.legacy-*"))) == 1

    # A retry (as the next real load() would trigger, since the checkpoint
    # is still pre-migration on disk) converges cleanly.
    install_migration(path, result)
    assert path.read_text(encoding="utf-8") != original
    assert len(tuple(tmp_path.glob("run.json.legacy-*"))) == 1
    assert json.loads((tmp_path / "migration-report.json").read_text())["status"] == "complete"


def test_unsupported_checkpoint_version_is_blocked(tmp_path) -> None:
    path = tmp_path / "future" / "run.json"
    path.parent.mkdir()
    original = {"version": 99, "run_id": "future"}
    path.write_text(json.dumps(original), encoding="utf-8")

    with pytest.raises(CorruptCheckpoint, match="migration blocked"):
        RunStore("future", tmp_path).load()

    assert json.loads(path.read_text()) == original
    assert (path.parent / "migration-error.json").exists()


def test_concurrent_loads_serialize_migration_and_leave_one_checkpoint(tmp_path) -> None:
    path = tmp_path / "concurrent" / "run.json"
    path.parent.mkdir()
    path.write_text(json.dumps(_legacy_spawned()), encoding="utf-8")

    def load_checkpoint():
        return RunStore("concurrent", tmp_path).load()

    with ThreadPoolExecutor(max_workers=2) as executor:
        states = list(executor.map(lambda _index: load_checkpoint(), range(2)))

    assert [state.version for state in states] == [SCHEMA_VERSION, SCHEMA_VERSION]
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == SCHEMA_VERSION
    assert len(tuple(path.parent.glob("run.json.legacy-*"))) == 1
    assert not tuple(path.parent.glob("*.tmp"))
