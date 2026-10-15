"""Build compatibility metadata remains part of the installed controller identity."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tl_loop.compatibility import PLAN_COMPATIBILITY_VERSION
import tl_loop.fingerprint as fingerprint_module
from tl_loop.fingerprint import _validated, fingerprint_report, source_fingerprint


def test_source_fingerprint_publishes_plan_compatibility_version() -> None:
    source = Path(__file__).parents[1]

    fingerprint = source_fingerprint(source)

    assert fingerprint["plan_compatibility_version"] == PLAN_COMPATIBILITY_VERSION


def test_archive_fingerprint_without_compatibility_version_is_rejected() -> None:
    legacy_shape = {
        "schema_version": 1,
        "git_commit": "legacy-build",
        "tree_sha256": hashlib.sha256(b"source").hexdigest(),
    }

    with pytest.raises(ValueError, match="unsupported shape"):
        _validated(legacy_shape)


def test_commit_only_difference_does_not_make_installed_controller_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "tl_loop"
    source.mkdir()
    expected = {
        "schema_version": 1,
        "git_commit": "workspace-commit",
        "tree_sha256": hashlib.sha256(b"same controller source").hexdigest(),
        "plan_compatibility_version": PLAN_COMPATIBILITY_VERSION,
    }
    embedded = {**expected, "git_commit": "installed-commit"}
    monkeypatch.setattr(fingerprint_module, "source_fingerprint", lambda _source: expected)
    monkeypatch.setattr(fingerprint_module, "embedded_fingerprint", lambda: embedded)

    report = fingerprint_report(tmp_path)

    assert report["status"] == "current"
