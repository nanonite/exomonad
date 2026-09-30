"""Step 1 of #1127, kept honest: no scenario may silently drive the retired model.

`tests/e2e/CODEX-TL-MIGRATION.md` is the inventory of every scenario that
described or validated an interactive Codex root TL, and what happened to it.
This test makes the inventory a maintained artifact rather than prose:

* a scenario that sets ``root_agent_type = "codex"``, passes ``--tl`` to
  ``init``, or hands ``init`` a non-JSON ``initial_prompt`` must be listed in the
  inventory, so a newly-retired scenario cannot join the suite unremarked;
* the three migrated Codex scenarios must actually be plan-driven, so the
  inventory's "Migrated" table cannot become a lie;
* the inventory must name the two scenarios that were flagged -- the one fixed
  in #1127 and the one declared out of scope -- with a rationale each.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = PROJECT_ROOT / "tests" / "e2e"
INVENTORY = E2E_DIR / "CODEX-TL-MIGRATION.md"

#: The three scenarios this migration moved onto plan.json.
MIGRATED = ("codex-messaging", "chainlink-codex", "python-tl-worker-notify")

#: Scenarios named in the review that must be accounted for explicitly.
FLAGGED = ("orphan-pr-guard", "recursive-crash-convergence")

#: Harness entry points: a scenario's driver plus its config-bearing scripts.
HARNESS_GLOBS = ("*/run.sh", "*/scenario.py", "*/run.py", "*/project.py")


def harness_files() -> list[Path]:
    found: list[Path] = []
    for pattern in HARNESS_GLOBS:
        found.extend(sorted(E2E_DIR.glob(pattern)))
    return [p for p in found if "__pycache__" not in p.parts]


def assigns_root_agent_type_codex(path: Path) -> bool:
    return bool(re.search(r"""root_agent_type\s*=\s*["']codex["']""", path.read_text()))


def passes_tl_flag(path: Path) -> bool:
    return bool(re.search(r"\binit\b[^\n]*--tl\b", path.read_text()))


def has_natural_language_initial_prompt(path: Path) -> bool:
    """An `initial_prompt` that is not a JSON WorkPlan document.

    A commented-out or negated mention does not count; only an assignment the
    harness would actually write into `.exo/config.toml`.
    """
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = re.search(r"""initial_prompt\s*=\s*(.*)$""", stripped)
        if not match:
            continue
        value = match.group(1).strip()
        if value in {'"""', '""" '}:  # the opening line of a multi-line block
            return True
        if value.startswith("{") or value.startswith("'") or value.startswith('"'):
            continue
        return True
    return False


def inventory_text() -> str:
    assert INVENTORY.is_file(), (
        "tests/e2e/CODEX-TL-MIGRATION.md is the step-1 inventory; a scenario may "
        "not join or leave the suite without it"
    )
    return INVENTORY.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("check", "label"),
    [
        (assigns_root_agent_type_codex, 'sets root_agent_type = "codex"'),
        (passes_tl_flag, "passes --tl to exomonad init"),
        (has_natural_language_initial_prompt, "feeds init a natural-language initial_prompt"),
    ],
)
def test_every_retired_model_scenario_is_in_the_inventory(
    check, label: str
) -> None:
    inventory = inventory_text()
    unlisted = [
        str(path.relative_to(PROJECT_ROOT))
        for path in harness_files()
        if check(path) and f"{path.parent.name}/" not in inventory
    ]
    assert not unlisted, (
        f"scenarios that {label} are not listed in {INVENTORY.name}: {unlisted}. "
        f"Either migrate them or record them as out of scope with a rationale."
    )


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_scenario_is_actually_plan_driven(name: str) -> None:
    """The inventory's "Migrated" table must stay true."""
    scenario = E2E_DIR / name
    plan = scenario / "plan.json"
    assert plan.is_file(), f"{name} must ship the plan the controller consumes"

    document = json.loads(plan.read_text(encoding="utf-8"))
    assert document["run_id"] == "root"
    assert document["plan"]["workers"], f"{name} must dispatch at least one worker"

    run = (scenario / "run.sh").read_text(encoding="utf-8")
    assert not assigns_root_agent_type_codex(scenario / "run.sh"), (
        f"{name} must not assign root_agent_type; init ignores it and it names a "
        f"root Codex agent that no longer exists"
    )
    assert "plan.json" in run, f"{name} must install the plan before init"


@pytest.mark.parametrize("name", FLAGGED)
def test_flagged_scenario_has_a_rationale(name: str) -> None:
    inventory = inventory_text()
    assert f"`{name}/`" in inventory, f"{name} must be named in the inventory"
    # The rationale has to be a paragraph, not just a mention.
    section = inventory.split(f"`{name}/`", 1)[1]
    following = section.split("\n|", 1)[0]
    assert len(following.split()) >= 25, (
        f"{name} needs a rationale, not a bare mention:\n{following[:200]}"
    )


def test_inventory_explains_the_out_of_scope_scenarios() -> None:
    inventory = inventory_text()
    assert "Out of scope" in inventory
    # The non-Codex scenarios are listed so the inventory is complete across the
    # whole e2e tree, not only the Codex part of it.
    for name in ("claude-only", "opencode-tl", "hook-rewrite"):
        assert name in inventory, f"{name} must be accounted for in the inventory"


def test_orphan_pr_guard_acknowledges_it_cannot_run() -> None:
    """The one un-migrated retired-model scenario must say so in its own file.

    It is opt-in and not part of any gate, so it will keep sitting there. A
    reader who opens it has to learn from the file, not from the inventory, that
    `init` no longer accepts `--tl` and no longer accepts a natural-language
    `initial_prompt`.
    """
    header = (E2E_DIR / "orphan-pr-guard" / "run.sh").read_text(encoding="utf-8")
    head = "\n".join(header.splitlines()[:30])
    assert "superseded" in head.lower() or "retired" in head.lower(), (
        "orphan-pr-guard/run.sh must say in its header that it targets the retired "
        "interactive Codex root TL"
    )
    assert "--tl" in head, (
        "orphan-pr-guard/run.sh must name the flag init no longer accepts"
    )
