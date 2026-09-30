"""The tmux-server isolation placement and the exit-status contract.

`e2e_python_tl_isolate_tmux_server` only does anything useful if it runs at top
level, before the harness's first `tmux` call and before `exomonad init` starts
the run's tmux server. An earlier revision of these harnesses put the call
inside `cleanup()`, where it could only affect teardown; the live run then kept
the host's `CODEX_HOME` and the worker seeded no hook trust, while the harness
still printed the isolation line's own directory. The placement is therefore a
contract, not a style preference, and it is pinned here.

The second half of this file pins the companion property that let a failing run
report success: `init` exits 0 as soon as it attaches, so a scenario may not
fall back to `init`'s status when the validator wrote no verdict.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = PROJECT_ROOT / "tests" / "e2e"
LIB = E2E_DIR / "lib" / "python-tl.sh"

SCENARIOS = ("codex-messaging", "chainlink-codex", "python-tl-worker-notify")

ISOLATION_CALL = 'e2e_python_tl_isolate_tmux_server "$WORK_DIR"'


def run_script(scenario: str) -> str:
    return (E2E_DIR / scenario / "run.sh").read_text(encoding="utf-8")


def function_bodies(text: str) -> list[str]:
    """Return the source of every `name() { ... }` block, brace-matched."""
    bodies: list[str] = []
    for match in re.finditer(r"^([a-zA-Z_][\w]*)\(\)\s*\{", text, re.M):
        depth = 0
        start = match.end() - 1
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    bodies.append(text[start : index + 1])
                    break
    return bodies


def call_line_number(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line and not line.strip().startswith("#"):
            return number
    raise AssertionError(f"no call to {needle!r} in the harness")


def function_line_numbers(text: str) -> set[int]:
    """Line numbers that sit inside a shell function body.

    A `tmux kill-session` inside `cleanup()` is textually early but runs at
    teardown, so ordering checks have to ignore it. Only top-level commands
    constrain the run.
    """
    inside: set[int] = set()
    for match in re.finditer(r"^([a-zA-Z_][\w]*)\(\)\s*\{", text, re.M):
        depth = 0
        start_offset = match.end() - 1
        start_line = text.count("\n", 0, start_offset) + 1
        for index in range(start_offset, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    end_line = text.count("\n", 0, index) + 1
                    inside.update(range(start_line, end_line + 1))
                    break
    return inside


def first_command_line(text: str, pattern: str, *, top_level_only: bool = True) -> int:
    """Line number of the first line that runs `pattern` as a command.

    A comment, a string, or a mention inside a quoted argument does not count --
    only a line whose first word is the command. By default commands inside a
    function body are skipped, because they run at teardown and cannot constrain
    the run's ordering.
    """
    inside = function_line_numbers(text) if top_level_only else set()
    for number, line in enumerate(text.splitlines(), 1):
        if number in inside:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(pattern, stripped):
            return number
    raise AssertionError(f"no command matching {pattern!r} in the harness")


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_isolation_is_not_called_from_a_function(scenario: str) -> None:
    """A call inside `cleanup()` cannot affect the run it is cleaning up."""
    text = run_script(scenario)
    for body in function_bodies(text):
        assert ISOLATION_CALL not in body, (
            f"{scenario}/run.sh calls the tmux isolation from inside a function. "
            f"It must run at top level, before the first tmux call, or the run's "
            f"tmux server keeps the host's captured CODEX_HOME:\n"
            f"{ISOLATION_CALL}\nfound in:\n{body[:200]}"
        )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_isolation_runs_before_the_first_tmux_call(scenario: str) -> None:
    text = run_script(scenario)
    isolated = call_line_number(text, ISOLATION_CALL)
    first_tmux = first_command_line(text, r"tmux\s")
    assert isolated < first_tmux, (
        f"{scenario}/run.sh isolates the tmux server at line {isolated}, but its "
        f"first `tmux` command is at line {first_tmux}. Every tmux call, including "
        f"the one that starts the run's server, must reach the isolated server."
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_isolation_runs_before_init(scenario: str) -> None:
    text = run_script(scenario)
    isolated = call_line_number(text, ISOLATION_CALL)
    init = first_command_line(text, r"\"?\$EXOMONAD_BIN\"?\s+init")
    assert isolated < init, (
        f"{scenario}/run.sh isolates at line {isolated} but runs `exomonad init` at "
        f"line {init}. The server captures its environment when init starts it, so "
        f"TMUX_TMPDIR has to be exported first."
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_shared_library_is_sourced_before_the_isolation_call(scenario: str) -> None:
    text = run_script(scenario)
    source = call_line_number(text, "lib/python-tl.sh")
    isolated = call_line_number(text, ISOLATION_CALL)
    assert source < isolated, (
        f"{scenario}/run.sh calls the isolation helper at line {isolated} but only "
        f"sources its definition at line {source}"
    )


def test_helper_exports_tmux_tmpdir_before_init_can_read_it() -> None:
    """The helper must export, not merely assign, TMUX_TMPDIR."""
    helper = LIB.read_text(encoding="utf-8")
    body = next(b for b in function_bodies(helper) if "e2e_python_tl_isolate_tmux_server" in b.split("{", 1)[0] or "TMUX_TMPDIR" in b)
    assert 'export TMUX_TMPDIR=' in body, (
        "the helper must export TMUX_TMPDIR; a local assignment would not reach "
        "`exomonad init` or the tmux server it starts"
    )
    # The opt-out exists for the A/B that showed a shared server reproduces the
    # product bug; it must default to isolation on.
    assert "E2E_PYTHON_TL_TMUX_ISOLATION" in body
    assert '${E2E_PYTHON_TL_TMUX_ISOLATION:-1}' in body, (
        "isolation must be on by default; the opt-out is for A/B only"
    )


# --------------------------------------------------------------------------
# Exit status
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_missing_validator_result_cannot_exit_zero(scenario: str) -> None:
    """`init` exits 0 on attach, so its status proves nothing about the run."""
    text = run_script(scenario)
    assert 'exit "$INIT_STATUS"' not in text, (
        f"{scenario}/run.sh falls back to init's exit status when the validator "
        f"wrote no result file. `init` exits 0 as soon as it attaches the "
        f"session, so that fallback reports success for a run that proved "
        f"nothing -- which is exactly what the 2026-09-30 live run did."
    )
    assert "validator wrote no result file" in text, (
        f"{scenario}/run.sh must say a missing validator result is a failure"
    )
    assert "Failures: 0" in text, (
        f"{scenario}/run.sh must require the validator's own zero-failure verdict"
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_result_file_is_checked_after_init_runs(scenario: str) -> None:
    """Reading the verdict before `init` would pass on the previous run's file."""
    text = run_script(scenario)
    init = first_command_line(text, r"\"?\$EXOMONAD_BIN\"?\s+init")
    verdict = call_line_number(text, "validator wrote no result file")
    assert verdict > init, (
        f"{scenario}/run.sh checks for the validator result at line {verdict}, "
        f"before `exomonad init` at line {init}"
    )
