"""The scenario docs must describe the assertions the helper actually makes.

`e2e_python_tl_assert_codex_trust` and
`e2e_python_tl_assert_codex_child_config` between them assert three distinct
things:

* the generated child config carries the three hook commands,
* the isolated Codex home carries one ``[hooks.state]`` entry with a
  ``trusted_hash`` per event, plus the project trust entry,
* the isolated Codex home does **not** carry the retired
  ``# BEGIN EXOMONAD CODEX HOOKS`` block.

A doc row that says the block is asserted *present* sends the next maintainer
to "fix" the helper backwards, back to a shape the product stopped writing in
8934378f (#210). Three scenario docs and E2E_STATUS said exactly that while the
helper asserted the opposite, and nothing caught it -- so the agreement is
checked here, against the helper's own source.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = PROJECT_ROOT / "tests" / "e2e"
HELPER = E2E_DIR / "lib" / "python-tl.sh"

#: The documents that describe what the validators assert.
DOCS = (
    *(E2E_DIR / name / "e2e-test.md" for name in
      ("codex-messaging", "chainlink-codex", "python-tl-worker-notify")),
    PROJECT_ROOT / "E2E_STATUS.md",
)

RETIRED_BLOCK = "# BEGIN EXOMONAD CODEX HOOKS"

#: A line may name the retired marker only to say it must be gone. Each of these
#: is an absence/retirement statement; a line that lacks all of them is claiming
#: the block is required.
ABSENCE_MARKERS = (
    "not ", "absent", "must not", "no longer", "never", "stripped", "removed in",
    "retired", "rejects", "without",
)

#: Phrases that assert existence, used to catch a line that says both things.
PRESENCE_PHRASES = (
    " is present", " are present", "is required", "are required",
    "must contain", "must include", "must have",
)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_doc_does_not_claim_the_retired_block_is_asserted_present(doc: Path) -> None:
    """No document may say the validator requires the retired block.

    The rule is deliberately blunt: any line naming the marker must also state
    its absence. That way a doc cannot "fix" the helper backwards by adding a
    row that claims the block is present, and cannot smuggle the claim into
    prose either.
    """
    for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        if RETIRED_BLOCK not in line:
            continue
        lowered = line.lower()
        assert any(marker in lowered for marker in ABSENCE_MARKERS), (
            f"{doc.name}:{number} names the retired hooks block without saying it must "
            f"be absent. The helper fails the run when that block is present, so a "
            f"reader following this line would revert the helper:\n  {line.strip()}"
        )
        assert not any(phrase in lowered for phrase in PRESENCE_PHRASES), (
            f"{doc.name}:{number} both disclaims and requires the retired hooks "
            f"block:\n  {line.strip()}"
        )


def helper_source() -> str:
    return HELPER.read_text(encoding="utf-8")


def test_helper_fails_the_run_when_the_retired_block_is_present() -> None:
    """The premise of the doc checks: the helper rejects the block."""
    helper = helper_source()
    guard = re.search(
        r"if grep -Fq '[^']*BEGIN EXOMONAD CODEX HOOKS'[^;]*; then(.*?)\n    fi",
        helper,
        re.S,
    )
    assert guard is not None, (
        "the helper must actively reject the retired block in the Codex user config"
    )
    body = guard.group(1)
    assert "return 1" in body, (
        f"rejecting the retired block must fail the assertion, got:\n{body}"
    )
    # And the marker must not also be required to be present anywhere.
    assert not re.search(r"missing the ExoMonad hooks block|hooks block is missing", helper), (
        "the helper must not still require the retired block"
    )


def test_helper_asserts_hook_commands_in_the_child_config() -> None:
    helper = helper_source()
    for event in ("pre-tool-use", "post-tool-use", "stop"):
        assert f"hook $event --runtime codex" in helper, (
            f"the helper must assert the {event} hook command in the child config"
        )


def test_helper_asserts_hook_and_project_trust_in_the_isolated_home() -> None:
    helper = helper_source()
    for event in ("pre_tool_use", "post_tool_use", "stop"):
        assert f':$event:0:0' in helper, (
            f"the helper must assert a hooks.state entry for {event}"
        )
    assert "trusted_hash" in helper, "a trust entry without a hash proves nothing"
    assert 'trust_level = \\"trusted\\"' in helper or 'trust_level = "trusted"' in helper


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_doc_describes_the_three_asserted_shapes(doc: Path) -> None:
    """A doc for a migrated scenario must name all three things."""
    text = doc.read_text(encoding="utf-8")
    if RETIRED_BLOCK not in text and "hooks.state" not in text:
        pytest.skip(f"{doc.name} does not describe the Codex trust contract")

    lowered = text.lower()
    assert "hooks.state" in lowered, (
        f"{doc.name} must say hook trust is recorded as hooks.state entries in the "
        f"isolated Codex home"
    )
    assert "trust_level" in lowered or "project trust" in lowered, (
        f"{doc.name} must mention the project trust entry"
    )
    assert "hook command" in lowered, (
        f"{doc.name} must say the hook commands live in the generated child config"
    )


# --------------------------------------------------------------------------
# The tmux-server isolation: shipped behaviour vs what the docs claim
# --------------------------------------------------------------------------

#: Documents that describe the harnesses' behaviour.
HARNESS_DOCS = (
    *(E2E_DIR / name / "e2e-test.md" for name in
      ("codex-messaging", "chainlink-codex", "python-tl-worker-notify")),
    PROJECT_ROOT / "E2E_STATUS.md",
)

#: Phrases that claim the isolation is not in force. Each would tell a reader
#: the worker's Codex home can still leak from the host.
ISOLATION_DISCLAIMED = (
    "it was reverted",
    "was reverted",
    "is not a fix",
    "is not used",
    "not enabled",
    "disabled by default",
    "opt-in only",
    "the controller was never reached",
    "trading a correct codex home for a dead server",
)


def harnesses_isolate_tmux() -> bool:
    """The shipped harnesses isolate each run's tmux server by default."""
    lib = (E2E_DIR / "lib" / "python-tl.sh").read_text(encoding="utf-8")
    if "e2e_python_tl_isolate_tmux_server" not in lib:
        return False
    # The opt-out must not be the default.
    return "${E2E_PYTHON_TL_TMUX_ISOLATION:-1}" in lib


