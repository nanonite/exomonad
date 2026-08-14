"""Regression coverage for the Rust-embedded TL controller archive."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).parents[2]
SOURCE_MODULE = REPOSITORY_ROOT / "tl_loop/__init__.py"


def _build_exomonad() -> None:
    subprocess.run(
        ["cargo", "build", "-p", "exomonad"],
        cwd=REPOSITORY_ROOT,
        check=True,
    )


def _test_embedded_archive(marker: str) -> None:
    environment = os.environ.copy()
    environment["EXOMONAD_TL_LOOP_EXPECT_MARKER"] = marker
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
    try:
        SOURCE_MODULE.write_text(
            f"{original.rstrip()}{chr(10)}# {marker}{chr(10)}",
            encoding="utf-8",
        )
        _test_embedded_archive(marker)
    finally:
        SOURCE_MODULE.write_text(original, encoding="utf-8")
        _build_exomonad()
