"""Fail-closed validation of the files required by the TL controller."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from tl_loop.rlm.review_input import load_review_policy
from tl_loop.select.capability import DEFAULT_CAPABILITY_CONTENT, load_capability
from tl_loop.select.policy import load_policy

REQUIRED_FILES = ("config.toml", "harness_policy.toml", "review-policy.toml", "harness_capability.toml")


class PreflightError(ValueError):
    """Raised when a required controller input is absent or invalid."""


@dataclass(frozen=True)
class PreflightReport:
    """Validated paths and capability coverage for one project."""

    project_root: Path
    files: tuple[Path, ...]


def run_preflight(project_root: str | Path) -> PreflightReport:
    """Validate all four required files before the controller starts."""
    root = Path(project_root).expanduser().resolve()
    exo = root / ".exo"
    missing = [exo / name for name in REQUIRED_FILES if not (exo / name).is_file()]
    if missing:
        detail = ", ".join(str(path) for path in missing)
        example = "\n\nExample harness_capability.toml:\n" + DEFAULT_CAPABILITY_CONTENT
        raise PreflightError(f"missing required TL file(s): {detail}{example}")
    try:
        with (exo / "config.toml").open("rb") as stream:
            tomllib.load(stream)
        policy = load_policy(exo / "harness_policy.toml")
        load_review_policy(exo / "review-policy.toml")
        load_capability(exo / "harness_capability.toml", policy_path=exo / "harness_policy.toml")
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        raise PreflightError(str(error)) from error
    return PreflightReport(root, tuple(exo / name for name in REQUIRED_FILES))


def default_capability_example() -> str:
    """Return the shared minimal capability-map example for diagnostics."""
    return DEFAULT_CAPABILITY_CONTENT
