"""The fixture configs a migrated harness writes must actually load.

`exomonad init` refuses to start when `.exo/config.toml` does not parse, so a
harness can ship a scenario that never runs: the validator companion is never
spawned, the controller is never dispatched, and the scenario dies at startup
with nothing in `.exo/logs` to explain it.

That is not hypothetical. `CompanionConfig::command` is a `String` with no serde
default, so a `[[companions]]` entry without `command` is a hard parse error --
and a companion entry is the one thing a Codex messaging scenario adds, because
a second plan worker cannot be dispatched (workers are sequential).

Two things are checked per harness:

* the `.exo/config.toml` heredoc is rendered and parsed as TOML, and every
  `[[companions]]` table carries the fields `CompanionConfig` requires;
* every MCP tool the plan's task text tells the worker to call is one the
  `worker` role is actually granted.

The second replaces the role-coverage preflight the retired scenario got from
`just test-wasm-integration` against the TL/root tool set. The migrated scenario
does not use the TL/root tool set, so the preflight has to follow the tools the
worker really calls. The role matrix is read from
`docs/architecture/agent-system.md`, which is the same source
`rust/exomonad-core/tests/wasm_integration.rs` parses, so this check agrees with
the WASM runtime by construction.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = PROJECT_ROOT / "tests" / "e2e"
TOOL_MATRIX_DOC = PROJECT_ROOT / "docs" / "architecture" / "agent-system.md"
COMPANION_CONFIG_SOURCE = PROJECT_ROOT / "rust" / "exomonad" / "src" / "config.rs"
MIGRATED = ("chainlink-codex", "codex-messaging", "python-tl-worker-notify")

#: `CompanionConfig` fields with no `#[serde(default)]`. A missing one is a
#: parse error, not a default.
REQUIRED_COMPANION_FIELDS = ("name", "command")


def render_fixture_config(scenario: str) -> dict:
    """Render the `.exo/config.toml` a harness writes, then parse it.

    The harness writes the file through an unquoted heredoc, so the body is
    extracted and its shell expansions replaced with inert literals. Anything
    that would only break at runtime -- an unescaped quote, a missing required
    field, a stray backtick -- breaks here instead.
    """
    run = (E2E_DIR / scenario / "run.sh").read_text()
    match = re.search(r"cat > \.exo/config\.toml <<EOF\n(.*?)\nEOF\n", run, re.S)
    assert match is not None, f"{scenario} must write .exo/config.toml with a heredoc"
    body = match.group(1)
    body = re.sub(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", "sentinel", body)
    # An unquoted heredoc is backslash-processed before the file exists, so a
    # `\\`` in the harness reaches TOML as a bare backtick. Applying only that
    # one substitution keeps the check about quoting, not about shell minutiae.
    body = body.replace("\\`", "`")
    return tomllib.loads(body)


@pytest.mark.parametrize("name", MIGRATED)
def test_generated_config_parses_and_every_companion_is_complete(name: str) -> None:
    config = render_fixture_config(name)
    companions = config.get("companions", [])
    assert companions, f"{name} must declare the validator companion that observes the run"
    for companion in companions:
        for required in REQUIRED_COMPANION_FIELDS:
            assert companion.get(required), (
                f"{name}: companion {companion.get('name', '<unnamed>')!r} has no "
                f"{required!r}; CompanionConfig requires it and exomonad init "
                f"refuses to start without it"
            )


def test_companion_command_really_is_required_by_the_product() -> None:
    """Pin the requirement in the product, so the test above is not a guess."""
    source = COMPANION_CONFIG_SOURCE.read_text()
    block = re.search(
        r"pub struct CompanionConfig \{(.*?)\n\}", source, re.S
    )
    assert block is not None, "CompanionConfig must still exist in config.rs"
    for required in REQUIRED_COMPANION_FIELDS:
        field = re.search(
            rf"^\s*pub {required}: ([A-Za-z]+),", block.group(1), re.MULTILINE
        )
        assert field is not None, f"CompanionConfig must still declare {required}"
        # A `#[serde(default = ...)]` on the line above would make it optional.
        preceding = block.group(1)[: field.start()].rstrip().split("\n")[-1]
        assert "serde(default" not in preceding, (
            f"CompanionConfig.{required} now has a serde default, so the harness "
            f"no longer has to supply it; update REQUIRED_COMPANION_FIELDS"
        )


# ---------------------------------------------------------------------------
# The worker role's real tool set
# ---------------------------------------------------------------------------

_ROLE_ORDER = ("root", "tl", "dev", "reviewer", "worker")


def _expand_matrix_cell(cell: str) -> set[str]:
    """Expand a `a/b/c` / `_suffix` matrix cell, as wasm_integration.rs does."""
    tools: set[str] = set()
    prefix = ""
    for part in cell.split("/"):
        tool = part.strip().strip("`")
        if not tool:
            continue
        if tool.startswith("_"):
            if prefix:
                tools.add(f"{prefix}_{tool[1:]}")
            continue
        base, sep, _ = tool.rpartition("_")
        prefix = base if sep else ""
        tools.add(tool)
    return tools


def role_tools(role: str) -> set[str]:
    """The MCP tools `role` is granted, from the matrix wasm_integration.rs reads."""
    tools: set[str] = set()
    in_table = False
    for line in TOOL_MATRIX_DOC.read_text().splitlines():
        trimmed = line.strip()
        if trimmed.startswith("| Tool |") or trimmed.startswith("| Chainlink tool |"):
            in_table = True
            continue
        if in_table and not trimmed.startswith("|"):
            in_table = False
            continue
        if not in_table or re.match(r"^\|-{3,}", trimmed):
            continue
        cells = [c.strip() for c in trimmed.strip("|").split("|")]
        if len(cells) != len(_ROLE_ORDER) + 1:
            continue
        if cells[_ROLE_ORDER.index(role) + 1] == "x":
            tools |= _expand_matrix_cell(cells[0])
    return tools


def test_the_worker_role_tool_set_is_readable() -> None:
    """Guard the parser itself: a doc restructure must not silently empty it."""
    tools = role_tools("worker")
    assert "notify_parent" in tools
    assert "send_tmux_message" in tools
    assert "chainlink_session_start" in tools
    assert "chainlink_issue_comment" in tools


#: Tools the retired scenario relied on that the worker role does not have. The
#: retired flow ran through the TL/root position, which holds issue lifecycle
#: authority; a dispatched worker does not.
ROOT_ONLY_CHAINLINK = (
    "chainlink_issue_create",
    "chainlink_issue_close",
    "chainlink_issue_update",
    "chainlink_session_status",
    "chainlink_timer_start",
    "spawn_leaf",
    "spawn_reviewer",
)


def test_worker_role_cannot_take_issue_lifecycle_authority() -> None:
    tools = role_tools("worker")
    for forbidden in ROOT_ONLY_CHAINLINK:
        assert forbidden not in tools, (
            f"the worker role now exposes {forbidden}; the Chainlink scenario's "
            f"ownership assertion depends on the worker not holding it"
        )


_BACKTICKED_TOOL = re.compile(r"`([a-z][a-z0-9_]*(?:_[a-z0-9]+)+)`")


@pytest.mark.parametrize("name", MIGRATED)
def test_plan_tasks_only_ask_the_worker_for_tools_it_has(name: str) -> None:
    """Every tool a plan task names must be one the worker role is granted.

    A task that instructs a worker to call a tool it does not have produces a
    run that silently skips a step the validator then waits for. This is the
    re-anchored role preflight: the retired scenario's coverage came from
    `just test-wasm-integration` checked against the TL/root tool set, and the
    migrated scenario has to be checked against the worker tool set instead.
    """
    plan = json.loads((E2E_DIR / name / "plan.json").read_text())
    granted = role_tools("worker")
    for entry in plan["plan"]["workers"]:
        named = set(_BACKTICKED_TOOL.findall(entry["task"]))
        unknown = sorted(named - granted)
        assert not unknown, (
            f"{name}: plan task for {entry['name']!r} names tools the worker role "
            f"is not granted: {unknown}. Granted worker tools: {sorted(granted)}"
        )


def test_chainlink_task_uses_the_role_scoped_workflow() -> None:
    name = "chainlink-codex"
    """The Chainlink scenario must exercise exactly its role's tool set.

    This is the positive half of the preflight: the four Chainlink calls the
    worker is granted, and the completion notification, are all named. A task
    that quietly dropped one would leave the validator waiting on a marker no
    agent ever writes.
    """
    plan = json.loads((E2E_DIR / name / "plan.json").read_text())
    granted = role_tools("worker")
    for entry in plan["plan"]["workers"]:
        named = set(_BACKTICKED_TOOL.findall(entry["task"]))
        chainlink_tools = {t for t in named if t.startswith("chainlink_")}
        assert chainlink_tools, f"{name}: the plan must drive a Chainlink tool"
        assert chainlink_tools <= granted, (
            f"{name}: {sorted(chainlink_tools - granted)} are not worker tools"
        )
