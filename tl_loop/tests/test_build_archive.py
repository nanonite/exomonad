"""Regression coverage for the Rust-embedded TL controller archive."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

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
