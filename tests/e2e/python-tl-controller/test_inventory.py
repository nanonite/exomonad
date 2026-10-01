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


def test_chainlink_codex_plan_renders_its_issue_id_everywhere() -> None:
    """The plan names the issue more than once, and the guard must allow it.

    A guard that required exactly one placeholder refused a correct plan: the
    task states the issue id in its opening line and repeats it on each tool call
    that takes one. Found by the 2026-09-30 live run, which died at fixture
    setup with "must contain exactly one ... placeholder, found 3".
    """
    run = (E2E_DIR / "chainlink-codex" / "run.sh").read_text(encoding="utf-8")
    # Only the embedded renderer counts. The prose around it is allowed to say
    # "exactly one" in order to explain why that requirement was wrong.
    renderer = run.split('ISSUE_ID="$ISSUE_ID" python3 -', 1)[1].split("\nPY", 1)[0]
    code = "\n".join(
        line for line in renderer.splitlines() if not line.strip().startswith("#")
    )
    assert "exactly one" not in code, (
        "chainlink-codex's plan renderer must not require exactly one "
        "placeholder; the plan legitimately references the issue id three times"
    )
    assert "count(placeholder) == 0" in code, (
        "the renderer must still require at least one placeholder, or it "
        "renders a plan with no issue reference and nothing notices"
    )

    # And the guard that actually matters is still there: nothing may survive
    # rendering, whatever the count.
    assert '"{{" in rendered' in code, (
        "the unrendered-placeholder check is the real safety net and must stay"
    )

    # The shipped plan must still use the placeholder, or the guard guards
    # nothing.
    plan = (E2E_DIR / "chainlink-codex" / "plan.json").read_text(encoding="utf-8")
    assert plan.count("{{CHAINLINK_ISSUE_ID}}") >= 2, (
        "the plan is expected to reference the issue id several times; if that "
        "changed, the guard above should be revisited"
    )


def test_chainlink_codex_does_not_claim_the_default_webhook_port() -> None:
    """The fixture must not contend for 7433, or one run's server breaks the next.

    `serve` is never reaped, so a server from an earlier run keeps holding the
    default `0.0.0.0:7433`. A fixture that sets no `port` then binds the UDS,
    loses the TCP bind, exits 1, and leaves a dead socket for `init`'s 30s health
    check to wait out -- which is how the 2026-09-30 runs of this scenario died
    before dispatch. Chainlink #1150 files the two product defects; the harness
    side of it is not claiming a fixed port. Every other e2e server scenario in
    the tree sets one (`claude-only` and `claude-teams-inbox` use `port = 0`).
    """
    run = (E2E_DIR / "chainlink-codex" / "run.sh").read_text(encoding="utf-8")
    config = run.split("cat > .exo/config.toml <<EOF", 1)[1].split("\nEOF", 1)[0]
    assert re.search(r"^port = 0$", config, re.MULTILINE), (
        "chainlink-codex's fixture config must set `port = 0`; without it this "
        "scenario contends for the default webhook port and a leaked server "
        "from any earlier run stops it at init's socket health check (#1150)"
    )


def test_chainlink_codex_config_heredoc_has_no_command_substitutions() -> None:
    """Nothing in the config heredoc may be a backtick.

    The delimiter is unquoted so `$SESSION` and the companion's arguments expand
    -- which also makes every backtick a command substitution. Backticking words
    in a prose comment is the natural way to write that comment, and it silently
    deletes them: the comment this file's own `port = 0` rationale is attached to
    reached the generated `.exo/config.toml` with its words stripped out, so the
    documentation never reached the file it documents.

    Worse, the substitution *runs*. `init` resolves to `/usr/sbin/init`, a symlink
    to systemd, so backticking it executed systemd during fixture setup on every
    run. And a backticked word that prints anything -- `serve`, `codex`,
    `git` are all commonly installed -- has its output spliced un-commented into
    the middle of the file the controller parses, failing the run at a TOML parse
    error with nothing to say why.

    A backslash-escaped backtick is not a substitution: it is the documented
    escape hatch for a genuine literal one, and it renders correctly. So the pin
    is on *unescaped* backticks, which are the ones that run. `codex-messaging`
    carries the same defect in its own comment block and is owned by #1152; this
    pin covers the scenario this slice owns.
    """
    run = (E2E_DIR / "chainlink-codex" / "run.sh").read_text(encoding="utf-8")
    config = run.split("cat > .exo/config.toml <<EOF", 1)[1].split("\nEOF", 1)[0]
    offenders = [
        line for line in config.splitlines() if re.search(r"(?<!\\)`", line)
    ]
    assert not offenders, (
        "chainlink-codex writes .exo/config.toml through an UNQUOTED heredoc, so "
        "an unescaped backtick is a command substitution, not punctuation. These "
        "lines would run their contents as commands and lose the backticked "
        "words:\n  "
        + "\n  ".join(offenders)
        + "\nWrite the words plainly, or escape the backtick as \\` if you truly "
        "need a literal one."
    )