@pytest.mark.parametrize("doc", HARNESS_DOCS, ids=lambda p: p.name)
def test_doc_does_not_claim_the_isolation_is_absent(doc: Path) -> None:
    """No document may say the isolation is off when the harnesses ship it on.

    The previous revision of these docs reported the isolation as reverted. That
    was true of the code at the time, but the cause was a placement bug inside
    `cleanup()`, and the harnesses now isolate by default. A doc that keeps the
    old claim would send the next maintainer to delete a working fix.
    """
    text = doc.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in text.splitlines()
        if any(phrase in line.lower() for phrase in ISOLATION_DISCLAIMED)
    ]
    # A doc may describe the *history* of the revert, as long as it says the
    # isolation is in force now.
    if not offenders:
        return
    lowered = text.lower()
    assert "now does this by default" in lowered or "by default" in lowered, (
        f"{doc.name} describes the tmux isolation as absent, but the harnesses "
        f"ship it on by default. If the history is the point, say so and state "
        f"the current behaviour:\\n" + "\\n".join(offenders)
    )


def test_the_isolation_claim_matches_the_helper() -> None:
    """The premise of the check above: the isolation is really on by default."""
    assert harnesses_isolate_tmux(), (
        "e2e_python_tl_isolate_tmux_server must exist in tests/e2e/lib/python-tl.sh "
        "and default to on. If it was removed, the doc checks above need removing "
        "too -- do not leave them asserting a helper that is gone."
    )
