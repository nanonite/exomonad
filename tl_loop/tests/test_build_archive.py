"""Regression coverage for the Rust-embedded TL controller archive."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import pytest

from scripts.build_tl_loop_archive import build_archive
from scripts.check_tl_loop_archive import ArchiveFingerprintError, check_archive_fingerprint

REPOSITORY_ROOT = Path(__file__).parents[2]
SOURCE_MODULE = REPOSITORY_ROOT / "tl_loop/preflight.py"
ARCHIVE_BUILDER = REPOSITORY_ROOT / "scripts/build_tl_loop_archive.py"
ARCHIVE_MEMBER = "tl_loop/preflight.py"


def _build_exomonad() -> None:
    subprocess.run(
        ["cargo", "build", "-p", "exomonad"],
        cwd=REPOSITORY_ROOT,
        check=True,
    )


def _test_embedded_archive(marker: str) -> None:
    environment = os.environ.copy()
    environment["EXOMONAD_TL_LOOP_EXPECT_MARKER"] = marker
    environment["EXOMONAD_TL_LOOP_EXPECT_MEMBER"] = ARCHIVE_MEMBER
    subprocess.run(
        [
            "cargo",
            "test",
            "-p",
            "exomonad",
            "--bin",
            "exomonad",
            "init::tests::embedded_archive_contains_expected_source",
            "--",
            "--exact",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
    )


def test_source_edit_is_present_in_rebuilt_embedded_archive() -> None:
    original = SOURCE_MODULE.read_text(encoding="utf-8")
    marker = f"source-edit-regression-{uuid.uuid4().hex}"
    _build_exomonad()
    try:
        SOURCE_MODULE.write_text(
            f"{original.rstrip()}{chr(10)}# {marker}{chr(10)}",
            encoding="utf-8",
        )
        _build_exomonad()
        _test_embedded_archive(marker)
    finally:
        SOURCE_MODULE.write_text(original, encoding="utf-8")
        _build_exomonad()


def test_new_nested_source_is_present_after_incremental_build() -> None:
    source_directory = REPOSITORY_ROOT / "tl_loop/client"
    source_file = source_directory / "archive_new_module.py"
    archive_member = "tl_loop/client/archive_new_module.py"
    marker = f"new-file-regression-{uuid.uuid4().hex}"
    _build_exomonad()
    try:
        source_file.write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
        _build_exomonad()
        environment = os.environ.copy()
        environment["EXOMONAD_TL_LOOP_EXPECT_MARKER"] = marker
        environment["EXOMONAD_TL_LOOP_EXPECT_MEMBER"] = archive_member
        subprocess.run(
            [
                "cargo",
                "test",
                "-p",
                "exomonad",
                "--bin",
                "exomonad",
                "init::tests::embedded_archive_contains_expected_source",
                "--",
                "--exact",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=True,
        )
    finally:
        source_file.unlink(missing_ok=True)
        _build_exomonad()


def test_rebuilt_archive_carries_the_ordered_recovery_evidence(tmp_path: Path) -> None:
    """The controller the server runs must contain the recovery fix itself.

    The ordered-recovery fix lives in the packaged archive, not in the binary's
    own source: the ExoMonad server executes this zipapp. A stale archive would
    therefore keep the pre-fix behavior — legacy Chainlink results rejected,
    answered gates re-armed, and a recovery relaunched against the exited child
    pane — while every source-level test passed. The evidence has to be read out
    of the rebuilt artifact, not out of the tree it was built from.
    """
    archive = tmp_path / "tl_loop.pyz"
    build_archive(REPOSITORY_ROOT / "tl_loop", archive)

    with zipfile.ZipFile(archive) as package:
        escalate = package.read("tl_loop/loop/escalate.py").decode("utf-8")
        driver = package.read("tl_loop/loop/driver.py").decode("utf-8")

    # The legacy Chainlink shape normalizes to one positive issue ID.
    assert "cicoIssueId" in escalate
    assert "def chainlink_issue_id(" in escalate
    assert "AmbiguousIssueId" in escalate
    # An approved gate authorizes recovery; a rejected one is never re-armed.
    assert "def _hold_ordered_recovery_gate(" in driver
    assert "authorized: bool = False" in driver
    assert "def _ordered_child_invocation_is_gone(" in driver
    assert "def _mint_ordered_child_invocation(" in driver
    assert '"sub_tl_recovered"' in driver
    # A reopened child must stay at a boundary the next proof accepts, or a second
    # failure of the same child is permanently unrecoverable.
    assert "RECONCILED_SUB_TL_DISPATCH_BOUNDARIES" in driver
    assert "dispatch_last_boundary not in RECONCILED_SUB_TL_DISPATCH_BOUNDARIES" in driver


def test_archive_excludes_interpreter_artifacts_and_tests(tmp_path: Path) -> None:
    archive_path = tmp_path / "tl_loop.pyz"
    subprocess.run(
        [
            sys.executable,
            str(ARCHIVE_BUILDER),
            "--source",
            str(REPOSITORY_ROOT / "tl_loop"),
            "--output",
            str(archive_path),
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
    )

    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()

    assert not [name for name in names if name.endswith(".pyc")]
    assert not [
        name
        for name in names
        if any(part in {"__pycache__", ".venv", "tests"} for part in Path(name).parts)
    ]


def test_archive_fingerprint_detects_source_edit(tmp_path: Path) -> None:
    source = tmp_path / "tl_loop"
    shutil.copytree(REPOSITORY_ROOT / "tl_loop", source)
    archive = tmp_path / "tl_loop.pyz"

    build_archive(source, archive)
    check_archive_fingerprint(archive, source)

    target = source / "preflight.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# stale-archive-regression\n",
        encoding="utf-8",
    )
    with pytest.raises(ArchiveFingerprintError, match="stale TL controller archive"):
        check_archive_fingerprint(archive, source)
