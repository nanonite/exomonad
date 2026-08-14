"""Fail-closed validation of the files required by the TL controller."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from tl_loop.plan_validation import PlanValidationError, validate_plan_document
from tl_loop.rlm.review_input import load_review_policy
from tl_loop.select.capability import load_capability
from tl_loop.select.policy import HarnessPolicy, load_policy

REQUIRED_FILES = ("config.toml", "harness_policy.toml", "review-policy.toml", "harness_capability.toml")
PLAN_PATH = Path(".exo/tl-loop/plan.json")


class PreflightError(ValueError):
    """Raised when a required controller input is absent or invalid."""


@dataclass(frozen=True)
class PreflightReport:
    """Validated paths and capability coverage for one project."""

    project_root: Path
    files: tuple[Path, ...]


def run_preflight(project_root: str | Path) -> PreflightReport:
    """Validate controller files, policy coverage, and the structured plan."""
    root = Path(project_root).expanduser().resolve()
    exo = root / ".exo"
    config_path = exo / "config.toml"
    policy_path = exo / "harness_policy.toml"
    review_path = exo / "review-policy.toml"
    capability_path = exo / "harness_capability.toml"
    plan_path = root / PLAN_PATH

    _require_files((config_path, policy_path, review_path, plan_path))
    try:
        with config_path.open("rb") as stream:
            tomllib.load(stream)
        policy = load_policy(policy_path)
        load_review_policy(review_path)
        _validate_plan(plan_path)
        if not capability_path.is_file():
            raise PreflightError(
                f"missing required TL file: {capability_path}\n\n"
                "Example harness_capability.toml for this policy:\n"
                + capability_example(policy)
            )
        load_capability(capability_path, policy_path=policy_path)
    except (OSError, ValueError, tomllib.TOMLDecodeError, PlanValidationError) as error:
        raise PreflightError(str(error)) from error
    return PreflightReport(root, (config_path, policy_path, review_path, capability_path, plan_path))


def capability_example(policy: HarnessPolicy) -> str:
    """Render a copy-ready map covering exactly the policy's allowed harnesses."""
    allowed = sorted({harness for role in policy.roles.values() for harness in role.allow})
    lines = ["# Static capability ratings. Each entry records the operator's basis.", "", "[capabilities]"]
    lines.extend(f'{json.dumps(harness)} = "standard"' for harness in allowed)
    return "\n".join(lines) + "\n"


def default_capability_example(policy: HarnessPolicy) -> str:
    """Compatibility name for the policy-derived diagnostic example."""
    return capability_example(policy)


def _require_files(paths: tuple[Path, ...]) -> None:
    missing = [path for path in paths if not path.is_file()]
    if missing:
        names = ", ".join(str(path) for path in missing)
        raise PreflightError(f"missing required TL file(s): {names}")


def _validate_plan(path: Path) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PreflightError(f"{path}: invalid plan: {error}") from error
    try:
        validate_plan_document(value)
    except PlanValidationError as error:
        raise PreflightError(f"{path}: invalid plan: {error}") from error
