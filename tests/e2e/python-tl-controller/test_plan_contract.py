"""The plan's worker names are the run.json slice keys -- prove it with the driver.

``run.json`` keys ``slices`` by the ``plan.json`` worker name, not by the agent
identity the controller dispatches it under. A validator that asserts the agent
identity (``<plan name>-codex``) reports a missing slice on a run that
dispatched correctly.

Rather than restate the rule, this asks the code that owns it. ``tl_loop`` is
importable without a build, so the expected slice keys are read from
``_initial_slices`` -- the same function the controller runs -- and compared
against what each validator asserts and against the plan it ships.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = PROJECT_ROOT / "tests" / "e2e"
MIGRATED = ("chainlink-codex", "codex-messaging", "python-tl-worker-notify")

# Read the real slice keys in a child interpreter: importing tl_loop in-process
# would let its module state leak into the rest of the suite, and a subprocess
# keeps this check honest about what the controller would actually see.
_DRIVER_PROBE = """
import json
import sys
from pathlib import Path

from tl_loop.loop.driver import WorkPlan, _initial_slices

print(json.dumps(sorted(_initial_slices(
    WorkPlan.from_mapping(json.loads(Path(sys.argv[1]).read_text())["plan"])
))))
"""


def driver_slice_keys(plan_path: Path) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", _DRIVER_PROBE, str(plan_path)],
        text=True,
        capture_output=True,
        cwd=PROJECT_ROOT,
        env={"PYTHONPATH": str(PROJECT_ROOT), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("name", MIGRATED)
def test_plan_worker_names_are_the_run_json_slice_keys(name: str) -> None:
    """The controller keys slices on the plan name, and the plan ships that name."""
    plan_path = E2E_DIR / name / "plan.json"
    declared = [w["name"] for w in json.loads(plan_path.read_text())["plan"]["workers"]]
    assert driver_slice_keys(plan_path) == sorted(declared)


@pytest.mark.parametrize("name", MIGRATED)
def test_validator_asserts_slice_keys_not_agent_identities(name: str) -> None:
    """No validator may assert a slice key that ends in the harness suffix.

    The agent identity is the right thing to look for on disk (the agent
    directory) and the wrong thing to look for in ``run.json``. Asserting the
    identity there fails on every run, so the two are pinned apart.
    """
    validate = (E2E_DIR / name / "validate.sh").read_text()

    call = re.search(
        r'e2e_python_tl_assert_slices "\$REPO_DIR" "([^"]+)"', validate
    )
    assert call is not None, f"{name} must assert its plan slices are present"
    assert call.group(1) == "$PLAN_SLICE", (
        f"{name} asserts slices by `{call.group(1)}`, but run.json keys them by the "
        f"plan name; assert $PLAN_SLICE"
    )

    # The identity is derived, not hardcoded, so the two cannot drift apart.
    assert 'WORKER_AGENT="$(e2e_python_tl_agent_identity "$PLAN_SLICE")"' in validate, (
        f"{name} must derive the agent identity from the plan slice name"
    )
    assert not re.search(r'^WORKER_AGENT="[^\"]*-codex"$', validate, re.MULTILINE), (
        f"{name} must not hardcode a `-codex` agent identity"
    )


@pytest.mark.parametrize("name", MIGRATED)
def test_validator_derives_the_slice_name_from_the_shipped_plan(name: str) -> None:
    """Deriving the name means renaming a plan slice cannot desync the validator."""
    validate = (E2E_DIR / name / "validate.sh").read_text()
    assert (
        'PLAN_SLICE="$(e2e_python_tl_plan_slices "$SCRIPT_DIR/plan.json")"' in validate
    ), f"{name} must read the slice name from the plan it ships"


def test_plan_slice_helper_agrees_with_the_driver() -> None:
    """The bash helper and the controller must name slices identically."""
    for name in MIGRATED:
        plan_path = E2E_DIR / name / "plan.json"
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; e2e_python_tl_plan_slices "$2"',
             "bash", str(E2E_DIR / "lib" / "python-tl.sh"), str(plan_path)],
            text=True, capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == driver_slice_keys(plan_path)


def test_agent_identity_helper_adds_the_harness_suffix_once() -> None:
    """The identity is the plan name plus exactly one harness suffix."""
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; e2e_python_tl_agent_identity "$2" "$3"',
         "bash", str(E2E_DIR / "lib" / "python-tl.sh"), "plan-slice", "codex"],
        text=True, capture_output=True, check=False,
    )
    assert result.stdout.strip() == "plan-slice-codex"
