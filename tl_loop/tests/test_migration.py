"""Legacy checkpoint migration and evidence-preservation coverage."""

from __future__ import annotations

import json

import pytest

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


def test_unsupported_checkpoint_version_is_blocked(tmp_path) -> None:
    path = tmp_path / "future" / "run.json"
    path.parent.mkdir()
    original = {"version": 99, "run_id": "future"}
    path.write_text(json.dumps(original), encoding="utf-8")

    with pytest.raises(CorruptCheckpoint, match="migration blocked"):
        RunStore("future", tmp_path).load()

    assert json.loads(path.read_text()) == original
    assert (path.parent / "migration-error.json").exists()
